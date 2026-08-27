"""Deterministic, read-only review of normalized historical stock fills."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .models import _json_value
from .rendering import markdown_text, terminal_text
from .trade_quality import canonical_quality_issues


class TradeReviewInputError(ValueError):
    """Historical source data cannot be interpreted conservatively."""


class TradePolicyConfigError(ValueError):
    """Private trade policy is unsafe or violates its strict schema."""


MAX_ANALYSIS_ROWS = 20_000
MAX_ANALYSIS_GROUPS = 20_000
MAX_ANALYSIS_TRIPS = 40_000


class AnalysisBudget:
    """Cumulative operation and wall-clock guard shared across analysis phases."""

    def __init__(self, max_operations: int, max_seconds: float, clock: Callable[[], float]):
        if max_operations <= 0 or max_seconds <= 0:
            raise TradeReviewInputError("analysis budgets must be positive")
        self.remaining = max_operations
        self.max_seconds = max_seconds
        self.clock = clock
        self.started = clock()

    def consume(self, operations: int = 1) -> None:
        self.remaining -= operations
        if self.remaining < 0 or self.clock() - self.started > self.max_seconds:
            raise TradeReviewInputError("ANALYSIS_BUDGET_EXCEEDED")


@dataclass(frozen=True)
class TradePolicy:
    market_open_no_trade_minutes: int | None = None
    market_open_no_trade_minutes_by_market: Mapping[str, int] = field(default_factory=dict)
    reentry_cooldown_minutes: int | None = None
    max_round_trips_per_symbol_day: int | None = None
    max_trades_per_day: int | None = None
    flag_add_to_loser: bool = False
    flag_cost_proximity: bool = False

    def __post_init__(self) -> None:
        market_rules = {
            str(market).upper(): value
            for market, value in self.market_open_no_trade_minutes_by_market.items()
        }
        if any(
            market not in _MARKET_TIME
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for market, value in market_rules.items()
        ):
            raise ValueError("market open rules require supported markets and non-negative minutes")
        object.__setattr__(
            self, "market_open_no_trade_minutes_by_market", MappingProxyType(market_rules)
        )
        for name in (
            "market_open_no_trade_minutes",
            "reentry_cooldown_minutes",
            "max_round_trips_per_symbol_day",
            "max_trades_per_day",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("flag_add_to_loser", "flag_cost_proximity"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")


@dataclass(frozen=True)
class NormalizedFill:
    deal_id: str
    order_id: str
    account_ref: str
    identity: str
    market: str
    currency: str
    fx_to_base: Decimal | None
    side: str
    quantity: Decimal
    price: Decimal
    executed_at: datetime
    exchange_timezone: str
    event_type: str


@dataclass(frozen=True)
class NormalizedOrder:
    order_id: str
    account_ref: str
    identity: str
    market: str
    currency: str
    side: str
    status: str
    quantity: Decimal
    dealt_quantity: Decimal
    price: Decimal
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class OrderGroup:
    group_ref: str
    account_ref: str
    order_id: str
    identity: str
    side: str
    quantity: Decimal
    average_price: Decimal
    fill_count: int
    effective_at: datetime
    first_fill_at: datetime
    last_fill_at: datetime


@dataclass(frozen=True)
class RoundTrip:
    account_ref: str
    identity: str
    currency: str
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime
    closed_at: datetime
    realized_pl_local: Decimal
    realized_pl_base: Decimal | None
    holding_seconds: Decimal


@dataclass(frozen=True)
class OpenLot:
    account_ref: str
    identity: str
    currency: str
    quantity: Decimal
    price: Decimal
    opened_at: datetime


@dataclass(frozen=True)
class TradeMetrics:
    trade_count: int | None
    order_count: int | None
    fill_count: int | None
    distinct_symbols: int | None
    notional_by_currency: Mapping[str, Mapping[str, Decimal]] | None
    realized_pl_by_currency: Mapping[str, Decimal] | None
    pnl_statistics_by_currency: Mapping[str, Mapping[str, Any]] | None
    buy_notional_base: Decimal | None
    sell_notional_base: Decimal | None
    portfolio_turnover: Decimal | None
    per_symbol_turnover: Mapping[str, Decimal] | None
    average_holding_seconds: Decimal | None
    median_holding_seconds: Decimal | None
    closed_trips: int | None
    win_rate: Decimal | None
    average_win_base: Decimal | None
    average_loss_base: Decimal | None
    payoff_ratio_base: Decimal | None
    profit_factor_base: Decimal | None
    expectancy_base: Decimal | None
    same_day_reversals: int | None
    same_day_reversal_rate: Decimal | None
    reentries_within_cooldown: int | None
    open_window_trades: int | None
    open_window_trade_rate: Decimal | None
    repeated_symbol_days: int | None
    add_to_winner_count: int | None
    add_to_loser_count: int | None
    add_to_loser_rate: Decimal | None
    cost_proximity_exit_count: int | None

    def __post_init__(self) -> None:
        if self.notional_by_currency is not None:
            object.__setattr__(
                self,
                "notional_by_currency",
                MappingProxyType(
                    {
                        key: MappingProxyType(dict(value))
                        for key, value in self.notional_by_currency.items()
                    }
                ),
            )
        if self.realized_pl_by_currency is not None:
            object.__setattr__(
                self,
                "realized_pl_by_currency",
                MappingProxyType(dict(self.realized_pl_by_currency)),
            )
        if self.pnl_statistics_by_currency is not None:
            object.__setattr__(
                self,
                "pnl_statistics_by_currency",
                MappingProxyType(
                    {
                        key: MappingProxyType(dict(value))
                        for key, value in self.pnl_statistics_by_currency.items()
                    }
                ),
            )
        if self.per_symbol_turnover is not None:
            object.__setattr__(
                self, "per_symbol_turnover", MappingProxyType(dict(self.per_symbol_turnover))
            )

    @classmethod
    def unavailable(cls) -> TradeMetrics:
        return cls(*([None] * len(cls.__dataclass_fields__)))


@dataclass(frozen=True)
class BehaviorFinding:
    type: str
    severity: str
    evidence: str
    verified: bool
    affected_events: tuple[str, ...]
    affected_symbols: tuple[str, ...]
    period_start: datetime
    period_end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "affected_events", tuple(self.affected_events))
        object.__setattr__(self, "affected_symbols", tuple(self.affected_symbols))


def _shown(value: Any) -> str:
    return "Unavailable" if value is None else str(value)


@dataclass(frozen=True)
class TradeReview:
    schema_version: str
    review_id: str
    as_of: datetime
    source: str
    base_currency: str | None
    portfolio_nav_base: Decimal | None
    source_snapshot_as_of: datetime | None
    start: date
    end: date
    coverage: Mapping[str, Any]
    normalized_orders: tuple[NormalizedOrder, ...]
    normalized_fills: tuple[NormalizedFill, ...]
    order_groups: tuple[OrderGroup, ...]
    round_trips: tuple[RoundTrip, ...]
    open_lots: tuple[OpenLot, ...]
    metrics: TradeMetrics
    policy: Mapping[str, Any] | None
    policy_status: str
    policy_checks: tuple[Mapping[str, Any], ...]
    behavior_findings: tuple[BehaviorFinding, ...]
    unverified_interpretations: tuple[str, ...]
    motive_status: str
    data_quality_status: str
    quality_issues: tuple[Mapping[str, str], ...]
    data_quality_notes: tuple[str, ...]
    provenance: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "coverage", MappingProxyType(dict(self.coverage)))
        object.__setattr__(self, "normalized_orders", tuple(self.normalized_orders))
        object.__setattr__(self, "normalized_fills", tuple(self.normalized_fills))
        object.__setattr__(self, "order_groups", tuple(self.order_groups))
        object.__setattr__(self, "round_trips", tuple(self.round_trips))
        object.__setattr__(self, "open_lots", tuple(self.open_lots))
        if self.policy is not None:
            frozen_policy = dict(self.policy)
            frozen_policy["market_open_no_trade_minutes_by_market"] = MappingProxyType(
                dict(frozen_policy["market_open_no_trade_minutes_by_market"])
            )
            object.__setattr__(self, "policy", MappingProxyType(frozen_policy))
        object.__setattr__(
            self, "policy_checks", tuple(MappingProxyType(dict(x)) for x in self.policy_checks)
        )
        object.__setattr__(self, "behavior_findings", tuple(self.behavior_findings))
        object.__setattr__(
            self, "unverified_interpretations", tuple(self.unverified_interpretations)
        )
        object.__setattr__(
            self, "quality_issues", tuple(MappingProxyType(dict(x)) for x in self.quality_issues)
        )
        object.__setattr__(self, "data_quality_notes", tuple(self.data_quality_notes))
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)

    def to_markdown(
        self, *, json_artifact: str | None = None, json_digest: str | None = None
    ) -> str:
        safe = markdown_text
        metrics = self.metrics
        lines = [
            "# Trade Review\n",
            f"- Review ID: {safe(self.review_id)}",
            f"- As of: {self.as_of.isoformat()}",
            f"- Range: {self.start.isoformat()} to {self.end.isoformat()}",
            f"- Base currency: {safe(self.base_currency or 'Unavailable')}",
            f"- Portfolio NAV ({safe(self.base_currency or 'base unavailable')}): {self.portfolio_nav_base if self.portfolio_nav_base is not None else 'Unavailable'}",
            f"- Source snapshot as of: {self.source_snapshot_as_of.isoformat() if self.source_snapshot_as_of is not None else 'Unavailable'}",
            f"- Account/context coverage: {self.coverage['accounts_covered']} of {self.coverage['accounts_requested']} accounts; {self.coverage['contexts_read']} of {self.coverage['contexts_requested']} contexts read",
            f"- Failed / budget-omitted contexts: {self.coverage['contexts_failed']} / {self.coverage['contexts_omitted_budget']}",
            f"- Monetary base coverage: {safe(self.coverage['monetary_base_coverage'])}",
            f"- Realized P&L base coverage: {safe(self.coverage['realized_pl_base_coverage'])}",
            f"- Corporate-action evidence: {safe(self.coverage['corporate_action_evidence_status'])}",
            f"- Data quality: {self.data_quality_status}",
            f"- Structured quality issues: {safe(json.dumps(_json_value(self.quality_issues), sort_keys=True))}",
            f"- Policy status: {self.policy_status}",
            f"- Policy configuration: {safe(json.dumps(_json_value(self.policy), sort_keys=True)) if self.policy is not None else 'Unconfigured'}",
            "- Motive: unverified; only observed execution patterns are described.",
            f"- Orders / fills / symbols: {_shown(metrics.order_count)} / {_shown(metrics.fill_count)} / {_shown(metrics.distinct_symbols)}",
            f"- Closed FIFO trips: {_shown(metrics.closed_trips)}",
            f"- Win rate: {metrics.win_rate if metrics.win_rate is not None else 'Unavailable'}",
            f"- Local-currency notional buckets: {safe(json.dumps(_json_value(metrics.notional_by_currency), sort_keys=True)) if metrics.notional_by_currency is not None else 'Unavailable'}",
            f"- Local-currency P&L statistics: {safe(json.dumps(_json_value(metrics.pnl_statistics_by_currency), sort_keys=True)) if metrics.pnl_statistics_by_currency is not None else 'Unavailable'}",
            f"- Buy / sell notional ({safe(self.base_currency or 'base unavailable')}): {metrics.buy_notional_base if metrics.buy_notional_base is not None else 'Unavailable'} / {metrics.sell_notional_base if metrics.sell_notional_base is not None else 'Unavailable'}",
            f"- Portfolio turnover: {_shown(metrics.portfolio_turnover)}",
            f"- Per-symbol turnover: {safe(json.dumps(_json_value(metrics.per_symbol_turnover), sort_keys=True)) if metrics.per_symbol_turnover is not None else 'Unavailable'}",
            f"- Average / median holding seconds: {_shown(metrics.average_holding_seconds)} / {_shown(metrics.median_holding_seconds)}",
            f"- Expectancy / profit factor ({safe(self.base_currency or 'base unavailable')}): {_shown(metrics.expectancy_base)} / {_shown(metrics.profit_factor_base)}",
            f"- Same-day reversals: {_shown(metrics.same_day_reversals)}",
            f"- Same-day reversal rate: {metrics.same_day_reversal_rate if metrics.same_day_reversal_rate is not None else 'Unavailable'}",
            f"- Reentries within configured cooldown: {_shown(metrics.reentries_within_cooldown)}",
            f"- Trades in configured market-open window: {_shown(metrics.open_window_trades)}",
            f"- Market-open-window rate: {metrics.open_window_trade_rate if metrics.open_window_trade_rate is not None else 'Unavailable'}",
            f"- Add-to-winner / add-to-loser: {_shown(metrics.add_to_winner_count)} / {_shown(metrics.add_to_loser_count)}",
            f"- Cost-proximity exits (within 1% of evidenced FIFO entry): {_shown(metrics.cost_proximity_exit_count)}",
            f"- Repeated symbol-days: {_shown(metrics.repeated_symbol_days)}",
            "\n## Policy checks\n",
            *(
                [
                    f"- {safe(json.dumps(_json_value(item), sort_keys=True))}"
                    for item in self.policy_checks
                ]
                or ["- None"]
            ),
            "\n## Verified behavior findings\n",
            *(
                [
                    f"- {safe(item.type)} [{safe(item.severity)}]: {safe(item.evidence)}"
                    for item in self.behavior_findings
                ]
                or ["- None"]
            ),
            "\n## Unverified interpretations\n",
            *([f"- {safe(item)}" for item in self.unverified_interpretations] or ["- None"]),
            "\n## Data quality notes\n",
            *([f"- {safe(item)}" for item in self.data_quality_notes] or ["- None"]),
            "\n## Provenance\n",
            f"- Orders: {safe(self.provenance['orders_command'])}",
            f"- Deals: {safe(self.provenance['deals_command'])}",
            f"- Time basis: {safe(self.provenance['timezone_basis'])}",
            f"- Monetary basis: {safe(self.provenance['monetary_basis'])}",
            "\n## Masked context coverage\n",
            f"- Requested: {safe(json.dumps(self.coverage['requested_contexts'], sort_keys=True))}",
            f"- Successful: {safe(json.dumps(self.coverage['successful_contexts'], sort_keys=True))}",
            f"- Failed: {safe(json.dumps(self.coverage['failed_contexts'], sort_keys=True))}",
            f"- Budget omitted: {safe(json.dumps(self.coverage['omitted_contexts'], sort_keys=True))}",
        ]
        if json_artifact:
            lines.append(f"\nAuthoritative JSON: `{safe(json_artifact)}`\n")
        if json_digest:
            lines.append(f"Authoritative JSON SHA-256: `{safe(json_digest)}`\n")
        return "\n".join(lines)

    def to_human(self) -> str:
        safe = terminal_text
        return "\n".join(
            [
                "Trade review",
                f"Review: {safe(self.review_id)}",
                f"As of: {self.as_of.isoformat()}",
                f"Range: {self.start.isoformat()} to {self.end.isoformat()}",
                f"Base currency: {safe(self.base_currency or 'Unavailable')}",
                f"Portfolio NAV: {self.portfolio_nav_base if self.portfolio_nav_base is not None else 'Unavailable'} {safe(self.base_currency or '')}".rstrip(),
                f"Source snapshot as of: {self.source_snapshot_as_of.isoformat() if self.source_snapshot_as_of is not None else 'Unavailable'}",
                f"Coverage: {self.coverage['accounts_covered']}/{self.coverage['accounts_requested']} accounts; {self.coverage['contexts_read']}/{self.coverage['contexts_requested']} contexts; {self.coverage['contexts_failed']} failed; {self.coverage['contexts_omitted_budget']} budget-omitted",
                f"Monetary base coverage: {safe(self.coverage['monetary_base_coverage'])}",
                f"Realized P&L base coverage: {safe(self.coverage['realized_pl_base_coverage'])}",
                f"Corporate-action evidence: {safe(self.coverage['corporate_action_evidence_status'])}",
                f"Quality: {self.data_quality_status}",
                f"Structured quality issues: {safe(json.dumps(_json_value(self.quality_issues), sort_keys=True))}",
                f"Policy: {self.policy_status}",
                f"Policy configuration: {safe(json.dumps(_json_value(self.policy), sort_keys=True)) if self.policy is not None else 'Unconfigured'}",
                "Motive: UNVERIFIED; findings describe observed behavior only.",
                f"Orders: {_shown(self.metrics.order_count)}",
                f"Fills: {_shown(self.metrics.fill_count)}",
                f"Closed FIFO trips: {_shown(self.metrics.closed_trips)}",
                f"Local-currency P&L statistics: {safe(json.dumps(_json_value(self.metrics.pnl_statistics_by_currency), sort_keys=True)) if self.metrics.pnl_statistics_by_currency is not None else 'Unavailable'}",
                f"Portfolio turnover: {_shown(self.metrics.portfolio_turnover)}",
                f"Holding average/median seconds: {_shown(self.metrics.average_holding_seconds)}/{_shown(self.metrics.median_holding_seconds)}",
                f"Expectancy/profit factor: {_shown(self.metrics.expectancy_base)}/{_shown(self.metrics.profit_factor_base)}",
                f"Repeated symbol-days: {_shown(self.metrics.repeated_symbol_days)}",
                *[
                    f"Policy check: {safe(json.dumps(_json_value(item), sort_keys=True))}"
                    for item in self.policy_checks
                ],
                *[
                    f"Verified finding: {safe(item.type)} [{safe(item.severity)}] {safe(item.evidence)}"
                    for item in self.behavior_findings
                ],
                *[
                    f"Unverified interpretation: {safe(item)}"
                    for item in self.unverified_interpretations
                ],
                f"Orders source: {safe(self.provenance['orders_command'])}",
                f"Deals source: {safe(self.provenance['deals_command'])}",
                f"Requested contexts: {safe(json.dumps(self.coverage['requested_contexts'], sort_keys=True))}",
                f"Successful contexts: {safe(json.dumps(self.coverage['successful_contexts'], sort_keys=True))}",
                f"Failed contexts: {safe(json.dumps(self.coverage['failed_contexts'], sort_keys=True))}",
                f"Budget-omitted contexts: {safe(json.dumps(self.coverage['omitted_contexts'], sort_keys=True))}",
                *[f"Quality note: {safe(item)}" for item in self.data_quality_notes],
            ]
        )


_MARKET_TIME = {
    "US": ("America/New_York", 9, 30),
    "HK": ("Asia/Hong_Kong", 9, 30),
    "CN": ("Asia/Shanghai", 9, 30),
    "SG": ("Asia/Singapore", 9, 0),
    "MY": ("Asia/Kuala_Lumpur", 9, 0),
    "JP": ("Asia/Tokyo", 9, 0),
    "CA": ("America/Toronto", 9, 30),
    "AU": ("Australia/Sydney", 10, 0),
}
_SOURCE_MARKET_ALIASES = {"SH": "CN", "SZ": "CN", "HKCC": "HK"}
NORMALIZED_ORDER_STATUSES = frozenset(
    {
        "WAITING_SUBMIT",
        "SUBMITTING",
        "SUBMITTED",
        "FILLED_PART",
        "FILLED_ALL",
        "CANCELLED_PART",
        "CANCELLED_ALL",
        "FAILED",
        "DISABLED",
        "DELETED",
    }
)


def normalize_order_status(raw: Any) -> str:
    status = str(raw).strip().upper().replace("ORDER_STATUS.", "").replace("ORDERSTATUS.", "")
    if status not in NORMALIZED_ORDER_STATUSES:
        raise TradeReviewInputError("order status is outside the normalized contract")
    return status


def canonical_context_market(raw: Any) -> str:
    market = str(raw).strip().upper()
    canonical = _SOURCE_MARKET_ALIASES.get(market, market)
    if canonical not in _MARKET_TIME:
        raise TradeReviewInputError("history context market is outside the canonical contract")
    return canonical


def exchange_local_date(timestamp: datetime, market: str) -> date:
    """Return an aware instant's date under the canonical exchange context."""

    if timestamp.tzinfo is None:
        raise TradeReviewInputError("exchange-local date requires a timezone-aware timestamp")
    canonical = canonical_context_market(market)
    return timestamp.astimezone(ZoneInfo(_MARKET_TIME[canonical][0])).date()


