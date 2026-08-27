"""Deterministic position-lifecycle analysis from user facts and immutable snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
import hashlib
import json
import re
import time
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
    severity: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", _deep_freeze(dict(self.evidence)))
        try:
            severity = {
                "P0": "HIGH",
                "P1": "MEDIUM",
                "P2": "LOW",
                "P3": "INFORMATIONAL",
            }[self.review_priority]
        except KeyError as exc:
            raise LifecycleInputError("unsupported lifecycle review priority") from exc
        object.__setattr__(self, "severity", severity)


@dataclass(frozen=True)
class LifecycleHistoryEvidence:
    snapshot_id: str
    as_of: Any
    identity: str
    present: bool
    weight: Decimal | None


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
    original_thesis_evidence: Mapping[str, Any] | None
    invalidation_conditions: tuple[str, ...]
    review_triggers: tuple[str, ...]
    notes: tuple[str, ...]
    opened_at: date | None
    holding_horizon: str | None
    last_reviewed_at: date | None
    history_evidence_status: str
    history_evidence: tuple[LifecycleHistoryEvidence, ...]
    history_excluded_pre_open: int
    role_drift_policy: Mapping[str, Any]
    findings: tuple[LifecycleFinding, ...]
    review_priority: str
    data_quality_notes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_weight",
            None if self.target_weight is None else _deep_freeze(dict(self.target_weight)),
        )
        object.__setattr__(
            self,
            "original_thesis_evidence",
            None
            if self.original_thesis_evidence is None
            else _deep_freeze(dict(self.original_thesis_evidence)),
        )
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "invalidation_conditions", tuple(self.invalidation_conditions))
        object.__setattr__(self, "review_triggers", tuple(self.review_triggers))
        object.__setattr__(self, "notes", tuple(self.notes))
        object.__setattr__(self, "history_evidence", tuple(self.history_evidence))
        object.__setattr__(self, "role_drift_policy", _deep_freeze(dict(self.role_drift_policy)))
        object.__setattr__(self, "data_quality_notes", tuple(self.data_quality_notes))


@dataclass(frozen=True)
class LifecycleReport:
    schema_version: str
    lifecycle_report_id: str
    as_of: Any
    source_snapshot_id: str
    status: str
    data_quality_status: str
    coverage: Mapping[str, Any]
    positions: tuple[LifecycleAssessment, ...]
    findings: tuple[LifecycleFinding, ...]
    data_quality_reasons: tuple[Mapping[str, str], ...]
    data_quality_notes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "coverage", _deep_freeze(dict(self.coverage)))
        object.__setattr__(self, "positions", tuple(self.positions))
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(
            self,
            "data_quality_reasons",
            tuple(_deep_freeze(dict(reason)) for reason in self.data_quality_reasons),
        )
        object.__setattr__(self, "data_quality_notes", tuple(self.data_quality_notes))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)

    def to_human(self) -> str:
        coverage = self.to_dict()["coverage"]
        lines = [
            f"Lifecycle report {terminal_text(self.lifecycle_report_id)}",
            f"source_snapshot_id: {terminal_text(self.source_snapshot_id)}",
            f"As of: {self.to_dict()['as_of']}",
            f"Status: {self.status}",
            f"Data quality status: {self.data_quality_status}",
            f"Coverage: {terminal_text(json.dumps(coverage, ensure_ascii=False, sort_keys=True))}",
            "Quality reasons:",
            *(
                [
                    f"- {reason['code']} [{terminal_text(reason['scope'])}]: {terminal_text(reason['detail'])}"
                    for reason in self.data_quality_reasons
                ]
                or ["- None"]
            ),
            "Quality notes:",
            *([f"- {terminal_text(note)}" for note in self.data_quality_notes] or ["- None"]),
        ]
        for item in self.positions:
            thesis = _thesis_display(item.original_thesis, item.original_thesis_evidence)
            lines.extend(
                [
                    f"Position {terminal_text(item.symbol)}",
                    f"Role / record status / lifecycle: {item.role} / {item.record_status or 'UNCONFIGURED'} / {item.lifecycle_status}",
                    f"Current weight: {item.current_weight if item.current_weight is not None else 'N/A'}",
                    "Target bounds: "
                    + (
                        f"{item.target_weight['min']}..{item.target_weight['max']}"
                        if item.target_weight is not None
                        else "N/A"
                    ),
                    f"Opened / horizon / last review: {item.opened_at or 'N/A'} / {item.holding_horizon or 'N/A'} / {item.last_reviewed_at or 'N/A'}",
                    f"Thesis status: {item.thesis_status}; Original thesis: {thesis}",
                    "Thesis provenance: "
                    + (
                        "USER_CONFIG; system inferred: false"
                        if item.original_thesis is not None
                        else "not configured; system inferred: false"
                    ),
                    "Invalidation conditions (USER_CONFIG; system inferred: false): "
                    + (
                        "; ".join(terminal_text(value) for value in item.invalidation_conditions)
                        or "None"
                    ),
                    "Review triggers (USER_CONFIG; system inferred: false): "
                    + ("; ".join(terminal_text(value) for value in item.review_triggers) or "None"),
                    "User notes (USER_CONFIG; system inferred: false): "
                    + ("; ".join(terminal_text(value) for value in item.notes) or "None"),
                    f"History status: {item.history_evidence_status}; observations={len(item.history_evidence)}; excluded_pre_open={item.history_excluded_pre_open}; policy={terminal_text(json.dumps(_json_value(item.role_drift_policy), sort_keys=True))}",
                ]
            )
            lines.extend(
                f"History evidence: {terminal_text(entry.snapshot_id)} | {entry.as_of.isoformat()} | {terminal_text(entry.identity)} | present={entry.present} | weight={entry.weight if entry.weight is not None else 'N/A'}"
                for entry in item.history_evidence
            )
            lines.extend(
                f"Finding: type={finding.type}; severity={finding.severity}; evidence={terminal_text(json.dumps(_json_value(finding.evidence), ensure_ascii=False, sort_keys=True))}; priority={finding.review_priority}; summary={terminal_text(finding.summary)}"
                for finding in item.findings
            )
            lines.extend(
                [f"Position quality: {terminal_text(note)}" for note in item.data_quality_notes]
                or ["Position quality: None"]
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
            f"- Data quality status: {self.data_quality_status}",
            "- Coverage: "
            + md(json.dumps(self.to_dict()["coverage"], ensure_ascii=False, sort_keys=True)),
            f"- Authoritative JSON: `{md(json_artifact)}`"
            if json_artifact
            else "- Authoritative JSON: current output",
            "",
            "## Quality reasons",
            "",
            *(
                [
                    f"- {reason['code']} [{md(reason['scope'])}]: {md(reason['detail'])}"
                    for reason in self.data_quality_reasons
                ]
                or ["- None"]
            ),
            "",
            "## Quality notes",
            "",
            *([f"- {md(note)}" for note in self.data_quality_notes] or ["- None"]),
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
                    f"- Role: {md(item.role)}; record status: {md(item.record_status or 'UNCONFIGURED')}",
                    f"- Current weight: {item.current_weight if item.current_weight is not None else 'N/A'}",
                    "- Target bounds: "
                    + (
                        f"{item.target_weight['min']} to {item.target_weight['max']}"
                        if item.target_weight is not None
                        else "N/A"
                    ),
                    f"- Thesis status: {item.thesis_status}",
                    "- Original thesis (USER_CONFIG; system inferred: false): "
                    + (
                        md(item.original_thesis)
                        if item.original_thesis is not None
                        else "Not configured"
                    ),
                    f"- Original thesis evidence: {_thesis_display(None, item.original_thesis_evidence)}",
                    "- Invalidation conditions (USER_CONFIG; system inferred: false): "
                    + ("; ".join(md(value) for value in item.invalidation_conditions) or "None"),
                    "- Review triggers (USER_CONFIG; system inferred: false): "
                    + ("; ".join(md(value) for value in item.review_triggers) or "None"),
                    "- User notes (USER_CONFIG; system inferred: false): "
                    + ("; ".join(md(value) for value in item.notes) or "None"),
                    f"- Last reviewed: {item.last_reviewed_at or 'Not configured'}",
                    f"- Opened at: {item.opened_at or 'Not configured'}",
                    f"- Holding horizon: {md(item.holding_horizon or 'Not configured')}",
                    f"- History status: {item.history_evidence_status}",
                    f"- History excluded pre-open: {item.history_excluded_pre_open}",
                    "- Role drift policy: "
                    + md(json.dumps(_json_value(item.role_drift_policy), sort_keys=True)),
                    f"- Review priority: {item.review_priority}",
                    *(
                        [f"- Note: {md(note)}" for note in item.data_quality_notes]
                        or ["- Note: None"]
                    ),
                    "",
                    "#### History evidence",
                    "",
                    *(
                        [
                            f"- `{md(entry.snapshot_id)}` | {entry.as_of.isoformat()} | {md(entry.identity)} | present={entry.present} | weight={entry.weight if entry.weight is not None else 'N/A'}"
                            for entry in item.history_evidence
                        ]
                        or ["- None"]
                    ),
                    "",
                    "#### Findings",
                    "",
                    *(
                        [
                            f"- type={finding.type}; severity={finding.severity}; priority={finding.review_priority}; "
                            f"summary={md(finding.summary)}; evidence={md(json.dumps(_json_value(finding.evidence), ensure_ascii=False, sort_keys=True))}"
                            for finding in item.findings
                        ]
                        or ["- None"]
                    ),
                    "",
                ]
            )
        return "\n".join(lines) + "\n"


def _thesis_evidence(value: str) -> Mapping[str, Any]:
    return {
        "configured": True,
        "provenance": "USER_CONFIG",
        "system_inferred": False,
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "characters": len(value),
    }


def _thesis_display(text: str | None, evidence: Mapping[str, Any] | None) -> str:
    if evidence is None:
        return "not configured"
    prefix = terminal_text(text) if text is not None else "configured"
    return f"{prefix} (sha256={evidence['sha256']}, characters={evidence['characters']})"


def _quality_note(reason: Mapping[str, str]) -> str:
    return f"{reason['code']} [{reason['scope']}]: {reason['detail']}"


def _quality_reasons(
    coverage: Mapping[str, Any], positions: Sequence[LifecycleAssessment]
) -> tuple[Mapping[str, str], ...]:
    history = coverage["history"]
    reasons: list[dict[str, str]] = []
    if history.get("issue") is not None:
        reasons.append(
            {
                "code": history["issue"]["code"],
                "scope": "HISTORY",
                "detail": history["issue"]["detail"],
            }
        )
    elif history["status"] != "AVAILABLE":
        reasons.append(
            {
                "code": f"HISTORY_{history['status']}",
                "scope": "HISTORY",
                "detail": "eligible lifecycle history is not fully available",
            }
        )
    for count_field, code, detail in (
        ("future_snapshots", "FUTURE_HISTORY_EXCLUDED", "future snapshots were excluded"),
        ("duplicate_snapshots", "DUPLICATE_HISTORY_EXCLUDED", "duplicate snapshots were excluded"),
        (
            "pre_open_observations",
            "PRE_OPEN_HISTORY_EXCLUDED",
            "pre-open observations were excluded",
        ),
    ):
        if history[count_field]:
            reasons.append(
                {
                    "code": code,
                    "scope": "HISTORY",
                    "detail": f"{history[count_field]} {detail}",
                }
            )
    for item in positions:
        reasons.extend(
            {
                "code": "POSITION_DATA_NOTE",
                "scope": item.symbol,
                "detail": note,
            }
            for note in item.data_quality_notes
        )
    return tuple(
        sorted(reasons, key=lambda reason: (reason["code"], reason["scope"], reason["detail"]))
    )


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


_ROLE_POLICY = {
    "threshold": Decimal("0.10"),
    "minimum_snapshots": 3,
    "minimum_span_days": 30,
}
MAX_HISTORY_SNAPSHOTS = 1_000
MAX_CURRENT_POSITIONS = 10_000
MAX_CONFIG_RECORDS = 10_000
MAX_POSITION_OBSERVATIONS = 2_000_000


def _history_evidence(
    record: LifecycleRecord,
    history: Sequence[tuple[PortfolioSnapshot, Mapping[str, EconomicPosition]]],
) -> tuple[tuple[LifecycleHistoryEvidence, ...], int]:
    observations: list[LifecycleHistoryEvidence] = []
    pre_open = 0
    for snapshot, positions in history:
        if snapshot.as_of.date() < record.opened_at:
            pre_open += 1
            continue
        item = positions.get(record.symbol)
        observations.append(
            LifecycleHistoryEvidence(
                snapshot.snapshot_id,
                snapshot.as_of,
                record.symbol,
                item is not None,
                None if item is None else item.weight,
            )
        )
    return tuple(sorted(observations, key=lambda item: (item.as_of, item.snapshot_id))), pre_open


def _history_role_drift(
    record: LifecycleRecord,
    current: EconomicPosition,
    evidence: Sequence[LifecycleHistoryEvidence],
) -> tuple[bool, str]:
    if record.role != "SATELLITE" or current.weight < _ROLE_POLICY["threshold"]:
        return False, "NOT_REQUIRED"
    sufficient = (
        len(evidence) >= _ROLE_POLICY["minimum_snapshots"]
        and (evidence[-1].as_of - evidence[0].as_of).days >= _ROLE_POLICY["minimum_span_days"]
        and all(item.present for item in evidence)
    )
    if not sufficient:
        return False, "PARTIAL"
    return (
        all(
            item.weight is not None and item.weight >= _ROLE_POLICY["threshold"]
            for item in evidence
        ),
        "AVAILABLE",
    )


def _assessment(
    current: EconomicPosition | None,
    record: LifecycleRecord | None,
    *,
    as_of: date,
    history: Sequence[tuple[PortfolioSnapshot, Mapping[str, EconomicPosition]]],
    thesis_review: str | None,
) -> LifecycleAssessment:
    symbol = current.identity if current is not None else record.symbol  # type: ignore[union-attr]
    if record is None:
        evidence = tuple(
            LifecycleHistoryEvidence(
                item.snapshot_id,
                item.as_of,
                symbol,
                symbol in positions,
                positions[symbol].weight if symbol in positions else None,
            )
            for item, positions in history
        )
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
            None,
            (),
            (),
            (),
            None,
            None,
            None,
            "NOT_REQUIRED",
            evidence,
            0,
            _ROLE_POLICY,
            (finding,),
            "P2",
            ("No thesis or target was inferred.",),
        )
    if record.last_reviewed_at > as_of or record.opened_at > as_of:
        raise LifecycleInputError("lifecycle record dates cannot be after snapshot as_of")
    evidence, pre_open = _history_evidence(record, history)
    history_notes: list[str] = []
    if pre_open:
        history_notes.append(
            f"history evidence excluded {pre_open} pre-open snapshot(s) for {record.symbol}"
        )
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
            _thesis_evidence(record.original_thesis),
            record.invalidation_conditions,
            record.review_triggers,
            record.notes,
            record.opened_at,
            record.holding_horizon,
            record.last_reviewed_at,
            "NOT_REQUIRED",
            evidence,
            pre_open,
            _ROLE_POLICY,
            (finding,),
            "P3",
            tuple(history_notes),
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
    role_drift, history_status = _history_role_drift(record, current, evidence)
    notes.extend(history_notes)
    if history_status == "PARTIAL":
        notes.append(
            "role drift unavailable: requires at least 3 eligible snapshots spanning 30 days"
        )
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
        _thesis_evidence(record.original_thesis),
        record.invalidation_conditions,
        record.review_triggers,
        record.notes,
        record.opened_at,
        record.holding_horizon,
        record.last_reviewed_at,
        history_status,
        evidence,
        pre_open,
        _ROLE_POLICY,
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
    history_issue: Mapping[str, str] | None = None,
    max_history_snapshots: int = MAX_HISTORY_SNAPSHOTS,
    max_operations: int = 3_000_000,
    max_seconds: float = 10.0,
    clock: Any = time.monotonic,
) -> LifecycleReport:
    if max_history_snapshots < 0 or max_operations <= 0 or max_seconds <= 0:
        raise LifecycleInputError("lifecycle analysis budget must be positive")
    candidate_history_count = len(history)
    normalized_history_issue: dict[str, str] | None = None
    if history_issue is not None:
        code = str(history_issue.get("code", ""))
        if code not in {"HISTORY_INVALID", "HISTORY_UNAVAILABLE"}:
            raise LifecycleInputError("lifecycle history issue code is unsupported")
        normalized_history_issue = {
            "code": code,
            "detail": terminal_text(history_issue.get("detail", "history repository unavailable")),
        }
    if candidate_history_count > max_history_snapshots:
        raise LifecycleInputError("lifecycle history snapshot budget exceeded")
    started = clock()
    remaining_operations = max_operations

    def consume(operations: int = 1) -> None:
        nonlocal remaining_operations
        remaining_operations -= operations
        if remaining_operations < 0 or clock() - started > max_seconds:
            raise LifecycleInputError("lifecycle analysis operation or wall-time budget exceeded")

    try:
        if len(snapshot.positions) > MAX_CURRENT_POSITIONS:
            raise LifecycleInputError("lifecycle current position budget exceeded")
        consume(len(snapshot.positions) + 1)
        snapshot = snapshot_from_document(snapshot.to_dict())
        validated_history_list: list[PortfolioSnapshot] = []
        for item in history:
            if len(item.positions) > MAX_CURRENT_POSITIONS:
                raise LifecycleInputError("lifecycle history position budget exceeded")
            consume(len(item.positions) + 1)
            validated_history_list.append(snapshot_from_document(item.to_dict()))
        validated_history = tuple(validated_history_list)
    except LifecycleInputError:
        raise
    except InvalidSnapshot as exc:
        raise LifecycleInputError("lifecycle input snapshot is invalid") from exc
    accepted_history: list[PortfolioSnapshot] = []
    seen_history: set[str] = set()
    duplicate_history = 0
    future_history = 0
    for item in sorted(validated_history, key=lambda value: (value.as_of, value.snapshot_id)):
        if item.snapshot_id in seen_history:
            duplicate_history += 1
            continue
        seen_history.add(item.snapshot_id)
        if item.as_of > snapshot.as_of:
            future_history += 1
            continue
        accepted_history.append(item)
    history = tuple(accepted_history)
    economic = aggregate_economic_positions(snapshot)
    if len(economic) > MAX_CURRENT_POSITIONS:
        raise LifecycleInputError("lifecycle current position budget exceeded")
    if any(
        not item.weight.is_finite() or not Decimal(0) <= item.weight <= Decimal(1)
        for item in economic
    ):
        raise LifecycleInputError("signed lifecycle portfolios are unsupported")
    current = {item.identity: item for item in economic}
    records = {
        item.symbol: item
        for item in repository.list(
            limit=MAX_CONFIG_RECORDS,
            max_operations=max_operations,
            max_seconds=max_seconds,
            clock=clock,
        )
    }
    consume(len(records) + 1)
    if len(records) > MAX_CONFIG_RECORDS:
        raise LifecycleInputError("lifecycle configuration record budget exceeded")
    history_index: list[tuple[PortfolioSnapshot, Mapping[str, EconomicPosition]]] = []
    for item in history:
        positions = aggregate_economic_positions(item)
        consume(len(item.positions) + len(positions) + 1)
        if any(
            not position.weight.is_finite() or not Decimal(0) <= position.weight <= Decimal(1)
            for position in positions
        ):
            raise LifecycleInputError("lifecycle history weight is outside the supported range")
        history_index.append((item, {position.identity: position for position in positions}))
    reviews = {str(key).upper(): value for key, value in (thesis_reviews or {}).items()}
    symbols = sorted(set(current) | set(records))
    observation_budget = len(history_index) * len(symbols)
    if observation_budget > MAX_POSITION_OBSERVATIONS:
        raise LifecycleInputError("lifecycle position observation budget exceeded")
    consume(observation_budget + len(symbols))
    assessments = tuple(
        _assessment(
            current.get(symbol),
            records.get(symbol),
            as_of=snapshot.as_of.date(),
            history=history_index,
            thesis_review=reviews.get(symbol),
        )
        for symbol in symbols
    )
    if normalized_history_issue is not None:
        position_note = f"{normalized_history_issue['code']}: {normalized_history_issue['detail']}"
        assessments = tuple(
            replace(item, data_quality_notes=(*item.data_quality_notes, position_note))
            for item in assessments
        )
    findings = tuple(
        sorted(
            (finding for item in assessments for finding in item.findings),
            key=lambda item: (item.review_priority, item.type, item.symbol),
        )
    )
    unconfigured = sum(item.lifecycle_status == "UNCONFIGURED" for item in assessments)
    coverage = {
        "current_positions": len(current),
        "configured_records": len(records),
        "assessed_positions": len(assessments),
        "unconfigured_positions": unconfigured,
        "history_snapshots": len({item.snapshot_id for item in history}),
        "history": {
            "status": (
                "PARTIAL"
                if normalized_history_issue is not None
                or future_history
                or duplicate_history
                or any(item.history_excluded_pre_open for item in assessments)
                or any(item.history_evidence_status == "PARTIAL" for item in assessments)
                else "AVAILABLE"
                if history
                else "UNAVAILABLE"
            ),
            "candidate_snapshots": candidate_history_count,
            "accepted_snapshots": len(history),
            "duplicate_snapshots": duplicate_history,
            "future_snapshots": future_history,
            "pre_open_observations": sum(item.history_excluded_pre_open for item in assessments),
            "position_observations": sum(len(item.history_evidence) for item in assessments),
            "window_start": history[0].as_of if history else None,
            "window_end": history[-1].as_of if history else None,
            "snapshots": tuple(
                {"snapshot_id": item.snapshot_id, "as_of": item.as_of} for item in history
            ),
            "issue": normalized_history_issue,
        },
    }
    reasons = _quality_reasons(coverage, assessments)
    notes = tuple(_quality_note(reason) for reason in reasons)
    status = "PARTIAL" if reasons else "COMPLETE"
    provisional = LifecycleReport(
        "1.0",
        "",
        snapshot.as_of,
        snapshot.snapshot_id,
        status,
        status,
        coverage,
        assessments,
        findings,
        reasons,
        notes,
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
        "UNAVAILABLE",
        {
            "current_positions": len(aggregate_economic_positions(snapshot)),
            "configured_records": 0,
            "assessed_positions": 0,
            "unconfigured_positions": 0,
            "history_snapshots": 0,
            "history": {
                "status": "UNAVAILABLE",
                "candidate_snapshots": 0,
                "accepted_snapshots": 0,
                "duplicate_snapshots": 0,
                "future_snapshots": 0,
                "pre_open_observations": 0,
                "position_observations": 0,
                "window_start": None,
                "window_end": None,
                "snapshots": (),
                "issue": None,
            },
        },
        (),
        (),
        (
            {
                "code": "MODULE_UNAVAILABLE",
                "scope": "LIFECYCLE",
                "detail": terminal_text(note),
            },
        ),
        (
            _quality_note(
                {"code": "MODULE_UNAVAILABLE", "scope": "LIFECYCLE", "detail": terminal_text(note)}
            ),
        ),
    )
    return LifecycleReport(
        **{**provisional.__dict__, "lifecycle_report_id": _identity(provisional)}
    )
