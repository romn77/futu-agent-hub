"""Conservative, evidence-bound portfolio performance attribution."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timezone
from decimal import Decimal
import hashlib
import inspect
import json
import queue
import re
import threading
import time as monotonic_time
from typing import Any, Callable, Mapping, Protocol, Sequence

from .models import PortfolioSnapshot, _deep_freeze, _json_value
from .rendering import markdown_text, terminal_text
from .snapshot_io import snapshot_from_document
from .trade_review import TradeReview


MAX_ATTRIBUTION_SNAPSHOTS = 366
MAX_ATTRIBUTION_POSITIONS = 2_000
MAX_ATTRIBUTION_FLOWS = 20_000
MAX_PRICE_REQUESTS = 100
MAX_PRICE_ROWS_PER_SYMBOL = 1_000
MAX_PRICE_TOTAL_ROWS = 20_000
PRICE_REQUEST_TIMEOUT_SECONDS = 2.0
MAX_PRICE_WALL_SECONDS = 30.0
MAX_CONSECUTIVE_PRICE_FAILURES = 3
MAX_ATTRIBUTION_PERIOD_DAYS = 366
MAX_CONTINUOUS_GAP_SECONDS = 36 * 60 * 60
TRADE_SOURCE_UNAVAILABLE_CODES = frozenset(
    {
        "TRADE_SOURCE_TIMEOUT",
        "TRADE_SOURCE_BUDGET_EXCEEDED",
        "TRADE_SOURCE_READ_FAILED",
        "TRADE_SOURCE_CONTRACT_INVALID",
        "TRADE_ALIAS_AUTHORITY_UNAVAILABLE",
    }
)


class AttributionInputError(ValueError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _decimal_is_bounded(value: Any) -> bool:
    if not isinstance(value, Decimal):
        return False
    parts = value.as_tuple()
    return (
        value.is_finite()
        and len(parts.digits) <= 256
        and abs(parts.exponent) <= 256
        and len(format(value, "f")) <= 256
    )


@dataclass(frozen=True)
class ExternalFlow:
    flow_id: str
    occurred_at: datetime
    flow_type: str
    currency: str
    amount_local: Decimal
    fx_to_base: Decimal
    amount_base: Decimal
    provenance: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", self.flow_id) is None:
            raise AttributionInputError("external flow ID is invalid")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise AttributionInputError("external flow timestamp must be timezone-aware")
        if self.flow_type not in {"DEPOSIT", "WITHDRAWAL", "INTERNAL_TRANSFER"}:
            raise AttributionInputError("external flow type is invalid")
        if (
            re.fullmatch(r"[A-Z]{3}", self.currency) is None
            or re.fullmatch(r"[A-Z][A-Z0-9_.:-]{0,127}", self.provenance) is None
        ):
            raise AttributionInputError("external flow currency and provenance are required")
        for value in (self.amount_local, self.fx_to_base, self.amount_base):
            if not _decimal_is_bounded(value):
                raise AttributionInputError("external flow values must be finite")
        if self.fx_to_base <= 0 or self.amount_local * self.fx_to_base != self.amount_base:
            raise AttributionInputError("external flow FX does not reconcile")
        if self.flow_type == "DEPOSIT" and self.amount_base <= 0:
            raise AttributionInputError("deposit must be positive")
        if self.flow_type == "WITHDRAWAL" and self.amount_base >= 0:
            raise AttributionInputError("withdrawal must be negative")


@dataclass(frozen=True)
class ExternalFlowEvidence:
    status: str
    source: str | None
    coverage_start: datetime | None
    coverage_end: datetime | None
    flows: tuple[ExternalFlow, ...]
    reason: str | None

    def __post_init__(self) -> None:
        if self.status not in {"COMPLETE", "UNAVAILABLE", "PARTIAL"}:
            raise AttributionInputError("flow evidence status is invalid")
        if self.reason is not None and (not self.reason or len(self.reason) > 2048):
            raise AttributionInputError("flow evidence reason is invalid")
        if len(self.flows) > MAX_ATTRIBUTION_FLOWS:
            raise AttributionInputError("external flow row budget exceeded")
        object.__setattr__(self, "flows", tuple(self.flows))
        identifiers = [item.flow_id for item in self.flows]
        if len(identifiers) != len(set(identifiers)):
            raise AttributionInputError("duplicate external flow ID")
        if self.status == "COMPLETE":
            if (
                self.source is None
                or re.fullmatch(r"[A-Z][A-Z0-9_.:-]{0,127}", self.source) is None
                or self.coverage_start is None
                or self.coverage_end is None
            ):
                raise AttributionInputError("complete flow evidence requires source and coverage")
            if (
                self.coverage_start.tzinfo is None
                or self.coverage_end.tzinfo is None
                or self.coverage_start > self.coverage_end
            ):
                raise AttributionInputError("flow evidence coverage is invalid")
            if self.reason is not None:
                raise AttributionInputError(
                    "complete flow evidence cannot have an unavailable reason"
                )
            if any(
                item.occurred_at < self.coverage_start or item.occurred_at > self.coverage_end
                for item in self.flows
            ):
                raise AttributionInputError("external flow lies outside evidence coverage")
        elif not self.reason:
            raise AttributionInputError("incomplete flow evidence requires a reason")
        elif self.status == "UNAVAILABLE" and (
            self.source is not None
            or self.coverage_start is not None
            or self.coverage_end is not None
            or self.flows
        ):
            raise AttributionInputError("unavailable flow evidence cannot contain source rows")
        elif self.status == "PARTIAL":
            if (
                self.source is None
                or re.fullmatch(r"[A-Z][A-Z0-9_.:-]{0,127}", self.source) is None
                or self.coverage_start is None
                or self.coverage_end is None
                or self.coverage_start.tzinfo is None
                or self.coverage_end.tzinfo is None
                or self.coverage_start > self.coverage_end
                or any(
                    item.occurred_at < self.coverage_start or item.occurred_at > self.coverage_end
                    for item in self.flows
                )
            ):
                raise AttributionInputError("partial flow evidence coverage is invalid")

    @classmethod
    def unavailable(cls, reason: str) -> ExternalFlowEvidence:
        return cls("UNAVAILABLE", None, None, None, (), reason)

    @classmethod
    def complete(
        cls,
        *,
        source: str,
        coverage_start: datetime,
        coverage_end: datetime,
        flows: Sequence[ExternalFlow] = (),
    ) -> ExternalFlowEvidence:
        return cls("COMPLETE", source, coverage_start, coverage_end, tuple(flows), None)

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


@dataclass(frozen=True)
class SnapshotInterval:
    snapshots: tuple[PortfolioSnapshot, ...]
    requested_start: date
    requested_end: date
    start_snapshot_id: str
    end_snapshot_id: str
    start_date_covered: bool
    end_date_covered: bool
    continuous: bool
    maximum_gap_seconds: int
    gaps: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshots", tuple(self.snapshots))
        object.__setattr__(self, "gaps", tuple(_deep_freeze(dict(item)) for item in self.gaps))


@dataclass(frozen=True)
class SymbolContribution:
    identity: str
    market: str
    symbol: str
    account_refs: tuple[str, ...]
    status: str
    method: str
    pnl_base: Decimal | None
    realized_pnl_base: Decimal | None
    unrealized_pnl_base: Decimal | None
    sector: str
    region: str
    themes: tuple[str, ...]
    evidence: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_refs", tuple(self.account_refs))
        object.__setattr__(self, "themes", tuple(self.themes))
        object.__setattr__(self, "evidence", _deep_freeze(dict(self.evidence)))


@dataclass(frozen=True)
class AttributionPricePoint:
    date: date
    adjusted_close: Decimal


@dataclass(frozen=True)
class PriceEvidence:
    identity: str
    status: str
    requested_start: date
    requested_end: date
    cutoff_date: date
    cutoff_policy: str
    first_date: date | None
    last_date: date | None
    rows: int
    points: tuple[AttributionPricePoint, ...]
    aligned_points: tuple[AttributionPricePoint, ...]
    source: str
    adjustment: str
    digest: str | None
    reason: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", tuple(self.points))
        object.__setattr__(self, "aligned_points", tuple(self.aligned_points))


@dataclass(frozen=True)
class TradeAttributionEvidence:
    status: str
    reason: str | None
    review_digest: str | None
    review: Mapping[str, Any] | None
    used_for_contributions: bool

    def __post_init__(self) -> None:
        if self.status not in {"COMPLETE", "UNAVAILABLE"}:
            raise AttributionInputError("trade attribution evidence status is invalid")
        object.__setattr__(
            self, "review", None if self.review is None else _deep_freeze(dict(self.review))
        )

    @classmethod
    def unavailable(cls, reason: str) -> TradeAttributionEvidence:
        return cls("UNAVAILABLE", reason, None, None, False)


@dataclass(frozen=True)
class PerformanceAttributionReport:
    schema_version: str
    attribution_id: str
    as_of: datetime
    analysis_now: datetime
    period: Mapping[str, Any]
    base_currency: str
    source_snapshot_ids: tuple[str, ...]
    source_snapshot_digests: Mapping[str, str]
    source_snapshot_quality: tuple[Mapping[str, Any], ...]
    coverage: Mapping[str, int]
    mode: str
    confidence: str
    contribution_status: str
    total_pnl_base: Decimal | None
    realized_pnl_base: Decimal | None
    unrealized_pnl_base: Decimal | None
    pnl_reconciliation: Mapping[str, Any]
    symbol_contributions: tuple[SymbolContribution, ...]
    sector_contributions: Mapping[str, Decimal]
    region_contributions: Mapping[str, Decimal]
    theme_full_contributions: Mapping[str, Decimal]
    theme_split_contributions: Mapping[str, Decimal]
    top_contributors: tuple[SymbolContribution, ...]
    top_detractors: tuple[SymbolContribution, ...]
    price_evidence: tuple[PriceEvidence, ...]
    trade_evidence: TradeAttributionEvidence
    flow_evidence: ExternalFlowEvidence
    external_flow_base: Decimal | None
    internal_transfer_base: Decimal | None
    return_status: str
    return_method: str | None
    portfolio_return: Decimal | None
    irr_status: str
    irr_method: str | None
    irr: Decimal | None
    quality_status: str
    quality_reasons: tuple[Mapping[str, str], ...]
    formulas: tuple[str, ...]
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "period",
            "source_snapshot_digests",
            "coverage",
            "pnl_reconciliation",
            "sector_contributions",
            "region_contributions",
            "theme_full_contributions",
            "theme_split_contributions",
        ):
            object.__setattr__(self, name, _deep_freeze(dict(getattr(self, name))))
        for name in (
            "source_snapshot_ids",
            "symbol_contributions",
            "top_contributors",
            "top_detractors",
            "price_evidence",
            "formulas",
            "limitations",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(
            self,
            "source_snapshot_quality",
            tuple(_deep_freeze(dict(item)) for item in self.source_snapshot_quality),
        )
        object.__setattr__(
            self,
            "quality_reasons",
            tuple(_deep_freeze(dict(item)) for item in self.quality_reasons),
        )

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)

    def to_human(self) -> str:
        safe = terminal_text
        payload = self.to_dict()
        lines = [
            f"Performance attribution {safe(self.attribution_id)}",
            f"Analysis now: {self.analysis_now.isoformat()}",
            f"Period: {payload['period']['requested_start']} to {payload['period']['requested_end']} (UTC dates)",
            f"Source snapshots: {', '.join(safe(item) for item in self.source_snapshot_ids)}",
            f"Mode / confidence: {self.mode} / {self.confidence}",
            f"Contribution status: {self.contribution_status}",
            f"P&L ({self.base_currency}): {self.total_pnl_base if self.total_pnl_base is not None else 'N/A'}",
            f"Realized / unrealized: {self.realized_pnl_base if self.realized_pnl_base is not None else 'N/A'} / {self.unrealized_pnl_base if self.unrealized_pnl_base is not None else 'N/A'}",
            f"P&L / NAV reconciliation: {safe(json.dumps(payload['pnl_reconciliation'], sort_keys=True))}",
            f"Portfolio return: {self.portfolio_return if self.portfolio_return is not None else 'N/A'} ({self.return_status})",
            f"Return method: {safe(self.return_method or 'N/A')}",
            f"IRR: N/A ({self.irr_status})",
            f"Coverage: {safe(json.dumps(payload['coverage'], sort_keys=True))}",
            f"Snapshot digests: {safe(json.dumps(payload['source_snapshot_digests'], sort_keys=True))}",
            f"Snapshot quality: {safe(json.dumps(payload['source_snapshot_quality'], sort_keys=True))}",
            f"Sector contributions: {safe(json.dumps(payload['sector_contributions'], ensure_ascii=False, sort_keys=True))}",
            f"Region contributions: {safe(json.dumps(payload['region_contributions'], ensure_ascii=False, sort_keys=True))}",
            f"Theme full contributions: {safe(json.dumps(payload['theme_full_contributions'], ensure_ascii=False, sort_keys=True))}",
            f"Theme split contributions: {safe(json.dumps(payload['theme_split_contributions'], ensure_ascii=False, sort_keys=True))}",
            "Top contributors:",
            *(
                [f"- {safe(item.identity)}: {item.pnl_base}" for item in self.top_contributors]
                or ["- None"]
            ),
            "Top detractors:",
            *(
                [f"- {safe(item.identity)}: {item.pnl_base}" for item in self.top_detractors]
                or ["- None"]
            ),
            "All symbol contributions:",
            *(
                [
                    f"- {safe(item.identity)}: status={item.status}; method={safe(item.method)}; pnl={item.pnl_base if item.pnl_base is not None else 'N/A'}; realized={item.realized_pnl_base if item.realized_pnl_base is not None else 'N/A'}; unrealized={item.unrealized_pnl_base if item.unrealized_pnl_base is not None else 'N/A'}; accounts={safe(','.join(item.account_refs))}"
                    for item in self.symbol_contributions
                ]
                or ["- None"]
            ),
            "Price evidence:",
            *(
                [
                    f"- {safe(item.identity)}: {item.status}; cutoff={item.cutoff_date} ({item.cutoff_policy}); rows={item.rows}; aligned_rows={len(item.aligned_points)}; digest={safe(item.digest or 'N/A')}; reason={safe(item.reason or 'N/A')}"
                    for item in self.price_evidence
                ]
                or ["- None"]
            ),
            f"Trade evidence: {safe(json.dumps(_json_value(self.trade_evidence), ensure_ascii=False, sort_keys=True))}",
            f"Flow evidence: {safe(json.dumps(self.flow_evidence.to_dict(), ensure_ascii=False, sort_keys=True))}",
            "Quality reasons:",
            *(
                [f"- {safe(item['code'])}: {safe(item['detail'])}" for item in self.quality_reasons]
                or ["- None"]
            ),
            "Limitations:",
            *([f"- {safe(item)}" for item in self.limitations] or ["- None"]),
            "Formulas:",
            *[f"- {safe(item)}" for item in self.formulas],
        ]
        return "\n".join(lines)

    def to_markdown(
        self, *, json_artifact: str | None = None, json_digest: str | None = None
    ) -> str:
        md = markdown_text
        payload = self.to_dict()
        lines = [
            "# Performance attribution",
            "",
            f"- attribution_id: `{md(self.attribution_id)}`",
            f"- Analysis now: `{md(self.analysis_now.isoformat())}`",
            f"- Period: {payload['period']['requested_start']} to {payload['period']['requested_end']} (inclusive UTC dates)",
            f"- Selected snapshots: `{md(self.source_snapshot_ids[0])}` to `{md(self.source_snapshot_ids[-1])}`",
            f"- Mode / confidence: {self.mode} / {self.confidence}",
            f"- Contribution status: {self.contribution_status}",
            f"- P&L ({md(self.base_currency)}): {self.total_pnl_base if self.total_pnl_base is not None else 'N/A'}",
            f"- Realized / unrealized: {self.realized_pnl_base if self.realized_pnl_base is not None else 'N/A'} / {self.unrealized_pnl_base if self.unrealized_pnl_base is not None else 'N/A'}",
            f"- P&L / NAV reconciliation: {md(json.dumps(payload['pnl_reconciliation'], sort_keys=True))}",
            f"- Portfolio return: {self.portfolio_return if self.portfolio_return is not None else 'N/A'} ({self.return_status})",
            f"- Return method: {md(self.return_method or 'N/A')}",
            f"- IRR: N/A ({self.irr_status})",
            f"- Coverage: {md(json.dumps(payload['coverage'], sort_keys=True))}",
            f"- Source snapshot digests: {md(json.dumps(payload['source_snapshot_digests'], sort_keys=True))}",
            f"- Source snapshot quality: {md(json.dumps(payload['source_snapshot_quality'], sort_keys=True))}",
            f"- Authoritative JSON: `{md(json_artifact)}`"
            if json_artifact
            else "- Authoritative JSON: current output",
            f"- Authoritative JSON SHA-256: `{md(json_digest)}`"
            if json_digest
            else "- Authoritative JSON SHA-256: current output",
            "",
            "## Top contributors",
            "",
            *(
                [f"- `{md(item.identity)}`: {item.pnl_base}" for item in self.top_contributors]
                or ["- None"]
            ),
            "",
            "## Top detractors",
            "",
            *(
                [f"- `{md(item.identity)}`: {item.pnl_base}" for item in self.top_detractors]
                or ["- None"]
            ),
            "",
            "## Contribution evidence",
            "",
        ]
        for item in self.symbol_contributions:
            lines.append(
                f"- `{md(item.identity)}`: status={item.status}; method={md(item.method)}; P&L={item.pnl_base if item.pnl_base is not None else 'N/A'}; realized={item.realized_pnl_base if item.realized_pnl_base is not None else 'N/A'}; unrealized={item.unrealized_pnl_base if item.unrealized_pnl_base is not None else 'N/A'}; accounts={md(','.join(item.account_refs))}"
            )
        lines.extend(["", "## Aggregates", ""])
        for label, values in (
            ("Sector", self.sector_contributions),
            ("Region", self.region_contributions),
            ("Theme full overlap", self.theme_full_contributions),
            ("Theme additive split", self.theme_split_contributions),
        ):
            lines.append(
                f"- {label}: {md(json.dumps(_json_value(values), ensure_ascii=False, sort_keys=True))}"
            )
        lines.extend(
            [
                "",
                "## Flow and return authority",
                "",
                f"- Flow evidence: {md(json.dumps(self.flow_evidence.to_dict(), ensure_ascii=False, sort_keys=True))}",
            ]
        )
        lines.extend(["", "## Price traceability", ""])
        lines.extend(
            [
                f"- `{md(item.identity)}`: {item.status}; cutoff={item.cutoff_date} ({item.cutoff_policy}); {item.first_date or 'N/A'} to {item.last_date or 'N/A'}; rows={item.rows}; aligned_rows={len(item.aligned_points)}; aligned_interval={(item.aligned_points[0].date if item.aligned_points else 'N/A')} to {(item.aligned_points[-1].date if item.aligned_points else 'N/A')}; digest={md(item.digest or 'N/A')}; reason={md(item.reason or 'N/A')}"
                for item in self.price_evidence
            ]
            or ["- No price series used; attribution is based on snapshot P&L fields."]
        )
        lines.extend(
            [
                "",
                "## Trade traceability",
                "",
                f"- {md(json.dumps(_json_value(self.trade_evidence), ensure_ascii=False, sort_keys=True))}",
            ]
        )
        lines.extend(["", "## Quality and limitations", ""])
        lines.extend(
            [f"- {md(item['code'])}: {md(item['detail'])}" for item in self.quality_reasons]
            or ["- No structured quality exceptions."]
        )
        lines.extend([f"- Limitation: {md(item)}" for item in self.limitations])
        lines.extend(["", "## Formulas", ""])
        lines.extend(f"- {md(item)}" for item in self.formulas)
        return "\n".join(lines) + "\n"


class PriceProvider(Protocol):
    def history(
        self, symbol: str, *, market: str, lookback: int, as_of: date, timeout_seconds: float = 5
    ) -> Any: ...


def align_price_sequences(
    series: Mapping[str, Sequence[Any]], *, start: date, end: date
) -> dict[str, tuple[Any, ...]]:
    """Align validated adjusted closes on common dates before forming return intervals."""

    if start > end or len(series) > MAX_PRICE_REQUESTS:
        raise AttributionInputError("price alignment range or symbol budget is invalid")
    normalized: dict[str, dict[date, Any]] = {}
    for identity, rows in sorted(series.items()):
        if len(rows) > MAX_PRICE_ROWS_PER_SYMBOL:
            raise AttributionInputError("price alignment row budget exceeded")
        dates: list[date] = []
        by_date: dict[date, Any] = {}
        for item in rows:
            if (
                item.date < start
                or item.date > end
                or item.adjusted_close <= 0
                or not _decimal_is_bounded(item.adjusted_close)
            ):
                raise AttributionInputError("price alignment contains an invalid row")
            dates.append(item.date)
            by_date[item.date] = item
        if dates != sorted(set(dates)):
            raise AttributionInputError("price alignment dates must be unique and ordered")
        normalized[identity] = by_date
    if not normalized:
        return {}
    common = set.intersection(*(set(rows) for rows in normalized.values()))
    ordered_common = tuple(sorted(common))
    return {
        identity: tuple(rows[day] for day in ordered_common)
        for identity, rows in normalized.items()
    }


def _validated_snapshots(snapshots: Sequence[PortfolioSnapshot]) -> tuple[PortfolioSnapshot, ...]:
    if len(snapshots) > MAX_ATTRIBUTION_SNAPSHOTS:
        raise AttributionInputError("snapshot interval budget exceeded")
    validated: list[PortfolioSnapshot] = []
    for item in snapshots:
        try:
            validated.append(snapshot_from_document(item.to_dict()))
        except Exception as exc:
            raise AttributionInputError("snapshot history contains invalid semantics") from exc
    by_id: dict[str, PortfolioSnapshot] = {}
    for item in validated:
        existing = by_id.get(item.snapshot_id)
        if existing is not None and existing.to_dict() != item.to_dict():
            raise AttributionInputError("snapshot interval contains conflicting duplicate identity")
        by_id[item.snapshot_id] = item
    ordered = tuple(sorted(by_id.values(), key=lambda item: (item.as_of, item.snapshot_id)))
    if len({item.as_of for item in ordered}) != len(ordered):
        raise AttributionInputError("snapshot interval contains conflicting same as-of timestamp")
    return ordered


def select_snapshot_interval(
    snapshots: Sequence[PortfolioSnapshot], *, start: date, end: date
) -> SnapshotInterval:
    if start > end or (end - start).days > MAX_ATTRIBUTION_PERIOD_DAYS:
        raise AttributionInputError("performance period is invalid or exceeds 366 days")
    ordered = _validated_snapshots(snapshots)
    start_at = datetime.combine(start, time.min, tzinfo=timezone.utc)
    end_at = datetime.combine(end, time.max, tzinfo=timezone.utc)
    selected = tuple(
        item for item in ordered if start_at <= item.as_of.astimezone(timezone.utc) <= end_at
    )
    if not selected:
        raise AttributionInputError("performance requires at least one validated snapshot")
    if any(len(item.positions) > MAX_ATTRIBUTION_POSITIONS for item in selected):
        raise AttributionInputError("performance position budget exceeded")
    gaps: list[Mapping[str, Any]] = []
    maximum_gap = 0
    for before, after in zip(selected, selected[1:]):
        delta = after.as_of - before.as_of
        microseconds = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
        seconds = (microseconds + 999_999) // 1_000_000
        maximum_gap = max(maximum_gap, seconds)
        if seconds > MAX_CONTINUOUS_GAP_SECONDS:
            gaps.append(
                {
                    "from_snapshot_id": before.snapshot_id,
                    "to_snapshot_id": after.snapshot_id,
                    "seconds": seconds,
                }
            )
    return SnapshotInterval(
        selected,
        start,
        end,
        selected[0].snapshot_id,
        selected[-1].snapshot_id,
        selected[0].as_of.astimezone(timezone.utc).date() == start,
        selected[-1].as_of.astimezone(timezone.utc).date() == end,
        not gaps,
        maximum_gap,
        tuple(gaps),
    )


def _identity(position: Any) -> str:
    return f"{position.market.upper()}.{position.symbol.upper()}"


def _group_positions(snapshot: PortfolioSnapshot) -> dict[str, tuple[Any, ...]]:
    grouped: dict[str, list[Any]] = {}
    for position in snapshot.positions:
        grouped.setdefault(_identity(position), []).append(position)
    return {key: tuple(value) for key, value in grouped.items()}


def _classification(rows: Sequence[Any]) -> tuple[str, str, tuple[str, ...]] | None:
    values = {(item.sector, item.region, tuple(sorted(item.themes))) for item in rows}
    return next(iter(values)) if len(values) == 1 else None


def _sum_optional(rows: Sequence[Any], field_name: str) -> Decimal | None:
    values = [getattr(item, field_name) for item in rows]
    if any(value is None for value in values):
        return None
    return sum(values, Decimal(0))


def _snapshot_digest(snapshot: PortfolioSnapshot) -> str:
    return hashlib.sha256(
        json.dumps(
            snapshot.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _position_fx_contract(snapshot: PortfolioSnapshot, row: Any) -> Mapping[str, Any]:
    evidence = next(
        (
            item
            for item in snapshot.fx_evidence
            if item.source_currency == row.currency
            and item.target_currency == snapshot.base_currency
        ),
        None,
    )
    if evidence is None:
        raise AttributionInputError("position FX rate lacks snapshot-bound evidence")
    return {
        "currency_provenance": row.currency_provenance,
        "fx_evidence_rate": evidence.rate,
        "fx_rate_target_currency": evidence.target_currency,
        "fx_rate_source": evidence.source,
        "fx_rate_effective_at": evidence.effective_at,
        "fx_rate_contract": "SNAPSHOT_AS_OF_NON_LOOKAHEAD_BASE_CONVERSION_V1",
    }


def _contributions(
    snapshots: Sequence[PortfolioSnapshot],
    *,
    consume: Any | None = None,
) -> tuple[tuple[SymbolContribution, ...], list[dict[str, str]]]:
    """Reconstruct every economic identity across every selected valuation."""

    if len(snapshots) < 2:
        raise AttributionInputError("snapshot contribution trajectory requires two valuations")
    grouped = tuple(_group_positions(snapshot) for snapshot in snapshots)
    reasons: list[dict[str, str]] = []
    output: list[SymbolContribution] = []
    identities = sorted(set().union(*(set(item) for item in grouped)))
    if len(identities) > MAX_ATTRIBUTION_POSITIONS:
        raise AttributionInputError("performance trajectory identity budget exceeded")
    for identity in identities:
        if consume is not None:
            consume()
        rows_by_snapshot = tuple(item.get(identity, ()) for item in grouped)
        if consume is not None:
            for rows in rows_by_snapshot:
                consume()
                for _row in rows:
                    consume()
        all_rows = tuple(row for rows in rows_by_snapshot for row in rows)
        start_rows, end_rows = rows_by_snapshot[0], rows_by_snapshot[-1]
        market, symbol = identity.split(".", 1)
        classification = _classification(all_rows)
        evidence = {
            "start_snapshot_id": snapshots[0].snapshot_id,
            "end_snapshot_id": snapshots[-1].snapshot_id,
            "start_present": bool(start_rows),
            "end_present": bool(end_rows),
            "start_rows": len(start_rows),
            "end_rows": len(end_rows),
            "trajectory": tuple(
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "as_of": snapshot.as_of,
                    "present": bool(rows),
                    "legs": tuple(
                        {
                            "account_ref": row.account_ref,
                            "quantity": row.quantity,
                            "cumulative_realized_pnl_local": row.realized_pl_local,
                            "cumulative_unrealized_pnl_local": row.unrealized_pl_local,
                            "cumulative_realized_pnl_base": row.realized_pl_base,
                            "cumulative_unrealized_pnl_base": row.unrealized_pl_base,
                            "currency": row.currency,
                            "fx_to_base": row.fx_to_base,
                            **_position_fx_contract(snapshot, row),
                            "sector": row.sector,
                            "region": row.region,
                            "themes": tuple(sorted(row.themes)),
                            "quality_notes": tuple(row.data_quality_notes),
                        }
                        for row in sorted(rows, key=lambda item: item.account_ref)
                    ),
                }
                for snapshot, rows in zip(snapshots, rows_by_snapshot)
            ),
        }
        if any(not rows for rows in rows_by_snapshot):
            reasons.append(
                {
                    "code": "TRAJECTORY_POSITION_DISCONTINUITY",
                    "scope": identity,
                    "detail": "position presence is discontinuous across selected snapshots and lacks authoritative reconciliation",
                }
            )
            output.append(
                SymbolContribution(
                    identity,
                    market,
                    symbol,
                    tuple(sorted({item.account_ref for item in all_rows})),
                    "UNAVAILABLE",
                    "TRAJECTORY_POSITION_DISCONTINUITY",
                    None,
                    None,
                    None,
                    *(classification or ("UNKNOWN", "UNKNOWN", ())),
                    evidence,
                )
            )
            continue
        account_sets = tuple({item.account_ref for item in rows} for rows in rows_by_snapshot)
        if any(accounts != account_sets[0] for accounts in account_sets[1:]):
            reasons.append(
                {
                    "code": "TRAJECTORY_ACCOUNT_LEGS_CHANGED",
                    "scope": identity,
                    "detail": "account-level position legs reset or transfer inside the selected trajectory",
                }
            )
            output.append(
                SymbolContribution(
                    identity,
                    market,
                    symbol,
                    tuple(sorted({item.account_ref for item in all_rows})),
                    "UNAVAILABLE",
                    "TRAJECTORY_ACCOUNT_LEGS_CHANGED",
                    None,
                    None,
                    None,
                    *(classification or ("UNKNOWN", "UNKNOWN", ())),
                    evidence,
                )
            )
            continue
        quantity_maps = tuple(
            {item.account_ref: item.quantity for item in rows} for rows in rows_by_snapshot
        )
        if any(quantities != quantity_maps[0] for quantities in quantity_maps[1:]):
            reasons.append(
                {
                    "code": "TRAJECTORY_QUANTITY_LIFECYCLE_UNRECONCILED",
                    "scope": identity,
                    "detail": "account-level quantity changes lack authoritative trade, transfer, or corporate-action reconciliation",
                }
            )
            output.append(
                SymbolContribution(
                    identity,
                    market,
                    symbol,
                    tuple(sorted({item.account_ref for item in all_rows})),
                    "UNAVAILABLE",
                    "TRAJECTORY_QUANTITY_LIFECYCLE_UNRECONCILED",
                    None,
                    None,
                    None,
                    *(classification or ("UNKNOWN", "UNKNOWN", ())),
                    evidence,
                )
            )
            continue
        financial_contracts = {
            (
                row.account_ref,
                row.currency,
                contract["currency_provenance"],
                contract["fx_rate_target_currency"],
                contract["fx_rate_source"],
                contract["fx_rate_contract"],
            )
            for snapshot, rows in zip(snapshots, rows_by_snapshot)
            for row in rows
            for contract in (_position_fx_contract(snapshot, row),)
        }
        if len(financial_contracts) != len(account_sets[0]):
            reasons.append(
                {
                    "code": "TRAJECTORY_FX_DISCONTINUITY",
                    "scope": identity,
                    "detail": "position currency or FX provenance/authority contract changes across account legs inside the trajectory",
                }
            )
            output.append(
                SymbolContribution(
                    identity,
                    market,
                    symbol,
                    tuple(sorted({item.account_ref for item in all_rows})),
                    "UNAVAILABLE",
                    "TRAJECTORY_FX_DISCONTINUITY",
                    None,
                    None,
                    None,
                    *(classification or ("UNKNOWN", "UNKNOWN", ())),
                    evidence,
                )
            )
            continue
        if classification is None:
            reasons.append(
                {
                    "code": "CLASSIFICATION_CHANGED",
                    "scope": identity,
                    "detail": "classification is inconsistent across accounts or endpoints",
                }
            )
            output.append(
                SymbolContribution(
                    identity,
                    market,
                    symbol,
                    tuple(sorted({item.account_ref for item in all_rows})),
                    "UNAVAILABLE",
                    "CLASSIFICATION_INCONSISTENT",
                    None,
                    None,
                    None,
                    "UNKNOWN",
                    "UNKNOWN",
                    (),
                    evidence,
                )
            )
            continue
        cumulative = tuple(
            (
                _sum_optional(rows, "realized_pl_base"),
                _sum_optional(rows, "unrealized_pl_base"),
            )
            for rows in rows_by_snapshot
        )
        if any(None in values for values in cumulative):
            reasons.append(
                {
                    "code": "PNL_FIELDS_INCOMPLETE",
                    "scope": identity,
                    "detail": "realized/unrealized base P&L is incomplete",
                }
            )
            output.append(
                SymbolContribution(
                    identity,
                    market,
                    symbol,
                    tuple(sorted({item.account_ref for item in all_rows})),
                    "UNAVAILABLE",
                    "SNAPSHOT_PNL_FIELDS_INCOMPLETE",
                    None,
                    None,
                    None,
                    *classification,
                    evidence,
                )
            )
            continue
        for _before, _after in zip(cumulative, cumulative[1:]):
            if consume is not None:
                consume()
        realized = sum(
            (after[0] - before[0] for before, after in zip(cumulative, cumulative[1:])),
            Decimal(0),
        )
        unrealized = sum(
            (after[1] - before[1] for before, after in zip(cumulative, cumulative[1:])),
            Decimal(0),
        )
        output.append(
            SymbolContribution(
                identity,
                market,
                symbol,
                tuple(sorted({item.account_ref for item in all_rows})),
                "AVAILABLE",
                "SNAPSHOT_ADJACENT_CUMULATIVE_PNL_DELTA",
                realized + unrealized,
                realized,
                unrealized,
                *classification,
                evidence,
            )
        )
    return tuple(output), reasons


def _aggregate(
    contributions: Sequence[SymbolContribution], attribute: str, *, split: bool = False
) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for item in contributions:
        if item.pnl_base is None:
            continue
        labels = getattr(item, attribute)
        if isinstance(labels, str):
            labels = (labels,)
        labels = tuple(sorted(labels)) or ("UNKNOWN",)
        if split:
            allocation = item.pnl_base / Decimal(len(labels))
            allocations = [allocation for _ in labels[:-1]]
            allocations.append(item.pnl_base - sum(allocations, Decimal(0)))
        else:
            allocations = [item.pnl_base for _ in labels]
        for label, allocation in zip(labels, allocations):
            totals[label] = totals.get(label, Decimal(0)) + allocation
    return dict(sorted(totals.items()))


def _price_evidence(
    identities: Sequence[str],
    *,
    provider: PriceProvider | None,
    start: date,
    end: date,
    end_instant: datetime,
) -> tuple[PriceEvidence, ...]:
    if provider is None:
        return ()
    output: list[PriceEvidence] = []
    started = monotonic_time.monotonic()
    consecutive_failures = 0
    circuit_reason: str | None = None
    total_rows = 0
    for identity in identities[:MAX_PRICE_REQUESTS]:
        market, symbol = identity.split(".", 1)
        from .risk import MARKET_CLOSE_SCHEDULE_VERSION, _latest_complete_market_close_date

        cutoff = min(end, _latest_complete_market_close_date(end_instant, market))
        if monotonic_time.monotonic() - started >= MAX_PRICE_WALL_SECONDS:
            circuit_reason = "price wall-time budget exhausted"
        if circuit_reason is not None:
            output.append(
                PriceEvidence(
                    identity,
                    "UNAVAILABLE",
                    start,
                    end,
                    cutoff,
                    MARKET_CLOSE_SCHEDULE_VERSION,
                    None,
                    None,
                    0,
                    (),
                    (),
                    "AUDITED_FUTU_GET_KLINE",
                    "FORWARD_ADJUSTED_DAILY_CLOSE",
                    None,
                    circuit_reason,
                )
            )
            continue
        try:
            result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

            def invoke(
                *,
                bound_provider: PriceProvider = provider,
                bound_result: queue.Queue[tuple[bool, Any]] = result,
                bound_symbol: str = symbol,
                bound_market: str = market,
                bound_cutoff: date = cutoff,
            ) -> None:
                try:
                    kwargs: dict[str, Any] = {
                        "market": bound_market,
                        "lookback": min(
                            MAX_PRICE_ROWS_PER_SYMBOL,
                            max((end - start).days * 2 + 10, 20),
                        ),
                        "as_of": bound_cutoff,
                    }
                    if "timeout_seconds" in inspect.signature(bound_provider.history).parameters:
                        kwargs["timeout_seconds"] = PRICE_REQUEST_TIMEOUT_SECONDS
                    bound_result.put((True, bound_provider.history(bound_symbol, **kwargs)))
                except BaseException as exc:
                    bound_result.put((False, exc))

            remaining = max(
                0.001,
                min(
                    PRICE_REQUEST_TIMEOUT_SECONDS,
                    MAX_PRICE_WALL_SECONDS - (monotonic_time.monotonic() - started),
                ),
            )
            worker = threading.Thread(target=invoke, daemon=True, name="performance-price-history")
            worker.start()
            worker.join(remaining)
            if worker.is_alive():
                raise TimeoutError("bounded price request timed out")
            ok, response = result.get_nowait()
            if not ok:
                raise response
            rows = response
            if rows is None:
                raw_rows = ()
            elif not isinstance(rows, Sequence) or len(rows) > MAX_PRICE_ROWS_PER_SYMBOL:
                raise AttributionInputError("price row budget exceeded")
            else:
                raw_rows = tuple(rows)
            if any(
                item.adjusted_close <= 0 or not _decimal_is_bounded(item.adjusted_close)
                for item in raw_rows
            ):
                raise AttributionInputError("price series contains invalid closes")
            if tuple(sorted({item.date for item in raw_rows})) != tuple(
                item.date for item in raw_rows
            ) or any(item.date > cutoff for item in raw_rows):
                raise AttributionInputError("price dates must be unique and ordered")
            bounded = tuple(item for item in raw_rows if start <= item.date)
            if total_rows + len(bounded) > MAX_PRICE_TOTAL_ROWS:
                circuit_reason = "price aggregate row budget exhausted"
                output.append(
                    PriceEvidence(
                        identity,
                        "UNAVAILABLE",
                        start,
                        end,
                        cutoff,
                        MARKET_CLOSE_SCHEDULE_VERSION,
                        None,
                        None,
                        0,
                        (),
                        (),
                        "AUDITED_FUTU_GET_KLINE",
                        "FORWARD_ADJUSTED_DAILY_CLOSE",
                        None,
                        circuit_reason,
                    )
                )
                continue
            total_rows += len(bounded)
            raw = "|".join(f"{item.date.isoformat()}:{item.adjusted_close}" for item in bounded)
            points = tuple(
                AttributionPricePoint(item.date, item.adjusted_close) for item in bounded
            )
            output.append(
                PriceEvidence(
                    identity,
                    "AVAILABLE" if bounded else "UNAVAILABLE",
                    start,
                    end,
                    cutoff,
                    MARKET_CLOSE_SCHEDULE_VERSION,
                    bounded[0].date if bounded else None,
                    bounded[-1].date if bounded else None,
                    len(bounded),
                    points,
                    (),
                    "AUDITED_FUTU_GET_KLINE",
                    "FORWARD_ADJUSTED_DAILY_CLOSE",
                    hashlib.sha256(raw.encode()).hexdigest() if bounded else None,
                    None
                    if bounded
                    else "no rows in requested interval before the completed market close cutoff",
                )
            )
            # None marks unavailable identity evidence, not a failed source request.
            if rows is not None:
                consecutive_failures = 0 if bounded else consecutive_failures + 1
        except Exception:
            output.append(
                PriceEvidence(
                    identity,
                    "UNAVAILABLE",
                    start,
                    end,
                    cutoff,
                    MARKET_CLOSE_SCHEDULE_VERSION,
                    None,
                    None,
                    0,
                    (),
                    (),
                    "AUDITED_FUTU_GET_KLINE",
                    "FORWARD_ADJUSTED_DAILY_CLOSE",
                    None,
                    "bounded price request failed",
                )
            )
            consecutive_failures += 1
        if consecutive_failures >= MAX_CONSECUTIVE_PRICE_FAILURES:
            circuit_reason = "price source circuit breaker opened after consecutive failures"
    return tuple(output)


def _align_price_evidence(
    evidence: Sequence[PriceEvidence], *, start: date, end: date
) -> tuple[PriceEvidence, ...]:
    """Bind every usable series to one common, non-look-ahead interval."""

    usable = {item.identity: item.points for item in evidence if item.status == "AVAILABLE"}
    if not usable:
        return tuple(evidence)
    aligned = align_price_sequences(usable, start=start, end=end)
    return tuple(replace(item, aligned_points=aligned.get(item.identity, ())) for item in evidence)


def _reconstructed_contributions(
    end_snapshot: PortfolioSnapshot,
    prices: Sequence[PriceEvidence],
) -> tuple[tuple[SymbolContribution, ...], list[dict[str, str]]]:
    """Estimate current gross unrealized P&L; never imply realized/exited coverage."""

    by_price = {item.identity: item for item in prices}
    output: list[SymbolContribution] = []
    reasons: list[dict[str, str]] = []
    for identity, rows in sorted(_group_positions(end_snapshot).items()):
        market, symbol = identity.split(".", 1)
        classification = _classification(rows) or ("UNKNOWN", "UNKNOWN", ())
        price = by_price.get(identity)
        evidence: dict[str, Any] = {
            "start_snapshot_id": end_snapshot.snapshot_id,
            "end_snapshot_id": end_snapshot.snapshot_id,
            "start_present": False,
            "end_present": True,
            "start_rows": 0,
            "end_rows": len(rows),
            "ending_quantity": sum((item.quantity for item in rows), Decimal(0)),
            "ending_quantity_by_account": {
                item.account_ref: item.quantity
                for item in sorted(rows, key=lambda row: row.account_ref)
            },
        }
        method = "RECONSTRUCTION_EVIDENCE_INCOMPLETE"
        amount: Decimal | None = None
        currencies = {item.currency for item in rows}
        fx_values = {item.fx_to_base for item in rows}
        if currencies != {end_snapshot.base_currency} or fx_values != {Decimal(1)}:
            evidence["reconstruction_reason"] = "TRANSACTION_PERIOD_FX_UNAVAILABLE"
            reasons.append(
                {
                    "code": "RECONSTRUCTED_TRANSACTION_PERIOD_FX_UNAVAILABLE",
                    "scope": identity,
                    "detail": "historical transaction-period FX is unavailable; cross-currency gross P&L is not estimated",
                }
            )
        elif price is None or len(price.aligned_points) < 2:
            evidence["reconstruction_reason"] = "PRICE_INTERVAL_UNAVAILABLE"
            reasons.append(
                {
                    "code": "RECONSTRUCTED_PRICE_INTERVAL_UNAVAILABLE",
                    "scope": identity,
                    "detail": "at least two common adjusted-close points are required for reconstruction",
                }
            )
        else:
            quantity = Decimal(evidence["ending_quantity"])
            first_price, last_price = price.aligned_points[0], price.aligned_points[-1]
            amount = quantity * (last_price.adjusted_close - first_price.adjusted_close)
            method = "PRICE_QUANTITY_GROSS_UNREALIZED_ESTIMATE"
            evidence.update(
                {
                    "price_digest": price.digest,
                    "aligned_start_date": first_price.date,
                    "aligned_end_date": last_price.date,
                    "aligned_start_close": first_price.adjusted_close,
                    "aligned_end_close": last_price.adjusted_close,
                    "ending_quantity": quantity,
                    "ending_fx_to_base": Decimal(1),
                }
            )
        output.append(
            SymbolContribution(
                identity,
                market,
                symbol,
                tuple(sorted({item.account_ref for item in rows})),
                "ESTIMATED" if amount is not None else "UNAVAILABLE",
                method,
                amount,
                None,
                amount,
                *classification,
                evidence,
            )
        )
    if output:
        reasons.append(
            {
                "code": "RECONSTRUCTED_REALIZED_AND_EXITED_UNAVAILABLE",
                "scope": "symbol_contributions",
                "detail": "no pinned normalized trade source was supplied; realized P&L and exited positions are excluded",
            }
        )
    return tuple(output), reasons


def _trade_attribution_evidence(
    trade_history: TradeReview | None,
    *,
    start: date,
    end: date,
    snapshot: PortfolioSnapshot,
    use_contributions: bool,
    unavailable_reason: str | None,
) -> TradeAttributionEvidence:
    if trade_history is None:
        return TradeAttributionEvidence.unavailable(
            unavailable_reason or "authoritative normalized trade history was not supplied"
        )
    from .schema import validate_trade_review_document

    document = trade_history.to_dict()
    try:
        validate_trade_review_document(document)
    except Exception as exc:
        raise AttributionInputError("trade history semantics are invalid") from exc
    if (
        trade_history.start != start
        or trade_history.end != end
        or trade_history.base_currency != snapshot.base_currency
        or trade_history.source_snapshot_as_of != snapshot.as_of
        or trade_history.coverage.get("source_snapshot_id") != snapshot.snapshot_id
    ):
        raise AttributionInputError("trade history period, currency, or source snapshot mismatch")
    account_refs = {item.account_ref for item in snapshot.accounts}
    if any(
        item.account_ref not in account_refs
        for item in (*trade_history.normalized_orders, *trade_history.normalized_fills)
    ):
        raise AttributionInputError("trade history account aliases do not match the snapshot")
    fatal_codes = {
        "CONTEXT_FAILED",
        "CONTEXT_OMITTED_BUDGET",
        "CORPORATE_ACTION_EVIDENCE_UNSUPPORTED",
        "NONTRADE_EVENTS_EXCLUDED",
        "ORPHAN_FILL",
        "ORDER_MISSING_FILLS",
        "ORDER_FILL_QTY_MISMATCH",
        "ORDER_STATUS_CONFLICT",
        "UNMATCHED_SELL",
        "SOURCE_UNAVAILABLE",
    }
    observed_codes = {str(item["code"]) for item in trade_history.quality_issues}
    current_identities = set(_group_positions(snapshot))
    unmatched_open_lot = any(
        item.identity not in current_identities for item in trade_history.open_lots
    )
    expected_open_quantities = {
        (item.account_ref, _identity(item)): item.quantity for item in snapshot.positions
    }
    observed_open_quantities: dict[tuple[str, str], Decimal] = {}
    for item in trade_history.open_lots:
        key = (item.account_ref, item.identity)
        observed_open_quantities[key] = (
            observed_open_quantities.get(key, Decimal(0)) + item.quantity
        )
    open_lots_reconcile = not use_contributions or (
        observed_open_quantities == expected_open_quantities
    )
    if observed_codes & fatal_codes or unmatched_open_lot or not open_lots_reconcile:
        return TradeAttributionEvidence(
            "UNAVAILABLE",
            "trade history contains incomplete contexts, opening inventory, nontrade events, or order/fill conflicts",
            hashlib.sha256(
                json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            document,
            False,
        )
    digest = hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return TradeAttributionEvidence("COMPLETE", None, digest, document, use_contributions)


def _merge_trade_fifo_contributions(
    contributions: Sequence[SymbolContribution],
    trade_evidence: TradeAttributionEvidence,
) -> tuple[tuple[SymbolContribution, ...], list[dict[str, str]]]:
    if not trade_evidence.used_for_contributions or trade_evidence.review is None:
        return tuple(contributions), []
    trips = tuple(trade_evidence.review["round_trips"])
    open_identities = {item["identity"] for item in trade_evidence.review["open_lots"]}
    open_by_identity: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for index, lot in enumerate(trade_evidence.review["open_lots"]):
        open_by_identity.setdefault(str(lot["identity"]), []).append((index, lot))
    by_identity: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for index, trip in enumerate(trips):
        by_identity.setdefault(str(trip["identity"]), []).append((index, trip))
    output = {item.identity: item for item in contributions}
    reasons: list[dict[str, str]] = []
    for identity, indexed in sorted(by_identity.items()):
        local_by_currency: dict[str, Decimal] = {}
        for _index, trip in indexed:
            currency = str(trip["currency"])
            local_by_currency[currency] = local_by_currency.get(currency, Decimal(0)) + Decimal(
                trip["realized_pl_local"]
            )
        base_values = [trip["realized_pl_base"] for _index, trip in indexed]
        existing = output.get(identity)
        evidence = (
            dict(existing.evidence)
            if existing is not None
            else {
                "start_snapshot_id": trade_evidence.review["coverage"]["source_snapshot_id"],
                "end_snapshot_id": trade_evidence.review["coverage"]["source_snapshot_id"],
                "start_present": False,
                "end_present": False,
                "start_rows": 0,
                "end_rows": 0,
            }
        )
        evidence.update(
            {
                "trade_review_id": trade_evidence.review["review_id"],
                "trade_review_digest": trade_evidence.review_digest,
                "trade_round_trip_indexes": tuple(index for index, _trip in indexed),
                "realized_local_by_currency": local_by_currency,
            }
        )
        market, symbol = identity.split(".", 1)
        accounts = tuple(sorted({str(trip["account_ref"]) for _index, trip in indexed}))
        if any(value is None for value in base_values) or len(local_by_currency) != 1:
            reasons.append(
                {
                    "code": "TRADE_FIFO_BASE_FX_UNAVAILABLE",
                    "scope": identity,
                    "detail": "FIFO realized gross P&L is retained only in local-currency buckets because transaction-date base FX is unavailable",
                }
            )
            if existing is None:
                output[identity] = SymbolContribution(
                    identity,
                    market,
                    symbol,
                    accounts,
                    "UNAVAILABLE",
                    "TRADE_FIFO_BASE_FX_UNAVAILABLE",
                    None,
                    None,
                    None,
                    "UNKNOWN",
                    "UNKNOWN",
                    (),
                    evidence,
                )
            else:
                output[identity] = replace(
                    existing,
                    account_refs=tuple(sorted(set(existing.account_refs) | set(accounts))),
                    status="UNAVAILABLE",
                    method="TRADE_FIFO_BASE_FX_UNAVAILABLE",
                    pnl_base=None,
                    realized_pnl_base=None,
                    evidence=evidence,
                )
            continue
        realized = sum((Decimal(value) for value in base_values), Decimal(0))
        if existing is None:
            if identity in open_identities:
                reasons.append(
                    {
                        "code": "TRADE_OPEN_LOT_POSITION_MISMATCH",
                        "scope": identity,
                        "detail": "FIFO leaves an open lot but the ending snapshot has no matching current position",
                    }
                )
                continue
            output[identity] = SymbolContribution(
                identity,
                market,
                symbol,
                accounts,
                "ESTIMATED",
                "TRADE_FIFO_REALIZED_GROSS_PRE_FEE",
                realized,
                realized,
                Decimal(0),
                "UNKNOWN",
                "UNKNOWN",
                (),
                evidence,
            )
        else:
            unrealized = existing.unrealized_pnl_base
            output[identity] = replace(
                existing,
                account_refs=tuple(sorted(set(existing.account_refs) | set(accounts))),
                status="ESTIMATED",
                method=(
                    "TRADE_FIFO_REALIZED_PLUS_PRICE_UNREALIZED_ESTIMATE"
                    if unrealized is not None
                    else "TRADE_FIFO_REALIZED_GROSS_PRE_FEE"
                ),
                pnl_base=realized + (unrealized or Decimal(0)),
                realized_pnl_base=realized,
                evidence=evidence,
            )
    for identity, indexed_lots in sorted(open_by_identity.items()):
        existing = output.get(identity)
        if existing is None:
            continue
        evidence = dict(existing.evidence)
        evidence.update(
            {
                "trade_review_id": trade_evidence.review["review_id"],
                "trade_review_digest": trade_evidence.review_digest,
                "trade_round_trip_indexes": evidence.get("trade_round_trip_indexes", ()),
                "realized_local_by_currency": evidence.get("realized_local_by_currency", {}),
                "open_lot_indexes": tuple(index for index, _lot in indexed_lots),
                "price_fill_consistency": "CORPORATE_ACTION_EVIDENCE_AVAILABLE_FORWARD_ADJUSTED",
            }
        )
        from .risk import _first_completed_market_session_on_or_after

        market = identity.split(".", 1)[0]
        first_session_dates = tuple(
            _first_completed_market_session_on_or_after(
                datetime.fromisoformat(str(lot["opened_at"]).replace("Z", "+00:00")), market
            )
            for _index, lot in indexed_lots
        )
        evidence["open_lot_first_completed_session_dates"] = first_session_dates
        price_close = evidence.get("aligned_end_close")
        aligned_end_date = evidence.get("aligned_end_date")
        base_currency = trade_evidence.review["base_currency"]
        if aligned_end_date is not None and any(
            aligned_end_date < first_session for first_session in first_session_dates
        ):
            output[identity] = replace(
                existing,
                status="UNAVAILABLE",
                method="TRADE_FIFO_OPEN_LOT_PRICE_PRE_ENTRY",
                pnl_base=None,
                realized_pnl_base=None,
                unrealized_pnl_base=None,
                evidence=evidence,
            )
            reasons.append(
                {
                    "code": "TRADE_FIFO_OPEN_LOT_PRICE_PRE_ENTRY",
                    "scope": identity,
                    "detail": "aligned ending close precedes an open lot's first eligible completed market session",
                }
            )
            continue
        if price_close is None or any(lot["currency"] != base_currency for _, lot in indexed_lots):
            output[identity] = replace(
                existing,
                status="UNAVAILABLE",
                method="TRADE_FIFO_OPEN_LOT_EVIDENCE_INCOMPLETE",
                pnl_base=None,
                realized_pnl_base=None,
                unrealized_pnl_base=None,
                evidence=evidence,
            )
            reasons.append(
                {
                    "code": "TRADE_FIFO_OPEN_LOT_EVIDENCE_INCOMPLETE",
                    "scope": identity,
                    "detail": "open-lot currency or ending price evidence cannot authorize gross unrealized P&L",
                }
            )
            continue
        unrealized = sum(
            (
                (Decimal(price_close) - Decimal(lot["price"])) * Decimal(lot["quantity"])
                for _, lot in indexed_lots
            ),
            Decimal(0),
        )
        realized = existing.realized_pnl_base or Decimal(0)
        evidence["open_lot_unrealized_base"] = unrealized
        output[identity] = replace(
            existing,
            status="ESTIMATED",
            method="TRADE_FIFO_OPEN_LOT_UNREALIZED_GROSS_PRE_FEE",
            pnl_base=realized + unrealized,
            realized_pnl_base=realized,
            unrealized_pnl_base=unrealized,
            evidence=evidence,
        )
    for identity, item in tuple(output.items()):
        if item.pnl_base is None or item.realized_pnl_base is not None:
            continue
        evidence = dict(item.evidence)
        evidence.update(
            {
                "trade_review_id": trade_evidence.review["review_id"],
                "trade_review_digest": trade_evidence.review_digest,
                "trade_round_trip_indexes": (),
                "realized_local_by_currency": {},
            }
        )
        output[identity] = replace(
            item,
            method="TRADE_FIFO_REALIZED_PLUS_PRICE_UNREALIZED_ESTIMATE",
            realized_pnl_base=Decimal(0),
            evidence=evidence,
        )
    if by_identity:
        reasons.append(
            {
                "code": "TRADE_FEES_UNAVAILABLE",
                "scope": "symbol_contributions",
                "detail": "FIFO realized P&L is gross pre-fee because authoritative fee evidence is unavailable",
            }
        )
    return tuple(output[key] for key in sorted(output)), reasons


def _merge_multi_snapshot_trade_contributions(
    contributions: Sequence[SymbolContribution],
    trade_evidence: TradeAttributionEvidence,
) -> tuple[tuple[SymbolContribution, ...], list[dict[str, str]]]:
    if not trade_evidence.used_for_contributions or trade_evidence.review is None:
        return tuple(contributions), []
    review = trade_evidence.review
    output = {item.identity: item for item in contributions}
    trips_by_identity: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    lots_by_identity: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for index, trip in enumerate(review["round_trips"]):
        trips_by_identity.setdefault(str(trip["identity"]), []).append((index, trip))
    for index, lot in enumerate(review["open_lots"]):
        lots_by_identity.setdefault(str(lot["identity"]), []).append((index, lot))
    reasons: list[dict[str, str]] = []
    for identity in sorted(set(output) | set(trips_by_identity)):
        indexed_trips = trips_by_identity.get(identity, [])
        indexed_lots = lots_by_identity.get(identity, [])
        local_by_currency: dict[str, Decimal] = {}
        for _index, trip in indexed_trips:
            currency = str(trip["currency"])
            local_by_currency[currency] = local_by_currency.get(currency, Decimal(0)) + Decimal(
                trip["realized_pl_local"]
            )
        from .risk import _first_completed_market_session_on_or_after

        market = identity.split(".", 1)[0]
        evidence_updates = {
            "trade_review_id": review["review_id"],
            "trade_review_digest": trade_evidence.review_digest,
            "trade_round_trip_indexes": tuple(index for index, _trip in indexed_trips),
            "open_lot_indexes": tuple(index for index, _lot in indexed_lots),
            "open_lot_first_completed_session_dates": tuple(
                _first_completed_market_session_on_or_after(
                    datetime.fromisoformat(str(lot["opened_at"]).replace("Z", "+00:00")),
                    market,
                )
                for _index, lot in indexed_lots
            ),
            "realized_local_by_currency": local_by_currency,
        }
        existing = output.get(identity)
        if existing is not None:
            evidence = dict(existing.evidence)
            evidence.update(evidence_updates)
            if existing.status != "AVAILABLE":
                output[identity] = replace(existing, evidence=evidence)
                continue
            base_values = [trip["realized_pl_base"] for _index, trip in indexed_trips]
            if any(value is None for value in base_values) or (
                existing.realized_pnl_base
                != sum((Decimal(value) for value in base_values), Decimal(0))
            ):
                output[identity] = replace(
                    existing,
                    status="UNAVAILABLE",
                    method="TRADE_SNAPSHOT_REALIZED_RECONCILIATION_MISMATCH",
                    pnl_base=None,
                    realized_pnl_base=None,
                    unrealized_pnl_base=None,
                    evidence=evidence,
                )
                reasons.append(
                    {
                        "code": "TRADE_SNAPSHOT_REALIZED_RECONCILIATION_MISMATCH",
                        "scope": identity,
                        "detail": "snapshot realized delta does not reconcile to complete normalized FIFO round trips",
                    }
                )
            else:
                output[identity] = replace(existing, evidence=evidence)
            continue
        if indexed_lots:
            raise AttributionInputError("trade-only identity cannot retain an unmatched open lot")
        market, symbol = identity.split(".", 1)
        accounts = tuple(sorted({str(trip["account_ref"]) for _index, trip in indexed_trips}))
        base_values = [trip["realized_pl_base"] for _index, trip in indexed_trips]
        evidence = {
            "start_snapshot_id": review["coverage"]["source_snapshot_id"],
            "end_snapshot_id": review["coverage"]["source_snapshot_id"],
            "start_present": False,
            "end_present": False,
            "start_rows": 0,
            "end_rows": 0,
            **evidence_updates,
        }
        if any(value is None for value in base_values) or len(local_by_currency) != 1:
            output[identity] = SymbolContribution(
                identity,
                market,
                symbol,
                accounts,
                "UNAVAILABLE",
                "TRADE_FIFO_BASE_FX_UNAVAILABLE",
                None,
                None,
                None,
                "UNKNOWN",
                "UNKNOWN",
                (),
                evidence,
            )
            reasons.append(
                {
                    "code": "TRADE_FIFO_BASE_FX_UNAVAILABLE",
                    "scope": identity,
                    "detail": "FIFO realized gross P&L is retained only in local-currency buckets because transaction-date base FX is unavailable",
                }
            )
        else:
            realized = sum((Decimal(value) for value in base_values), Decimal(0))
            output[identity] = SymbolContribution(
                identity,
                market,
                symbol,
                accounts,
                "AVAILABLE",
                "TRADE_FIFO_REALIZED_GROSS_PRE_FEE",
                realized,
                realized,
                Decimal(0),
                "UNKNOWN",
                "UNKNOWN",
                (),
                evidence,
            )
    if review["round_trips"]:
        reasons.append(
            {
                "code": "TRADE_FEES_UNAVAILABLE",
                "scope": "symbol_contributions",
                "detail": "FIFO realized P&L is gross pre-fee because authoritative fee evidence is unavailable",
            }
        )
    return tuple(output[key] for key in sorted(output)), reasons


def _twr(
    interval: SnapshotInterval, flow_evidence: ExternalFlowEvidence
) -> tuple[str, str | None, Decimal | None, Decimal | None, Decimal | None, list[dict[str, str]]]:
    external = sum(
        (item.amount_base for item in flow_evidence.flows if item.flow_type != "INTERNAL_TRANSFER"),
        Decimal(0),
    )
    internal = sum(
        (item.amount_base for item in flow_evidence.flows if item.flow_type == "INTERNAL_TRANSFER"),
        Decimal(0),
    )
    if (
        flow_evidence.status != "COMPLETE"
        or flow_evidence.coverage_start is None
        or flow_evidence.coverage_end is None
        or flow_evidence.coverage_start > interval.snapshots[0].as_of
        or flow_evidence.coverage_end < interval.snapshots[-1].as_of
    ):
        return (
            "UNAVAILABLE",
            None,
            None,
            external if flow_evidence.flows else None,
            internal if flow_evidence.flows else None,
            [
                {
                    "code": "EXTERNAL_FLOW_COVERAGE_INCOMPLETE",
                    "scope": "portfolio_return",
                    "detail": flow_evidence.reason
                    or "flow evidence does not cover selected valuation timestamps",
                }
            ],
        )
    if not interval.continuous or not interval.start_date_covered or not interval.end_date_covered:
        return (
            "UNAVAILABLE",
            None,
            None,
            external,
            internal,
            [
                {
                    "code": "VALUATION_COVERAGE_INCOMPLETE",
                    "scope": "portfolio_return",
                    "detail": "TWR requires continuous snapshots and both requested UTC dates",
                }
            ],
        )
    chained = Decimal(1)
    for before, after in zip(interval.snapshots, interval.snapshots[1:]):
        if before.nav_base <= 0:
            return (
                "UNAVAILABLE",
                None,
                None,
                external,
                internal,
                [
                    {
                        "code": "INVALID_RETURN_DENOMINATOR",
                        "scope": before.snapshot_id,
                        "detail": "subperiod NAV must be positive",
                    }
                ],
            )
        subperiod_flow = sum(
            (
                item.amount_base
                for item in flow_evidence.flows
                if item.flow_type != "INTERNAL_TRANSFER"
                and before.as_of < item.occurred_at <= after.as_of
            ),
            Decimal(0),
        )
        chained *= (after.nav_base - subperiod_flow) / before.nav_base
    return (
        "AVAILABLE",
        "TWR_END_OF_SUBPERIOD_FLOW_ADJUSTMENT",
        chained - Decimal(1),
        external,
        internal,
        [],
    )


def require_compatible_nav_basis(snapshots: Sequence[PortfolioSnapshot]) -> None:
    bases = {
        "LEGACY_RECONSTRUCTED" if item.schema_version == "1.0" else item.nav_source
        for item in snapshots
    }
    if len(bases) > 1:
        raise AttributionInputError(
            "NAV_BASIS_MISMATCH: select a period with one NAV source basis; "
            "legacy snapshots must not be rewritten or recomputed"
        )


def analyze_performance(
    snapshots: Sequence[PortfolioSnapshot],
    *,
    start: date,
    end: date,
    flow_evidence: ExternalFlowEvidence | None = None,
    price_provider: PriceProvider | None = None,
    trade_history: TradeReview | None = None,
    trade_unavailable_reason: str | None = None,
    analysis_now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> PerformanceAttributionReport:
    if trade_history is not None and trade_unavailable_reason is not None:
        raise AttributionInputError(
            "trade history and an unavailable reason cannot both be supplied"
        )
    interval = select_snapshot_interval(snapshots, start=start, end=end)
    selected = interval.snapshots
    require_compatible_nav_basis(selected)
    first = selected[0]
    analysis_now = analysis_now if analysis_now is not None else (clock or _utc_now)()
    if analysis_now.tzinfo is None or analysis_now.utcoffset() is None:
        raise AttributionInputError("analysis_now must be timezone-aware")
    analysis_now = analysis_now.astimezone(timezone.utc)
    if any(item.as_of.astimezone(timezone.utc) > analysis_now for item in selected):
        raise AttributionInputError("FUTURE_SNAPSHOT")
    if any(item.base_currency != first.base_currency for item in selected):
        raise AttributionInputError("snapshot base currency changes inside the period")
    if any(item.trd_env != first.trd_env for item in selected):
        raise AttributionInputError("snapshot environment changes inside the period")
    if any(tuple(item.accounts) != tuple(first.accounts) for item in selected[1:]):
        raise AttributionInputError("snapshot account identities change inside the period")
    trade_evidence = _trade_attribution_evidence(
        trade_history,
        start=start,
        end=end,
        snapshot=selected[-1],
        use_contributions=True,
        unavailable_reason=trade_unavailable_reason,
    )
    requested_identities = tuple(
        sorted(set().union(*(set(_group_positions(snapshot)) for snapshot in selected)))
    )
    prices = _align_price_evidence(
        _price_evidence(
            requested_identities,
            provider=price_provider,
            start=start,
            end=end,
            end_instant=min(selected[-1].as_of.astimezone(timezone.utc), analysis_now),
        ),
        start=start,
        end=end,
    )
    if len(selected) == 1:
        contributions, reasons = _reconstructed_contributions(selected[-1], prices)
        contributions, trade_reasons = _merge_trade_fifo_contributions(
            contributions, trade_evidence
        )
        if trade_evidence.used_for_contributions:
            reasons = [
                item
                for item in reasons
                if item["code"] != "RECONSTRUCTED_REALIZED_AND_EXITED_UNAVAILABLE"
            ]
        reasons.extend(trade_reasons)
    else:
        contributions, reasons = _contributions(selected)
        contributions, trade_reasons = _merge_multi_snapshot_trade_contributions(
            contributions, trade_evidence
        )
        reasons.extend(trade_reasons)
    if trade_evidence.status == "UNAVAILABLE" and (
        len(selected) >= 2
        or trade_history is not None
        or trade_unavailable_reason in TRADE_SOURCE_UNAVAILABLE_CODES
    ):
        reasons = [
            item
            for item in reasons
            if item["code"] != "RECONSTRUCTED_REALIZED_AND_EXITED_UNAVAILABLE"
        ]
        reasons.append(
            {
                "code": "TRADE_HISTORY_UNAVAILABLE",
                "scope": "trade_evidence",
                "detail": trade_evidence.reason
                or "authoritative normalized trade history is unavailable",
            }
        )
    source_snapshot_quality = tuple(
        {
            "snapshot_id": snapshot.snapshot_id,
            "status": snapshot.data_quality.status,
            "warnings": tuple(snapshot.data_quality.warnings),
            "missing_fields": tuple(snapshot.data_quality.missing_fields),
            "source_errors": tuple(snapshot.data_quality.source_errors),
            "position_quality_notes": tuple(
                {
                    "account_ref": position.account_ref,
                    "identity": _identity(position),
                    "notes": tuple(position.data_quality_notes),
                }
                for position in sorted(
                    snapshot.positions, key=lambda item: (item.account_ref, _identity(item))
                )
                if position.data_quality_notes
            ),
        }
        for snapshot in selected
    )
    source_quality_complete = all(
        item.data_quality.status == "COMPLETE"
        and not item.data_quality.warnings
        and not item.data_quality.missing_fields
        and not item.data_quality.source_errors
        and all(not position.data_quality_notes for position in item.positions)
        for item in selected
    )
    if not source_quality_complete:
        reasons.append(
            {
                "code": "SOURCE_DATA_QUALITY_INCOMPLETE",
                "scope": "source_snapshots",
                "detail": "all relevant snapshot financial, FX, classification, and position quality evidence must be COMPLETE",
            }
        )
    if not interval.continuous:
        reasons.append(
            {
                "code": "SNAPSHOT_CADENCE_GAP",
                "scope": "snapshot_interval",
                "detail": "one or more valuation gaps exceed the documented 36-hour cadence",
            }
        )
    if not interval.start_date_covered or not interval.end_date_covered:
        reasons.append(
            {
                "code": "ENDPOINT_DATE_COVERAGE_INCOMPLETE",
                "scope": "snapshot_interval",
                "detail": "both requested inclusive UTC dates require a snapshot",
            }
        )
    fx_provenance = {
        tuple(
            sorted(
                (item.source_currency, item.target_currency, item.source)
                for item in snapshot.fx_evidence
            )
        )
        for snapshot in selected
    }
    fx_consistent = len(fx_provenance) == 1
    if not fx_consistent:
        reasons.append(
            {
                "code": "FX_PROVENANCE_CHANGED",
                "scope": "snapshot_interval",
                "detail": "FX currency-pair source provenance changes inside the period",
            }
        )
    available = tuple(item for item in contributions if item.pnl_base is not None)
    exact = tuple(item for item in contributions if item.status == "AVAILABLE")
    aligned_by_identity = {item.identity: item.aligned_points for item in prices}
    authoritative_identities = {
        item.identity for item in exact if item.evidence.get("trajectory") is not None
    }
    prices_complete = bool(authoritative_identities) and all(
        len(aligned_by_identity.get(identity, ())) >= 2 for identity in authoritative_identities
    )
    all_available = (
        len(exact) == len(contributions)
        and bool(contributions)
        and len(selected) >= 2
        and interval.continuous
        and interval.start_date_covered
        and interval.end_date_covered
        and fx_consistent
        and prices_complete
        and source_quality_complete
        and trade_evidence.status == "COMPLETE"
        and trade_evidence.used_for_contributions
    )
    total = (
        sum((item.pnl_base for item in available if item.pnl_base is not None), Decimal(0))
        if available
        else None
    )
    realized = (
        sum(
            (item.realized_pnl_base for item in available if item.realized_pnl_base is not None),
            Decimal(0),
        )
        if available
        and all(item.realized_pnl_base is not None for item in available)
        and not any(item.method == "TRADE_FIFO_BASE_FX_UNAVAILABLE" for item in contributions)
        else None
    )
    unrealized = (
        sum(
            (
                item.unrealized_pnl_base
                for item in available
                if item.unrealized_pnl_base is not None
            ),
            Decimal(0),
        )
        if available and all(item.unrealized_pnl_base is not None for item in available)
        else None
    )
    flow_evidence = flow_evidence or ExternalFlowEvidence.unavailable(
        "authoritative external cash-flow evidence was not supplied"
    )
    if flow_evidence.flows:
        flow_evidence = replace(
            flow_evidence,
            flows=tuple(
                item
                for item in flow_evidence.flows
                if first.as_of < item.occurred_at <= selected[-1].as_of
            ),
        )
    return_status, return_method, portfolio_return, external_flow, internal_flow, return_reasons = (
        _twr(interval, flow_evidence)
    )
    reasons.extend(return_reasons)
    flow_covers_endpoints = (
        flow_evidence.status == "COMPLETE"
        and flow_evidence.coverage_start is not None
        and flow_evidence.coverage_end is not None
        and flow_evidence.coverage_start <= first.as_of
        and flow_evidence.coverage_end >= selected[-1].as_of
    )
    nav_change = selected[-1].nav_base - first.nav_base
    field_population_complete = len(exact) == len(contributions) and bool(contributions)
    if len(selected) >= 2 and flow_covers_endpoints and total is not None:
        expected_net_pnl = nav_change - (external_flow or Decimal(0))
        if field_population_complete:
            residual = expected_net_pnl - total
            reconciliation_status = "MATCHED" if residual == 0 else "MISMATCH"
            if residual != 0:
                reasons.append(
                    {
                        "code": "PNL_NAV_RECONCILIATION_MISMATCH",
                        "scope": "pnl_reconciliation",
                        "detail": "attributed cumulative P&L deltas do not reconcile to NAV change after verified external flows",
                    }
                )
                all_available = False
        else:
            residual = None
            reconciliation_status = "PARTIAL"
    else:
        expected_net_pnl = None
        residual = None
        reconciliation_status = "UNAVAILABLE"
    pnl_reconciliation = {
        "status": reconciliation_status,
        "basis": "ENDING_NAV_MINUS_BEGINNING_NAV_MINUS_EXTERNAL_FLOWS",
        "nav_change_base": nav_change,
        "external_flow_base": external_flow if flow_covers_endpoints else None,
        "expected_net_pnl_base": expected_net_pnl,
        "attributed_pnl_base": total,
        "residual_base": residual,
    }
    mode = (
        "ACCURATE_PNL"
        if all_available
        else "RECONSTRUCTED"
        if contributions and (len(selected) >= 2 or available)
        else "UNAVAILABLE"
    )
    confidence = "HIGH" if all_available else ("LOW" if mode == "RECONSTRUCTED" else "NONE")
    contribution_status = (
        "COMPLETE" if all_available else ("PARTIAL" if available else "UNAVAILABLE")
    )
    if mode != "ACCURATE_PNL":
        return_status, return_method, portfolio_return = "UNAVAILABLE", None, None
        if mode == "RECONSTRUCTED" and not any(
            item["code"] == "RECONSTRUCTED_RETURN_UNAVAILABLE" for item in reasons
        ):
            reasons.append(
                {
                    "code": "RECONSTRUCTED_RETURN_UNAVAILABLE",
                    "scope": "portfolio_return",
                    "detail": "reconstructed attribution cannot authorize portfolio return or TWR",
                }
            )
    if price_provider is None and contributions:
        reasons.append(
            {
                "code": "PRICE_SOURCE_UNAVAILABLE",
                "scope": "price_evidence",
                "detail": "the audited adjusted-close provider is unavailable; snapshot P&L attribution remains isolated",
            }
        )
    elif len(prices) < len(requested_identities):
        reasons.append(
            {
                "code": "PRICE_BUDGET_LIMIT",
                "scope": "price_evidence",
                "detail": "price request count was capped by the documented budget",
            }
        )
    if prices and sum(item.status == "AVAILABLE" for item in prices) < len(prices):
        reasons.append(
            {
                "code": "PRICE_DATA_PARTIAL",
                "scope": "price_evidence",
                "detail": "one or more bounded adjusted-close histories are unavailable",
            }
        )
    if authoritative_identities and not prices_complete:
        reasons.append(
            {
                "code": "PRICE_ALIGNMENT_INCOMPLETE",
                "scope": "price_evidence",
                "detail": "all authoritative contributed symbols require at least two common adjusted-close points",
            }
        )
    ranked = sorted(available, key=lambda item: (-(item.pnl_base or Decimal(0)), item.identity))
    contributors = tuple(item for item in ranked if (item.pnl_base or Decimal(0)) > 0)[:5]
    detractors = tuple(
        sorted(
            (item for item in available if (item.pnl_base or Decimal(0)) < 0),
            key=lambda item: (item.pnl_base or Decimal(0), item.identity),
        )
    )[:5]
    period = {
        "requested_start": start,
        "requested_end": end,
        "date_semantics": "INCLUSIVE_UTC_CALENDAR_DATES",
        "selected_start_as_of": first.as_of,
        "selected_end_as_of": selected[-1].as_of,
        "start_date_covered": interval.start_date_covered,
        "end_date_covered": interval.end_date_covered,
        "continuous": interval.continuous,
        "maximum_gap_seconds": interval.maximum_gap_seconds,
        "gaps": interval.gaps,
        "valuations": tuple(
            {
                "snapshot_id": item.snapshot_id,
                "as_of": item.as_of,
                "nav_base": item.nav_base,
            }
            for item in selected
        ),
    }
    limitations = [
        "IRR is not calculated.",
        "Exact snapshot P&L attribution sums signed adjacent cumulative realized/unrealized base-field deltas across the full selected trajectory; a decline is a valid detractor, while quantity lifecycle changes, transfers and corporate actions require authoritative reconciliation.",
    ]
    if mode == "RECONSTRUCTED":
        limitations.append(
            "Reconstructed output is LOW confidence and does not claim complete account P&L."
        )
        if trade_evidence.used_for_contributions:
            limitations.append(
                "Realized FIFO contribution covers only complete in-period normalized buy/sell evidence and is gross pre-fee; opening inventory, transfers, corporate actions, and conflicted orders fail closed."
            )
        else:
            limitations.append(
                "Authoritative normalized trade history is unavailable; price-only reconstruction covers current holdings, while all other trajectory identities remain explicit but unavailable; realized P&L and exited-position reconstruction are excluded."
            )
    if return_status != "AVAILABLE":
        limitations.append(
            "Portfolio return and TWR are disabled for reconstructed mode or without complete external-flow and valuation coverage."
        )
    draft = PerformanceAttributionReport(
        "1.0",
        "pending",
        selected[-1].as_of,
        analysis_now,
        period,
        first.base_currency,
        tuple(item.snapshot_id for item in selected),
        {item.snapshot_id: _snapshot_digest(item) for item in selected},
        source_snapshot_quality,
        {
            "source_snapshots": len(selected),
            "source_position_rows": sum(len(item.positions) for item in selected),
            "canonical_identities": len(contributions),
            "trajectory_identities": len(requested_identities),
            "available_contributions": len(available),
            "unavailable_contributions": len(contributions) - len(available),
            "price_requested": len(requested_identities),
            "price_analyzed": len(prices),
            "price_omitted": len(requested_identities) - len(prices),
            "price_available": sum(item.status == "AVAILABLE" for item in prices),
            "price_aligned": sum(len(item.aligned_points) >= 2 for item in prices),
            "trade_orders": len(trade_evidence.review["normalized_orders"])
            if trade_evidence.review is not None
            else 0,
            "trade_fills": len(trade_evidence.review["normalized_fills"])
            if trade_evidence.review is not None
            else 0,
            "trade_round_trips": len(trade_evidence.review["round_trips"])
            if trade_evidence.review is not None
            else 0,
            "flow_rows": len(flow_evidence.flows),
        },
        mode,
        confidence,
        contribution_status,
        total,
        realized,
        unrealized,
        pnl_reconciliation,
        contributions,
        _aggregate(contributions, "sector"),
        _aggregate(contributions, "region"),
        _aggregate(contributions, "themes"),
        _aggregate(contributions, "themes", split=True),
        contributors,
        detractors,
        prices,
        trade_evidence,
        flow_evidence,
        external_flow,
        internal_flow,
        return_status,
        return_method,
        portfolio_return,
        "NOT_CALCULATED",
        None,
        None,
        "COMPLETE" if not reasons else "PARTIAL",
        tuple(reasons),
        (
            "symbol_pnl = sum over every adjacent snapshot leg of signed delta(cumulative_realized_pl_base) + signed delta(cumulative_unrealized_pl_base); positive and negative deltas are valid",
            "snapshot cumulative_pnl_base = cumulative_pnl_local * snapshot_bound_fx_evidence_rate; signed base deltas include valid FX-rate movement when currency, source authority, and non-lookahead contract remain continuous",
            "reconstructed_gross_unrealized_pnl_base = ending_quantity * (aligned_ending_adjusted_close - aligned_beginning_adjusted_close) * ending_fx_to_base; only base-currency FX=1 is authorized",
            "fifo_realized_gross_local = sum(matched_quantity * (sell_price - buy_price)) by account, canonical identity, and currency; base-currency fills convert exactly 1:1 and non-base fills require transaction-date FX",
            "fifo_open_lot_unrealized_gross_base = sum((aligned_ending_close - lot_cost) * open_lot_quantity) for authoritative base-currency lots whose total exactly matches ending snapshot account legs",
            "theme_split_pnl divides across sorted themes with the final theme receiving symbol_pnl minus prior shares, preserving exact Decimal equality; UNKNOWN receives the full amount when no theme exists",
            "TWR = product((ending_NAV - external_flows_in_subperiod) / beginning_NAV) - 1; flows are adjusted at subperiod end",
        ),
        tuple(limitations),
    )
    body = draft.to_dict()
    body.pop("attribution_id")
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return replace(draft, attribution_id=f"pa-{digest}")
