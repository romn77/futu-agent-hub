"""Deterministic position-lifecycle analysis from user facts and immutable snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from .lifecycle_repository import (
    LifecycleConfigError as LifecycleConfigError,
    LifecycleRecord,
    LifecycleRepository,
)
from .models import PortfolioSnapshot, _deep_freeze, _json_value
from .rendering import markdown_text, terminal_text
from .risk import EconomicPosition, aggregate_economic_positions
from .snapshot_io import InvalidSnapshot, snapshot_from_document


LIFECYCLE_STATES = frozenset(
    {
        "UNCONFIGURED",
        "INTACT",
        "STRENGTHENING",
        "WEAKENING",
        "BROKEN",
        "ROLE_DRIFT",
        "WEIGHT_DRIFT",
        "HORIZON_DRIFT",
        "EXITED",
    }
)
THESIS_STATES = frozenset({"UNREVIEWED", "INTACT", "STRENGTHENING", "WEAKENING", "BROKEN"})
_HORIZON = re.compile(
    r"^(\d+)(?:\s*-\s*(\d+))?\s*(day|days|week|weeks|month|months|year|years)$", re.I
)


class LifecycleInputError(ValueError):
    pass


@dataclass(frozen=True)
class LifecycleFinding:
    type: str
    symbol: str
    summary: str
    evidence: Mapping[str, Any]
    review_priority: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", _deep_freeze(dict(self.evidence)))


@dataclass(frozen=True)
class LifecycleAssessment:
    symbol: str
    role: str
    record_status: str | None
    lifecycle_status: str
    current_weight: Decimal | None
    target_weight: Mapping[str, Decimal] | None
    thesis_status: str
    original_thesis: str | None
    invalidation_conditions: tuple[str, ...]
    review_triggers: tuple[str, ...]
    notes: tuple[str, ...]
    opened_at: date | None
    holding_horizon: str | None
    last_reviewed_at: date | None
    findings: tuple[LifecycleFinding, ...]
    review_priority: str
    data_quality_notes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_weight",
            None if self.target_weight is None else _deep_freeze(dict(self.target_weight)),
        )
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "invalidation_conditions", tuple(self.invalidation_conditions))
        object.__setattr__(self, "review_triggers", tuple(self.review_triggers))
        object.__setattr__(self, "notes", tuple(self.notes))
        object.__setattr__(self, "data_quality_notes", tuple(self.data_quality_notes))


@dataclass(frozen=True)
class LifecycleReport:
    schema_version: str
    lifecycle_report_id: str
    as_of: Any
    source_snapshot_id: str
    status: str
    coverage: Mapping[str, int | str]
    positions: tuple[LifecycleAssessment, ...]
    findings: tuple[LifecycleFinding, ...]
    data_quality_notes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "coverage", _deep_freeze(dict(self.coverage)))
        object.__setattr__(self, "positions", tuple(self.positions))
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "data_quality_notes", tuple(self.data_quality_notes))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)

    def to_human(self) -> str:
        lines = [
            f"Lifecycle report {terminal_text(self.lifecycle_report_id)}",
            f"Source snapshot: {terminal_text(self.source_snapshot_id)}",
            f"Status: {self.status}",
        ]
        lines.extend(
            f"{terminal_text(item.symbol)}: {item.lifecycle_status} ({item.review_priority})"
            for item in self.positions
        )
        return "\n".join(lines)

    def to_markdown(self, *, json_artifact: str | None = None) -> str:
        md = markdown_text
        lines = [
            "# Position lifecycle review",
            "",
            f"- lifecycle_report_id: `{md(self.lifecycle_report_id)}`",
            f"- source_snapshot_id: `{md(self.source_snapshot_id)}`",
            f"- As of: {self.to_dict()['as_of']}",
            f"- Status: {self.status}",
            f"- Authoritative JSON: `{md(json_artifact)}`"
            if json_artifact
            else "- Authoritative JSON: current output",
            "",
            "## Positions",
            "",
        ]
        for item in self.positions:
            lines.extend(
                [
                    f"### {md(item.symbol)}",
                    "",
                    f"- Lifecycle: {item.lifecycle_status}",
                    f"- Role: {md(item.role)}",
                    f"- Weight: {item.current_weight if item.current_weight is not None else 'N/A'}",
                    f"- Thesis review: {item.thesis_status}",
                    f"- Original thesis: {md(item.original_thesis) if item.original_thesis else 'Not configured'}",
                    "- Invalidation conditions: "
                    + ("; ".join(md(value) for value in item.invalidation_conditions) or "None"),
                    "- Review triggers: "
                    + ("; ".join(md(value) for value in item.review_triggers) or "None"),
                    "- User notes: " + ("; ".join(md(value) for value in item.notes) or "None"),
                    f"- Last reviewed: {item.last_reviewed_at or 'Not configured'}",
                    f"- Review priority: {item.review_priority}",
                    *(
                        [f"- Note: {md(note)}" for note in item.data_quality_notes]
                        or ["- Note: None"]
                    ),
                    "",
                ]
            )
        return "\n".join(lines) + "\n"


def _horizon_days(text: str) -> int:
    match = _HORIZON.fullmatch(text.strip())
    if match is None:
        raise LifecycleInputError("holding_horizon uses an unsupported deterministic format")
    maximum = int(match.group(2) or match.group(1))
    if maximum < 1 or maximum > 1200:
        raise LifecycleInputError("holding_horizon is outside the supported bound")
    factor = {
        "day": 1,
        "days": 1,
        "week": 7,
        "weeks": 7,
        "month": 30,
        "months": 30,
        "year": 365,
        "years": 365,
    }[match.group(3).lower()]
    return maximum * factor


def _finding(
    kind: str, symbol: str, summary: str, evidence: Mapping[str, Any], priority: str
) -> LifecycleFinding:
    return LifecycleFinding(kind, symbol, summary, evidence, priority)


def _history_role_drift(
    record: LifecycleRecord,
    current: EconomicPosition,
    history: Sequence[PortfolioSnapshot],
) -> tuple[bool, str | None]:
    if record.role != "SATELLITE" or current.weight < Decimal("0.10"):
        return False, None
    observations: dict[str, tuple[Any, Decimal]] = {}
    for snapshot in history:
        for item in aggregate_economic_positions(snapshot):
            if item.identity == record.symbol:
                observations[snapshot.snapshot_id] = (snapshot.as_of, item.weight)
                break
    ordered = sorted(observations.values())
    if len(ordered) < 3 or (ordered[-1][0] - ordered[0][0]).days < 30:
        return False, "role drift unavailable: requires at least 3 snapshots spanning 30 days"
    return all(weight >= Decimal("0.10") for _as_of, weight in ordered), None


def _assessment(
    current: EconomicPosition | None,
    record: LifecycleRecord | None,
    *,
    as_of: date,
    history: Sequence[PortfolioSnapshot],
    thesis_review: str | None,
) -> LifecycleAssessment:
    symbol = current.identity if current is not None else record.symbol  # type: ignore[union-attr]
    if record is None:
        finding = _finding(
            "UNCONFIGURED",
            symbol,
            "Current position has no user lifecycle record",
            {"current_weight": current.weight},
            "P2",
        )
        return LifecycleAssessment(
            symbol,
            "UNCLASSIFIED",
            None,
            "UNCONFIGURED",
            current.weight,
            None,
            "UNREVIEWED",
            None,
            (),
            (),
            (),
            None,
            None,
            None,
            (finding,),
            "P2",
            ("No thesis or target was inferred.",),
        )
    if record.last_reviewed_at > as_of or record.opened_at > as_of:
        raise LifecycleInputError("lifecycle record dates cannot be after snapshot as_of")
    if current is None:
        finding = _finding(
            "EXITED",
            symbol,
            "Recorded position is absent from the current economic portfolio",
            {"record_status": record.status},
            "P3",
        )
        return LifecycleAssessment(
            symbol,
            record.role,
            record.status,
            "EXITED",
            None,
            {"min": record.target_weight.minimum, "max": record.target_weight.maximum},
            "UNREVIEWED",
            record.original_thesis,
            record.invalidation_conditions,
            record.review_triggers,
            record.notes,
            record.opened_at,
            record.holding_horizon,
            record.last_reviewed_at,
            (finding,),
            "P3",
            (),
        )

    reviewed = "UNREVIEWED" if thesis_review is None else thesis_review.upper()
    if reviewed not in THESIS_STATES:
        raise LifecycleInputError("explicit thesis review status is unsupported")
    findings: list[LifecycleFinding] = []
    notes: list[str] = []
    if record.status == "EXITED":
        findings.append(
            _finding(
                "BROKEN",
                symbol,
                "Position is present although its user record is EXITED",
                {"record_status": "EXITED", "current_weight": current.weight},
                "P0",
            )
        )
    role_drift, role_note = _history_role_drift(record, current, history)
    if role_note:
        notes.append(role_note)
    if role_drift:
        findings.append(
            _finding(
                "ROLE_DRIFT",
                symbol,
                "SATELLITE weight remained at or above the documented 10% core-level threshold",
                {"threshold": Decimal("0.10"), "minimum_snapshots": 3, "minimum_span_days": 30},
                "P1",
            )
        )
    if not record.target_weight.minimum <= current.weight <= record.target_weight.maximum:
        findings.append(
            _finding(
                "WEIGHT_DRIFT",
                symbol,
                "Current weight is outside the user-authored target interval",
                {
                    "current_weight": current.weight,
                    "target_min": record.target_weight.minimum,
                    "target_max": record.target_weight.maximum,
                },
                "P1",
            )
        )
    if as_of < record.opened_at:
        raise LifecycleInputError("snapshot as_of precedes lifecycle opened_at")
    horizon_days = _horizon_days(record.holding_horizon)
    held_days = (as_of - record.opened_at).days
    if held_days > horizon_days:
        findings.append(
            _finding(
                "HORIZON_DRIFT",
                symbol,
                "Holding age exceeds the deterministic upper horizon",
                {"held_days": held_days, "upper_horizon_days": horizon_days},
                "P1",
            )
        )
    if reviewed == "BROKEN":
        findings.append(
            _finding(
                "BROKEN",
                symbol,
                "User review explicitly marked the thesis BROKEN",
                {"thesis_status": reviewed},
                "P0",
            )
        )
    elif reviewed in {"WEAKENING", "STRENGTHENING"}:
        findings.append(
            _finding(
                reviewed,
                symbol,
                f"User review explicitly marked the thesis {reviewed}",
                {"thesis_status": reviewed},
                "P2",
            )
        )
    precedence = (
        "BROKEN",
        "ROLE_DRIFT",
        "WEIGHT_DRIFT",
        "HORIZON_DRIFT",
        "WEAKENING",
        "STRENGTHENING",
    )
    status = next(
        (state for state in precedence if any(item.type == state for item in findings)), "INTACT"
    )
    priority = min((item.review_priority for item in findings), default="P3")
    return LifecycleAssessment(
        symbol,
        record.role,
        record.status,
        status,
        current.weight,
        {"min": record.target_weight.minimum, "max": record.target_weight.maximum},
        reviewed,
        record.original_thesis,
        record.invalidation_conditions,
        record.review_triggers,
        record.notes,
        record.opened_at,
        record.holding_horizon,
        record.last_reviewed_at,
        tuple(findings),
        priority,
        tuple(notes),
    )


def _identity(report: LifecycleReport) -> str:
    body = report.to_dict()
    body.pop("lifecycle_report_id", None)
    digest = hashlib.sha256(
        json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()[:20]
    return f"lr-{digest}"


def analyze_lifecycle(
    snapshot: PortfolioSnapshot,
    repository: LifecycleRepository,
    *,
    history: Sequence[PortfolioSnapshot] = (),
    thesis_reviews: Mapping[str, str] | None = None,
) -> LifecycleReport:
    try:
        snapshot = snapshot_from_document(snapshot.to_dict())
        history = tuple(snapshot_from_document(item.to_dict()) for item in history)
    except InvalidSnapshot as exc:
        raise LifecycleInputError("lifecycle input snapshot is invalid") from exc
    economic = aggregate_economic_positions(snapshot)
    if any(item.weight < 0 for item in economic):
        raise LifecycleInputError("signed lifecycle portfolios are unsupported")
    current = {item.identity: item for item in economic}
    records = {item.symbol: item for item in repository.list()}
    reviews = {str(key).upper(): value for key, value in (thesis_reviews or {}).items()}
    symbols = sorted(set(current) | set(records))
    assessments = tuple(
        _assessment(
            current.get(symbol),
            records.get(symbol),
            as_of=snapshot.as_of.date(),
            history=history,
            thesis_review=reviews.get(symbol),
        )
        for symbol in symbols
    )
    findings = tuple(
        sorted(
            (finding for item in assessments for finding in item.findings),
            key=lambda item: (item.review_priority, item.type, item.symbol),
        )
    )
    notes = sorted({note for item in assessments for note in item.data_quality_notes})
    unconfigured = sum(item.lifecycle_status == "UNCONFIGURED" for item in assessments)
    status = "PARTIAL" if unconfigured or notes else "COMPLETE"
    provisional = LifecycleReport(
        "1.0",
        "",
        snapshot.as_of,
        snapshot.snapshot_id,
        status,
        {
            "current_positions": len(current),
            "configured_records": len(records),
            "assessed_positions": len(assessments),
            "unconfigured_positions": unconfigured,
            "history_snapshots": len({item.snapshot_id for item in history}),
        },
        assessments,
        findings,
        tuple(notes),
    )
    return LifecycleReport(
        **{**provisional.__dict__, "lifecycle_report_id": _identity(provisional)}
    )


def unavailable_lifecycle_report(snapshot: PortfolioSnapshot, note: str) -> LifecycleReport:
    """Represent an independently failed optional lifecycle module without masking risk."""

    provisional = LifecycleReport(
        "1.0",
        "",
        snapshot.as_of,
        snapshot.snapshot_id,
        "UNAVAILABLE",
        {
            "current_positions": len(aggregate_economic_positions(snapshot)),
            "configured_records": 0,
            "assessed_positions": 0,
            "unconfigured_positions": 0,
            "history_snapshots": 0,
        },
        (),
        (),
        (terminal_text(note),),
    )
    return LifecycleReport(
        **{**provisional.__dict__, "lifecycle_report_id": _identity(provisional)}
    )
