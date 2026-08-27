"""Deterministic, read-only review of normalized historical stock fills."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import stat
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .models import _json_value
from .rendering import markdown_text, terminal_text


class TradeReviewInputError(ValueError):
    """Historical source data cannot be interpreted conservatively."""


class TradePolicyConfigError(ValueError):
    """Private trade policy is unsafe or violates its strict schema."""


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
    side: str
    quantity: Decimal
    price: Decimal
    executed_at: datetime
    exchange_timezone: str


@dataclass(frozen=True)
class OrderGroup:
    account_ref: str
    order_id: str
    identity: str
    side: str
    quantity: Decimal
    average_price: Decimal
    fill_count: int
    first_fill_at: datetime
    last_fill_at: datetime


@dataclass(frozen=True)
class RoundTrip:
    account_ref: str
    identity: str
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime
    closed_at: datetime
    realized_pl: Decimal
    holding_seconds: Decimal


@dataclass(frozen=True)
class OpenLot:
    account_ref: str
    identity: str
    quantity: Decimal
    price: Decimal
    opened_at: datetime


@dataclass(frozen=True)
class TradeMetrics:
    trade_count: int
    order_count: int
    fill_count: int
    distinct_symbols: int
    buy_notional: Decimal
    sell_notional: Decimal
    portfolio_turnover: Decimal | None
    per_symbol_turnover: Mapping[str, Decimal]
    average_holding_seconds: Decimal | None
    median_holding_seconds: Decimal | None
    closed_trips: int
    win_rate: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    payoff_ratio: Decimal | None
    profit_factor: Decimal | None
    expectancy: Decimal | None
    same_day_reversals: int
    same_day_reversal_rate: Decimal | None
    reentries_within_cooldown: int
    open_window_trades: int
    open_window_trade_rate: Decimal | None
    repeated_symbol_days: int
    add_to_winner_count: int | None
    add_to_loser_count: int | None
    add_to_loser_rate: Decimal | None
    cost_proximity_exit_count: int | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "per_symbol_turnover", MappingProxyType(dict(self.per_symbol_turnover))
        )


@dataclass(frozen=True)
class TradeReview:
    schema_version: str
    review_id: str
    source: str
    start: date
    end: date
    coverage: Mapping[str, Any]
    order_groups: tuple[OrderGroup, ...]
    round_trips: tuple[RoundTrip, ...]
    open_lots: tuple[OpenLot, ...]
    metrics: TradeMetrics
    policy_status: str
    policy_checks: tuple[Mapping[str, Any], ...]
    findings: tuple[str, ...]
    motive_status: str
    data_quality_status: str
    data_quality_notes: tuple[str, ...]
    provenance: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "coverage", MappingProxyType(dict(self.coverage)))
        object.__setattr__(self, "order_groups", tuple(self.order_groups))
        object.__setattr__(self, "round_trips", tuple(self.round_trips))
        object.__setattr__(self, "open_lots", tuple(self.open_lots))
        object.__setattr__(
            self, "policy_checks", tuple(MappingProxyType(dict(x)) for x in self.policy_checks)
        )
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "data_quality_notes", tuple(self.data_quality_notes))
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)

    def to_markdown(self, *, json_artifact: str | None = None) -> str:
        safe = markdown_text
        metrics = self.metrics
        lines = [
            "# Trade Review\n",
            f"- Review ID: {safe(self.review_id)}",
            f"- Range: {self.start.isoformat()} to {self.end.isoformat()}",
            f"- Data quality: {self.data_quality_status}",
            f"- Policy status: {self.policy_status}",
            "- Motive: unverified; only observed execution patterns are described.",
            f"- Orders / fills / symbols: {metrics.order_count} / {metrics.fill_count} / {metrics.distinct_symbols}",
            f"- Closed FIFO trips: {metrics.closed_trips}",
            f"- Win rate: {metrics.win_rate if metrics.win_rate is not None else 'Unavailable'}",
            f"- Buy / sell notional: {metrics.buy_notional} / {metrics.sell_notional}",
            f"- Same-day reversals: {metrics.same_day_reversals}",
            f"- Same-day reversal rate: {metrics.same_day_reversal_rate if metrics.same_day_reversal_rate is not None else 'Unavailable'}",
            f"- Reentries within configured cooldown: {metrics.reentries_within_cooldown}",
            f"- Trades in configured market-open window: {metrics.open_window_trades}",
            f"- Market-open-window rate: {metrics.open_window_trade_rate if metrics.open_window_trade_rate is not None else 'Unavailable'}",
            f"- Add-to-winner / add-to-loser: {metrics.add_to_winner_count} / {metrics.add_to_loser_count}",
            f"- Cost-proximity exits (within 1% of evidenced FIFO entry): {metrics.cost_proximity_exit_count}",
            "\n## Findings\n",
            *([f"- {safe(item)}" for item in self.findings] or ["- None"]),
            "\n## Data quality notes\n",
            *([f"- {safe(item)}" for item in self.data_quality_notes] or ["- None"]),
            "\n## Provenance\n",
            f"- Orders: {safe(self.provenance['orders_command'])}",
            f"- Deals: {safe(self.provenance['deals_command'])}",
            f"- Time basis: {safe(self.provenance['timezone_basis'])}",
        ]
        if json_artifact:
            lines.append(f"\nAuthoritative JSON: `{safe(json_artifact)}`\n")
        return "\n".join(lines)

    def to_human(self) -> str:
        safe = terminal_text
        return "\n".join(
            [
                "Trade review",
                f"Review: {safe(self.review_id)}",
                f"Range: {self.start.isoformat()} to {self.end.isoformat()}",
                f"Quality: {self.data_quality_status}",
                f"Policy: {self.policy_status}",
                "Motive: UNVERIFIED; findings describe observed behavior only.",
                f"Orders: {self.metrics.order_count}",
                f"Fills: {self.metrics.fill_count}",
                f"Closed FIFO trips: {self.metrics.closed_trips}",
                *[f"Finding: {safe(item)}" for item in self.findings],
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


def _fill(raw: Mapping[str, Any], start: date, end: date) -> NormalizedFill:
    required = {"deal_id", "order_id", "account_ref", "code", "side", "qty", "price", "create_time"}
    if set(raw) - required or not required <= set(raw):
        raise TradeReviewInputError("deal contract fields changed or are incomplete")
    code = str(raw["code"]).strip().upper()
    parts = code.split(".", 1)
    market = _SOURCE_MARKET_ALIASES.get(parts[0], parts[0]) if len(parts) == 2 else ""
    if len(parts) != 2 or market not in _MARKET_TIME or not parts[1]:
        raise TradeReviewInputError("deal requires a supported market-qualified identity")
    side = str(raw["side"]).strip().upper().replace("TRD_SIDE.", "").replace("TRDSIDE.", "")
    if side not in {"BUY", "SELL"}:
        raise TradeReviewInputError("deal side must be BUY or SELL")
    timezone_name = _MARKET_TIME[market][0]
    try:
        parsed = datetime.fromisoformat(str(raw["create_time"]))
    except ValueError:
        raise TradeReviewInputError("deal time must use ISO date-time format") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    else:
        parsed = parsed.astimezone(ZoneInfo(timezone_name))
    if not start <= parsed.date() <= end:
        raise TradeReviewInputError("deal falls outside the requested review range")
    identifiers = tuple(str(raw[name]).strip() for name in ("deal_id", "order_id", "account_ref"))
    if not all(identifiers):
        raise TradeReviewInputError("deal identifiers must be non-empty")
    return NormalizedFill(
        identifiers[0],
        identifiers[1],
        identifiers[2],
        f"{market}.{parts[1]}",
        market,
        side,
        _decimal(raw["qty"], "deal quantity"),
        _decimal(raw["price"], "deal price"),
        parsed,
        timezone_name,
    )


def _group_orders(fills: Sequence[NormalizedFill]) -> tuple[OrderGroup, ...]:
    groups: dict[tuple[str, str], list[NormalizedFill]] = {}
    for fill in fills:
        groups.setdefault((fill.account_ref, fill.order_id), []).append(fill)
    result = []
    for key in sorted(groups):
        items = sorted(groups[key], key=lambda x: (x.executed_at, x.deal_id))
        identity_side = {(x.identity, x.side) for x in items}
        if len(identity_side) != 1:
            raise TradeReviewInputError("one order ID spans conflicting security or side values")
        quantity = sum((x.quantity for x in items), Decimal(0))
        notional = sum((x.quantity * x.price for x in items), Decimal(0))
        result.append(
            OrderGroup(
                key[0],
                key[1],
                items[0].identity,
                items[0].side,
                quantity,
                notional / quantity,
                len(items),
                items[0].executed_at,
                items[-1].executed_at,
            )
        )
    return tuple(result)


def _fifo(
    fills: Sequence[NormalizedFill],
) -> tuple[tuple[RoundTrip, ...], tuple[OpenLot, ...], int]:
    inventories: dict[tuple[str, str], list[list[Any]]] = {}
    trips: list[RoundTrip] = []
    unmatched_sells = 0
    for fill in sorted(fills, key=lambda x: (x.executed_at, x.deal_id)):
        key = (fill.account_ref, fill.identity)
        lots = inventories.setdefault(key, [])
        if fill.side == "BUY":
            lots.append([fill.quantity, fill.price, fill.executed_at])
            continue
        remaining = fill.quantity
        while remaining and lots:
            lot = lots[0]
            matched = min(remaining, lot[0])
            seconds = Decimal(str((fill.executed_at - lot[2]).total_seconds()))
            trips.append(
                RoundTrip(
                    fill.account_ref,
                    fill.identity,
                    matched,
                    lot[1],
                    fill.price,
                    lot[2],
                    fill.executed_at,
                    (fill.price - lot[1]) * matched,
                    seconds,
                )
            )
            lot[0] -= matched
            remaining -= matched
            if lot[0] == 0:
                lots.pop(0)
        if remaining:
            unmatched_sells += 1
    open_lots = tuple(
        OpenLot(account, identity, lot[0], lot[1], lot[2])
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


def build_trade_review(
    *,
    orders: Sequence[Mapping[str, Any]],
    deals: Sequence[Mapping[str, Any]],
    start: date,
    end: date,
    policy: TradePolicy | None = None,
    source_notes: Iterable[str] = (),
    portfolio_nav: Decimal | None = None,
    source_snapshot_id: str | None = None,
) -> TradeReview:
    if start > end:
        raise TradeReviewInputError("review start must not follow end")
    normalized = tuple(_fill(raw, start, end) for raw in deals)
    deal_ids = [fill.deal_id for fill in normalized]
    if len(deal_ids) != len(set(deal_ids)):
        raise TradeReviewInputError("duplicate deal IDs are forbidden")
    groups = _group_orders(normalized)
    trips, open_lots, unmatched_sells = _fifo(normalized)
    buy = sum((x.quantity * x.price for x in normalized if x.side == "BUY"), Decimal(0))
    sell = sum((x.quantity * x.price for x in normalized if x.side == "SELL"), Decimal(0))
    per_symbol = {
        identity: sum(
            (x.quantity * x.price for x in normalized if x.identity == identity), Decimal(0)
        )
        for identity in sorted({x.identity for x in normalized})
    }
    holdings = [x.holding_seconds for x in trips]
    profits = [x.realized_pl for x in trips if x.realized_pl > 0]
    losses = [x.realized_pl for x in trips if x.realized_pl < 0]
    wins_total = sum(profits, Decimal(0))
    losses_total = -sum(losses, Decimal(0))
    chronological = sorted(normalized, key=lambda x: (x.executed_at, x.deal_id))
    by_security: dict[tuple[str, str], list[NormalizedFill]] = {}
    for fill in chronological:
        by_security.setdefault((fill.account_ref, fill.identity), []).append(fill)
    adjacent = [pair for items in by_security.values() for pair in zip(items, items[1:])]
    reversals = sum(
        1
        for left, right in adjacent
        if left.executed_at.date() == right.executed_at.date() and left.side != right.side
    )
    cooldown = policy.reentry_cooldown_minutes if policy else None
    reentries = sum(
        1
        for left, right in adjacent
        if cooldown is not None
        and left.side == "SELL"
        and right.side == "BUY"
        and right.executed_at - left.executed_at <= timedelta(minutes=cooldown)
    )
    open_minutes = policy.market_open_no_trade_minutes if policy else None
    market_open_rules = policy.market_open_no_trade_minutes_by_market if policy else {}
    open_count = 0
    if open_minutes is not None or market_open_rules:
        for fill in normalized:
            fill_window = market_open_rules.get(fill.market, open_minutes)
            if fill_window is None:
                continue
            _, hour, minute = _MARKET_TIME[fill.market]
            start_minute = hour * 60 + minute
            current = fill.executed_at.hour * 60 + fill.executed_at.minute
            open_count += int(start_minute <= current < start_minute + fill_window)
    activity: dict[tuple[str, str, date], int] = {}
    for fill in normalized:
        key = (fill.account_ref, fill.identity, fill.executed_at.date())
        activity[key] = activity.get(key, 0) + 1
    add_winner = 0
    add_loser = 0
    inventory_cost: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
    for fill in chronological:
        key = (fill.account_ref, fill.identity)
        quantity, cost = inventory_cost.get(key, (Decimal(0), Decimal(0)))
        if fill.side == "BUY":
            if quantity > 0:
                average = cost / quantity
                add_winner += int(fill.price > average)
                add_loser += int(fill.price < average)
            inventory_cost[key] = (quantity + fill.quantity, cost + fill.quantity * fill.price)
        elif quantity > 0:
            average = cost / quantity if quantity else Decimal(0)
            matched = min(quantity, fill.quantity)
            inventory_cost[key] = (quantity - matched, cost - average * matched)
    turnover = (
        {}
        if portfolio_nav is None or portfolio_nav <= 0
        else {key: value / (Decimal(2) * portfolio_nav) for key, value in per_symbol.items()}
    )
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
        len(per_symbol),
        buy,
        sell,
        None
        if portfolio_nav is None or portfolio_nav <= 0
        else (buy + sell) / (Decimal(2) * portfolio_nav),
        turnover,
        None if not holdings else sum(holdings, Decimal(0)) / len(holdings),
        _median(holdings),
        len(trips),
        None if not trips else Decimal(len(profits)) / len(trips),
        None if not profits else wins_total / len(profits),
        None if not losses else sum(losses, Decimal(0)) / len(losses),
        None
        if not profits or not losses
        else (wins_total / len(profits)) / (-sum(losses, Decimal(0)) / len(losses)),
        None if losses_total == 0 else wins_total / losses_total,
        None if not trips else sum((x.realized_pl for x in trips), Decimal(0)) / len(trips),
        reversals,
        None if not normalized else Decimal(reversals) / len(normalized),
        reentries,
        open_count,
        None if not normalized else Decimal(open_count) / len(normalized),
        sum(1 for count in activity.values() if count > 1),
        add_winner,
        add_loser,
        None if not additions else Decimal(add_loser) / additions,
        cost_proximity,
    )
    checks: list[dict[str, Any]] = []
    if policy:
        per_day: dict[tuple[str, date], int] = {}
        round_trips_day: dict[tuple[str, str, date], int] = {}
        for fill in normalized:
            day_key = (fill.account_ref, fill.executed_at.date())
            per_day[day_key] = per_day.get(day_key, 0) + 1
        for trip in trips:
            trip_key = (trip.account_ref, trip.identity, trip.closed_at.date())
            round_trips_day[trip_key] = round_trips_day.get(trip_key, 0) + 1
        for name, observed, limit in (
            (
                "market_open_no_trade_minutes",
                open_count,
                0 if open_minutes is not None or market_open_rules else None,
            ),
            ("reentry_cooldown_minutes", reentries, 0 if cooldown is not None else None),
            ("max_trades_per_day", max(per_day.values(), default=0), policy.max_trades_per_day),
            (
                "max_round_trips_per_symbol_day",
                max(round_trips_day.values(), default=0),
                policy.max_round_trips_per_symbol_day,
            ),
            ("flag_add_to_loser", add_loser, 0 if policy.flag_add_to_loser else None),
            (
                "flag_cost_proximity_stop",
                cost_proximity,
                0 if policy.flag_cost_proximity else None,
            ),
        ):
            if limit is not None:
                checks.append(
                    {
                        "name": name,
                        "observed": observed,
                        "limit": limit,
                        "status": "BREACH" if observed > limit else "PASS",
                    }
                )
    notes = tuple(str(note) for note in source_notes)
    if unmatched_sells:
        notes = (
            *notes,
            f"{unmatched_sells} sell fills lack evidenced opening inventory in the requested history window; they were excluded from FIFO trip metrics",
        )
    status = (
        "BREACH"
        if any(x["status"] == "BREACH" for x in checks)
        else ("PASS" if checks else "UNCONFIGURED")
    )
    findings = tuple(
        f"Observed {x['name']} policy breach." for x in checks if x["status"] == "BREACH"
    )
    identity_body = json.dumps(
        {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "deals": [_json_value(x) for x in normalized],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    review_id = "tr_" + hashlib.sha256(identity_body.encode()).hexdigest()[:16]
    return TradeReview(
        "1.0",
        review_id,
        "futuapi",
        start,
        end,
        {
            "orders_received": len(orders),
            "deals_received": len(deals),
            "source_snapshot_id": source_snapshot_id,
        },
        groups,
        trips,
        open_lots,
        metrics,
        status,
        tuple(checks),
        findings,
        "UNVERIFIED",
        "PARTIAL" if notes else "COMPLETE",
        notes,
        {
            "orders_command": "get_history_orders",
            "deals_command": "get_history_order_fill_list",
            "timezone_basis": "EXCHANGE_LOCAL",
        },
    )
