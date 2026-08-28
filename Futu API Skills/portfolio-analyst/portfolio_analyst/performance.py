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
from typing import Any, Mapping, Protocol, Sequence

from .models import PortfolioSnapshot, _deep_freeze, _json_value
from .rendering import markdown_text, terminal_text
from .snapshot_io import snapshot_from_document


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


class AttributionInputError(ValueError):
    pass


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
    source: str
    adjustment: str
    digest: str | None
    reason: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", tuple(self.points))


@dataclass(frozen=True)
class PerformanceAttributionReport:
    schema_version: str
    attribution_id: str
    as_of: datetime
    period: Mapping[str, Any]
    base_currency: str
    source_snapshot_ids: tuple[str, ...]
    source_snapshot_digests: Mapping[str, str]
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
                    f"- {safe(item.identity)}: {item.status}; cutoff={item.cutoff_date} ({item.cutoff_policy}); rows={item.rows}; digest={safe(item.digest or 'N/A')}; reason={safe(item.reason or 'N/A')}"
                    for item in self.price_evidence
                ]
                or ["- None"]
            ),
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
                f"- `{md(item.identity)}`: {item.status}; cutoff={item.cutoff_date} ({item.cutoff_policy}); {item.first_date or 'N/A'} to {item.last_date or 'N/A'}; rows={item.rows}; digest={md(item.digest or 'N/A')}; reason={md(item.reason or 'N/A')}"
                for item in self.price_evidence
            ]
            or ["- No price series used; attribution is based on snapshot P&L fields."]
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
    ordered = tuple(sorted(validated, key=lambda item: (item.as_of, item.snapshot_id)))
    if len({item.snapshot_id for item in ordered}) != len(ordered):
        raise AttributionInputError("snapshot interval contains duplicate identities")
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
    if len(selected) < 2:
        raise AttributionInputError("performance requires at least two validated snapshots")
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