def load_trade_policy(data_home: Path) -> TradePolicy | None:
    """Load the small YAML scalar subset used by the private trade policy."""

    if os.name != "posix":
        path = data_home.expanduser().absolute() / "config" / "trade-policy.yaml"
        try:
            if path.is_symlink() or not path.is_file():
                raise TradePolicyConfigError("trade policy is unavailable or unsafe")
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
    else:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            root_fd = os.open(data_home.expanduser().absolute(), directory_flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise TradePolicyConfigError("private data home is unsafe") from exc
        try:
            try:
                config_fd = os.open("config", directory_flags, dir_fd=root_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise TradePolicyConfigError("private config directory is unsafe") from exc
            try:
                if stat.S_IMODE(os.fstat(config_fd).st_mode) != 0o700:
                    raise TradePolicyConfigError("private config directory must have mode 0700")
                try:
                    file_fd = os.open("trade-policy.yaml", file_flags, dir_fd=config_fd)
                except FileNotFoundError:
                    return None
                except OSError as exc:
                    raise TradePolicyConfigError("trade policy is unavailable or unsafe") from exc
                try:
                    metadata = os.fstat(file_fd)
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or stat.S_IMODE(metadata.st_mode) != 0o600
                    ):
                        raise TradePolicyConfigError("trade policy must be a regular 0600 file")
                    chunks: list[bytes] = []
                    total = 0
                    while True:
                        chunk = os.read(file_fd, min(8192, 65537 - total))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > 65536:
                            raise TradePolicyConfigError("trade policy exceeds the bounded size")
                    raw = b"".join(chunks)
                    check_fd = os.open("trade-policy.yaml", file_flags, dir_fd=config_fd)
                    try:
                        current = os.fstat(check_fd)
                        if (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino):
                            raise TradePolicyConfigError("trade policy changed during read")
                    finally:
                        os.close(check_fd)
                finally:
                    os.close(file_fd)
                check_config_fd = os.open("config", directory_flags, dir_fd=root_fd)
                try:
                    anchored_config = os.fstat(config_fd)
                    current_config = os.fstat(check_config_fd)
                    if (anchored_config.st_dev, anchored_config.st_ino) != (
                        current_config.st_dev,
                        current_config.st_ino,
                    ):
                        raise TradePolicyConfigError("private config directory changed during read")
                finally:
                    os.close(check_config_fd)
            finally:
                os.close(config_fd)
        finally:
            os.close(root_fd)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TradePolicyConfigError("trade policy must be UTF-8") from exc
    scalars: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for line in text.splitlines():
        if "\t" in line:
            raise TradePolicyConfigError("trade policy indentation must use spaces")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent % 2 or indent > 4:
            raise TradePolicyConfigError("trade policy indentation is invalid")
        key, separator, scalar = stripped.partition(":")
        key, scalar = key.strip(), scalar.strip()
        if not separator or not key:
            raise TradePolicyConfigError("trade policy contains invalid YAML")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not scalar:
            stack.append((indent, key))
            continue
        if scalar.startswith(("[", "{", "&", "*", "!", "|", ">")):
            raise TradePolicyConfigError("trade policy supports scalar values only")
        dotted = ".".join([*(item[1] for item in stack), key])
        if dotted in scalars:
            raise TradePolicyConfigError("trade policy contains a duplicate key")
        scalars[dotted] = scalar.strip("'\"")
    if scalars.pop("schema_version", None) != "1.0":
        raise TradePolicyConfigError("trade policy requires schema_version 1.0")
    market_rules: dict[str, int] = {}
    values: dict[str, Any] = {}
    field_map = {
        "reentry.cooldown_minutes": "reentry_cooldown_minutes",
        "frequency.max_round_trips_per_symbol_per_day": "max_round_trips_per_symbol_day",
        "frequency.max_trades_per_day": "max_trades_per_day",
        "position_management.flag_add_to_loser": "flag_add_to_loser",
        "position_management.flag_cost_proximity_stop": "flag_cost_proximity",
    }
    for dotted, scalar in scalars.items():
        parts = dotted.split(".")
        if (
            len(parts) == 3
            and parts[0] == "market_open_rules"
            and parts[2] == "no_trade_minutes_after_open"
            and parts[1] in _MARKET_TIME
        ):
            try:
                market_rules[parts[1]] = int(scalar)
            except ValueError:
                raise TradePolicyConfigError("trade policy limits must be integers") from None
            continue
        field_name = field_map.get(dotted)
        if field_name is None:
            raise TradePolicyConfigError("trade policy contains an unknown key")
        if field_name.startswith("flag_"):
            if scalar.lower() not in {"true", "false"}:
                raise TradePolicyConfigError("trade policy flags must be boolean")
            values[field_name] = scalar.lower() == "true"
        else:
            try:
                values[field_name] = int(scalar)
            except ValueError:
                raise TradePolicyConfigError("trade policy limits must be integers") from None
    values["market_open_no_trade_minutes_by_market"] = market_rules
    try:
        return TradePolicy(**values)
    except (TypeError, ValueError) as exc:
        raise TradePolicyConfigError(str(exc)) from exc


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise TradeReviewInputError(f"{label} must be a decimal") from None
    if not result.is_finite() or result <= 0:
        raise TradeReviewInputError(f"{label} must be finite and positive")
    return result


def _exchange_datetime(raw: Any, timezone_name: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        raise TradeReviewInputError(f"{label} must use ISO date-time format") from None
    zone = ZoneInfo(timezone_name)
    if parsed.tzinfo is not None:
        return parsed.astimezone(zone)
    first = parsed.replace(tzinfo=zone, fold=0)
    second = parsed.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise TradeReviewInputError(f"{label} is ambiguous or nonexistent in exchange timezone")
    if first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != parsed:
        raise TradeReviewInputError(f"{label} is ambiguous or nonexistent in exchange timezone")
    return first


def _fill(raw: Mapping[str, Any], start: date, end: date) -> NormalizedFill:
    required = {"deal_id", "order_id", "account_ref", "code", "side", "qty", "price", "create_time"}
    optional = {"currency", "fx_to_base", "event_type"}
    if set(raw) - required - optional or not required <= set(raw):
        raise TradeReviewInputError("deal contract fields changed or are incomplete")
    code = str(raw["code"]).strip().upper()
    parts = code.split(".", 1)
    market = canonical_context_market(parts[0]) if len(parts) == 2 else ""
    identity = f"{market}.{parts[1]}" if len(parts) == 2 else ""
    if (
        len(parts) != 2
        or market not in _MARKET_TIME
        or re.fullmatch(r"[A-Z]{2,4}\.[A-Z0-9]+(?:[.-][A-Z0-9]+)*", identity) is None
    ):
        raise TradeReviewInputError("deal requires a supported market-qualified identity")
    side = str(raw["side"]).strip().upper().replace("TRD_SIDE.", "").replace("TRDSIDE.", "")
    if side not in {"BUY", "SELL"}:
        raise TradeReviewInputError("deal side must be BUY or SELL")
    timezone_name = _MARKET_TIME[market][0]
    parsed = _exchange_datetime(raw["create_time"], timezone_name, "deal time")
    if not start <= parsed.date() <= end:
        raise TradeReviewInputError("deal falls outside the requested review range")
    identifiers = tuple(str(raw[name]).strip() for name in ("deal_id", "order_id", "account_ref"))
    if not all(identifiers):
        raise TradeReviewInputError("deal identifiers must be non-empty")
    currency = str(raw.get("currency", "UNKNOWN")).strip().upper()
    if currency != "UNKNOWN" and re.fullmatch(r"[A-Z]{3}", currency) is None:
        raise TradeReviewInputError("deal currency must be an ISO-style code or UNKNOWN")
    fx_raw = raw.get("fx_to_base")
    fx_to_base = None if fx_raw is None else _decimal(fx_raw, "deal FX rate")
    event_type = str(raw.get("event_type", "TRADE")).strip().upper()
    if event_type not in {"TRADE", "CORPORATE_ACTION", "TRANSFER", "COST_ADJUSTMENT"}:
        raise TradeReviewInputError("deal event type is unsupported")
    return NormalizedFill(
        identifiers[0],
        identifiers[1],
        identifiers[2],
        identity,
        market,
        currency,
        fx_to_base,
        side,
        _decimal(raw["qty"], "deal quantity"),
        _decimal(raw["price"], "deal price"),
        parsed,
        timezone_name,
        event_type,
    )


def _order(raw: Mapping[str, Any], start: date, end: date) -> NormalizedOrder:
    required = {
        "order_id",
        "account_ref",
        "code",
        "side",
        "status",
        "qty",
        "price",
        "dealt_qty",
        "create_time",
        "updated_time",
    }
    optional = {"currency"}
    if set(raw) - required - optional or not required <= set(raw):
        raise TradeReviewInputError("order contract fields changed or are incomplete")
    code = str(raw["code"]).strip().upper()
    parts = code.split(".", 1)
    market = canonical_context_market(parts[0]) if len(parts) == 2 else ""
    identity = f"{market}.{parts[1]}" if len(parts) == 2 else ""
    if (
        len(parts) != 2
        or market not in _MARKET_TIME
        or re.fullmatch(r"[A-Z]{2,4}\.[A-Z0-9]+(?:[.-][A-Z0-9]+)*", identity) is None
    ):
        raise TradeReviewInputError("order requires a supported market-qualified identity")
    side = str(raw["side"]).strip().upper().replace("TRD_SIDE.", "").replace("TRDSIDE.", "")
    if side not in {"BUY", "SELL"}:
        raise TradeReviewInputError("order side must be BUY or SELL")
    status = normalize_order_status(raw["status"])
    timezone_name = _MARKET_TIME[market][0]
    created = _exchange_datetime(raw["create_time"], timezone_name, "order create time")
    updated = _exchange_datetime(raw["updated_time"], timezone_name, "order update time")
    if not start <= created.date() <= end or updated < created:
        raise TradeReviewInputError("order timestamps are outside the requested range or reversed")
    quantity = _decimal(raw["qty"], "order quantity")
    try:
        dealt = Decimal(str(raw["dealt_qty"]))
    except (InvalidOperation, ValueError):
        raise TradeReviewInputError("order dealt quantity is invalid") from None
    if not dealt.is_finite() or dealt < 0 or dealt > quantity:
        raise TradeReviewInputError("order dealt quantity is invalid")
    currency = str(raw.get("currency", "UNKNOWN")).strip().upper()
    if currency != "UNKNOWN" and re.fullmatch(r"[A-Z]{3}", currency) is None:
        raise TradeReviewInputError("order currency must be an ISO-style code or UNKNOWN")
    order_id = str(raw["order_id"]).strip()
    account_ref = str(raw["account_ref"]).strip()
    if not order_id or not account_ref:
        raise TradeReviewInputError("order identifiers must be non-empty")
    return NormalizedOrder(
        order_id,
        account_ref,
        identity,
        market,
        currency,
        side,
        status,
        quantity,
        dealt,
        _decimal(raw["price"], "order price"),
        created,
        updated,
    )


def _group_orders(
    fills: Sequence[NormalizedFill], budget: AnalysisBudget | None = None
) -> tuple[OrderGroup, ...]:
    groups: dict[tuple[str, str], list[NormalizedFill]] = {}
    for fill in fills:
        if budget:
            budget.consume()
        groups.setdefault((fill.account_ref, fill.order_id), []).append(fill)
    result = []
    for key in sorted(groups):
        if budget:
            budget.consume(len(groups[key]) + 1)
        items = sorted(groups[key], key=lambda x: (x.executed_at, x.deal_id))
        identity_side = {(x.identity, x.side) for x in items}
        if len(identity_side) != 1:
            raise TradeReviewInputError("one order ID spans conflicting security or side values")
        quantity = sum((x.quantity for x in items), Decimal(0))
        notional = sum((x.quantity * x.price for x in items), Decimal(0))
        result.append(
            OrderGroup(
                f"{key[0]}/{key[1]}",
                key[0],
                key[1],
                items[0].identity,
                items[0].side,
                quantity,
                notional / quantity,
                len(items),
                items[0].executed_at,
                items[0].executed_at,
                items[-1].executed_at,
            )
        )
    return tuple(result)


def _fifo(
    fills: Sequence[NormalizedFill],
    base_currency: str | None,
    budget: AnalysisBudget | None = None,
) -> tuple[tuple[RoundTrip, ...], tuple[OpenLot, ...], int]:
    inventories: dict[tuple[str, str], deque[list[Any]]] = {}
    trips: list[RoundTrip] = []
    unmatched_sells = 0
    for fill in sorted(fills, key=lambda x: (x.executed_at, x.deal_id)):
        if budget:
            budget.consume()
        key = (fill.account_ref, fill.identity)
        lots = inventories.setdefault(key, deque())
        if fill.side == "BUY":
            lots.append(
                [fill.quantity, fill.price, fill.executed_at, fill.currency, fill.fx_to_base]
            )
            continue
        remaining = fill.quantity
        while remaining and lots:
            if budget:
                budget.consume()
            lot = lots[0]
            matched = min(remaining, lot[0])
            seconds = Decimal(str((fill.executed_at - lot[2]).total_seconds()))
            if lot[3] != fill.currency:
                raise TradeReviewInputError("one security has conflicting fill currencies")
            realized_local = (fill.price - lot[1]) * matched
            # Currency identity is exact transaction evidence. A base-currency
            # transaction therefore has an exact 1:1 conversion without snapshot FX.
            realized_base = realized_local if fill.currency == base_currency else None
            trips.append(
                RoundTrip(
                    fill.account_ref,
                    fill.identity,
                    fill.currency,
                    matched,
                    lot[1],
                    fill.price,
                    lot[2],
                    fill.executed_at,
                    realized_local,
                    realized_base,
                    seconds,
                )
            )
            lot[0] -= matched
            remaining -= matched
            if lot[0] == 0:
                lots.popleft()
        if remaining:
            unmatched_sells += 1
    open_lots = tuple(
        OpenLot(account, identity, lot[3], lot[0], lot[1], lot[2])
        for (account, identity), lots in sorted(inventories.items())
        for lot in lots
        if lot[0]
    )
    return tuple(trips), open_lots, unmatched_sells


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _pnl_statistics_by_currency(
    trips: Sequence[RoundTrip],
) -> dict[str, dict[str, Any]]:
    pools: dict[str, list[Decimal]] = {}
    for trip in trips:
        if trip.currency != "UNKNOWN":
            pools.setdefault(trip.currency, []).append(trip.realized_pl_local)
    result: dict[str, dict[str, Any]] = {}
    for currency, values in sorted(pools.items()):
        wins = [value for value in values if value > 0]
        losses = [value for value in values if value < 0]
        win_total = sum(wins, Decimal(0))
        loss_total = -sum(losses, Decimal(0))
        average_win = None if not wins else win_total / len(wins)
        average_loss = None if not losses else -loss_total / len(losses)
        result[currency] = {
            "closed_trips": len(values),
            "wins": len(wins),
            "losses": len(losses),
            "gross_profit": win_total,
            "gross_loss_absolute": loss_total,
            "average_win": average_win,
            "average_loss": average_loss,
            "payoff_ratio": (
                None if average_win is None or average_loss is None else average_win / -average_loss
            ),
            "profit_factor": None if loss_total == 0 else win_total / loss_total,
            "expectancy": sum(values, Decimal(0)) / len(values),
        }
    return result


def build_trade_review(
    *,
    orders: Sequence[Mapping[str, Any]],
    deals: Sequence[Mapping[str, Any]],
    start: date,
    end: date,
    policy: TradePolicy | None = None,
    source_notes: Iterable[str] = (),
    base_currency: str | None = None,
    portfolio_nav_base: Decimal | None = None,
    source_snapshot_id: str | None = None,
    acquired_at: datetime | None = None,
    source_snapshot_as_of: datetime | None = None,
    coverage_context: Mapping[str, Any] | None = None,
    corporate_action_evidence_status: str = "UNSUPPORTED_EVIDENCE",
    provenance_context: Mapping[str, Any] | None = None,
    max_analysis_operations: int = 2_000_000,
    max_analysis_seconds: float = 10.0,
    analysis_clock: Callable[[], float] = time.monotonic,
) -> TradeReview:
    if len(orders) + len(deals) > MAX_ANALYSIS_ROWS:
        raise TradeReviewInputError("ANALYSIS_ROW_LIMIT_EXCEEDED")
    budget = AnalysisBudget(max_analysis_operations, max_analysis_seconds, analysis_clock)
    budget.consume(len(orders) + len(deals))
    if start > end:
        raise TradeReviewInputError("review start must not follow end")
    acquisition = acquired_at or datetime.now(timezone.utc)
    if acquisition.tzinfo is None:
        raise TradeReviewInputError("acquisition timestamp must be timezone-aware")
    acquisition = acquisition.astimezone(timezone.utc)
    if source_snapshot_as_of is not None:
        if source_snapshot_as_of.tzinfo is None:
            raise TradeReviewInputError("source snapshot as-of must be timezone-aware")
        source_snapshot_as_of = source_snapshot_as_of.astimezone(timezone.utc)
    if (source_snapshot_id is None) != (source_snapshot_as_of is None):
        raise TradeReviewInputError("source snapshot identity and as-of must be supplied together")
    if base_currency is not None and re.fullmatch(r"[A-Z]{3}", base_currency) is None:
        raise TradeReviewInputError("base currency must be an uppercase ISO-style code")
    if portfolio_nav_base is not None and (
        not portfolio_nav_base.is_finite() or portfolio_nav_base <= 0 or base_currency is None
    ):
        raise TradeReviewInputError("portfolio NAV requires positive base-currency evidence")
    if corporate_action_evidence_status not in {"AVAILABLE", "UNSUPPORTED_EVIDENCE"}:
        raise TradeReviewInputError("corporate-action evidence status is invalid")
    normalized_fill_rows = []
    for raw in deals:
        budget.consume()
        normalized_fill_rows.append(_fill(raw, start, end))
    normalized_all = tuple(
        sorted(
            normalized_fill_rows,
            key=lambda item: (item.account_ref, item.order_id, item.executed_at, item.deal_id),
        )
    )
    normalized_order_rows = []
    for raw in orders:
        budget.consume()
        normalized_order_rows.append(_order(raw, start, end))
    normalized_orders = tuple(
        sorted(normalized_order_rows, key=lambda item: (item.account_ref, item.order_id))
    )
    budget.consume(len(normalized_orders) + len(normalized_all))
    if base_currency is not None and any(
        fill.currency == base_currency
        and fill.fx_to_base is not None
        and fill.fx_to_base != Decimal(1)
        for fill in normalized_all
    ):
        raise TradeReviewInputError("base-currency fill FX must equal one")
    source_unavailable = (
        not coverage_context.get("successful_contexts", ())
        if coverage_context is not None
        else not normalized_all and not normalized_orders
    )
    requested_markets = {
        str(item.get("market", ""))
        for item in (coverage_context or {}).get("requested_contexts", ())
        if isinstance(item, Mapping)
    } or {item.market for item in (*normalized_orders, *normalized_all)}
    local_dates = {
        market: acquisition.astimezone(ZoneInfo(_MARKET_TIME[market][0])).date()
        for market in requested_markets
        if market in _MARKET_TIME
    }
    if local_dates and any(local_date < end for local_date in local_dates.values()):
        raise TradeReviewInputError("acquisition precedes the requested exchange-local period end")
    if not local_dates and acquisition.date() < end:
        raise TradeReviewInputError("acquisition timestamp precedes the requested period end")
    in_progress_markets = tuple(
        sorted(market for market, local_date in local_dates.items() if local_date == end)
    )
    deal_ids = [fill.deal_id for fill in normalized_all]
    if len(deal_ids) != len(set(deal_ids)):
        raise TradeReviewInputError("duplicate deal IDs are forbidden")
    order_keys = [(order.account_ref, order.order_id) for order in normalized_orders]
    if len(order_keys) != len(set(order_keys)):
        raise TradeReviewInputError("duplicate account/order identities are forbidden")
    excluded_events = tuple(fill for fill in normalized_all if fill.event_type != "TRADE")
    normalized = tuple(fill for fill in normalized_all if fill.event_type == "TRADE")
    groups = _group_orders(normalized, budget)
    trips, open_lots, unmatched_sells = _fifo(normalized, base_currency, budget)
    if len(groups) > MAX_ANALYSIS_GROUPS or len(trips) > MAX_ANALYSIS_TRIPS:
        raise TradeReviewInputError("ANALYSIS_DERIVED_ROW_LIMIT_EXCEEDED")

    orders_by_key = {(item.account_ref, item.order_id): item for item in normalized_orders}
    fills_by_key: dict[tuple[str, str], list[NormalizedFill]] = {}
    for fill in normalized:
        fills_by_key.setdefault((fill.account_ref, fill.order_id), []).append(fill)
    partial_fill_orders = 0
    orders_missing_fills = 0
    orphan_fill_orders = 0
    order_fill_qty_mismatches = 0
    order_status_conflicts = 0
    for key, order in orders_by_key.items():
        budget.consume(len(fills_by_key.get(key, ())) + 1)
        fill_qty = sum((item.quantity for item in fills_by_key.get(key, [])), Decimal(0))
        if fill_qty == 0 and order.dealt_quantity > 0:
            orders_missing_fills += 1
        if Decimal(0) < fill_qty < order.quantity:
            partial_fill_orders += 1
        if fill_qty != order.dealt_quantity:
            order_fill_qty_mismatches += 1
        no_fill_statuses = {
            "WAITING_SUBMIT",
            "SUBMITTING",
            "SUBMITTED",
            "CANCELLED_ALL",
            "FAILED",
            "DISABLED",
            "DELETED",
        }
        status_conflict = (
            (
                order.status == "FILLED_ALL"
                and not order.dealt_quantity == fill_qty == order.quantity
            )
            or (
                order.status in {"FILLED_PART", "CANCELLED_PART"}
                and not Decimal(0) < order.dealt_quantity == fill_qty < order.quantity
            )
            or (order.status in no_fill_statuses and (order.dealt_quantity != 0 or fill_qty != 0))
            or any(
                fill.identity != order.identity or fill.side != order.side
                for fill in fills_by_key.get(key, [])
            )
        )
        order_status_conflicts += int(status_conflict)
    orphan_fill_orders = len(set(fills_by_key) - set(orders_by_key))

    currency_buckets: dict[str, dict[str, Decimal]] = {}
    for fill in normalized:
        budget.consume()
        if fill.currency == "UNKNOWN":
            continue
        bucket = currency_buckets.setdefault(fill.currency, {"buy": Decimal(0), "sell": Decimal(0)})
        bucket[fill.side.lower()] += fill.quantity * fill.price
    realized_by_currency: dict[str, Decimal] = {}
    for trip in trips:
        budget.consume()
        if trip.currency != "UNKNOWN":
            realized_by_currency[trip.currency] = (
                realized_by_currency.get(trip.currency, Decimal(0)) + trip.realized_pl_local
            )
    pnl_statistics_by_currency = _pnl_statistics_by_currency(trips)
    base_complete = (
        not source_unavailable
        and bool(base_currency and source_snapshot_id and source_snapshot_as_of)
        and all(fill.currency != "UNKNOWN" and fill.fx_to_base is not None for fill in normalized)
    )
    buy_base = (
        sum(
            (
                fill.quantity * fill.price * fill.fx_to_base
                for fill in normalized
                if fill.side == "BUY"
            ),
            Decimal(0),
        )
        if base_complete
        else None
    )
    sell_base = (
        sum(
            (
                fill.quantity * fill.price * fill.fx_to_base
                for fill in normalized
                if fill.side == "SELL"
            ),
            Decimal(0),
        )
        if base_complete
        else None
    )
    symbol_notional_base: dict[str, Decimal] = {}
    if base_complete:
        for fill in normalized:
            symbol_notional_base[fill.identity] = (
                symbol_notional_base.get(fill.identity, Decimal(0))
                + fill.quantity * fill.price * fill.fx_to_base
            )
    per_symbol_turnover = (
        {
            identity: value / (Decimal(2) * portfolio_nav_base)
            for identity, value in sorted(symbol_notional_base.items())
        }
        if base_complete and portfolio_nav_base is not None
        else {}
    )
    holdings = [x.holding_seconds for x in trips]
    pnl_base_complete = bool(trips) and all(trip.realized_pl_base is not None for trip in trips)
    profitable_trips = [x for x in trips if x.realized_pl_local > 0]
    profits = [
        x.realized_pl_base
        for x in trips
        if x.realized_pl_base is not None and x.realized_pl_base > 0
    ]
    losses = [
        x.realized_pl_base
        for x in trips
        if x.realized_pl_base is not None and x.realized_pl_base < 0
    ]
    wins_total = sum(profits, Decimal(0))
    losses_total = -sum(losses, Decimal(0))
    chronological_groups = sorted(groups, key=lambda x: (x.effective_at, x.group_ref))
    by_security: dict[tuple[str, str], list[OrderGroup]] = {}
    for group in chronological_groups:
        budget.consume()
        by_security.setdefault((group.account_ref, group.identity), []).append(group)
    adjacent = [pair for items in by_security.values() for pair in zip(items, items[1:])]
    reversals = sum(
        1
        for left, right in adjacent
        if left.effective_at.date() == right.effective_at.date() and left.side != right.side
    )
    cooldown = policy.reentry_cooldown_minutes if policy else None
    reentry_pairs = [
        (left, right)
        for left, right in adjacent
        if cooldown is not None
        and left.side == "SELL"
        and right.side == "BUY"
        and right.effective_at - left.effective_at <= timedelta(minutes=cooldown)
    ]
    observed_reentry_pairs = [
        (left, right)
        for left, right in adjacent
        if left.side == "SELL"
        and right.side == "BUY"
        and left.effective_at.date() == right.effective_at.date()
    ]
    reentries = len(reentry_pairs) if cooldown is not None else None
    open_minutes = policy.market_open_no_trade_minutes if policy else None
    market_open_rules = policy.market_open_no_trade_minutes_by_market if policy else {}
    open_events: list[OrderGroup] = []
    if open_minutes is not None or market_open_rules:
        for group in groups:
            market = group.identity.split(".", 1)[0]
            group_window = market_open_rules.get(market, open_minutes)
            if group_window is None:
                continue
            _, hour, minute = _MARKET_TIME[market]
            start_minute = hour * 60 + minute
            current = group.effective_at.hour * 60 + group.effective_at.minute
            if start_minute <= current < start_minute + group_window:
                open_events.append(group)
    observed_open_events: list[OrderGroup] = []
    for group in groups:
        budget.consume()
        market = group.identity.split(".", 1)[0]
        _, hour, minute = _MARKET_TIME[market]
        start_minute = hour * 60 + minute
        current = group.effective_at.hour * 60 + group.effective_at.minute
        if start_minute <= current < start_minute + 30:
            observed_open_events.append(group)
    open_count = len(open_events) if open_minutes is not None or market_open_rules else None
    activity: dict[tuple[str, str, date], int] = {}
    for group in groups:
        key = (group.account_ref, group.identity, group.effective_at.date())
        activity[key] = activity.get(key, 0) + 1
    add_winner = 0
    add_loser = 0
    add_winner_events: list[OrderGroup] = []
    add_loser_events: list[OrderGroup] = []
    inventory_cost: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
    round_trip_groups_day: dict[tuple[str, str, date], int] = {}
    for group in chronological_groups:
        budget.consume()
        key = (group.account_ref, group.identity)
        quantity, cost = inventory_cost.get(key, (Decimal(0), Decimal(0)))
        if group.side == "BUY":
            if quantity > 0:
                average = cost / quantity
                if group.average_price > average:
                    add_winner += 1
                    add_winner_events.append(group)
                elif group.average_price < average:
                    add_loser += 1
                    add_loser_events.append(group)
            inventory_cost[key] = (
                quantity + group.quantity,
                cost + group.quantity * group.average_price,
            )
        elif quantity > 0:
            average = cost / quantity if quantity else Decimal(0)
            matched = min(quantity, group.quantity)
            inventory_cost[key] = (quantity - matched, cost - average * matched)
            if matched > 0 and quantity - matched == 0:
                trip_key = (group.account_ref, group.identity, group.effective_at.date())
                round_trip_groups_day[trip_key] = round_trip_groups_day.get(trip_key, 0) + 1
    additions = add_winner + add_loser
    cost_proximity = sum(
        1
        for trip in trips
        if abs(trip.exit_price - trip.entry_price) / trip.entry_price <= Decimal("0.01")
    )
    metrics = TradeMetrics(
        len(groups),
        len(
            {
                *((x.account_ref, x.order_id) for x in normalized),
                *(
                    (str(x.get("account_ref", "")), str(x.get("order_id", "")))
                    for x in orders
                    if x.get("account_ref") and x.get("order_id")
                ),
            }
        ),
        len(normalized),
        len({fill.identity for fill in normalized}),
        currency_buckets,
        realized_by_currency,
        pnl_statistics_by_currency,
        buy_base,
        sell_base,
        None
        if buy_base is None or sell_base is None or portfolio_nav_base is None
        else (buy_base + sell_base) / (Decimal(2) * portfolio_nav_base),
        per_symbol_turnover,
        None if not holdings else sum(holdings, Decimal(0)) / len(holdings),
        _median(holdings),
        len(trips),
        None if not trips else Decimal(len(profitable_trips)) / len(trips),
        None if not pnl_base_complete or not profits else wins_total / len(profits),
        None if not pnl_base_complete or not losses else sum(losses, Decimal(0)) / len(losses),
        None
        if not pnl_base_complete or not profits or not losses
        else (wins_total / len(profits)) / (-sum(losses, Decimal(0)) / len(losses)),
        None if not pnl_base_complete or losses_total == 0 else wins_total / losses_total,
        None
        if not pnl_base_complete or not trips
        else sum((x.realized_pl_base for x in trips), Decimal(0)) / len(trips),
        reversals,
        None if not groups else Decimal(reversals) / len(groups),
        reentries,
        open_count,
        None if open_count is None or not groups else Decimal(open_count) / len(groups),
        sum(1 for count in activity.values() if count > 1),
        add_winner,
        add_loser,
        None if not additions else Decimal(add_loser) / additions,
        cost_proximity,
    )
    if source_unavailable:
        metrics = TradeMetrics.unavailable()
    checks: list[dict[str, Any]] = []
    if policy and not source_unavailable:
        per_day: dict[tuple[str, date], int] = {}
        for group in groups:
            day_key = (group.account_ref, group.effective_at.date())
            per_day[day_key] = per_day.get(day_key, 0) + 1
        for name, observed, limit in (
            (
                "market_open_no_trade_minutes",
                open_count,
                0 if open_count is not None else None,
            ),
            ("reentry_cooldown_minutes", reentries, 0 if reentries is not None else None),
            ("max_trades_per_day", max(per_day.values(), default=0), policy.max_trades_per_day),
            (
                "max_round_trips_per_symbol_day",
                max(round_trip_groups_day.values(), default=0),
                policy.max_round_trips_per_symbol_day,
            ),
            ("flag_add_to_loser", add_loser, 0 if policy.flag_add_to_loser else None),
            (
                "flag_cost_proximity_stop",
                cost_proximity,
                0 if policy.flag_cost_proximity else None,
            ),
        ):
            if limit is not None and observed is not None:
                checks.append(
                    {
                        "name": name,
                        "observed": observed,
                        "limit": limit,
                        "status": "BREACH" if observed > limit else "PASS",
                    }
                )
    notes = tuple(str(note) for note in source_notes)
    if in_progress_markets:
        notes = (
            *notes,
            "requested range includes the current exchange-local date for "
            + ", ".join(in_progress_markets)
            + "; the session may be incomplete",
        )
    if corporate_action_evidence_status == "UNSUPPORTED_EVIDENCE":
        notes = (
            *notes,
            "corporate-action, transfer, and cost-adjustment evidence is unsupported by the audited upstream fields",
        )
    if excluded_events:
        notes = (
            *notes,
            f"{len(excluded_events)} recognized non-trade events were excluded from FIFO and trade metrics",
        )
    if not base_complete and normalized:
        notes = (
            *notes,
            "base-currency monetary aggregation is unavailable because fill currency/FX evidence is incomplete",
        )
    if trips and not pnl_base_complete:
        notes = (
            *notes,
            "cross-currency historical base P&L is unavailable without transaction-date FX; snapshot FX is current-valuation evidence only",
        )
    if trips:
        notes = (*notes, "realized P&L is gross pre-fee because fee evidence is unavailable")
    if cooldown is None:
        notes = (*notes, "reentry metric is UNAVAILABLE because no cooldown policy is configured")
    if open_count is None:
        notes = (
            *notes,
            "market-open metric is UNAVAILABLE because no open-window policy is configured",
        )
    reconciliation_issues = (
        orders_missing_fills
        + orphan_fill_orders
        + order_fill_qty_mismatches
        + order_status_conflicts
    )
    if reconciliation_issues:
        notes = (*notes, "order/fill reconciliation contains incomplete or conflicting evidence")
    if unmatched_sells:
        notes = (
            *notes,
            f"{unmatched_sells} sell fills lack evidenced opening inventory in the requested history window; they were excluded from FIFO trip metrics",
        )
    if source_unavailable:
        notes = (
            *notes,
            "historical activity metrics are UNAVAILABLE because no requested context completed successfully",
        )
    status = (
        "BREACH"
        if any(x["status"] == "BREACH" for x in checks)
        else ("PASS" if checks else "UNCONFIGURED")
    )
    behavior: list[BehaviorFinding] = []
    policy_document = _json_value(policy) if policy is not None else None

    def add_behavior(
        kind: str,
        count: int,
        events: Sequence[OrderGroup],
        policy_name: str | None = None,
    ) -> None:
        if count <= 0 or not events:
            return
        ordered_events = sorted(
            {item.group_ref: item for item in events}.values(),
            key=lambda item: (item.effective_at, item.group_ref),
        )
        policy_breach = False
        if policy_name == "max_trades_per_day" and policy and policy.max_trades_per_day is not None:
            policy_breach = any(
                per_day.get((item.account_ref, item.effective_at.date()), 0)
                > policy.max_trades_per_day
                for item in ordered_events
            )
        elif policy_name == "flag_add_to_loser" and policy:
            policy_breach = policy.flag_add_to_loser
        elif policy_name in {"market_open_no_trade_minutes", "reentry_cooldown_minutes"}:
            policy_breach = any(
                item["name"] == policy_name and item["status"] == "BREACH" for item in checks
            )
        behavior.append(
            BehaviorFinding(
                kind,
                "MEDIUM" if policy_breach else "INFORMATIONAL",
                f"Observed {count} qualifying events in normalized execution evidence.",
                True,
                tuple(item.group_ref for item in ordered_events),
                tuple(sorted({item.identity for item in ordered_events})),
                ordered_events[0].effective_at,
                ordered_events[-1].effective_at,
            )
        )

    reversal_events = [
        item
        for pair in adjacent
        if pair[0].effective_at.date() == pair[1].effective_at.date()
        and pair[0].side != pair[1].side
        for item in pair
    ]
    add_behavior("SAME_DAY_REVERSAL", reversals, reversal_events)
    add_behavior(
        "SELL_THEN_REENTRY",
        len(observed_reentry_pairs),
        [item for pair in observed_reentry_pairs for item in pair],
    )
    add_behavior("MARKET_OPEN_ACTIVITY", len(observed_open_events), observed_open_events)
    add_behavior(
        "REENTRY_WITHIN_COOLDOWN",
        reentries or 0,
        [item for pair in reentry_pairs for item in pair],
        "reentry_cooldown_minutes",
    )
    add_behavior(
        "EARLY_SESSION_TRADING", open_count or 0, open_events, "market_open_no_trade_minutes"
    )
    repeated_keys = {key for key, count in activity.items() if count > 1}
    repeated_events: dict[tuple[str, str, date], list[OrderGroup]] = {}
    for group in groups:
        key = (group.account_ref, group.identity, group.effective_at.date())
        if key in repeated_keys:
            repeated_events.setdefault(key, []).append(group)
    for repeated_key in sorted(repeated_keys):
        key_events = repeated_events[repeated_key]
        add_behavior("REPEATED_SYMBOL_ACTIVITY", 1, key_events, "max_trades_per_day")
    budget.consume(len(normalized_orders) + len(normalized_all) + len(groups) + len(trips))
    add_behavior("ADD_TO_WINNER", add_winner, add_winner_events)
    add_behavior("ADD_TO_LOSER", add_loser, add_loser_events, "flag_add_to_loser")
    if source_unavailable:
        behavior.clear()
    evidenced_accounts = sorted(
        {fill.account_ref for fill in normalized_all}
        | {order.account_ref for order in normalized_orders}
    )
    evidenced_contexts = [
        {"account_ref": account, "market": market}
        for account, market in sorted(
            {(fill.account_ref, fill.market) for fill in normalized_all}
            | {(order.account_ref, order.market) for order in normalized_orders}
        )
    ]
    coverage = {
        "orders_received": len(orders),
        "deals_received": len(deals),
        "source_snapshot_id": source_snapshot_id,
        "accounts_requested": len(evidenced_accounts),
        "accounts_covered": len(evidenced_accounts),
        "contexts_requested": len(evidenced_contexts),
        "contexts_read": len(evidenced_contexts),
        "contexts_failed": 0,
        "contexts_omitted_budget": 0,
        "requested_contexts": evidenced_contexts,
        "successful_contexts": evidenced_contexts,
        "failed_contexts": [],
        "omitted_contexts": [],
        "partial_fill_orders": partial_fill_orders,
        "orders_missing_fills": orders_missing_fills,
        "orphan_fill_orders": orphan_fill_orders,
        "order_fill_qty_mismatches": order_fill_qty_mismatches,
        "order_status_conflicts": order_status_conflicts,
        "excluded_nontrade_events": len(excluded_events),
        "corporate_action_evidence_status": corporate_action_evidence_status,
        "monetary_base_coverage": "COMPLETE" if base_complete else "PARTIAL",
        "realized_pl_base_coverage": (
            "UNAVAILABLE" if not trips else "COMPLETE" if pnl_base_complete else "PARTIAL"
        ),
    }
    coverage.update(dict(coverage_context or {}))
    provenance = {
        "orders_command": "get_history_orders",
        "deals_command": "get_history_order_fill_list",
        "timezone_basis": "EXCHANGE_LOCAL",
        "monetary_basis": "LOCAL_CURRENCY_BUCKETS_BASE_IDENTITY_PNL_AND_VERIFIED_SNAPSHOT_FX",
        "realized_pl_basis": "GROSS_PRE_FEE",
        "orders_option_policy": "acc-id,market,trd-env,start,end,security-firm,ctx-type,limit=200,json",
        "deals_option_policy": "acc-id,market,trd-env,start,end,security-firm,ctx-type,json",
    }
    provenance.update(dict(provenance_context or {}))
    quality_issue_details: dict[str, str] = {}
    if coverage["failed_contexts"]:
        quality_issue_details["CONTEXT_FAILED"] = "one or more requested contexts failed atomically"
    if coverage["omitted_contexts"]:
        quality_issue_details["CONTEXT_OMITTED_BUDGET"] = (
            "one or more requested contexts were omitted by a bounded work budget"
        )
    if in_progress_markets:
        quality_issue_details["CURRENT_SESSION_INCOMPLETE"] = (
            "the requested range includes an exchange-local session that may still be in progress"
        )
    if corporate_action_evidence_status == "UNSUPPORTED_EVIDENCE":
        quality_issue_details["CORPORATE_ACTION_EVIDENCE_UNSUPPORTED"] = (
            "corporate-action, transfer, and cost-adjustment evidence is unsupported"
        )
    if excluded_events:
        quality_issue_details["NONTRADE_EVENTS_EXCLUDED"] = (
            "recognized corporate-action, transfer, or cost-adjustment rows were excluded from trade metrics"
        )
    if orphan_fill_orders:
        quality_issue_details["ORPHAN_FILL"] = "one or more fills have no matching normalized order"
    if orders_missing_fills:
        quality_issue_details["ORDER_MISSING_FILLS"] = (
            "one or more dealt orders lack normalized fills"
        )
    if order_fill_qty_mismatches:
        quality_issue_details["ORDER_FILL_QTY_MISMATCH"] = (
            "order dealt quantity and fill quantity do not reconcile"
        )
    if order_status_conflicts:
        quality_issue_details["ORDER_STATUS_CONFLICT"] = (
            "order status conflicts with dealt quantity or fills"
        )
    if unmatched_sells:
        quality_issue_details["UNMATCHED_SELL"] = (
            "sell evidence lacks opening inventory in the requested range"
        )
    if not base_complete and normalized:
        quality_issue_details["BASE_MONETARY_FX_INCOMPLETE"] = (
            "base monetary aggregation lacks complete verified FX evidence"
        )
    if trips and not pnl_base_complete:
        quality_issue_details["BASE_REALIZED_PNL_FX_INCOMPLETE"] = (
            "historical base realized P&L lacks transaction-date FX evidence"
        )
    if trips:
        quality_issue_details["FEES_UNAVAILABLE"] = (
            "realized P&L is gross pre-fee because fee evidence is unavailable"
        )
    if source_unavailable:
        quality_issue_details["SOURCE_UNAVAILABLE"] = "no requested context completed successfully"
    quality_issue_details["SOURCE_HISTORY_ROW_CAP"] = ""
    if cooldown is None:
        quality_issue_details["REENTRY_POLICY_UNCONFIGURED"] = ""
    if open_count is None:
        quality_issue_details["OPEN_WINDOW_POLICY_UNCONFIGURED"] = ""
    if source_snapshot_id is not None:
        quality_issue_details["TURNOVER_SNAPSHOT_NAV"] = ""
    quality_issues = tuple(canonical_quality_issues(quality_issue_details))
    notes = tuple(item["detail"] for item in quality_issues)
    identity_body = json.dumps(
        _json_value(
            {
                "schema_version": "1.5",
                "as_of": acquisition,
                "source": "futuapi",
                "base_currency": base_currency,
                "portfolio_nav_base": portfolio_nav_base,
                "source_snapshot_as_of": source_snapshot_as_of,
                "start": start,
                "end": end,
                "coverage": coverage,
                "normalized_orders": normalized_orders,
                "normalized_fills": normalized_all,
                "order_groups": groups,
                "round_trips": trips,
                "open_lots": open_lots,
                "metrics": metrics,
                "policy": policy_document,
                "policy_status": status,
                "policy_checks": checks,
                "behavior_findings": behavior,
                "unverified_interpretations": (
                    "Psychological motive cannot be verified from orders and fills alone.",
                ),
                "motive_status": "UNVERIFIED",
                "data_quality_status": (
                    "UNAVAILABLE" if source_unavailable else "PARTIAL" if notes else "COMPLETE"
                ),
                "quality_issues": quality_issues,
                "data_quality_notes": notes,
                "provenance": provenance,
            }
        ),
        sort_keys=True,
        separators=(",", ":"),
    )
    review_id = "tr_" + hashlib.sha256(identity_body.encode()).hexdigest()[:16]
    return TradeReview(
        "1.5",
        review_id,
        acquisition,
        "futuapi",
        base_currency,
        portfolio_nav_base,
        source_snapshot_as_of,
        start,
        end,
        coverage,
        normalized_orders,
        normalized_all,
        groups,
        trips,
        open_lots,
        metrics,
        policy_document,
        status,
        tuple(checks),
        tuple(behavior),
        ("Psychological motive cannot be verified from orders and fills alone.",),
        "UNVERIFIED",
        "UNAVAILABLE" if source_unavailable else "PARTIAL" if notes else "COMPLETE",
        quality_issues,
        notes,
        provenance,
    )


def validate_trade_context_rows(
    *,
    orders: Sequence[Mapping[str, Any]],
    deals: Sequence[Mapping[str, Any]],
    start: date,
    end: date,
    account_ref: str,
    queried_market: str,
) -> None:
    """Fully normalize one context before it is admitted as successful evidence."""

    context = {"account_ref": account_ref, "market": queried_market}
    probe = build_trade_review(
        orders=orders,
        deals=deals,
        start=start,
        end=end,
        acquired_at=datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc),
        coverage_context={
            "accounts_requested": 1,
            "accounts_covered": 1,
            "contexts_requested": 1,
            "contexts_read": 1,
            "contexts_failed": 0,
            "contexts_omitted_budget": 0,
            "requested_contexts": [context],
            "successful_contexts": [context],
            "failed_contexts": [],
            "omitted_contexts": [],
        },
        corporate_action_evidence_status="AVAILABLE",
    )
    if any(
        item.market != queried_market
        for item in (*probe.normalized_orders, *probe.normalized_fills)
    ):
        raise TradeReviewInputError("historical evidence market conflicts with query context")
    if any(
        probe.coverage[name]
        for name in (
            "orders_missing_fills",
            "orphan_fill_orders",
            "order_fill_qty_mismatches",
            "order_status_conflicts",
        )
    ):
        raise TradeReviewInputError("historical context order/fill evidence is inconsistent")
