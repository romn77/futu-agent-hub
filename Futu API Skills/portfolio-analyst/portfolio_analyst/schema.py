"""Runtime validation against the published snapshot schema."""

from __future__ import annotations

import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NoReturn

import jsonschema
from referencing import Registry, Resource

from .normalization import canonical_unknown


class ReportSemanticError(ValueError):
    """A structurally valid report violates the risk evidence contract."""


def _schema_path(name: str = "portfolio_snapshot.schema.json") -> Path:
    candidates = (
        Path(__file__).resolve().parents[1] / "schemas" / name,
        Path(sys.prefix) / "schemas" / name,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"{name} is not installed")


def validate_snapshot_document(payload: dict[str, Any]) -> None:
    schema = json.loads(_schema_path().read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    validator.validate(payload)


def _validate(payload: dict[str, Any], name: str) -> None:
    schema = json.loads(_schema_path(name).read_text(encoding="utf-8"))
    definitions = json.loads(_schema_path("report_defs.schema.json").read_text(encoding="utf-8"))
    risk_schema = json.loads(_schema_path("risk_report.schema.json").read_text(encoding="utf-8"))
    registry = Registry().with_resources(
        [
            (definitions["$id"], Resource.from_contents(definitions)),
            (risk_schema["$id"], Resource.from_contents(risk_schema)),
        ]
    )
    validator = jsonschema.Draft202012Validator(
        schema, registry=registry, format_checker=jsonschema.FormatChecker()
    )
    validator.validate(payload)


def _semantic_error(detail: str) -> NoReturn:
    raise ReportSemanticError(f"correlation semantic validation failed: {detail}")


def _finite_decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        _semantic_error(f"{field} must be a finite decimal")
    if not result.is_finite():
        _semantic_error(f"{field} must be a finite decimal")
    return result


def _high_components(
    identities: set[str], coefficients: dict[tuple[str, str], Decimal], high: Decimal
) -> set[frozenset[str]]:
    adjacency = {identity: set() for identity in identities}
    for (left, right), coefficient in coefficients.items():
        if coefficient >= high:
            adjacency[left].add(right)
            adjacency[right].add(left)
    components: set[frozenset[str]] = set()
    remaining = set(identities)
    while remaining:
        start = min(remaining)
        stack = [start]
        component: set[str] = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(sorted(adjacency[current] - component, reverse=True))
        remaining -= component
        if len(component) > 1:
            components.add(frozenset(component))
    return components


def _validate_correlation_semantics(correlation: dict[str, Any]) -> None:
    lookback = correlation["lookback"]
    minimum = correlation["minimum_observations"]
    if isinstance(lookback, bool) or not isinstance(lookback, int) or not 2 <= lookback <= 5000:
        _semantic_error("lookback must be an integer between 2 and 5000")
    if (
        isinstance(minimum, bool)
        or not isinstance(minimum, int)
        or minimum <= 0
        or minimum > lookback - 1
    ):
        _semantic_error("minimum observations must fit the lookback return capacity")
    medium = _finite_decimal(correlation["medium_threshold"], "medium threshold")
    high = _finite_decimal(correlation["high_threshold"], "high threshold")
    if not Decimal("0") <= medium <= high <= Decimal("1"):
        _semantic_error("thresholds require 0 <= medium <= high <= 1")

    requested = correlation["coverage"]["requested"]
    available = correlation["coverage"]["available"]
    if not 0 <= available <= requested:
        _semantic_error("coverage requires 0 <= available <= requested")
    matrix = correlation["matrix"]
    identities = set(matrix)
    if len(identities) != available:
        _semantic_error("matrix identities must match available coverage")

    coefficients: dict[tuple[str, str], Decimal] = {}
    for left, row in matrix.items():
        if set(row) != identities:
            _semantic_error("matrix must be square with consistent row identities")
        diagonal = _finite_decimal(row[left], f"matrix diagonal {left}")
        if diagonal != Decimal("1"):
            _semantic_error("matrix diagonal must be exactly 1")
        for right, raw_coefficient in row.items():
            if raw_coefficient is None:
                if left == right:
                    _semantic_error("matrix diagonal cannot be unavailable")
                continue
            coefficient = _finite_decimal(raw_coefficient, f"matrix coefficient {left}/{right}")
            if abs(coefficient) > 1:
                _semantic_error("matrix coefficients must be between -1 and 1")
            reverse = matrix[right][left]
            if (
                reverse is None
                or _finite_decimal(reverse, f"matrix coefficient {right}/{left}") != coefficient
            ):
                _semantic_error("matrix must be symmetric")
            if left != right:
                coefficients[tuple(sorted((left, right)))] = coefficient

    expected_count = available * (available - 1) // 2
    complete = available == requested and available >= 2 and len(coefficients) == expected_count
    expected_status = "COMPLETE" if complete else "PARTIAL" if coefficients else "UNAVAILABLE"
    if correlation["status"] != expected_status:
        _semantic_error(f"status must be {expected_status} for the supplied coverage and matrix")

    observed_pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for pair in correlation["pairs"]:
        left, right = pair["symbols"]
        if left == right or left not in identities or right not in identities:
            _semantic_error("pair identities must be distinct available matrix members")
        key = tuple(sorted((left, right)))
        if key in observed_pairs:
            _semantic_error("pair identities must be unique when treated as unordered")
        if key not in coefficients:
            _semantic_error("pair must reference an available matrix coefficient")
        coefficient = _finite_decimal(pair["coefficient"], "pair coefficient")
        if coefficient != coefficients[key]:
            _semantic_error("pair coefficient must match the matrix")
        sample_count = pair["sample_count"]
        if not minimum <= sample_count <= lookback - 1:
            _semantic_error("pair sample count must fit minimum and lookback return capacity")
        expected_band = (
            "HIGH" if coefficient >= high else "MEDIUM" if coefficient >= medium else None
        )
        if pair["band"] != expected_band:
            _semantic_error("pair band must match configured thresholds")
        observed_pairs[key] = pair

    expected_pair_keys = {key for key, coefficient in coefficients.items() if coefficient >= medium}
    if set(observed_pairs) != expected_pair_keys:
        _semantic_error("pairs must cover every positive coefficient at or above medium threshold")

    observed_inverse: set[tuple[str, str]] = set()
    for pair in correlation["inverse_pairs"]:
        left, right = pair["symbols"]
        if left == right or left not in identities or right not in identities:
            _semantic_error("inverse pair identities must be distinct available matrix members")
        key = tuple(sorted((left, right)))
        if key in observed_inverse or key not in coefficients:
            _semantic_error("inverse pair identities must be unique available coefficients")
        coefficient = _finite_decimal(pair["coefficient"], "inverse pair coefficient")
        if coefficient != coefficients[key]:
            _semantic_error("inverse pair coefficient must match the matrix")
        sample_count = pair["sample_count"]
        if not minimum <= sample_count <= lookback - 1:
            _semantic_error("inverse pair sample count must fit the return capacity")
        expected_band = (
            "HIGH" if coefficient <= -high else "MEDIUM" if coefficient <= -medium else None
        )
        if pair["band"] != expected_band:
            _semantic_error("inverse pair band must match negative thresholds")
        observed_inverse.add(key)
    expected_inverse = {key for key, coefficient in coefficients.items() if coefficient <= -medium}
    if observed_inverse != expected_inverse:
        _semantic_error("inverse pairs must cover every strong negative coefficient")

    observed_clusters: set[frozenset[str]] = set()
    for cluster in correlation["clusters"]:
        members = frozenset(cluster)
        if len(members) != len(cluster) or not members <= identities:
            _semantic_error("clusters require distinct available matrix identities")
        if members in observed_clusters:
            _semantic_error("duplicate cluster representation is not allowed")
        observed_clusters.add(members)
    if observed_clusters != _high_components(identities, coefficients, high):
        _semantic_error("clusters must equal high-correlation connected components")


def _validate_report_semantics(payload: dict[str, Any]) -> None:
    correlation = payload["correlation"]
    _validate_correlation_semantics(correlation)
    coverage = payload["coverage"]
    if coverage["positions"] + coverage["omitted_positions"] != coverage["total_positions"]:
        _semantic_error("position budget coverage does not reconcile")
    expected_budget = "BUDGET_LIMIT" if coverage["budget_reason"] else "COMPLETE"
    if coverage["budget_status"] != expected_budget:
        _semantic_error("budget status and reason are inconsistent")
    if payload["coverage"]["price_histories"] != correlation["coverage"]:
        _semantic_error("report price-history coverage must match correlation coverage")
    if correlation["coverage"]["requested"] > payload["coverage"]["positions"]:
        _semantic_error("requested histories cannot exceed economic-position coverage")
    pnl_status = coverage["pnl_evidence_status"]
    exposure_totals: dict[str, Decimal] = {}
    for view_name in ("sector", "region", "theme_full", "theme_split"):
        total = Decimal("0")
        for label, raw_weight in payload["exposures"][view_name]["weights"].items():
            if canonical_unknown(label) != label:
                raise ReportSemanticError(
                    "exposure semantic validation failed: labels must be canonical"
                )
            weight = _finite_decimal(raw_weight, f"{view_name} exposure {label}")
            if weight < 0:
                raise ReportSemanticError(
                    "exposure semantic validation failed: long-only weights cannot be negative"
                )
            total += weight
        exposure_totals[view_name] = total
    additive_total = exposure_totals["theme_split"]
    if any(
        abs(exposure_totals[name] - additive_total) > Decimal("1e-24")
        for name in ("sector", "region")
    ):
        raise ReportSemanticError(
            "exposure semantic validation failed: additive NAV views do not reconcile"
        )
    checks = payload["policy_checks"]
    if payload["policy_status"] == "NOT_CONFIGURED" and checks:
        raise ReportSemanticError("unconfigured policy cannot contain threshold checks")
    for name, check in checks.items():
        measured = _finite_decimal(check["measured"], f"policy {name} measured")
        threshold = _finite_decimal(check["threshold"], f"policy {name} threshold")
        if check["breached"] != (measured > threshold):
            raise ReportSemanticError(f"policy breach boolean is inconsistent for {name}")
    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNRATED": 4}
    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    findings = payload.get("top_risks", payload.get("largest_risks"))
    sorted_findings = sorted(
        findings,
        key=lambda finding: (
            severity_order[finding["severity"]],
            priority_order[finding["review_priority"]],
            finding["type"],
            tuple(finding["affected_positions"]),
        ),
    )
    if findings != sorted_findings:
        raise ReportSemanticError("risk findings are not deterministically severity-sorted")
    rated = [finding["severity"] for finding in findings if finding["severity"] != "UNRATED"]
    expected_overall = min(rated, key=severity_order.get) if rated else "UNRATED"
    if payload["overall_risk"] != expected_overall:
        raise ReportSemanticError("overall risk is inconsistent with rated findings")
    if payload["policy_status"] == "NOT_CONFIGURED" and rated:
        raise ReportSemanticError("findings cannot be rated without USER_CONFIG policy evidence")
    finding_checks = {
        "POSITION_CONCENTRATION": tuple(
            name for name in ("single_position", "top_3", "top_5") if name in checks
        ),
        "SECTOR_CONCENTRATION": ("sector",) if "sector" in checks else (),
        "REGION_CONCENTRATION": ("region",) if "region" in checks else (),
        "THEME_CONCENTRATION": ("theme_split",) if "theme_split" in checks else (),
    }
    finding_types = {finding["type"] for finding in findings}
    if "PNL_CONCENTRATION" in finding_types and pnl_status != "COMPLETE":
        raise ReportSemanticError(
            "P&L semantic validation failed: concentration requires complete evidence"
        )
    for finding in findings:
        if finding["type"] != "PNL_CONCENTRATION":
            continue
        evidence = finding["evidence"]
        for total_field, share_field in (
            ("profit_total_base", "largest_profit_share"),
            ("loss_total_abs_base", "largest_loss_share"),
        ):
            total = _finite_decimal(evidence[total_field], f"P&L {total_field}")
            raw_share = evidence[share_field]
            if total < 0 or (total == 0) != (raw_share is None):
                raise ReportSemanticError(
                    "P&L semantic validation failed: pool totals and shares are inconsistent"
                )
            if raw_share is not None:
                share = _finite_decimal(raw_share, f"P&L {share_field}")
                if not Decimal("0") < share <= Decimal("1"):
                    raise ReportSemanticError(
                        "P&L semantic validation failed: concentration share is outside (0, 1]"
                    )
        if (
            _finite_decimal(evidence["profit_total_base"], "P&L profit total") == 0
            and _finite_decimal(evidence["loss_total_abs_base"], "P&L loss total") == 0
        ):
            raise ReportSemanticError(
                "P&L semantic validation failed: an empty P&L population is not a finding"
            )
    for finding in findings:
        relevant = finding_checks.get(finding["type"], ())
        applied = bool(relevant)
        evidence_applied = finding["evidence"].get("policy_threshold_applied")
        if evidence_applied is not None and evidence_applied != applied:
            raise ReportSemanticError(
                "finding policy evidence flag does not match configured checks"
            )
        expected_severity = (
            "HIGH"
            if any(checks[name]["breached"] for name in relevant)
            else "LOW"
            if relevant
            else "UNRATED"
        )
        if finding["severity"] != expected_severity:
            raise ReportSemanticError("finding severity is not supported by policy evidence")
    breached_candidate_types = {
        candidate_type
        for check_name, candidate_type in {
            "single_position": "POSITION_CONCENTRATION",
            "top_3": "POSITION_CONCENTRATION",
            "top_5": "POSITION_CONCENTRATION",
            "sector": "SECTOR_CONCENTRATION",
            "region": "REGION_CONCENTRATION",
            "theme_split": "THEME_CONCENTRATION",
        }.items()
        if checks.get(check_name, {}).get("breached")
    }
    missing_breaches = breached_candidate_types - finding_types
    candidate_priority = {
        "POSITION_CONCENTRATION": 1,
        "REGION_CONCENTRATION": 2,
        "SECTOR_CONCENTRATION": 2,
        "THEME_CONCENTRATION": 3,
    }
    if missing_breaches:
        worst_reported = (
            severity_order[findings[-1]["severity"]],
            priority_order[findings[-1]["review_priority"]],
            findings[-1]["type"],
        )
        if any(
            (severity_order["HIGH"], candidate_priority[item], item) < worst_reported
            for item in missing_breaches
        ):
            raise ReportSemanticError(
                "breached policy candidate was omitted in favor of a lower-ranked finding"
            )


def validate_risk_document(payload: dict[str, Any]) -> None:
    _validate(payload, "risk_report.schema.json")
    _validate_report_semantics(payload)


def validate_portfolio_report_document(payload: dict[str, Any]) -> None:
    _validate(payload, "portfolio_report.schema.json")
    _validate_report_semantics(payload)
    embedded = payload["risk_report"]
    _validate_report_semantics(embedded)
    for portfolio_field, risk_field in (
        ("as_of", "as_of"),
        ("source_snapshot_id", "source_snapshot_id"),
        ("coverage", "coverage"),
        ("data_quality_status", "data_quality_status"),
        ("data_quality_notes", "data_quality_notes"),
        ("overall_risk", "overall_risk"),
        ("concentration", "concentration"),
        ("exposures", "exposures"),
        ("correlation", "correlation"),
        ("largest_risks", "top_risks"),
        ("policy_status", "policy_status"),
        ("policy_checks", "policy_checks"),
    ):
        if payload[portfolio_field] != embedded[risk_field]:
            raise ReportSemanticError(
                f"embedded risk report does not match portfolio field {portfolio_field}"
            )