def _contributions(
    start: PortfolioSnapshot, end: PortfolioSnapshot
) -> tuple[tuple[SymbolContribution, ...], list[dict[str, str]]]:
    before, after = _group_positions(start), _group_positions(end)
    reasons: list[dict[str, str]] = []
    output: list[SymbolContribution] = []
    for identity in sorted(set(before) | set(after)):
        start_rows, end_rows = before.get(identity), after.get(identity)
        all_rows = (start_rows or ()) + (end_rows or ())
        market, symbol = identity.split(".", 1)
        classification = _classification(all_rows)
        evidence = {
            "start_snapshot_id": start.snapshot_id,
            "end_snapshot_id": end.snapshot_id,
            "start_present": start_rows is not None,
            "end_present": end_rows is not None,
            "start_rows": len(start_rows or ()),
            "end_rows": len(end_rows or ()),
        }
        if start_rows is None or end_rows is None:
            reasons.append(
                {
                    "code": "POSITION_ENDPOINT_MISSING",
                    "scope": identity,
                    "detail": "position opened or closed inside the interval; trades/corporate actions are not authoritative",
                }
            )
            output.append(
                SymbolContribution(
                    identity,
                    market,
                    symbol,
                    tuple(sorted({item.account_ref for item in all_rows})),
                    "UNAVAILABLE",
                    "ENDPOINT_POSITION_MISSING",
                    None,
                    None,
                    None,
                    *(classification or ("UNKNOWN", "UNKNOWN", ())),
                    evidence,
                )
            )
            continue
        if {item.account_ref for item in start_rows} != {item.account_ref for item in end_rows}:
            reasons.append(
                {
                    "code": "ACCOUNT_LEGS_CHANGED",
                    "scope": identity,
                    "detail": "account-level position legs differ across endpoints; transfer or closure is ambiguous",
                }
            )
            output.append(
                SymbolContribution(
                    identity,
                    market,
                    symbol,
                    tuple(sorted({item.account_ref for item in all_rows})),
                    "UNAVAILABLE",
                    "ACCOUNT_LEGS_CHANGED",
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
        start_realized = _sum_optional(start_rows, "realized_pl_base")
        end_realized = _sum_optional(end_rows, "realized_pl_base")
        start_unrealized = _sum_optional(start_rows, "unrealized_pl_base")
        end_unrealized = _sum_optional(end_rows, "unrealized_pl_base")
        if None in (start_realized, end_realized, start_unrealized, end_unrealized):
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
        realized = end_realized - start_realized
        unrealized = end_unrealized - start_unrealized
        output.append(
            SymbolContribution(
                identity,
                market,
                symbol,
                tuple(sorted({item.account_ref for item in all_rows})),
                "AVAILABLE",
                "SNAPSHOT_CUMULATIVE_PNL_DELTA",
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
        labels = tuple(labels) or ("UNKNOWN",)
        allocation = item.pnl_base / Decimal(len(labels)) if split else item.pnl_base
        for label in labels:
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
                    "AUDITED_FUTU_GET_KLINE",
                    "FORWARD_ADJUSTED_DAILY_CLOSE",
                    hashlib.sha256(raw.encode()).hexdigest() if bounded else None,
                    None
                    if bounded
                    else "no rows in requested interval before the completed market close cutoff",
                )
            )
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


def analyze_performance(
    snapshots: Sequence[PortfolioSnapshot],
    *,
    start: date,
    end: date,
    flow_evidence: ExternalFlowEvidence | None = None,
    price_provider: PriceProvider | None = None,
) -> PerformanceAttributionReport:
    interval = select_snapshot_interval(snapshots, start=start, end=end)
    selected = interval.snapshots
    first = selected[0]
    if any(item.base_currency != first.base_currency for item in selected):
        raise AttributionInputError("snapshot base currency changes inside the period")
    if any(item.trd_env != first.trd_env for item in selected):
        raise AttributionInputError("snapshot environment changes inside the period")
    if any(tuple(item.accounts) != tuple(first.accounts) for item in selected[1:]):
        raise AttributionInputError("snapshot account identities change inside the period")
    contributions, reasons = _contributions(first, selected[-1])
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
    all_available = (
        len(available) == len(contributions)
        and bool(contributions)
        and interval.continuous
        and interval.start_date_covered
        and interval.end_date_covered
        and fx_consistent
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
        if available
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
    field_population_complete = len(available) == len(contributions) and bool(contributions)
    if flow_covers_endpoints and total is not None:
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
        "ACCURATE_PNL" if all_available else ("RECONSTRUCTED" if contributions else "UNAVAILABLE")
    )
    confidence = "HIGH" if all_available else ("LOW" if contributions else "NONE")
    contribution_status = (
        "COMPLETE" if all_available else ("PARTIAL" if available else "UNAVAILABLE")
    )
    prices = _price_evidence(
        tuple(item.identity for item in contributions),
        provider=price_provider,
        start=start,
        end=end,
        end_instant=selected[-1].as_of,
    )
    if price_provider is None and contributions:
        reasons.append(
            {
                "code": "PRICE_SOURCE_UNAVAILABLE",
                "scope": "price_evidence",
                "detail": "the audited adjusted-close provider is unavailable; snapshot P&L attribution remains isolated",
            }
        )
    elif len(prices) < len(contributions):
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
        "Snapshot P&L attribution uses cumulative realized/unrealized base-field deltas; fees, FX timing, transfers and corporate actions require authoritative source coverage.",
    ]
    if mode == "RECONSTRUCTED":
        limitations.append(
            "Reconstructed output is LOW confidence and does not claim complete account P&L."
        )
    if return_status != "AVAILABLE":
        limitations.append(
            "Portfolio return and TWR are disabled without complete external-flow and valuation coverage."
        )
    draft = PerformanceAttributionReport(
        "1.0",
        "pending",
        selected[-1].as_of,
        period,
        first.base_currency,
        tuple(item.snapshot_id for item in selected),
        {item.snapshot_id: _snapshot_digest(item) for item in selected},
        {
            "source_snapshots": len(selected),
            "source_position_rows": sum(len(item.positions) for item in selected),
            "canonical_identities": len(contributions),
            "available_contributions": len(available),
            "unavailable_contributions": len(contributions) - len(available),
            "price_requested": len(contributions),
            "price_analyzed": len(prices),
            "price_omitted": len(contributions) - len(prices),
            "price_available": sum(item.status == "AVAILABLE" for item in prices),
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
            "symbol_pnl = delta(cumulative_realized_pl_base) + delta(cumulative_unrealized_pl_base)",
            "theme_split_pnl = symbol_pnl / number_of_themes; UNKNOWN receives the full amount when no theme exists",
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
