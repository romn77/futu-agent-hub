"""Runtime validation against the published snapshot schema."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, NoReturn, Sequence
from zoneinfo import ZoneInfo

import jsonschema
from referencing import Registry, Resource

from .models import _json_value
from .identity import InvalidSnapshotIdentity, parse_snapshot_identity, utc_floor_second
from .normalization import canonical_unknown
from .risk import _latest_complete_market_close_date
from .trade_quality import canonical_external_source_evidence, canonical_quality_issues


class ReportSemanticError(ValueError):
    """A structurally valid report violates the risk evidence contract."""


def _snapshot_id_timestamp(value: str, *, label: str) -> datetime:
    try:
        return parse_snapshot_identity(value).timestamp
    except InvalidSnapshotIdentity as exc:
        raise ReportSemanticError(f"{label} snapshot identity is invalid") from exc


_TRADE_ARRAY_LIMITS = {
    "normalized_orders": 20_000,
    "normalized_fills": 20_000,
    "order_groups": 20_000,
    "round_trips": 20_000,
    "open_lots": 20_000,
    "policy_checks": 6,
    "behavior_findings": 10_007,
    "unverified_interpretations": 16,
    "external_source_evidence": 20,
    "quality_issues": 20,
    "data_quality_notes": 20,
    "coverage.requested_contexts": 20,
    "coverage.successful_contexts": 20,
    "coverage.failed_contexts": 20,
    "coverage.omitted_contexts": 20,
}
_TRADE_PRIMARY_ROW_ARRAYS = {
    "normalized_orders",
    "normalized_fills",
    "order_groups",
    "round_trips",
    "open_lots",
}
_TRADE_MAP_LIMITS = {
    "": 27,
    "coverage": 22,
    "metrics": 30,
    "metrics.notional_by_currency": 64,
    "metrics.realized_pl_by_currency": 64,
    "metrics.pnl_statistics_by_currency": 64,
    "metrics.per_symbol_turnover": 20_000,
    "policy": 7,
    "policy.market_open_no_trade_minutes_by_market": 8,
    "provenance": 9,
    "provenance.audited_source_digests": 4,
}
_TRADE_ITEM_MAP_LIMITS = {
    "normalized_orders[]": 12,
    "normalized_fills[]": 13,
    "order_groups[]": 11,
    "round_trips[]": 11,
    "open_lots[]": 6,
    "coverage.requested_contexts[]": 2,
    "coverage.successful_contexts[]": 2,
    "coverage.failed_contexts[]": 3,
    "coverage.omitted_contexts[]": 2,
    "policy_checks[]": 4,
    "behavior_findings[]": 8,
    "quality_issues[]": 2,
    "external_source_evidence[]": 3,
}
_TRADE_MAX_TOTAL_NODES = 1_500_000
_TRADE_MAX_TOTAL_ARRAY_ITEMS = 450_000
_TRADE_MAX_SINGLE_ARRAY_ITEMS = 120_000
_TRADE_MAX_TOTAL_MAP_ENTRIES = 1_500_000
_TRADE_MAX_TOTAL_STRING_BYTES = 64 * 1024 * 1024
_TRADE_MAX_NORMALIZED_BYTES = 80 * 1024 * 1024
_TRADE_MAX_STRING_BYTES = 1024 * 1024
_TRADE_MAX_DEPTH = 64


def _trade_map_business_limit(path: str) -> int | None:
    if path.startswith("metrics.notional_by_currency."):
        return 2
    if path.startswith("metrics.pnl_statistics_by_currency."):
        return 10
    return _TRADE_MAP_LIMITS.get(path, _TRADE_ITEM_MAP_LIMITS.get(path))


def _normalized_json_string_bytes(value: str, remaining: int) -> tuple[int, int]:
    """Count UTF-8 content and ensure-ascii JSON bytes without materializing either."""

    content_bytes = 0
    normalized_bytes = 2  # quotes
    for character in value:
        codepoint = ord(character)
        if codepoint <= 0x7F:
            content_bytes += 1
            if character in {'"', "\\"} or character in "\b\f\n\r\t":
                normalized_bytes += 2
            else:
                normalized_bytes += 6 if codepoint < 0x20 else 1
        elif codepoint <= 0x7FF:
            content_bytes += 2
            normalized_bytes += 6
        elif codepoint <= 0xFFFF:
            content_bytes += 3
            normalized_bytes += 6
        else:
            content_bytes += 4
            normalized_bytes += 12
        if content_bytes > _TRADE_MAX_STRING_BYTES or normalized_bytes > remaining:
            raise ReportSemanticError("VALIDATION_DOCUMENT_SIZE_EXCEEDED")
    return content_bytes, normalized_bytes


def _preflight_trade_review_shape(payload: Any) -> None:
    """Bound the complete untrusted JSON shape before invoking JSON Schema."""

    if isinstance(payload, dict):
        orders = payload.get("normalized_orders")
        fills = payload.get("normalized_fills")
        groups = payload.get("order_groups")
        trips = payload.get("round_trips")
        open_lots = payload.get("open_lots")
        findings = payload.get("behavior_findings")
        if all(isinstance(rows, list) for rows in (orders, fills, groups, trips, open_lots)):
            assert isinstance(orders, list)
            assert isinstance(fills, list)
            assert isinstance(groups, list)
            assert isinstance(trips, list)
            assert isinstance(open_lots, list)
            if len(orders) > 20_000 or len(fills) > 20_000 or len(orders) + len(fills) > 20_000:
                raise ReportSemanticError("VALIDATION_ROW_LIMIT_EXCEEDED")
            if len(groups) > len(fills) or len(trips) > len(fills) or len(open_lots) > len(fills):
                raise ReportSemanticError("VALIDATION_RELATIONSHIP_LIMIT_EXCEEDED")
            if isinstance(findings, list):
                if len(findings) > len(groups) // 2 + 7:
                    raise ReportSemanticError("VALIDATION_RELATIONSHIP_LIMIT_EXCEEDED")
                event_references = 0
                symbol_references = 0
                for finding in findings:
                    if not isinstance(finding, dict):
                        continue
                    events = finding.get("affected_events")
                    symbols = finding.get("affected_symbols")
                    if isinstance(events, list) and isinstance(symbols, list):
                        if len(events) > len(groups) or len(symbols) > len(events):
                            raise ReportSemanticError("VALIDATION_RELATIONSHIP_LIMIT_EXCEEDED")
                        event_references += len(events)
                        symbol_references += len(symbols)
                if event_references > 8 * len(groups) or symbol_references > 8 * len(groups):
                    raise ReportSemanticError("VALIDATION_RELATIONSHIP_LIMIT_EXCEEDED")

    nodes = 0
    array_items = 0
    map_entries = 0
    string_bytes = 0
    normalized_bytes = 0
    stack: list[tuple[Any, str, int]] = [(payload, "", 0)]
    while stack:
        value, path, depth = stack.pop()
        nodes += 1
        if nodes > _TRADE_MAX_TOTAL_NODES or depth > _TRADE_MAX_DEPTH:
            raise ReportSemanticError("VALIDATION_SHAPE_LIMIT_EXCEEDED")
        if isinstance(value, dict):
            entries = len(value)
            business_limit = _trade_map_business_limit(path)
            if business_limit is not None and entries > business_limit:
                raise ReportSemanticError(f"VALIDATION_SHAPE_LIMIT_EXCEEDED: {path or '$'}")
            map_entries += entries
            normalized_bytes += 2 + max(entries - 1, 0) + entries
            if map_entries > _TRADE_MAX_TOTAL_MAP_ENTRIES:
                raise ReportSemanticError("VALIDATION_SHAPE_LIMIT_EXCEEDED")
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ReportSemanticError("VALIDATION_SHAPE_TYPE_INVALID")
                key_content, key_normalized = _normalized_json_string_bytes(
                    key, _TRADE_MAX_NORMALIZED_BYTES - normalized_bytes
                )
                string_bytes += key_content
                normalized_bytes += key_normalized
                if string_bytes > _TRADE_MAX_TOTAL_STRING_BYTES:
                    raise ReportSemanticError("VALIDATION_DOCUMENT_SIZE_EXCEEDED")
                child_path = f"{path}.{key}" if path else str(key)
                stack.append((child, child_path, depth + 1))
        elif isinstance(value, (list, tuple)):
            items = len(value)
            business_limit = _TRADE_ARRAY_LIMITS.get(path)
            if business_limit is None and path.endswith((".affected_events", ".affected_symbols")):
                business_limit = 20_000
            if business_limit is None and items > _TRADE_MAX_SINGLE_ARRAY_ITEMS:
                raise ReportSemanticError(f"VALIDATION_SHAPE_LIMIT_EXCEEDED: {path}")
            if business_limit is not None and items > business_limit:
                code = (
                    "VALIDATION_ROW_LIMIT_EXCEEDED"
                    if path in _TRADE_PRIMARY_ROW_ARRAYS
                    else "VALIDATION_SHAPE_LIMIT_EXCEEDED"
                )
                raise ReportSemanticError(f"{code}: {path}")
            array_items += items
            normalized_bytes += 2 + max(items - 1, 0)
            if array_items > _TRADE_MAX_TOTAL_ARRAY_ITEMS:
                raise ReportSemanticError("VALIDATION_SHAPE_LIMIT_EXCEEDED")
            for child in value:
                stack.append((child, f"{path}[]", depth + 1))
        elif isinstance(value, str):
            content, normalized = _normalized_json_string_bytes(
                value, _TRADE_MAX_NORMALIZED_BYTES - normalized_bytes
            )
            string_bytes += content
            normalized_bytes += normalized
        elif value is None:
            normalized_bytes += 4
        elif isinstance(value, bool):
            normalized_bytes += 4 if value else 5
        elif isinstance(value, int):
            normalized_bytes += max(1, (value.bit_length() * 30_103) // 100_000 + 1) + (value < 0)
        else:
            normalized_bytes += 32
        if string_bytes > _TRADE_MAX_TOTAL_STRING_BYTES:
            raise ReportSemanticError("VALIDATION_DOCUMENT_SIZE_EXCEEDED")
        if normalized_bytes > _TRADE_MAX_NORMALIZED_BYTES:
            raise ReportSemanticError("VALIDATION_DOCUMENT_SIZE_EXCEEDED")


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
    rebalance_request_schema = json.loads(
        _schema_path("rebalance_request.schema.json").read_text(encoding="utf-8")
    )
    portfolio_snapshot_schema = json.loads(
        _schema_path("portfolio_snapshot.schema.json").read_text(encoding="utf-8")
    )
    lifecycle_schema = json.loads(
        _schema_path("position_lifecycle.schema.json").read_text(encoding="utf-8")
    )
    trade_schema = json.loads(_schema_path("trade_review.schema.json").read_text(encoding="utf-8"))
    option_risk_schema = json.loads(
        _schema_path("option_risk.schema.json").read_text(encoding="utf-8")
    )
    registry = Registry().with_resources(
        [
            (definitions["$id"], Resource.from_contents(definitions)),
            (risk_schema["$id"], Resource.from_contents(risk_schema)),
            (
                rebalance_request_schema["$id"],
                Resource.from_contents(rebalance_request_schema),
            ),
            (
                portfolio_snapshot_schema["$id"],
                Resource.from_contents(portfolio_snapshot_schema),
            ),
            (lifecycle_schema["$id"], Resource.from_contents(lifecycle_schema)),
            (trade_schema["$id"], Resource.from_contents(trade_schema)),
            (option_risk_schema["$id"], Resource.from_contents(option_risk_schema)),
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


def _validate_correlation_semantics(correlation: dict[str, Any], snapshot_as_of: str) -> None:
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
    cutoffs = correlation["date_cutoffs"]
    if len(cutoffs) != requested or not identities.issubset(cutoffs):
        _semantic_error("date cutoffs must cover every requested canonical identity")
    try:
        instant = datetime.fromisoformat(snapshot_as_of.replace("Z", "+00:00"))
        expected_cutoffs = {
            identity: _latest_complete_market_close_date(
                instant, identity.split(".", 1)[0]
            ).isoformat()
            for identity in cutoffs
        }
    except (KeyError, ValueError) as exc:
        raise ReportSemanticError(
            "correlation semantic validation failed: date cutoff input is invalid"
        ) from exc
    if cutoffs != expected_cutoffs:
        _semantic_error("date cutoff must equal the latest completed market session boundary")
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
    encoded_source_timestamp = _snapshot_id_timestamp(
        payload["source_snapshot_id"], label="risk source"
    )
    report_timestamp = datetime.fromisoformat(payload["as_of"].replace("Z", "+00:00"))
    if encoded_source_timestamp != utc_floor_second(report_timestamp):
        raise ReportSemanticError("risk source snapshot identity timestamp is inconsistent")
    correlation = payload["correlation"]
    _validate_correlation_semantics(correlation, payload["as_of"])
    coverage = payload["coverage"]
    if coverage["positions"] + coverage["omitted_positions"] != coverage["total_positions"]:
        _semantic_error("position budget coverage does not reconcile")
    if coverage["positions"] != coverage["total_positions"] or coverage["omitted_positions"] != 0:
        raise ReportSemanticError(
            "risk semantic validation failed: structural population cannot be budget-omitted"
        )
    expected_budget = "BUDGET_LIMIT" if coverage["budget_reason"] else "COMPLETE"
    if coverage["budget_status"] != expected_budget:
        _semantic_error("budget status and reason are inconsistent")
    if payload["coverage"]["price_histories"] != correlation["coverage"]:
        _semantic_error("report price-history coverage must match correlation coverage")
    if correlation["coverage"]["requested"] > payload["coverage"]["positions"]:
        _semantic_error("requested histories cannot exceed economic-position coverage")
    pnl_status = coverage["pnl_evidence_status"]
    if pnl_status == "PARTIAL" and payload["data_quality_status"] == "COMPLETE":
        raise ReportSemanticError(
            "P&L semantic validation failed: partial evidence requires partial top-level quality"
        )
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
    policy_candidate_types = {
        candidate_type
        for check_name, candidate_type in {
            "single_position": "POSITION_CONCENTRATION",
            "top_3": "POSITION_CONCENTRATION",
            "top_5": "POSITION_CONCENTRATION",
            "sector": "SECTOR_CONCENTRATION",
            "region": "REGION_CONCENTRATION",
            "theme_split": "THEME_CONCENTRATION",
        }.items()
        if check_name in checks
    }
    required_candidate_types = set(policy_candidate_types)
    if coverage["positions"]:
        required_candidate_types.add("REGION_CONCENTRATION")
    missing_candidates = required_candidate_types - finding_types
    candidate_priority = {
        "POSITION_CONCENTRATION": 1,
        "REGION_CONCENTRATION": 2,
        "SECTOR_CONCENTRATION": 2,
        "THEME_CONCENTRATION": 3,
    }
    candidate_checks = {
        "POSITION_CONCENTRATION": ("single_position", "top_3", "top_5"),
        "REGION_CONCENTRATION": ("region",),
        "SECTOR_CONCENTRATION": ("sector",),
        "THEME_CONCENTRATION": ("theme_split",),
    }
    if missing_candidates:
        worst_reported = (
            severity_order[findings[-1]["severity"]],
            priority_order[findings[-1]["review_priority"]],
            findings[-1]["type"],
        )

        def omitted_rank(candidate_type: str) -> tuple[int, int, str]:
            relevant = tuple(name for name in candidate_checks[candidate_type] if name in checks)
            severity = (
                "HIGH"
                if any(checks[name]["breached"] for name in relevant)
                else "LOW"
                if relevant
                else "UNRATED"
            )
            return severity_order[severity], candidate_priority[candidate_type], candidate_type

        displaced = {item for item in missing_candidates if omitted_rank(item) < worst_reported}
        if displaced:
            subject = (
                "region candidate" if "REGION_CONCENTRATION" in displaced else "policy candidate"
            )
            raise ReportSemanticError(f"{subject} was omitted in favor of a lower-ranked finding")


def validate_risk_document(payload: dict[str, Any]) -> None:
    _validate(payload, "risk_report.schema.json")
    _validate_report_semantics(payload)


_OPTION_VALIDATION_MAX_NODES = 1_000_000
_OPTION_VALIDATION_MAX_BYTES = 64 * 1024 * 1024
_OPTION_VALIDATION_MAX_OPERATIONS = 2_000_000
_OPTION_VALIDATION_WALL_SECONDS = 10.0


def _preflight_option_risk_shape(payload: object) -> None:
    started = time.monotonic()
    nodes = 0
    string_bytes = 0
    operations = 0
    stack: list[tuple[object, int]] = [(payload, 0)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        operations += 1
        if (
            nodes > _OPTION_VALIDATION_MAX_NODES
            or operations > _OPTION_VALIDATION_MAX_OPERATIONS
            or depth > 64
        ):
            raise ReportSemanticError("option risk validation shape budget exceeded")
        if operations % 1024 == 0 and time.monotonic() - started > _OPTION_VALIDATION_WALL_SECONDS:
            raise ReportSemanticError("option risk validation wall budget exceeded")
        if isinstance(value, dict):
            if len(value) > 64:
                raise ReportSemanticError("option risk validation map budget exceeded")
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ReportSemanticError("option risk validation requires string keys")
                string_bytes += len(key.encode("utf-8"))
                stack.append((item, depth + 1))
        elif isinstance(value, list):
            if len(value) > 10_000:
                raise ReportSemanticError("option risk validation array budget exceeded")
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str):
            size = len(value.encode("utf-8"))
            if size > 1024 * 1024:
                raise ReportSemanticError("option risk validation string budget exceeded")
            string_bytes += size
        if string_bytes > _OPTION_VALIDATION_MAX_BYTES:
            raise ReportSemanticError("option risk validation document budget exceeded")


def _option_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ReportSemanticError(f"option risk {label} is invalid") from None
    if parsed.tzinfo is None:
        raise ReportSemanticError(f"option risk {label} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _replay_option_risk(payload: dict[str, Any]) -> dict[str, Any]:
    from .futu_adapter import FutuReadError
    from .option_quote import OptionQuoteError, OptionQuoteProviderError
    from .option_risk import (
        MAX_SINGLE_RESPONSE_BYTES,
        MAX_TOTAL_RESPONSE_BYTES,
        PER_CALL_TIMEOUT_SECONDS,
        TOTAL_WALL_SECONDS,
        _bounded_response_bytes,
        analyze_option_risk,
    )
    from .snapshot_io import InvalidSnapshot, snapshot_from_document

    try:
        snapshot = snapshot_from_document(payload["provenance"]["source_snapshot"])
    except (InvalidSnapshot, ValueError, TypeError, KeyError) as exc:
        raise ReportSemanticError("embedded option source snapshot is invalid") from exc
    canonical_snapshot = snapshot.to_dict()
    canonical_digest = hashlib.sha256(
        json.dumps(
            canonical_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    if (
        canonical_snapshot != payload["provenance"]["source_snapshot"]
        or snapshot.snapshot_id != payload["source_snapshot_id"]
        or canonical_snapshot["as_of"] != payload["as_of"]
        or snapshot.base_currency != payload["base_currency"]
        or canonical_digest != payload["source_snapshot_digest"]
    ):
        raise ReportSemanticError("option source snapshot binding is inconsistent")

    evidence = payload["quote_evidence"]
    analysis_now = _option_datetime(payload["analysis_now"], "analysis_now")
    if snapshot.as_of > analysis_now:
        raise ReportSemanticError("option source snapshot follows analysis_now")
    clock_values: list[datetime] = []
    previous_finished = analysis_now
    response_bytes = 0
    for item in evidence:
        started = _option_datetime(item["request_started_at"], "request_started_at")
        finished = _option_datetime(item["request_finished_at"], "request_finished_at")
        duration = (finished - started).total_seconds()
        if (
            started < analysis_now
            or started < previous_finished
            or finished < started
            or duration > PER_CALL_TIMEOUT_SECONDS
            or (finished - analysis_now).total_seconds() > TOTAL_WALL_SECONDS
        ):
            raise ReportSemanticError("option quote request window is inconsistent")
        previous_finished = finished
        status = item["status"]
        reason = item["reason_code"]
        source_response = item["source_response"]
        normalized_response = item["response"]
        byte_count = item["response_byte_count"]
        if source_response is not None:
            measured = len(_bounded_response_bytes(source_response))
            if measured != byte_count or measured > MAX_SINGLE_RESPONSE_BYTES:
                raise ReportSemanticError("option quote response byte evidence is inconsistent")
        elif reason == "QUOTE_RESPONSE_TOO_LARGE":
            if byte_count != MAX_SINGLE_RESPONSE_BYTES + 1:
                raise ReportSemanticError("oversized option quote byte evidence is inconsistent")
        elif byte_count != 0:
            raise ReportSemanticError("empty option quote response must have zero bytes")
        if status == "SUCCESS":
            if reason is not None or source_response is None or normalized_response is None:
                raise ReportSemanticError("successful option quote evidence is inconsistent")
            response_bytes += byte_count
            if response_bytes > MAX_TOTAL_RESPONSE_BYTES:
                raise ReportSemanticError("successful option quote bytes exceed total budget")
            clock_values.extend((started, finished))
        elif status == "OMITTED":
            if reason not in {"QUOTE_CIRCUIT_OPEN", "QUOTE_TOTAL_BUDGET_EXCEEDED"}:
                raise ReportSemanticError("omitted option quote reason is inconsistent")
            if source_response is not None or normalized_response is not None or byte_count:
                raise ReportSemanticError("omitted option quote cannot contain a response")
            if finished != started:
                raise ReportSemanticError("unattempted option quote window must be instantaneous")
            clock_values.append(started)
        elif status == "FAILURE":
            if reason is None or normalized_response is not None:
                raise ReportSemanticError("failed option quote evidence is inconsistent")
            if reason == "QUOTE_CAPABILITY_UNAVAILABLE":
                if source_response is not None:
                    raise ReportSemanticError(
                        "unavailable option capability evidence is inconsistent"
                    )
                if started != analysis_now or finished != analysis_now:
                    clock_values.extend((started, finished))
            else:
                if (
                    source_response is not None
                    and reason != "QUOTE_RESPONSE_CONTRACT_INVALID"
                    and reason != "QUOTE_TOTAL_BUDGET_EXCEEDED"
                ):
                    raise ReportSemanticError("option failure response evidence is inconsistent")
                if source_response is not None and reason == "QUOTE_RESPONSE_CONTRACT_INVALID":
                    response_bytes += byte_count
                    if response_bytes > MAX_TOTAL_RESPONSE_BYTES:
                        raise ReportSemanticError("invalid option response exceeds total budget")
                if source_response is not None and reason == "QUOTE_TOTAL_BUDGET_EXCEEDED":
                    if response_bytes + byte_count <= MAX_TOTAL_RESPONSE_BYTES:
                        raise ReportSemanticError("option total byte failure lacks budget evidence")
                clock_values.extend((started, finished))
        else:
            raise ReportSemanticError("option quote evidence status is invalid")

    if len({item["identity"] for item in evidence}) != len(evidence):
        raise ReportSemanticError("option quote identities must be unique")
    if [item["selection_rank"] for item in evidence] != list(range(1, len(evidence) + 1)):
        raise ReportSemanticError("option quote evidence order is inconsistent")
    iterator = iter(clock_values)

    def replay_clock() -> datetime:
        try:
            return next(iterator)
        except StopIteration:
            raise ReportSemanticError("option quote clock evidence is incomplete") from None

    class ReplayProvider:
        def __init__(self) -> None:
            self.items = iter(evidence)

        def quote(self, identity: str, *, timeout: float) -> object:
            if timeout <= 0 or timeout > PER_CALL_TIMEOUT_SECONDS:
                raise ReportSemanticError("option quote replay request is inconsistent")
            try:
                item = next(self.items)
            except StopIteration:
                raise ReportSemanticError("unexpected option quote replay request") from None
            if identity != item["identity"] or item["status"] == "OMITTED":
                raise ReportSemanticError("option quote replay order is inconsistent")
            reason = item["reason_code"]
            if item["status"] == "SUCCESS" or item["source_response"] is not None:
                return item["source_response"]
            if reason == "QUOTE_SOURCE_TIMEOUT":
                raise TimeoutError("recorded timeout")
            if reason == "QUOTE_SOURCE_READ_FAILED":
                raise FutuReadError("recorded read failure")
            if reason == "QUOTE_RESPONSE_TOO_LARGE":
                return {"data": [], "padding": "x" * MAX_SINGLE_RESPONSE_BYTES}
            if reason == "QUOTE_CAPABILITY_UNAVAILABLE":
                raise OptionQuoteProviderError(reason)
            if reason == "QUOTE_RESPONSE_CONTRACT_INVALID":
                raise OptionQuoteError("recorded response contract failure")
            raise RuntimeError("recorded source unavailable")

    reasons = {item["reason_code"] for item in evidence}
    provider: Any
    capability_not_accessed = bool(evidence) and all(
        item["reason_code"] == "QUOTE_CAPABILITY_UNAVAILABLE"
        and item["request_started_at"] == payload["analysis_now"]
        and item["request_finished_at"] == payload["analysis_now"]
        for item in evidence
    )
    if capability_not_accessed:
        if reasons != {"QUOTE_CAPABILITY_UNAVAILABLE"}:
            raise ReportSemanticError("option source unavailable evidence cannot be mixed")
        provider = None
    else:
        provider = ReplayProvider()
    expected = analyze_option_risk(
        snapshot,
        quote_provider=provider,
        analysis_now=analysis_now,
        clock=replay_clock,
    ).to_dict()
    try:
        next(iterator)
    except StopIteration:
        pass
    else:
        raise ReportSemanticError("option quote clock evidence contains unused values")
    return expected


def validate_option_risk_document(payload: dict[str, Any]) -> None:
    _preflight_option_risk_shape(payload)
    _validate(payload, "option_risk.schema.json")
    try:
        expected = _replay_option_risk(payload)
    except ReportSemanticError:
        raise
    except Exception as exc:
        raise ReportSemanticError(f"option risk semantic validation failed: {exc}") from exc
    if payload != expected:
        raise ReportSemanticError("option risk report does not match reconstructed evidence")


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
    lifecycle = payload["lifecycle"]
    if lifecycle.get("status") != "UNAVAILABLE_UNTIL_PHASE_4":
        validate_lifecycle_document(lifecycle)
        if (
            lifecycle["source_snapshot_id"] != payload["source_snapshot_id"]
            or lifecycle["as_of"] != payload["as_of"]
        ):
            raise ReportSemanticError("lifecycle and risk modules reference different snapshots")
    options = payload["options"]
    validate_option_risk_document(options)
    if (
        options["source_snapshot_id"] != payload["source_snapshot_id"]
        or options["as_of"] != payload["as_of"]
        or options["base_currency"] != payload["base_currency"]
    ):
        raise ReportSemanticError("options and risk modules reference different snapshots")


_LIFECYCLE_MAX_BYTES = 128 * 1024 * 1024
_LIFECYCLE_MAX_NODES = 3_000_000
_LIFECYCLE_MAX_PROPERTIES = 64
_LIFECYCLE_MAX_POSITIONS = 20_000
_LIFECYCLE_MAX_HISTORY = 1_000
_LIFECYCLE_MAX_OBSERVATIONS = 2_000_000


def _preflight_lifecycle_shape(payload: object, consume: Callable[[int], None]) -> None:
    stack: list[tuple[str, object]] = [("", payload)]
    containers: set[int] = set()
    observations = 0
    nodes = 0
    while stack:
        path, value = stack.pop()
        nodes += 1
        if nodes > _LIFECYCLE_MAX_NODES:
            raise ReportSemanticError("lifecycle report node budget exceeded")
        consume()
        if isinstance(value, dict):
            identity = id(value)
            if identity in containers:
                raise ReportSemanticError("lifecycle report contains a cyclic container")
            containers.add(identity)
            if len(value) > _LIFECYCLE_MAX_PROPERTIES:
                raise ReportSemanticError("lifecycle report object property budget exceeded")
            stack.extend(
                (f"{path}.{key}" if path else str(key), item) for key, item in value.items()
            )
        elif isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in containers:
                raise ReportSemanticError("lifecycle report contains a cyclic container")
            containers.add(identity)
            leaf = path.rsplit(".", 1)[-1]
            maximum = {
                "positions": _LIFECYCLE_MAX_POSITIONS,
                "coverage.history.snapshots": _LIFECYCLE_MAX_HISTORY,
                "data_quality_reasons": 400_000,
                "data_quality_notes": 400_000,
            }.get(path, _LIFECYCLE_MAX_OBSERVATIONS)
            if leaf in {"invalidation_conditions", "review_triggers", "notes"}:
                maximum = 32
            elif leaf == "findings":
                maximum = 80_000 if path == "findings" else 8
            elif leaf == "data_quality_notes" and path != "data_quality_notes":
                maximum = 16
            if path.endswith("history_evidence"):
                observations += len(value)
                maximum = _LIFECYCLE_MAX_HISTORY
            if len(value) > maximum or observations > _LIFECYCLE_MAX_OBSERVATIONS:
                raise ReportSemanticError("lifecycle report array or observation budget exceeded")
            stack.extend((path, item) for item in value)


def validate_lifecycle_document(
    payload: dict[str, Any],
    *,
    max_validation_operations: int = 3_000_000,
    max_validation_seconds: float = 10.0,
    validation_clock: Callable[[], float] = time.monotonic,
) -> None:
    """Validate shape, complete identity, coverage, ordering, and flattened findings."""

    if max_validation_operations <= 0 or max_validation_seconds <= 0:
        raise ReportSemanticError("lifecycle validation budget is invalid")
    started = validation_clock()
    remaining = max_validation_operations

    def consume(amount: int = 1) -> None:
        nonlocal remaining
        remaining -= amount
        if remaining < 0 or validation_clock() - started > max_validation_seconds:
            raise ReportSemanticError("lifecycle validation operation or wall-time budget exceeded")

    _preflight_lifecycle_shape(payload, consume)
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReportSemanticError("lifecycle report is not bounded JSON") from exc
    if len(encoded) > _LIFECYCLE_MAX_BYTES:
        raise ReportSemanticError("lifecycle report byte budget exceeded")
    consume()
    _validate(payload, "position_lifecycle.schema.json")
    consume()
    body = dict(payload)
    claimed = body.pop("lifecycle_report_id")
    expected = (
        "lr-"
        + hashlib.sha256(
            json.dumps(
                body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        ).hexdigest()[:20]
    )
    if claimed != expected:
        raise ReportSemanticError("lifecycle report complete-body identity is invalid")
    encoded_source_timestamp = _snapshot_id_timestamp(
        payload["source_snapshot_id"], label="lifecycle source"
    )
    report_timestamp = datetime.fromisoformat(payload["as_of"].replace("Z", "+00:00"))
    if utc_floor_second(report_timestamp) != encoded_source_timestamp:
        raise ReportSemanticError("lifecycle source snapshot identity timestamp is inconsistent")
    positions = payload["positions"]
    if payload["status"] == "UNAVAILABLE":
        reasons = payload["data_quality_reasons"]
        if (
            positions
            or payload["findings"]
            or payload["data_quality_status"] != "UNAVAILABLE"
            or len(reasons) != 1
            or reasons[0]["code"] != "MODULE_UNAVAILABLE"
            or reasons[0]["scope"] != "LIFECYCLE"
            or payload["data_quality_notes"]
            != [f"{reasons[0]['code']} [{reasons[0]['scope']}]: {reasons[0]['detail']}"]
        ):
            raise ReportSemanticError(
                "unavailable lifecycle report must contain canonical quality evidence"
            )
    if [item["symbol"] for item in positions] != sorted(item["symbol"] for item in positions):
        raise ReportSemanticError("lifecycle positions must use stable symbol ordering")
    coverage = payload["coverage"]
    if coverage["assessed_positions"] != len(positions):
        raise ReportSemanticError("lifecycle assessment coverage is inconsistent")
    if coverage["unconfigured_positions"] != sum(
        item["lifecycle_status"] == "UNCONFIGURED" for item in positions
    ):
        raise ReportSemanticError("lifecycle unconfigured coverage is inconsistent")
    history_coverage = coverage["history"]
    history_catalog = history_coverage["snapshots"]
    catalog_keys = [(entry["as_of"], entry["snapshot_id"]) for entry in history_catalog]
    if (
        len(history_catalog) != history_coverage["accepted_snapshots"]
        or catalog_keys != sorted(catalog_keys)
        or len({entry["snapshot_id"] for entry in history_catalog}) != len(history_catalog)
    ):
        raise ReportSemanticError("lifecycle accepted history catalog is inconsistent")
    for entry in history_catalog:
        consume()
        catalog_timestamp = datetime.fromisoformat(entry["as_of"].replace("Z", "+00:00"))
        if (
            _snapshot_id_timestamp(entry["snapshot_id"], label="lifecycle accepted history")
            != utc_floor_second(catalog_timestamp)
            or catalog_timestamp > report_timestamp
        ):
            raise ReportSemanticError("lifecycle accepted history catalog identity is inconsistent")
    observed_history_ids = {
        entry["snapshot_id"] for item in positions for entry in item["history_evidence"]
    }
    if (
        coverage["history_snapshots"] != history_coverage["accepted_snapshots"]
        or history_coverage["candidate_snapshots"]
        != history_coverage["accepted_snapshots"]
        + history_coverage["duplicate_snapshots"]
        + history_coverage["future_snapshots"]
        or history_coverage["position_observations"]
        != sum(len(item["history_evidence"]) for item in positions)
        or len(observed_history_ids) > history_coverage["accepted_snapshots"]
        or history_coverage["pre_open_observations"]
        != sum(item["history_excluded_pre_open"] for item in positions)
    ):
        raise ReportSemanticError("lifecycle history coverage is inconsistent")
    if (history_coverage["accepted_snapshots"] == 0) != (
        history_coverage["window_start"] is None and history_coverage["window_end"] is None
    ):
        raise ReportSemanticError("lifecycle history window availability is inconsistent")
    if history_coverage["window_start"] is not None:
        window_start = datetime.fromisoformat(
            history_coverage["window_start"].replace("Z", "+00:00")
        )
        window_end = datetime.fromisoformat(history_coverage["window_end"].replace("Z", "+00:00"))
        if window_start > window_end or window_end > datetime.fromisoformat(
            payload["as_of"].replace("Z", "+00:00")
        ):
            raise ReportSemanticError("lifecycle history window is inconsistent")
        if any(
            not window_start
            <= datetime.fromisoformat(entry["as_of"].replace("Z", "+00:00"))
            <= window_end
            for item in positions
            for entry in item["history_evidence"]
        ):
            raise ReportSemanticError("lifecycle history evidence falls outside coverage window")
        if (
            history_coverage["window_start"] != history_catalog[0]["as_of"]
            or history_coverage["window_end"] != history_catalog[-1]["as_of"]
        ):
            raise ReportSemanticError("lifecycle history window does not match its catalog")
    expected_history_coverage_status = (
        "PARTIAL"
        if history_coverage["issue"] is not None
        or history_coverage["future_snapshots"]
        or history_coverage["duplicate_snapshots"]
        or history_coverage["pre_open_observations"]
        or any(item["history_evidence_status"] == "PARTIAL" for item in positions)
        else "AVAILABLE"
        if history_coverage["accepted_snapshots"]
        else "UNAVAILABLE"
    )
    if history_coverage["status"] != expected_history_coverage_status:
        raise ReportSemanticError("lifecycle history quality status is inconsistent")
    if payload["status"] != "UNAVAILABLE":
        expected_reasons: list[dict[str, str]] = []
        if history_coverage["issue"] is not None:
            expected_reasons.append(
                {
                    "code": history_coverage["issue"]["code"],
                    "scope": "HISTORY",
                    "detail": history_coverage["issue"]["detail"],
                }
            )
        elif history_coverage["status"] != "AVAILABLE":
            expected_reasons.append(
                {
                    "code": f"HISTORY_{history_coverage['status']}",
                    "scope": "HISTORY",
                    "detail": "eligible lifecycle history is not fully available",
                }
            )
        for field, code, detail in (
            ("future_snapshots", "FUTURE_HISTORY_EXCLUDED", "future snapshots were excluded"),
            (
                "duplicate_snapshots",
                "DUPLICATE_HISTORY_EXCLUDED",
                "duplicate snapshots were excluded",
            ),
            (
                "pre_open_observations",
                "PRE_OPEN_HISTORY_EXCLUDED",
                "pre-open observations were excluded",
            ),
        ):
            if history_coverage[field]:
                expected_reasons.append(
                    {
                        "code": code,
                        "scope": "HISTORY",
                        "detail": f"{history_coverage[field]} {detail}",
                    }
                )
        for item in positions:
            expected_reasons.extend(
                {
                    "code": "POSITION_DATA_NOTE",
                    "scope": item["symbol"],
                    "detail": note,
                }
                for note in item["data_quality_notes"]
            )
        expected_reasons.sort(
            key=lambda reason: (reason["code"], reason["scope"], reason["detail"])
        )
        expected_notes = [
            f"{reason['code']} [{reason['scope']}]: {reason['detail']}"
            for reason in expected_reasons
        ]
        expected_quality_status = "PARTIAL" if expected_reasons else "COMPLETE"
        if (
            payload["data_quality_reasons"] != expected_reasons
            or payload["data_quality_notes"] != expected_notes
            or payload["data_quality_status"] != expected_quality_status
            or payload["status"] != expected_quality_status
        ):
            raise ReportSemanticError("lifecycle report quality evidence is inconsistent")
    flattened = [finding for item in positions for finding in item["findings"]]
    ordered = sorted(
        flattened, key=lambda item: (item["review_priority"], item["type"], item["symbol"])
    )
    if payload["findings"] != ordered:
        raise ReportSemanticError("lifecycle findings are incomplete or non-deterministic")
    if payload["status"] != "UNAVAILABLE" and (
        coverage["current_positions"]
        != sum(item["current_weight"] is not None for item in positions)
        or coverage["configured_records"]
        != sum(item["record_status"] is not None for item in positions)
    ):
        raise ReportSemanticError("lifecycle source coverage is inconsistent")
    precedence = (
        "BROKEN",
        "ROLE_DRIFT",
        "WEIGHT_DRIFT",
        "HORIZON_DRIFT",
        "WEAKENING",
        "STRENGTHENING",
    )
    report_date = datetime.fromisoformat(payload["as_of"].replace("Z", "+00:00")).date()
    for item in positions:
        consume(len(item["history_evidence"]) + len(item["findings"]) + 1)
        types = {finding["type"] for finding in item["findings"]}
        for finding in item["findings"]:
            expected_severity = {
                "P0": "HIGH",
                "P1": "MEDIUM",
                "P2": "LOW",
                "P3": "INFORMATIONAL",
            }[finding["review_priority"]]
            if finding["severity"] != expected_severity:
                raise ReportSemanticError("lifecycle finding severity is inconsistent")
        if any(finding["symbol"] != item["symbol"] for finding in item["findings"]):
            raise ReportSemanticError("lifecycle finding symbol is inconsistent")
        history = item["history_evidence"]
        history_keys = [(entry["as_of"], entry["snapshot_id"]) for entry in history]
        if history_keys != sorted(history_keys) or len(
            {entry["snapshot_id"] for entry in history}
        ) != len(history):
            raise ReportSemanticError("lifecycle history evidence is not stable and unique")
        if any(
            entry["identity"] != item["symbol"]
            or datetime.fromisoformat(entry["as_of"].replace("Z", "+00:00"))
            > datetime.fromisoformat(payload["as_of"].replace("Z", "+00:00"))
            for entry in history
        ):
            raise ReportSemanticError("lifecycle history identity or time bound is invalid")
        if any(
            (entry["present"] and entry["weight"] is None)
            or (not entry["present"] and entry["weight"] is not None)
            or (
                entry["weight"] is not None
                and not Decimal(0) <= Decimal(entry["weight"]) <= Decimal(1)
            )
            for entry in history
        ):
            raise ReportSemanticError("lifecycle history presence or weight is inconsistent")
        for entry in history:
            timestamp = datetime.fromisoformat(entry["as_of"].replace("Z", "+00:00"))
            encoded = _snapshot_id_timestamp(entry["snapshot_id"], label="lifecycle history")
            if utc_floor_second(timestamp) != encoded:
                raise ReportSemanticError("lifecycle history identity timestamp is inconsistent")
        if item["history_excluded_pre_open"] and not any(
            "pre-open" in note for note in item["data_quality_notes"]
        ):
            raise ReportSemanticError("excluded pre-open history lacks a quality note")
        opened_for_history = (
            None if item["opened_at"] is None else date.fromisoformat(item["opened_at"])
        )
        eligible_catalog = [
            entry
            for entry in history_catalog
            if opened_for_history is None
            or datetime.fromisoformat(entry["as_of"].replace("Z", "+00:00")).date()
            >= opened_for_history
        ]
        if [entry["snapshot_id"] for entry in history] != [
            entry["snapshot_id"] for entry in eligible_catalog
        ] or item["history_excluded_pre_open"] != len(history_catalog) - len(eligible_catalog):
            raise ReportSemanticError("lifecycle history observations are incomplete")
        if item["record_status"] is None:
            expected_status = "UNCONFIGURED"
            if (
                item["target_weight"] is not None
                or item["original_thesis"] is not None
                or item["original_thesis_evidence"] is not None
                or item["opened_at"] is not None
                or item["holding_horizon"] is not None
                or item["last_reviewed_at"] is not None
                or item["invalidation_conditions"]
                or item["review_triggers"]
                or item["notes"]
                or types != {"UNCONFIGURED"}
                or item["thesis_status"] != "UNREVIEWED"
            ):
                raise ReportSemanticError("unconfigured lifecycle assessment fabricated user facts")
            only = item["findings"][0]
            if (
                only["summary"] != "Current position has no user lifecycle record"
                or only["evidence"] != {"current_weight": item["current_weight"]}
                or only["review_priority"] != "P2"
            ):
                raise ReportSemanticError("unconfigured lifecycle evidence is inconsistent")
        elif item["current_weight"] is None:
            expected_status = "EXITED"
            if types != {"EXITED"} or item["thesis_status"] != "UNREVIEWED":
                raise ReportSemanticError("exited lifecycle assessment is inconsistent")
            if any(
                item[field] is None
                for field in (
                    "target_weight",
                    "original_thesis",
                    "original_thesis_evidence",
                    "opened_at",
                    "holding_horizon",
                    "last_reviewed_at",
                )
            ):
                raise ReportSemanticError("exited lifecycle assessment omitted user facts")
            exited_opened = date.fromisoformat(item["opened_at"])
            exited_reviewed = date.fromisoformat(item["last_reviewed_at"])
            if (
                exited_reviewed < exited_opened
                or exited_reviewed > report_date
                or exited_opened > report_date
                or any(
                    datetime.fromisoformat(entry["as_of"].replace("Z", "+00:00")).date()
                    < exited_opened
                    for entry in history
                )
            ):
                raise ReportSemanticError("exited lifecycle dates or history are inconsistent")
            only = item["findings"][0]
            if (
                only["summary"] != "Recorded position is absent from the current economic portfolio"
                or only["evidence"] != {"record_status": item["record_status"]}
                or only["review_priority"] != "P3"
            ):
                raise ReportSemanticError("exited lifecycle evidence is inconsistent")
        else:
            if types & {"UNCONFIGURED", "EXITED", "INTACT"}:
                raise ReportSemanticError("configured current lifecycle findings are invalid")
            expected_status = next((state for state in precedence if state in types), "INTACT")
            if any(
                item[field] is None
                for field in (
                    "target_weight",
                    "original_thesis",
                    "original_thesis_evidence",
                    "opened_at",
                    "holding_horizon",
                    "last_reviewed_at",
                )
            ):
                raise ReportSemanticError("configured lifecycle assessment omitted user facts")
        thesis_text = item["original_thesis"]
        thesis_evidence = item["original_thesis_evidence"]
        if thesis_text is not None:
            expected_thesis_evidence = {
                "configured": True,
                "provenance": "USER_CONFIG",
                "system_inferred": False,
                "sha256": hashlib.sha256(thesis_text.encode("utf-8")).hexdigest(),
                "characters": len(thesis_text),
            }
            if thesis_evidence != expected_thesis_evidence:
                raise ReportSemanticError("lifecycle original thesis evidence is inconsistent")
        elif thesis_evidence is not None:
            raise ReportSemanticError("lifecycle original thesis evidence lacks authoritative text")
        if item["lifecycle_status"] != expected_status:
            raise ReportSemanticError("lifecycle status does not match deterministic precedence")
        expected_priority = min(
            (finding["review_priority"] for finding in item["findings"]), default="P3"
        )
        if item["review_priority"] != expected_priority:
            raise ReportSemanticError("lifecycle review priority is inconsistent")
        target = item["target_weight"]
        current = item["current_weight"]
        if current is not None and not Decimal(0) <= Decimal(current) <= Decimal(1):
            raise ReportSemanticError("lifecycle current weight is outside the supported range")
        if target is not None and current is not None:
            drift = not Decimal(target["min"]) <= Decimal(current) <= Decimal(target["max"])
            if drift != ("WEIGHT_DRIFT" in types):
                raise ReportSemanticError("lifecycle weight-drift evidence is inconsistent")
            opened = datetime.fromisoformat(item["opened_at"]).date()
            reviewed = datetime.fromisoformat(item["last_reviewed_at"]).date()
            if reviewed < opened or reviewed > report_date or opened > report_date:
                raise ReportSemanticError("lifecycle record dates are inconsistent")
            if any(
                datetime.fromisoformat(entry["as_of"].replace("Z", "+00:00")).date() < opened
                for entry in history
            ):
                raise ReportSemanticError("lifecycle history includes pre-open evidence")
            horizon = re.fullmatch(
                r"(\d+)(?:\s*-\s*(\d+))?\s*(day|days|week|weeks|month|months|year|years)",
                item["holding_horizon"],
                flags=re.IGNORECASE,
            )
            if horizon is None:
                raise ReportSemanticError("lifecycle holding horizon is unsupported")
            maximum = int(horizon.group(2) or horizon.group(1))
            if not 1 <= maximum <= 1200:
                raise ReportSemanticError("lifecycle holding horizon is outside the bound")
            factor = {
                "day": 1,
                "days": 1,
                "week": 7,
                "weeks": 7,
                "month": 30,
                "months": 30,
                "year": 365,
                "years": 365,
            }[horizon.group(3).lower()]
            horizon_drift = (report_date - opened).days > maximum * factor
            if horizon_drift != ("HORIZON_DRIFT" in types):
                raise ReportSemanticError("lifecycle horizon-drift evidence is inconsistent")
            expected_by_type: dict[str, list[tuple[str, dict[str, Any], str]]] = {
                "WEIGHT_DRIFT": [
                    (
                        "Current weight is outside the user-authored target interval",
                        {
                            "current_weight": current,
                            "target_min": target["min"],
                            "target_max": target["max"],
                        },
                        "P1",
                    )
                ]
                if drift
                else [],
                "HORIZON_DRIFT": [
                    (
                        "Holding age exceeds the deterministic upper horizon",
                        {
                            "held_days": (report_date - opened).days,
                            "upper_horizon_days": maximum * factor,
                        },
                        "P1",
                    )
                ]
                if horizon_drift
                else [],
                "ROLE_DRIFT": [
                    (
                        "SATELLITE weight remained at or above the documented 10% core-level threshold",
                        {
                            "threshold": "0.1",
                            "minimum_snapshots": 3,
                            "minimum_span_days": 30,
                        },
                        "P1",
                    )
                ]
                if "ROLE_DRIFT" in types
                else [],
                "WEAKENING": [
                    (
                        "User review explicitly marked the thesis WEAKENING",
                        {"thesis_status": "WEAKENING"},
                        "P2",
                    )
                ]
                if item["thesis_status"] == "WEAKENING"
                else [],
                "STRENGTHENING": [
                    (
                        "User review explicitly marked the thesis STRENGTHENING",
                        {"thesis_status": "STRENGTHENING"},
                        "P2",
                    )
                ]
                if item["thesis_status"] == "STRENGTHENING"
                else [],
            }
            broken_expected: list[tuple[str, dict[str, Any], str]] = []
            if item["record_status"] == "EXITED":
                broken_expected.append(
                    (
                        "Position is present although its user record is EXITED",
                        {"record_status": "EXITED", "current_weight": current},
                        "P0",
                    )
                )
            if item["thesis_status"] == "BROKEN":
                broken_expected.append(
                    (
                        "User review explicitly marked the thesis BROKEN",
                        {"thesis_status": "BROKEN"},
                        "P0",
                    )
                )
            expected_by_type["BROKEN"] = broken_expected
            for finding_type, expected in expected_by_type.items():
                actual = [
                    (finding["summary"], finding["evidence"], finding["review_priority"])
                    for finding in item["findings"]
                    if finding["type"] == finding_type
                ]
                if actual != expected:
                    raise ReportSemanticError(
                        f"lifecycle {finding_type.lower()} evidence is inconsistent"
                    )
            role_check_required = item["role"] == "SATELLITE" and Decimal(current) >= Decimal(
                "0.10"
            )
            history_times = [
                datetime.fromisoformat(entry["as_of"].replace("Z", "+00:00")) for entry in history
            ]
            sufficient_history = bool(
                len(history) >= 3
                and (history_times[-1] - history_times[0]).days >= 30
                and all(entry["present"] for entry in history)
            )
            expected_history_status = (
                "AVAILABLE"
                if role_check_required and sufficient_history
                else "PARTIAL"
                if role_check_required
                else "NOT_REQUIRED"
            )
            if item["history_evidence_status"] != expected_history_status:
                raise ReportSemanticError("lifecycle history availability is inconsistent")
            expected_role_drift = bool(
                role_check_required
                and sufficient_history
                and all(
                    entry["weight"] is not None and Decimal(entry["weight"]) >= Decimal("0.10")
                    for entry in history
                )
            )
            if expected_role_drift != ("ROLE_DRIFT" in types):
                raise ReportSemanticError("lifecycle role drift is not supported by history")
            if expected_history_status == "PARTIAL" and not any(
                "role drift unavailable" in note for note in item["data_quality_notes"]
            ):
                raise ReportSemanticError("partial lifecycle history lacks a quality note")
            thesis = item["thesis_status"]
            for state in ("BROKEN", "WEAKENING", "STRENGTHENING"):
                if (thesis == state) != (state in types):
                    # BROKEN may also be caused solely by an EXITED/current conflict.
                    if not (
                        state == "BROKEN"
                        and thesis != "BROKEN"
                        and item["record_status"] == "EXITED"
                        and "BROKEN" in types
                    ):
                        raise ReportSemanticError("lifecycle thesis evidence is inconsistent")
            if (item["record_status"] == "EXITED") != (
                "BROKEN" in types
                and any(
                    finding["evidence"].get("record_status") == "EXITED"
                    for finding in item["findings"]
                )
            ):
                raise ReportSemanticError(
                    "lifecycle current/exited conflict evidence is inconsistent"
                )


def validate_lifecycle_record_document(payload: dict[str, Any]) -> None:
    _validate(payload, "lifecycle_record.schema.json")
    target = payload["target_weight"]
    if Decimal(target["min"]) > Decimal(target["max"]):
        raise ReportSemanticError("lifecycle target min cannot exceed max")
    opened = date.fromisoformat(payload["opened_at"])
    reviewed = date.fromisoformat(payload["last_reviewed_at"])
    if reviewed < opened:
        raise ReportSemanticError("lifecycle review date cannot precede opened date")


def validate_journal_document(payload: dict[str, Any]) -> None:
    _validate(payload, "journal_event.schema.json")
    _snapshot_id_timestamp(payload["source_snapshot_id"], label="journal source")
    if (payload["event_type"] == "USER_DECISION") != (payload["decision"] is not None):
        raise ReportSemanticError("journal decision semantics are inconsistent")
    if (payload["event_type"] == "PORTFOLIO_ANALYSIS") != (payload["artifact_path"] is not None):
        raise ReportSemanticError("journal artifact semantics are inconsistent")


def validate_performance_document(
    payload: dict[str, Any],
    *,
    source_snapshots: Sequence[Any] | None = None,
    max_validation_operations: int = 750_000,
    max_validation_seconds: float = 10.0,
    validation_clock: Callable[[], float] = time.monotonic,
) -> None:
    """Validate strict attribution shape and reconstruct every authority-bearing total."""

    started = validation_clock()
    remaining = max_validation_operations
    if remaining <= 0 or max_validation_seconds <= 0:
        raise ReportSemanticError("performance validation budgets must be positive")

    def consume(count: int = 1) -> None:
        nonlocal remaining
        remaining -= count
        if remaining < 0 or validation_clock() - started > max_validation_seconds:
            raise ReportSemanticError("performance validation budget exceeded")

    stack: list[tuple[Any, int]] = [(payload, 0)]
    nodes = strings = 0
    while stack:
        value, depth = stack.pop()
        consume()
        nodes += 1
        if nodes > 400_000 or depth > 32:
            raise ReportSemanticError("performance validation shape exceeded")
        if isinstance(value, dict):
            if len(value) > 4096:
                raise ReportSemanticError("performance validation map exceeded")
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ReportSemanticError("performance validation key is invalid")
                strings += len(key.encode("utf-8"))
                stack.append((child, depth + 1))
        elif isinstance(value, list):
            if len(value) > 20_000:
                raise ReportSemanticError("performance validation array exceeded")
            stack.extend((child, depth + 1) for child in value)
        elif isinstance(value, str):
            strings += len(value.encode("utf-8"))
        if strings > 32 * 1024 * 1024:
            raise ReportSemanticError("performance validation document exceeded")
    _validate(payload, "performance_attribution.schema.json")
    consume()

    verified_fx_changed: bool | None = None
    if source_snapshots is not None:
        from .performance import (
            TradeAttributionEvidence,
            _classification,
            _contributions,
            _group_positions,
            _merge_multi_snapshot_trade_contributions,
            _snapshot_digest,
        )
        from .snapshot_io import snapshot_from_document

        try:
            if len(source_snapshots) > 366:
                raise ValueError("source snapshot validation budget exceeded")
            validated_source_rows = []
            for item in source_snapshots:
                consume()
                for _position in item.positions:
                    consume()
                validated_source_rows.append(snapshot_from_document(item.to_dict()))
            validated_sources = tuple(
                sorted(validated_source_rows, key=lambda item: (item.as_of, item.snapshot_id))
            )
            if tuple(item.snapshot_id for item in validated_sources) != tuple(
                payload["source_snapshot_ids"]
            ):
                raise ValueError("source snapshot sequence mismatch")
            if {item.snapshot_id: _snapshot_digest(item) for item in validated_sources} != payload[
                "source_snapshot_digests"
            ]:
                raise ValueError("source snapshot digest mismatch")
            expected_source_quality = [
                {
                    "snapshot_id": item.snapshot_id,
                    "status": item.data_quality.status,
                    "warnings": list(item.data_quality.warnings),
                    "missing_fields": list(item.data_quality.missing_fields),
                    "source_errors": list(item.data_quality.source_errors),
                    "position_quality_notes": [
                        {
                            "account_ref": position.account_ref,
                            "identity": f"{position.market}.{position.symbol}",
                            "notes": list(position.data_quality_notes),
                        }
                        for position in sorted(
                            item.positions,
                            key=lambda position: (
                                position.account_ref,
                                position.market,
                                position.symbol,
                            ),
                        )
                        if position.data_quality_notes
                    ],
                }
                for item in validated_sources
            ]
            if payload["source_snapshot_quality"] != expected_source_quality:
                raise ValueError("source snapshot quality evidence mismatch")
            expected_valuations = [
                {
                    "snapshot_id": item.snapshot_id,
                    "as_of": _json_value(item.as_of),
                    "nav_base": _json_value(item.nav_base),
                }
                for item in validated_sources
            ]
            if expected_valuations != payload["period"]["valuations"]:
                raise ValueError("source valuation evidence mismatch")
            if (
                not validated_sources
                or payload["base_currency"] != validated_sources[0].base_currency
            ):
                raise ValueError("source base currency mismatch")
            if payload["coverage"]["source_position_rows"] != sum(
                len(item.positions) for item in validated_sources
            ):
                raise ValueError("source position coverage mismatch")
            trade_review = payload["trade_evidence"]["review"]
            if trade_review is not None:
                expected_trade_accounts = {
                    item.account_ref for item in validated_sources[-1].accounts
                }
                observed_trade_accounts = {
                    item["account_ref"]
                    for key in ("normalized_orders", "normalized_fills")
                    for item in trade_review[key]
                }
                if not observed_trade_accounts <= expected_trade_accounts:
                    raise ValueError("trade evidence account aliases do not match source snapshot")
            if len(validated_sources) > 1:
                expected_contributions, _unused_reasons = _contributions(
                    validated_sources, consume=consume
                )
                observed_trade = payload["trade_evidence"]
                if observed_trade["used_for_contributions"]:
                    expected_contributions, _unused_trade_reasons = (
                        _merge_multi_snapshot_trade_contributions(
                            expected_contributions,
                            TradeAttributionEvidence(
                                observed_trade["status"],
                                observed_trade["reason"],
                                observed_trade["review_digest"],
                                observed_trade["review"],
                                True,
                            ),
                        )
                    )
                if payload["symbol_contributions"] != [
                    _json_value(item) for item in expected_contributions
                ]:
                    raise ValueError("source snapshot trajectory contribution evidence mismatch")
            else:
                final_rows = _group_positions(validated_sources[-1])
                trade_review = payload["trade_evidence"]["review"]
                trade_identities = (
                    {item["identity"] for item in trade_review["round_trips"]}
                    if trade_review is not None
                    and payload["trade_evidence"]["used_for_contributions"]
                    else set()
                )
                trade_trips_by_identity: dict[str, list[dict[str, Any]]] = {}
                if trade_review is not None and payload["trade_evidence"]["used_for_contributions"]:
                    for trip in trade_review["round_trips"]:
                        trade_trips_by_identity.setdefault(trip["identity"], []).append(trip)
                if {item["identity"] for item in payload["symbol_contributions"]} != (
                    set(final_rows) | trade_identities
                ):
                    raise ValueError("reconstruction identity coverage mismatch")
                for contribution in payload["symbol_contributions"]:
                    rows = final_rows.get(contribution["identity"], ())
                    market, symbol = contribution["identity"].split(".", 1)
                    trips = trade_trips_by_identity.get(contribution["identity"], ())
                    expected_accounts = sorted(
                        {item.account_ref for item in rows}
                        | {item["account_ref"] for item in trips}
                    )
                    classification = _classification(rows) if rows else None
                    classification = classification or ("UNKNOWN", "UNKNOWN", ())
                    evidence = contribution["evidence"]
                    if (
                        contribution["market"] != market
                        or contribution["symbol"] != symbol
                        or contribution["account_refs"] != expected_accounts
                        or (
                            contribution["sector"],
                            contribution["region"],
                            tuple(contribution["themes"]),
                        )
                        != classification
                        or evidence["start_snapshot_id"] != validated_sources[-1].snapshot_id
                        or evidence["end_snapshot_id"] != validated_sources[-1].snapshot_id
                        or evidence["start_present"]
                        or evidence["end_present"] != bool(rows)
                        or evidence["start_rows"] != 0
                        or evidence["end_rows"] != len(rows)
                    ):
                        raise ValueError("reconstruction source position evidence mismatch")
                    cross_currency = bool(rows) and (
                        {item.currency for item in rows} != {validated_sources[-1].base_currency}
                        or {item.fx_to_base for item in rows} != {Decimal(1)}
                    )
                    observed_reason = contribution["evidence"].get("reconstruction_reason")
                    if cross_currency != (observed_reason == "TRANSACTION_PERIOD_FX_UNAVAILABLE"):
                        raise ValueError("reconstruction FX evidence mismatch")
                    if (
                        contribution["status"] == "ESTIMATED"
                        and "ending_quantity" in evidence
                        and Decimal(evidence["ending_quantity"])
                        != sum((item.quantity for item in rows), Decimal(0))
                    ):
                        raise ValueError("reconstruction ending quantity mismatch")
                    if "ending_quantity_by_account" in evidence and {
                        key: Decimal(value)
                        for key, value in evidence["ending_quantity_by_account"].items()
                    } != {item.account_ref: item.quantity for item in rows}:
                        raise ValueError("reconstruction ending account quantities mismatch")
            verified_fx_changed = (
                len(
                    {
                        tuple(
                            sorted(
                                (
                                    evidence.source_currency,
                                    evidence.target_currency,
                                    evidence.source,
                                )
                                for evidence in item.fx_evidence
                            )
                        )
                        for item in validated_sources
                    }
                )
                != 1
            )
        except Exception as exc:
            raise ReportSemanticError(f"performance source validation failed: {exc}") from exc

    def fail(detail: str) -> NoReturn:
        raise ReportSemanticError(f"performance semantic validation failed: {detail}")

    try:

        def decimal(value: Any) -> Decimal:
            return Decimal(value)

        source_ids = payload["source_snapshot_ids"]
        valuations = payload["period"]["valuations"]
        if [item["snapshot_id"] for item in valuations] != source_ids:
            fail("valuation identities must exactly match source snapshots")
        if set(payload["source_snapshot_digests"]) != set(source_ids):
            fail("snapshot digests must exactly cover source snapshots")
        coverage = payload["coverage"]
        if coverage["source_snapshots"] != len(source_ids):
            fail("snapshot coverage does not reconcile")
        timestamps = [
            datetime.fromisoformat(item["as_of"].replace("Z", "+00:00")) for item in valuations
        ]
        analysis_now = datetime.fromisoformat(payload["analysis_now"].replace("Z", "+00:00"))
        if analysis_now.tzinfo is None or analysis_now.utcoffset() is None:
            fail("analysis_now must be timezone-aware")
        analysis_now = analysis_now.astimezone(timezone.utc)
        if timestamps != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
            fail("valuation timestamps must be unique and ordered")
        if any(instant.astimezone(timezone.utc) > analysis_now for instant in timestamps):
            fail("future snapshot exceeds trusted analysis_now")
        for source_id, instant in zip(source_ids, timestamps):
            if _snapshot_id_timestamp(source_id, label="performance source") != utc_floor_second(
                instant
            ):
                fail("snapshot identity timestamp disagrees with valuation")
        if payload["as_of"] != valuations[-1]["as_of"]:
            fail("report as_of must equal final valuation")
        period = payload["period"]
        start_date, end_date = (
            date.fromisoformat(period["requested_start"]),
            date.fromisoformat(period["requested_end"]),
        )
        if start_date > end_date or (end_date - start_date).days > 366:
            fail("requested period is invalid")
        if (
            period["selected_start_as_of"] != valuations[0]["as_of"]
            or period["selected_end_as_of"] != valuations[-1]["as_of"]
        ):
            fail("selected endpoint evidence is inconsistent")
        expected_start_covered = timestamps[0].astimezone(timezone.utc).date() == start_date
        expected_end_covered = timestamps[-1].astimezone(timezone.utc).date() == end_date
        if (
            period["start_date_covered"] != expected_start_covered
            or period["end_date_covered"] != expected_end_covered
        ):
            fail("date coverage flags are inconsistent")
        gaps = []
        maximum_gap = 0
        for index, (before, after) in enumerate(zip(timestamps, timestamps[1:])):
            delta = after - before
            microseconds = (
                delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
            )
            seconds = (microseconds + 999_999) // 1_000_000
            maximum_gap = max(maximum_gap, seconds)
            if seconds > 36 * 60 * 60:
                gaps.append(
                    {
                        "from_snapshot_id": source_ids[index],
                        "to_snapshot_id": source_ids[index + 1],
                        "seconds": seconds,
                    }
                )
        if (
            period["gaps"] != gaps
            or period["maximum_gap_seconds"] != maximum_gap
            or period["continuous"] != (not gaps)
        ):
            fail("valuation gap evidence is inconsistent")

        contributions = payload["symbol_contributions"]
        source_quality = payload["source_snapshot_quality"]
        if [item["snapshot_id"] for item in source_quality] != source_ids:
            fail("source snapshot quality sequence is inconsistent")
        source_quality_complete = all(
            item["status"] == "COMPLETE"
            and not item["warnings"]
            and not item["missing_fields"]
            and not item["source_errors"]
            and not item["position_quality_notes"]
            for item in source_quality
        )
        identities = [item["identity"] for item in contributions]
        if identities != sorted(set(identities)):
            fail("symbol contributions must be unique and identity-sorted")
        price_expected_identities = [
            item["identity"]
            for item in contributions
            if item["evidence"].get("trajectory") or item["evidence"]["end_present"]
        ]
        if (
            coverage["canonical_identities"] != len(contributions)
            or coverage["trajectory_identities"] != len(price_expected_identities)
            or coverage["price_requested"] != len(price_expected_identities)
        ):
            fail("identity coverage does not reconcile")
        available = []
        exact = []
        trajectory_quality_complete = True
        for item in contributions:
            consume(10)
            values = (item["pnl_base"], item["realized_pnl_base"], item["unrealized_pnl_base"])
            trajectory = item["evidence"].get("trajectory")
            if trajectory is not None:
                if (
                    [entry["snapshot_id"] for entry in trajectory] != source_ids
                    or [entry["as_of"] for entry in trajectory]
                    != [entry["as_of"] for entry in valuations]
                    or item["evidence"]["start_present"] != trajectory[0]["present"]
                    or item["evidence"]["end_present"] != trajectory[-1]["present"]
                    or item["evidence"]["start_rows"] != len(trajectory[0]["legs"])
                    or item["evidence"]["end_rows"] != len(trajectory[-1]["legs"])
                ):
                    fail("symbol trajectory endpoints do not bind source valuations")
                account_sets = []
                quantity_maps = []
                classification_values = set()
                financial_contracts = set()
                cumulative = []
                for entry in trajectory:
                    consume()
                    legs = entry["legs"]
                    if entry["present"] != bool(legs) or [
                        leg["account_ref"] for leg in legs
                    ] != sorted({leg["account_ref"] for leg in legs}):
                        fail("symbol trajectory presence or account ordering is inconsistent")
                    account_sets.append({leg["account_ref"] for leg in legs})
                    quantity_maps.append(
                        {leg["account_ref"]: decimal(leg["quantity"]) for leg in legs}
                    )
                    realized_values = []
                    unrealized_values = []
                    for leg in legs:
                        consume()
                        if leg["quality_notes"]:
                            trajectory_quality_complete = False
                        classification_values.add(
                            (leg["sector"], leg["region"], tuple(leg["themes"]))
                        )
                        effective_at = datetime.fromisoformat(
                            leg["fx_rate_effective_at"].replace("Z", "+00:00")
                        )
                        entry_as_of = datetime.fromisoformat(entry["as_of"].replace("Z", "+00:00"))
                        if (
                            decimal(leg["fx_to_base"]) <= 0
                            or abs(decimal(leg["fx_to_base"]) - decimal(leg["fx_evidence_rate"]))
                            > max(Decimal(1), abs(decimal(leg["fx_evidence_rate"])))
                            * Decimal("1e-18")
                            or leg["fx_rate_target_currency"] != payload["base_currency"]
                            or not leg["fx_rate_source"]
                            or leg["fx_rate_source"] == "UNKNOWN"
                            or effective_at.tzinfo is None
                            or effective_at.utcoffset() is None
                            or effective_at.astimezone(timezone.utc)
                            > entry_as_of.astimezone(timezone.utc)
                        ):
                            fail("symbol trajectory FX evidence is invalid or looks ahead")
                        for local_field, base_field in (
                            (
                                "cumulative_realized_pnl_local",
                                "cumulative_realized_pnl_base",
                            ),
                            (
                                "cumulative_unrealized_pnl_local",
                                "cumulative_unrealized_pnl_base",
                            ),
                        ):
                            local_value, base_value = leg[local_field], leg[base_field]
                            if (local_value is None) != (base_value is None):
                                fail("symbol trajectory local/base P&L availability differs")
                            if local_value is not None:
                                expected_base = decimal(local_value) * decimal(
                                    leg["fx_evidence_rate"]
                                )
                                if abs(decimal(base_value) - expected_base) > max(
                                    Decimal(1), abs(expected_base)
                                ) * Decimal("1e-18"):
                                    fail(
                                        "symbol trajectory base P&L does not reconcile to FX evidence"
                                    )
                        financial_contracts.add(
                            (
                                leg["account_ref"],
                                leg["currency"],
                                leg["currency_provenance"],
                                leg["fx_rate_target_currency"],
                                leg["fx_rate_source"],
                                leg["fx_rate_contract"],
                            )
                        )
                        realized_values.append(leg["cumulative_realized_pnl_base"])
                        unrealized_values.append(leg["cumulative_unrealized_pnl_base"])
                    cumulative.append(
                        (
                            None
                            if any(value is None for value in realized_values)
                            else sum((decimal(value) for value in realized_values), Decimal(0)),
                            None
                            if any(value is None for value in unrealized_values)
                            else sum((decimal(value) for value in unrealized_values), Decimal(0)),
                        )
                    )
                expected_trajectory_method = "SNAPSHOT_ADJACENT_CUMULATIVE_PNL_DELTA"
                if any(not accounts for accounts in account_sets):
                    expected_trajectory_method = "TRAJECTORY_POSITION_DISCONTINUITY"
                elif any(accounts != account_sets[0] for accounts in account_sets[1:]):
                    expected_trajectory_method = "TRAJECTORY_ACCOUNT_LEGS_CHANGED"
                elif any(quantities != quantity_maps[0] for quantities in quantity_maps[1:]):
                    expected_trajectory_method = "TRAJECTORY_QUANTITY_LIFECYCLE_UNRECONCILED"
                elif len(financial_contracts) != len(account_sets[0]):
                    expected_trajectory_method = "TRAJECTORY_FX_DISCONTINUITY"
                elif len(classification_values) != 1:
                    expected_trajectory_method = "CLASSIFICATION_INCONSISTENT"
                elif any(None in pair for pair in cumulative):
                    expected_trajectory_method = "SNAPSHOT_PNL_FIELDS_INCOMPLETE"
                else:
                    for before, after in zip(cumulative, cumulative[1:]):
                        consume()
                    if expected_trajectory_method == "SNAPSHOT_ADJACENT_CUMULATIVE_PNL_DELTA":
                        expected_realized_trajectory = sum(
                            (
                                after[0] - before[0]
                                for before, after in zip(cumulative, cumulative[1:])
                            ),
                            Decimal(0),
                        )
                        expected_unrealized_trajectory = sum(
                            (
                                after[1] - before[1]
                                for before, after in zip(cumulative, cumulative[1:])
                            ),
                            Decimal(0),
                        )
                        classification = next(iter(classification_values))
                        if item["method"] != "TRADE_SNAPSHOT_REALIZED_RECONCILIATION_MISMATCH" and (
                            item["status"] != "AVAILABLE"
                            or item["method"] != expected_trajectory_method
                            or decimal(item["realized_pnl_base"]) != expected_realized_trajectory
                            or decimal(item["unrealized_pnl_base"])
                            != expected_unrealized_trajectory
                            or decimal(item["pnl_base"])
                            != expected_realized_trajectory + expected_unrealized_trajectory
                            or item["account_refs"] != sorted(account_sets[0])
                            or (item["sector"], item["region"], tuple(item["themes"]))
                            != classification
                        ):
                            fail("symbol adjacent trajectory P&L does not reconstruct")
                if (
                    expected_trajectory_method != "SNAPSHOT_ADJACENT_CUMULATIVE_PNL_DELTA"
                    and item["method"] != expected_trajectory_method
                ):
                    fail("symbol trajectory discontinuity method is inconsistent")
            if item["status"] == "AVAILABLE":
                if any(value is None for value in values) or item["method"] not in {
                    "SNAPSHOT_ADJACENT_CUMULATIVE_PNL_DELTA",
                    "TRADE_FIFO_REALIZED_GROSS_PRE_FEE",
                }:
                    fail("available contribution lacks exact P&L evidence")
                if item["method"] == "TRADE_FIFO_REALIZED_GROSS_PRE_FEE" and (
                    trajectory is not None
                    or item["evidence"]["start_present"]
                    or item["evidence"]["end_present"]
                    or not item["evidence"].get("trade_round_trip_indexes")
                ):
                    fail("trade-only exact contribution overlaps snapshot trajectory")
                if decimal(item["pnl_base"]) != decimal(item["realized_pnl_base"]) + decimal(
                    item["unrealized_pnl_base"]
                ):
                    fail("symbol P&L does not reconcile")
                available.append(item)
                exact.append(item)
            elif item["status"] == "ESTIMATED":
                evidence = item["evidence"]
                if item["method"] == "PRICE_QUANTITY_GROSS_UNREALIZED_ESTIMATE":
                    if (
                        item["pnl_base"] is None
                        or item["realized_pnl_base"] is not None
                        or item["unrealized_pnl_base"] is None
                    ):
                        fail("estimated contribution lacks gross price reconstruction evidence")
                    expected_estimate = (
                        decimal(evidence["ending_quantity"])
                        * (
                            decimal(evidence["aligned_end_close"])
                            - decimal(evidence["aligned_start_close"])
                        )
                        * decimal(evidence["ending_fx_to_base"])
                    )
                    if (
                        decimal(evidence["ending_fx_to_base"]) != 1
                        or decimal(item["pnl_base"]) != expected_estimate
                    ):
                        fail("estimated contribution formula does not reconstruct")
                elif item["method"] in {
                    "TRADE_FIFO_REALIZED_PLUS_PRICE_UNREALIZED_ESTIMATE",
                    "TRADE_FIFO_REALIZED_GROSS_PRE_FEE",
                    "TRADE_FIFO_OPEN_LOT_UNREALIZED_GROSS_PRE_FEE",
                }:
                    if item["pnl_base"] is None or item["realized_pnl_base"] is None:
                        fail("FIFO contribution lacks realized gross P&L")
                    expected_pnl = decimal(item["realized_pnl_base"]) + (
                        decimal(item["unrealized_pnl_base"])
                        if item["unrealized_pnl_base"] is not None
                        else Decimal(0)
                    )
                    if decimal(item["pnl_base"]) != expected_pnl:
                        fail("FIFO contribution split does not reconcile")
                else:
                    fail("estimated contribution method is invalid")
                available.append(item)
            else:
                if any(value is not None for value in values):
                    fail("unavailable contribution cannot contain P&L")
                if item["method"] == "RECONSTRUCTION_EVIDENCE_INCOMPLETE" and item["evidence"].get(
                    "reconstruction_reason"
                ) not in {
                    "TRANSACTION_PERIOD_FX_UNAVAILABLE",
                    "PRICE_INTERVAL_UNAVAILABLE",
                }:
                    fail("unavailable reconstruction lacks a structured reason")
        source_quality_complete = source_quality_complete and trajectory_quality_complete
        expected_complete = (
            bool(contributions)
            and len(exact) == len(contributions)
            and len(source_ids) >= 2
            and period["continuous"]
            and expected_start_covered
            and expected_end_covered
            and coverage["price_aligned"]
            == sum(item["evidence"].get("trajectory") is not None for item in exact)
            and source_quality_complete
            and payload["trade_evidence"]["status"] == "COMPLETE"
            and payload["trade_evidence"]["used_for_contributions"]
            and not any(
                reason["code"] in {"FX_PROVENANCE_CHANGED", "PNL_NAV_RECONCILIATION_MISMATCH"}
                for reason in payload["quality_reasons"]
            )
        )
        expected_status = (
            "COMPLETE" if expected_complete else "PARTIAL" if available else "UNAVAILABLE"
        )
        expected_mode = (
            "ACCURATE_PNL"
            if expected_complete
            else "RECONSTRUCTED"
            if contributions and (len(source_ids) >= 2 or available)
            else "UNAVAILABLE"
        )
        expected_confidence = (
            "HIGH" if expected_complete else "LOW" if expected_mode == "RECONSTRUCTED" else "NONE"
        )
        if (payload["contribution_status"], payload["mode"], payload["confidence"]) != (
            expected_status,
            expected_mode,
            expected_confidence,
        ):
            fail("mode, confidence and contribution coverage disagree")
        if coverage["available_contributions"] != len(available) or coverage[
            "unavailable_contributions"
        ] != len(contributions) - len(available):
            fail("contribution coverage does not reconcile")
        expected_total = (
            sum((decimal(item["pnl_base"]) for item in available), Decimal(0))
            if available
            else None
        )
        expected_realized = (
            sum((decimal(item["realized_pnl_base"]) for item in available), Decimal(0))
            if available
            and all(item["realized_pnl_base"] is not None for item in available)
            and not any(
                item["method"] == "TRADE_FIFO_BASE_FX_UNAVAILABLE" for item in contributions
            )
            else None
        )
        expected_unrealized = (
            sum((decimal(item["unrealized_pnl_base"]) for item in available), Decimal(0))
            if available and all(item["unrealized_pnl_base"] is not None for item in available)
            else None
        )
        for field, expected in (
            ("total_pnl_base", expected_total),
            ("realized_pnl_base", expected_realized),
            ("unrealized_pnl_base", expected_unrealized),
        ):
            observed = None if payload[field] is None else decimal(payload[field])
            if observed != expected:
                fail(f"{field} does not reconcile")

        def aggregate(attribute: str, *, split: bool = False) -> dict[str, Decimal]:
            result: dict[str, Decimal] = {}
            for item in available:
                labels = item[attribute]
                if isinstance(labels, str):
                    labels = [labels]
                labels = sorted(labels) or ["UNKNOWN"]
                if split:
                    amount = decimal(item["pnl_base"]) / Decimal(len(labels))
                    allocations = [amount for _ in labels[:-1]]
                    allocations.append(decimal(item["pnl_base"]) - sum(allocations, Decimal(0)))
                else:
                    allocations = [decimal(item["pnl_base"]) for _ in labels]
                for label, amount in zip(labels, allocations):
                    result[label] = result.get(label, Decimal(0)) + amount
            return dict(sorted(result.items()))

        for field, attribute, split in (
            ("sector_contributions", "sector", False),
            ("region_contributions", "region", False),
            ("theme_full_contributions", "themes", False),
            ("theme_split_contributions", "themes", True),
        ):
            observed = {key: decimal(value) for key, value in payload[field].items()}
            if observed != aggregate(attribute, split=split):
                fail(f"{field} does not reconcile")
        expected_contributors = sorted(
            (item for item in available if decimal(item["pnl_base"]) > 0),
            key=lambda item: (-decimal(item["pnl_base"]), item["identity"]),
        )[:5]
        expected_detractors = sorted(
            (item for item in available if decimal(item["pnl_base"]) < 0),
            key=lambda item: (decimal(item["pnl_base"]), item["identity"]),
        )[:5]
        if (
            payload["top_contributors"] != expected_contributors
            or payload["top_detractors"] != expected_detractors
        ):
            fail("top contribution rankings are inconsistent")

        price_identities: set[str] = set()
        if [item["identity"] for item in payload["price_evidence"]] != price_expected_identities[
            : len(payload["price_evidence"])
        ]:
            fail("price evidence must follow the bounded contribution identity prefix")
        for evidence in payload["price_evidence"]:
            consume(5 + len(evidence["points"]) + len(evidence["aligned_points"]))
            if evidence["identity"] in price_identities:
                fail("price evidence identities must be unique")
            price_identities.add(evidence["identity"])
            points = evidence["points"]
            aligned_points = evidence["aligned_points"]
            point_dates = [date.fromisoformat(item["date"]) for item in points]
            market = evidence["identity"].split(".", 1)[0]
            if (
                evidence["requested_start"] != period["requested_start"]
                or evidence["requested_end"] != period["requested_end"]
            ):
                fail("price request period must match report period")
            expected_cutoff = min(
                date.fromisoformat(evidence["requested_end"]),
                _latest_complete_market_close_date(
                    min(timestamps[-1].astimezone(timezone.utc), analysis_now), market
                ),
            )
            if date.fromisoformat(evidence["cutoff_date"]) != expected_cutoff:
                fail("price completed-close cutoff is inconsistent")
            if point_dates != sorted(set(point_dates)):
                fail("price dates must be unique and ordered")
            if any(
                day < date.fromisoformat(evidence["requested_start"]) or day > expected_cutoff
                for day in point_dates
            ):
                fail("price evidence contains look-ahead or pre-period rows")
            if evidence["rows"] != len(points):
                fail("price row coverage does not reconcile")
            if any(item not in points for item in aligned_points):
                fail("aligned price points must come from raw price evidence")
            if points:
                raw = "|".join(f"{item['date']}:{item['adjusted_close']}" for item in points)
                expected_digest = hashlib.sha256(raw.encode()).hexdigest()
                if (
                    evidence["status"] != "AVAILABLE"
                    or evidence["first_date"] != points[0]["date"]
                    or evidence["last_date"] != points[-1]["date"]
                    or evidence["digest"] != expected_digest
                    or evidence["reason"] is not None
                    or any(decimal(item["adjusted_close"]) <= 0 for item in points)
                ):
                    fail("available price evidence is inconsistent")
            elif (
                evidence["status"] != "UNAVAILABLE"
                or evidence["first_date"] is not None
                or evidence["last_date"] is not None
                or evidence["digest"] is not None
                or not evidence["reason"]
            ):
                fail("unavailable price evidence is inconsistent")
        available_price_rows = [
            item for item in payload["price_evidence"] if item["status"] == "AVAILABLE"
        ]
        common_dates = (
            sorted(
                set.intersection(
                    *(
                        set(point["date"] for point in item["points"])
                        for item in available_price_rows
                    )
                )
            )
            if available_price_rows
            else []
        )
        for evidence in payload["price_evidence"]:
            expected_aligned = (
                [point for point in evidence["points"] if point["date"] in common_dates]
                if evidence["status"] == "AVAILABLE"
                else []
            )
            if evidence["aligned_points"] != expected_aligned:
                fail("price common-interval alignment is inconsistent")
        if (
            coverage["price_analyzed"] != len(payload["price_evidence"])
            or coverage["price_omitted"] != coverage["price_requested"] - coverage["price_analyzed"]
            or coverage["price_available"]
            != sum(item["status"] == "AVAILABLE" for item in payload["price_evidence"])
            or coverage["price_aligned"]
            != sum(len(item["aligned_points"]) >= 2 for item in payload["price_evidence"])
        ):
            fail("price coverage does not reconcile")
        price_by_identity = {item["identity"]: item for item in payload["price_evidence"]}
        for item in contributions:
            if item["method"] in {
                "PRICE_QUANTITY_GROSS_UNREALIZED_ESTIMATE",
                "TRADE_FIFO_REALIZED_PLUS_PRICE_UNREALIZED_ESTIMATE",
                "TRADE_FIFO_OPEN_LOT_UNREALIZED_GROSS_PRE_FEE",
            }:
                evidence = price_by_identity.get(item["identity"])
                if (
                    evidence is None
                    or len(evidence["aligned_points"]) < 2
                    or item["evidence"]["price_digest"] != evidence["digest"]
                    or item["evidence"]["aligned_start_date"]
                    != evidence["aligned_points"][0]["date"]
                    or item["evidence"]["aligned_end_date"]
                    != evidence["aligned_points"][-1]["date"]
                    or item["evidence"]["aligned_start_close"]
                    != evidence["aligned_points"][0]["adjusted_close"]
                    or item["evidence"]["aligned_end_close"]
                    != evidence["aligned_points"][-1]["adjusted_close"]
                ):
                    fail("estimated contribution price evidence is not traceable")
        if sum(item["rows"] for item in payload["price_evidence"]) > 20_000:
            fail("price aggregate row budget exceeded")

        trade_evidence = payload["trade_evidence"]
        trade_review = trade_evidence["review"]
        if trade_review is None:
            if (
                trade_evidence["status"] != "UNAVAILABLE"
                or not trade_evidence["reason"]
                or trade_evidence["review_digest"] is not None
                or trade_evidence["used_for_contributions"]
            ):
                fail("unavailable trade evidence is inconsistent")
            expected_trade_orders = expected_trade_fills = expected_trade_trips = 0
        else:
            validate_trade_review_document(trade_review)
            trade_digest = hashlib.sha256(
                json.dumps(trade_review, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if trade_evidence["review_digest"] != trade_digest:
                fail("trade review digest does not bind normalized evidence")
            if (
                trade_review["start"] != period["requested_start"]
                or trade_review["end"] != period["requested_end"]
                or trade_review["base_currency"] != payload["base_currency"]
                or trade_review["source_snapshot_as_of"] != valuations[-1]["as_of"]
                or trade_review["coverage"]["source_snapshot_id"] != source_ids[-1]
            ):
                fail("trade evidence period, currency, or source snapshot is inconsistent")
            fatal_trade_codes = {
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
            observed_trade_codes = {item["code"] for item in trade_review["quality_issues"]}
            ending_identities = {
                item["identity"] for item in contributions if item["evidence"]["end_present"]
            }
            open_lot_identities = {item["identity"] for item in trade_review["open_lots"]}
            trade_is_authoritative = not (observed_trade_codes & fatal_trade_codes) and (
                open_lot_identities <= ending_identities
            )
            expected_open_quantities: dict[tuple[str, str], Decimal] = {}
            for contribution in contributions:
                evidence = contribution["evidence"]
                if not evidence["end_present"]:
                    continue
                quantities = evidence.get("ending_quantity_by_account")
                if quantities is None and evidence.get("trajectory"):
                    quantities = {
                        leg["account_ref"]: leg["quantity"]
                        for leg in evidence["trajectory"][-1]["legs"]
                    }
                for account_ref, quantity in (quantities or {}).items():
                    expected_open_quantities[(account_ref, contribution["identity"])] = decimal(
                        quantity
                    )
            observed_open_quantities: dict[tuple[str, str], Decimal] = {}
            for lot in trade_review["open_lots"]:
                key = (lot["account_ref"], lot["identity"])
                observed_open_quantities[key] = observed_open_quantities.get(
                    key, Decimal(0)
                ) + decimal(lot["quantity"])
            trade_is_authoritative = (
                trade_is_authoritative and observed_open_quantities == expected_open_quantities
            )
            if (trade_evidence["status"] == "COMPLETE") != trade_is_authoritative:
                fail("trade evidence authority is not reconstructed from normalized evidence")
            if trade_evidence["status"] == "COMPLETE":
                if trade_evidence["reason"] is not None:
                    fail("complete trade evidence cannot have an unavailable reason")
            elif trade_evidence["used_for_contributions"] or not trade_evidence["reason"]:
                fail("unavailable trade evidence cannot authorize contributions")
            if trade_evidence["used_for_contributions"] != (trade_evidence["status"] == "COMPLETE"):
                fail("trade contribution authority path is inconsistent")
            expected_trade_orders = len(trade_review["normalized_orders"])
            expected_trade_fills = len(trade_review["normalized_fills"])
            expected_trade_trips = len(trade_review["round_trips"])
            expected_open_lots = len(trade_review["open_lots"])
            referenced_trip_indexes: list[int] = []
            referenced_open_lot_indexes: list[int] = []
            for item in contributions:
                evidence = item["evidence"]
                indexes = evidence.get("trade_round_trip_indexes")
                if indexes is None:
                    continue
                if (
                    evidence.get("trade_review_id") != trade_review["review_id"]
                    or evidence.get("trade_review_digest") != trade_digest
                    or indexes != sorted(set(indexes))
                    or any(index >= expected_trade_trips for index in indexes)
                ):
                    fail("symbol FIFO evidence references are inconsistent")
                referenced_trip_indexes.extend(indexes)
                trips = [trade_review["round_trips"][index] for index in indexes]
                if any(trip["identity"] != item["identity"] for trip in trips):
                    fail("symbol FIFO evidence crosses canonical identities")
                trip_accounts = {trip["account_ref"] for trip in trips}
                if not trip_accounts <= set(item["account_refs"]):
                    fail("symbol FIFO evidence crosses contribution accounts")
                local: dict[str, Decimal] = {}
                for trip in trips:
                    currency = trip["currency"]
                    local[currency] = local.get(currency, Decimal(0)) + decimal(
                        trip["realized_pl_local"]
                    )
                if {
                    key: decimal(value)
                    for key, value in evidence.get("realized_local_by_currency", {}).items()
                } != local:
                    fail("symbol local-currency FIFO buckets do not reconcile")
                if item["realized_pnl_base"] is not None:
                    if any(trip["realized_pl_base"] is None for trip in trips) or decimal(
                        item["realized_pnl_base"]
                    ) != sum((decimal(trip["realized_pl_base"]) for trip in trips), Decimal(0)):
                        fail("symbol FIFO realized base P&L does not reconcile")
                trajectory = evidence.get("trajectory")
                if trajectory and item["method"] in {
                    "SNAPSHOT_ADJACENT_CUMULATIVE_PNL_DELTA",
                    "TRADE_SNAPSHOT_REALIZED_RECONCILIATION_MISMATCH",
                }:
                    trajectory_realized = Decimal(0)
                    for previous, current in zip(trajectory, trajectory[1:]):
                        previous_by_account = {
                            leg["account_ref"]: decimal(leg["cumulative_realized_pnl_base"])
                            for leg in previous["legs"]
                        }
                        current_by_account = {
                            leg["account_ref"]: decimal(leg["cumulative_realized_pnl_base"])
                            for leg in current["legs"]
                        }
                        if previous_by_account.keys() != current_by_account.keys():
                            fail("trade reconciliation crosses discontinuous account legs")
                        trajectory_realized += sum(
                            (
                                current_by_account[account_ref] - previous_by_account[account_ref]
                                for account_ref in sorted(previous_by_account)
                            ),
                            Decimal(0),
                        )
                    trade_realized_complete = all(
                        trip["realized_pl_base"] is not None for trip in trips
                    )
                    trade_realized = (
                        sum(
                            (decimal(trip["realized_pl_base"]) for trip in trips),
                            Decimal(0),
                        )
                        if trade_realized_complete
                        else None
                    )
                    realized_matches = (
                        trade_realized_complete and trade_realized == trajectory_realized
                    )
                    if (
                        item["method"] == "TRADE_SNAPSHOT_REALIZED_RECONCILIATION_MISMATCH"
                    ) == realized_matches:
                        fail("snapshot and FIFO realized P&L reconciliation is inconsistent")
                lot_indexes = evidence.get("open_lot_indexes")
                if lot_indexes is not None:
                    if (
                        evidence.get("trade_review_id") != trade_review["review_id"]
                        or evidence.get("trade_review_digest") != trade_digest
                        or lot_indexes != sorted(set(lot_indexes))
                        or any(index >= expected_open_lots for index in lot_indexes)
                    ):
                        fail("symbol FIFO open-lot references are inconsistent")
                    referenced_open_lot_indexes.extend(lot_indexes)
                    lots = [trade_review["open_lots"][index] for index in lot_indexes]
                    if any(
                        lot["identity"] != item["identity"]
                        or lot["account_ref"] not in item["account_refs"]
                        for lot in lots
                    ):
                        fail("symbol FIFO open lots cross identity or account")
                    from .risk import _first_completed_market_session_on_or_after

                    market = item["identity"].split(".", 1)[0]
                    expected_first_sessions = [
                        _first_completed_market_session_on_or_after(
                            datetime.fromisoformat(lot["opened_at"].replace("Z", "+00:00")),
                            market,
                        ).isoformat()
                        for lot in lots
                    ]
                    if evidence.get("open_lot_first_completed_session_dates") != (
                        expected_first_sessions
                    ):
                        fail("FIFO open-lot completed-session dates are inconsistent")
                    aligned_end_date = evidence.get("aligned_end_date")
                    price_pre_entry = aligned_end_date is not None and any(
                        date.fromisoformat(aligned_end_date) < date.fromisoformat(first_session)
                        for first_session in expected_first_sessions
                    )
                    if (
                        item["method"]
                        in {
                            "TRADE_FIFO_OPEN_LOT_UNREALIZED_GROSS_PRE_FEE",
                            "TRADE_FIFO_OPEN_LOT_EVIDENCE_INCOMPLETE",
                            "TRADE_FIFO_OPEN_LOT_PRICE_PRE_ENTRY",
                        }
                        and (item["method"] == "TRADE_FIFO_OPEN_LOT_PRICE_PRE_ENTRY")
                        != price_pre_entry
                    ):
                        fail("FIFO open-lot price date authority is inconsistent")
                    if item["method"] == "TRADE_FIFO_OPEN_LOT_UNREALIZED_GROSS_PRE_FEE":
                        if any(lot["currency"] != payload["base_currency"] for lot in lots):
                            fail("FIFO open-lot base currency is inconsistent")
                        if (
                            trade_review["coverage"]["corporate_action_evidence_status"]
                            != "AVAILABLE"
                            or evidence.get("price_fill_consistency")
                            != "CORPORATE_ACTION_EVIDENCE_AVAILABLE_FORWARD_ADJUSTED"
                        ):
                            fail("FIFO open-lot price/fill consistency is unsupported")
                        expected_open_unrealized = sum(
                            (
                                (decimal(evidence["aligned_end_close"]) - decimal(lot["price"]))
                                * decimal(lot["quantity"])
                                for lot in lots
                            ),
                            Decimal(0),
                        )
                        if (
                            decimal(evidence["open_lot_unrealized_base"])
                            != expected_open_unrealized
                            or decimal(item["unrealized_pnl_base"]) != expected_open_unrealized
                        ):
                            fail("FIFO open-lot unrealized P&L does not reconstruct")
            if trade_evidence["used_for_contributions"] and sorted(referenced_trip_indexes) != list(
                range(expected_trade_trips)
            ):
                fail("FIFO round trips are omitted or double counted")
            if trade_evidence["used_for_contributions"] and sorted(
                referenced_open_lot_indexes
            ) != list(range(expected_open_lots)):
                fail("FIFO open lots are omitted or double counted")
        if (
            coverage["trade_orders"] != expected_trade_orders
            or coverage["trade_fills"] != expected_trade_fills
            or coverage["trade_round_trips"] != expected_trade_trips
        ):
            fail("trade evidence coverage does not reconcile")

        flows = payload["flow_evidence"]["flows"]
        if coverage["flow_rows"] != len(flows):
            fail("flow coverage does not reconcile")
        if len({item["flow_id"] for item in flows}) != len(flows):
            fail("flow IDs are not unique")
        for item in flows:
            amount_base = decimal(item["amount_base"])
            if (
                decimal(item["fx_to_base"]) <= 0
                or decimal(item["amount_local"]) * decimal(item["fx_to_base"]) != amount_base
                or (item["flow_type"] == "DEPOSIT" and amount_base <= 0)
                or (item["flow_type"] == "WITHDRAWAL" and amount_base >= 0)
            ):
                fail("flow FX does not reconcile")
        external = sum(
            (
                decimal(item["amount_base"])
                for item in flows
                if item["flow_type"] != "INTERNAL_TRANSFER"
            ),
            Decimal(0),
        )
        internal = sum(
            (
                decimal(item["amount_base"])
                for item in flows
                if item["flow_type"] == "INTERNAL_TRANSFER"
            ),
            Decimal(0),
        )
        expected_external = (
            external if flows or payload["flow_evidence"]["status"] == "COMPLETE" else None
        )
        expected_internal = (
            internal if flows or payload["flow_evidence"]["status"] == "COMPLETE" else None
        )
        if (
            None
            if payload["external_flow_base"] is None
            else decimal(payload["external_flow_base"])
        ) != expected_external or (
            None
            if payload["internal_transfer_base"] is None
            else decimal(payload["internal_transfer_base"])
        ) != expected_internal:
            fail("flow totals do not reconcile")
        flow_coverage = payload["flow_evidence"]
        if flow_coverage["status"] == "COMPLETE":
            if (
                flow_coverage["source"] is None
                or flow_coverage["coverage_start"] is None
                or flow_coverage["coverage_end"] is None
                or flow_coverage["reason"] is not None
            ):
                fail("complete flow evidence is inconsistent")
            coverage_start = datetime.fromisoformat(
                flow_coverage["coverage_start"].replace("Z", "+00:00")
            )
            coverage_end = datetime.fromisoformat(
                flow_coverage["coverage_end"].replace("Z", "+00:00")
            )
            if coverage_start > coverage_end or any(
                not (
                    coverage_start
                    <= datetime.fromisoformat(item["occurred_at"].replace("Z", "+00:00"))
                    <= coverage_end
                )
                for item in flows
            ):
                fail("flow rows lie outside declared source coverage")
            if any(
                not (
                    timestamps[0]
                    < datetime.fromisoformat(item["occurred_at"].replace("Z", "+00:00"))
                    <= timestamps[-1]
                )
                for item in flows
            ):
                fail("report flow rows must be scoped to the selected valuation interval")
        elif flow_coverage["status"] == "UNAVAILABLE":
            if (
                flow_coverage["source"] is not None
                or flow_coverage["coverage_start"] is not None
                or flow_coverage["coverage_end"] is not None
                or flows
                or not flow_coverage["reason"]
            ):
                fail("unavailable flow evidence is inconsistent")
        else:
            if (
                flow_coverage["source"] is None
                or flow_coverage["coverage_start"] is None
                or flow_coverage["coverage_end"] is None
                or not flow_coverage["reason"]
            ):
                fail("partial flow evidence is inconsistent")
            partial_start = datetime.fromisoformat(
                flow_coverage["coverage_start"].replace("Z", "+00:00")
            )
            partial_end = datetime.fromisoformat(
                flow_coverage["coverage_end"].replace("Z", "+00:00")
            )
            if partial_start > partial_end or any(
                not (
                    partial_start
                    <= datetime.fromisoformat(item["occurred_at"].replace("Z", "+00:00"))
                    <= partial_end
                )
                for item in flows
            ):
                fail("partial flow rows lie outside source coverage")
        coverage_complete = (
            flow_coverage["status"] == "COMPLETE"
            and datetime.fromisoformat(flow_coverage["coverage_start"].replace("Z", "+00:00"))
            <= timestamps[0]
            and datetime.fromisoformat(flow_coverage["coverage_end"].replace("Z", "+00:00"))
            >= timestamps[-1]
        )
        valuation_return_inputs_complete = (
            coverage_complete and not gaps and expected_start_covered and expected_end_covered
        )
        return_authorized = valuation_return_inputs_complete and payload["mode"] == "ACCURATE_PNL"
        reconciliation = payload["pnl_reconciliation"]
        nav_change = decimal(valuations[-1]["nav_base"]) - decimal(valuations[0]["nav_base"])
        field_population_complete = bool(contributions) and len(exact) == len(contributions)
        if coverage_complete and expected_total is not None and len(source_ids) >= 2:
            expected_net = nav_change - external
            if field_population_complete:
                expected_residual = expected_net - expected_total
                expected_reconciliation_status = "MATCHED" if expected_residual == 0 else "MISMATCH"
            else:
                expected_residual = None
                expected_reconciliation_status = "PARTIAL"
        else:
            expected_net = None
            expected_residual = None
            expected_reconciliation_status = "UNAVAILABLE"
        expected_reconciliation = {
            "status": expected_reconciliation_status,
            "basis": "ENDING_NAV_MINUS_BEGINNING_NAV_MINUS_EXTERNAL_FLOWS",
            "nav_change_base": nav_change,
            "external_flow_base": external if coverage_complete else None,
            "expected_net_pnl_base": expected_net,
            "attributed_pnl_base": expected_total,
            "residual_base": expected_residual,
        }
        observed_reconciliation = {
            key: decimal(value) if key.endswith("_base") and value is not None else value
            for key, value in reconciliation.items()
        }
        if observed_reconciliation != expected_reconciliation:
            fail("P&L to NAV reconciliation evidence is inconsistent")
        if return_authorized:
            chained = Decimal(1)
            for before_index, (before_time, after_time) in enumerate(
                zip(timestamps, timestamps[1:])
            ):
                beginning_nav = decimal(valuations[before_index]["nav_base"])
                if beginning_nav <= 0:
                    fail("TWR beginning NAV must be positive")
                subflow = sum(
                    (
                        decimal(item["amount_base"])
                        for item in flows
                        if item["flow_type"] != "INTERNAL_TRANSFER"
                        and before_time
                        < datetime.fromisoformat(item["occurred_at"].replace("Z", "+00:00"))
                        <= after_time
                    ),
                    Decimal(0),
                )
                chained *= (
                    decimal(valuations[before_index + 1]["nav_base"]) - subflow
                ) / beginning_nav
            if (
                payload["return_status"] != "AVAILABLE"
                or payload["return_method"] != "TWR_END_OF_SUBPERIOD_FLOW_ADJUSTMENT"
                or decimal(payload["portfolio_return"]) != chained - Decimal(1)
            ):
                fail("TWR does not reconstruct from valuations and flows")
        elif (
            payload["return_status"] != "UNAVAILABLE"
            or payload["return_method"] is not None
            or payload["portfolio_return"] is not None
        ):
            fail("return is claimed without complete flow and valuation authority")
        if (
            payload["irr_status"] != "NOT_CALCULATED"
            or payload["irr_method"] is not None
            or payload["irr"] is not None
        ):
            fail("IRR authority is inconsistent")

        expected_reasons: list[dict[str, str]] = []
        contribution_reason_details = {
            "ENDPOINT_POSITION_MISSING": (
                "POSITION_ENDPOINT_MISSING",
                "position opened or closed inside the interval; trades/corporate actions are not authoritative",
            ),
            "ACCOUNT_LEGS_CHANGED": (
                "ACCOUNT_LEGS_CHANGED",
                "account-level position legs differ across endpoints; transfer or closure is ambiguous",
            ),
            "CLASSIFICATION_INCONSISTENT": (
                "CLASSIFICATION_CHANGED",
                "classification is inconsistent across accounts or endpoints",
            ),
            "SNAPSHOT_PNL_FIELDS_INCOMPLETE": (
                "PNL_FIELDS_INCOMPLETE",
                "realized/unrealized base P&L is incomplete",
            ),
            "TRADE_FIFO_BASE_FX_UNAVAILABLE": (
                "TRADE_FIFO_BASE_FX_UNAVAILABLE",
                "FIFO realized gross P&L is retained only in local-currency buckets because transaction-date base FX is unavailable",
            ),
            "TRAJECTORY_POSITION_DISCONTINUITY": (
                "TRAJECTORY_POSITION_DISCONTINUITY",
                "position presence is discontinuous across selected snapshots and lacks authoritative reconciliation",
            ),
            "TRAJECTORY_ACCOUNT_LEGS_CHANGED": (
                "TRAJECTORY_ACCOUNT_LEGS_CHANGED",
                "account-level position legs reset or transfer inside the selected trajectory",
            ),
            "TRAJECTORY_QUANTITY_LIFECYCLE_UNRECONCILED": (
                "TRAJECTORY_QUANTITY_LIFECYCLE_UNRECONCILED",
                "account-level quantity changes lack authoritative trade, transfer, or corporate-action reconciliation",
            ),
            "TRAJECTORY_FX_DISCONTINUITY": (
                "TRAJECTORY_FX_DISCONTINUITY",
                "position currency or FX provenance/authority contract changes across account legs inside the trajectory",
            ),
            "TRADE_SNAPSHOT_REALIZED_RECONCILIATION_MISMATCH": (
                "TRADE_SNAPSHOT_REALIZED_RECONCILIATION_MISMATCH",
                "snapshot realized delta does not reconcile to complete normalized FIFO round trips",
            ),
            "TRADE_FIFO_OPEN_LOT_EVIDENCE_INCOMPLETE": (
                "TRADE_FIFO_OPEN_LOT_EVIDENCE_INCOMPLETE",
                "open-lot currency or ending price evidence cannot authorize gross unrealized P&L",
            ),
            "TRADE_FIFO_OPEN_LOT_PRICE_PRE_ENTRY": (
                "TRADE_FIFO_OPEN_LOT_PRICE_PRE_ENTRY",
                "aligned ending close precedes an open lot's first eligible completed market session",
            ),
        }
        for item in contributions:
            reason = contribution_reason_details.get(item["method"])
            if reason is not None:
                expected_reasons.append(
                    {"code": reason[0], "scope": item["identity"], "detail": reason[1]}
                )
            reconstruction_reason = item["evidence"].get("reconstruction_reason")
            if reconstruction_reason == "TRANSACTION_PERIOD_FX_UNAVAILABLE":
                expected_reasons.append(
                    {
                        "code": "RECONSTRUCTED_TRANSACTION_PERIOD_FX_UNAVAILABLE",
                        "scope": item["identity"],
                        "detail": "historical transaction-period FX is unavailable; cross-currency gross P&L is not estimated",
                    }
                )
            elif reconstruction_reason == "PRICE_INTERVAL_UNAVAILABLE":
                expected_reasons.append(
                    {
                        "code": "RECONSTRUCTED_PRICE_INTERVAL_UNAVAILABLE",
                        "scope": item["identity"],
                        "detail": "at least two common adjusted-close points are required for reconstruction",
                    }
                )
        reconstruction_without_realized = any(
            item["method"]
            in {
                "PRICE_QUANTITY_GROSS_UNREALIZED_ESTIMATE",
                "RECONSTRUCTION_EVIDENCE_INCOMPLETE",
            }
            for item in contributions
        )
        from .performance import TRADE_SOURCE_UNAVAILABLE_CODES

        trade_source_failed = (
            payload["trade_evidence"]["review"] is None
            and payload["trade_evidence"]["reason"] in TRADE_SOURCE_UNAVAILABLE_CODES
        )
        if len(source_ids) >= 2 and payload["trade_evidence"]["status"] == "UNAVAILABLE":
            expected_reasons.append(
                {
                    "code": "TRADE_HISTORY_UNAVAILABLE",
                    "scope": "trade_evidence",
                    "detail": payload["trade_evidence"]["reason"],
                }
            )
        elif trade_source_failed:
            expected_reasons.append(
                {
                    "code": "TRADE_HISTORY_UNAVAILABLE",
                    "scope": "trade_evidence",
                    "detail": payload["trade_evidence"]["reason"],
                }
            )
        elif reconstruction_without_realized and payload["trade_evidence"]["review"] is None:
            expected_reasons.append(
                {
                    "code": "RECONSTRUCTED_REALIZED_AND_EXITED_UNAVAILABLE",
                    "scope": "symbol_contributions",
                    "detail": "no pinned normalized trade source was supplied; realized P&L and exited positions are excluded",
                }
            )
        elif (
            reconstruction_without_realized and payload["trade_evidence"]["status"] == "UNAVAILABLE"
        ):
            expected_reasons.append(
                {
                    "code": "TRADE_HISTORY_UNAVAILABLE",
                    "scope": "trade_evidence",
                    "detail": payload["trade_evidence"]["reason"],
                }
            )
        if (
            payload["trade_evidence"]["used_for_contributions"]
            and payload["coverage"]["trade_round_trips"]
        ):
            expected_reasons.append(
                {
                    "code": "TRADE_FEES_UNAVAILABLE",
                    "scope": "symbol_contributions",
                    "detail": "FIFO realized P&L is gross pre-fee because authoritative fee evidence is unavailable",
                }
            )
        if not source_quality_complete:
            expected_reasons.append(
                {
                    "code": "SOURCE_DATA_QUALITY_INCOMPLETE",
                    "scope": "source_snapshots",
                    "detail": "all relevant snapshot financial, FX, classification, and position quality evidence must be COMPLETE",
                }
            )
        if gaps:
            expected_reasons.append(
                {
                    "code": "SNAPSHOT_CADENCE_GAP",
                    "scope": "snapshot_interval",
                    "detail": "one or more valuation gaps exceed the documented 36-hour cadence",
                }
            )
        if not expected_start_covered or not expected_end_covered:
            expected_reasons.append(
                {
                    "code": "ENDPOINT_DATE_COVERAGE_INCOMPLETE",
                    "scope": "snapshot_interval",
                    "detail": "both requested inclusive UTC dates require a snapshot",
                }
            )
        observed_fx_reason = any(
            item["code"] == "FX_PROVENANCE_CHANGED" for item in payload["quality_reasons"]
        )
        if verified_fx_changed is True or (verified_fx_changed is None and observed_fx_reason):
            expected_reasons.append(
                {
                    "code": "FX_PROVENANCE_CHANGED",
                    "scope": "snapshot_interval",
                    "detail": "FX currency-pair source provenance changes inside the period",
                }
            )
        if verified_fx_changed is False and observed_fx_reason:
            fail("FX provenance reason contradicts source snapshots")
        if not valuation_return_inputs_complete:
            if not coverage_complete:
                expected_reasons.append(
                    {
                        "code": "EXTERNAL_FLOW_COVERAGE_INCOMPLETE",
                        "scope": "portfolio_return",
                        "detail": flow_coverage["reason"]
                        or "flow evidence does not cover selected valuation timestamps",
                    }
                )
            else:
                expected_reasons.append(
                    {
                        "code": "VALUATION_COVERAGE_INCOMPLETE",
                        "scope": "portfolio_return",
                        "detail": "TWR requires continuous snapshots and both requested UTC dates",
                    }
                )
        if expected_reconciliation_status == "MISMATCH":
            expected_reasons.append(
                {
                    "code": "PNL_NAV_RECONCILIATION_MISMATCH",
                    "scope": "pnl_reconciliation",
                    "detail": "attributed cumulative P&L deltas do not reconcile to NAV change after verified external flows",
                }
            )
        if payload["mode"] == "RECONSTRUCTED":
            expected_reasons.append(
                {
                    "code": "RECONSTRUCTED_RETURN_UNAVAILABLE",
                    "scope": "portfolio_return",
                    "detail": "reconstructed attribution cannot authorize portfolio return or TWR",
                }
            )
        if coverage["price_analyzed"] == 0 and coverage["price_requested"]:
            expected_reasons.append(
                {
                    "code": "PRICE_SOURCE_UNAVAILABLE",
                    "scope": "price_evidence",
                    "detail": "the audited adjusted-close provider is unavailable; snapshot P&L attribution remains isolated",
                }
            )
        elif coverage["price_omitted"]:
            expected_reasons.append(
                {
                    "code": "PRICE_BUDGET_LIMIT",
                    "scope": "price_evidence",
                    "detail": "price request count was capped by the documented budget",
                }
            )
        if coverage["price_analyzed"] and coverage["price_available"] < coverage["price_analyzed"]:
            expected_reasons.append(
                {
                    "code": "PRICE_DATA_PARTIAL",
                    "scope": "price_evidence",
                    "detail": "one or more bounded adjusted-close histories are unavailable",
                }
            )
        if contributions and coverage["price_aligned"] < sum(
            item["evidence"].get("trajectory") is not None for item in exact
        ):
            expected_reasons.append(
                {
                    "code": "PRICE_ALIGNMENT_INCOMPLETE",
                    "scope": "price_evidence",
                    "detail": "all authoritative contributed symbols require at least two common adjusted-close points",
                }
            )
        if payload["quality_reasons"] != expected_reasons:
            fail("quality reasons do not reconstruct from report evidence")
        if payload["quality_status"] != (
            "COMPLETE" if not payload["quality_reasons"] else "PARTIAL"
        ):
            fail("quality status does not match structured reasons")

        canonical = dict(payload)
        canonical.pop("attribution_id")
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:20]
        if payload["attribution_id"] != f"pa-{digest}":
            fail("attribution ID does not bind the complete report body")
    except ReportSemanticError:
        raise
    except (KeyError, InvalidOperation, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ReportSemanticError(f"performance semantic validation failed: {exc}") from exc


def validate_trade_review_document(
    payload: dict[str, Any],
    *,
    max_validation_operations: int = 3_000_000,
    max_validation_seconds: float = 30.0,
    validation_clock: Callable[[], float] = time.monotonic,
) -> None:
    if max_validation_operations <= 0 or max_validation_seconds <= 0:
        raise ReportSemanticError("validation budgets must be positive")
    _preflight_trade_review_shape(payload)
    validation_started = validation_clock()
    remaining_operations = max_validation_operations

    def validation_consume(operations: int = 1) -> None:
        nonlocal remaining_operations
        remaining_operations -= operations
        if (
            remaining_operations < 0
            or validation_clock() - validation_started > max_validation_seconds
        ):
            raise ReportSemanticError("VALIDATION_BUDGET_EXCEEDED")

    bounded_arrays = {name: limit for name, limit in _TRADE_ARRAY_LIMITS.items() if "." not in name}
    if any(
        isinstance(payload.get(name), list) and len(payload[name]) > limit
        for name, limit in bounded_arrays.items()
    ) or (
        isinstance(payload.get("normalized_orders"), list)
        and isinstance(payload.get("normalized_fills"), list)
        and len(payload["normalized_orders"]) + len(payload["normalized_fills"]) > 20_000
    ):
        raise ReportSemanticError("VALIDATION_ROW_LIMIT_EXCEEDED")
    _validate(payload, "trade_review.schema.json")
    validation_consume(
        sum(len(payload[name]) for name in bounded_arrays if isinstance(payload.get(name), list))
    )
    try:
        identity_payload = dict(payload)
        observed_review_id = identity_payload.pop("review_id")
        identity_body = json.dumps(identity_payload, sort_keys=True, separators=(",", ":"))
        expected_review_id = "tr_" + hashlib.sha256(identity_body.encode()).hexdigest()[:16]
        if observed_review_id != expected_review_id:
            raise ValueError("review identity does not match the complete report body")
        as_of = datetime.fromisoformat(payload["as_of"].replace("Z", "+00:00"))
        if payload["source_snapshot_as_of"] is not None:
            datetime.fromisoformat(payload["source_snapshot_as_of"].replace("Z", "+00:00"))
        start = datetime.fromisoformat(payload["start"]).date()
        end = datetime.fromisoformat(payload["end"]).date()
        if start > end:
            raise ValueError("period is reversed")
        metrics = payload["metrics"]
        groups = payload["order_groups"]
        trips = payload["round_trips"]
        fills = [item for item in payload["normalized_fills"] if item["event_type"] == "TRADE"]
        orders = payload["normalized_orders"]
        coverage = payload["coverage"]
        validation_consume(len(fills) + len(orders) + len(groups) + len(trips))
        if payload["base_currency"] is not None and any(
            fill["currency"] == payload["base_currency"]
            and fill["fx_to_base"] is not None
            and Decimal(fill["fx_to_base"]) != Decimal(1)
            for fill in payload["normalized_fills"]
        ):
            raise ValueError("base-currency fill FX must equal one")
        if (coverage["source_snapshot_id"] is None) != (payload["source_snapshot_as_of"] is None):
            raise ValueError("source snapshot identity and as-of must be supplied together")
        if coverage["orders_received"] != len(orders) or coverage["deals_received"] != len(
            payload["normalized_fills"]
        ):
            raise ValueError("source row coverage does not match normalized evidence")

        def context_key(item: dict[str, Any]) -> tuple[str, str]:
            return item["account_ref"], item["market"]

        requested_contexts = {context_key(item) for item in coverage["requested_contexts"]}
        exchange_zones = {
            "US": "America/New_York",
            "HK": "Asia/Hong_Kong",
            "CN": "Asia/Shanghai",
            "SG": "Asia/Singapore",
            "MY": "Asia/Kuala_Lumpur",
            "JP": "Asia/Tokyo",
            "CA": "America/Toronto",
            "AU": "Australia/Sydney",
        }

        def group_local_datetime(group: dict[str, Any]) -> datetime:
            market = group["identity"].split(".", 1)[0]
            instant = datetime.fromisoformat(group["effective_at"].replace("Z", "+00:00"))
            return instant.astimezone(ZoneInfo(exchange_zones[market]))

        current_session = any(
            as_of.astimezone(ZoneInfo(exchange_zones[market])).date() == end
            for _, market in requested_contexts
        )
        if any(
            as_of.astimezone(ZoneInfo(exchange_zones[market])).date() < end
            for _, market in requested_contexts
        ) or (not requested_contexts and as_of.date() < end):
            raise ValueError("as-of precedes the requested exchange-local period end")
        successful_contexts = {context_key(item) for item in coverage["successful_contexts"]}
        failed_contexts = {context_key(item) for item in coverage["failed_contexts"]}
        omitted_contexts = {context_key(item) for item in coverage["omitted_contexts"]}
        for values, rows in (
            (requested_contexts, coverage["requested_contexts"]),
            (successful_contexts, coverage["successful_contexts"]),
            (failed_contexts, coverage["failed_contexts"]),
            (omitted_contexts, coverage["omitted_contexts"]),
        ):
            if len(values) != len(rows):
                raise ValueError("context coverage contains duplicates")
        if (
            successful_contexts & failed_contexts
            or successful_contexts & omitted_contexts
            or failed_contexts & omitted_contexts
            or successful_contexts | failed_contexts | omitted_contexts != requested_contexts
        ):
            raise ValueError("context coverage partitions do not reconcile")
        if (
            coverage["contexts_requested"] != len(requested_contexts)
            or coverage["contexts_read"] != len(successful_contexts)
            or coverage["contexts_failed"] != len(failed_contexts)
            or coverage["contexts_omitted_budget"] != len(omitted_contexts)
        ):
            raise ValueError("context coverage counts do not reconcile")
        requested_accounts = {account for account, _ in requested_contexts}
        covered_accounts = {account for account, _ in successful_contexts}
        if coverage["accounts_requested"] != len(requested_accounts) or coverage[
            "accounts_covered"
        ] != len(covered_accounts):
            raise ValueError("account coverage counts do not reconcile")
        evidenced_contexts = {
            (item["account_ref"], item["market"])
            for item in (*orders, *payload["normalized_fills"])
        }
        if any(
            item["identity"].split(".", 1)[0] != item["market"]
            for item in (*orders, *payload["normalized_fills"])
        ):
            raise ValueError("normalized identity and canonical market do not agree")
        if not evidenced_contexts <= successful_contexts:
            raise ValueError("normalized rows must come from successful contexts")
        source_unavailable = not successful_contexts
        observed_quality_codes = [item["code"] for item in payload["quality_issues"]]
        if len(observed_quality_codes) != len(set(observed_quality_codes)):
            raise ValueError("quality issue codes must be unique")
        external_source_evidence = payload["external_source_evidence"]
        if external_source_evidence != canonical_external_source_evidence(
            item["detail"] for item in external_source_evidence
        ):
            raise ValueError("external source evidence is not canonical")
        if source_unavailable:
            required_codes = {"SOURCE_UNAVAILABLE", "SOURCE_HISTORY_ROW_CAP"}
            policy = payload["policy"]
            if policy is None or policy["reentry_cooldown_minutes"] is None:
                required_codes.add("REENTRY_POLICY_UNCONFIGURED")
            if policy is None or (
                policy["market_open_no_trade_minutes"] is None
                and not policy["market_open_no_trade_minutes_by_market"]
            ):
                required_codes.add("OPEN_WINDOW_POLICY_UNCONFIGURED")
            if coverage["source_snapshot_id"] is not None:
                required_codes.add("TURNOVER_SNAPSHOT_NAV")
            if failed_contexts:
                required_codes.add("CONTEXT_FAILED")
            if omitted_contexts:
                required_codes.add("CONTEXT_OMITTED_BUDGET")
            if current_session:
                required_codes.add("CURRENT_SESSION_INCOMPLETE")
            if external_source_evidence:
                required_codes.add("EXTERNAL_SOURCE_NOTE")
            if coverage["corporate_action_evidence_status"] == "UNSUPPORTED_EVIDENCE":
                required_codes.add("CORPORATE_ACTION_EVIDENCE_UNSUPPORTED")
            if coverage["excluded_nontrade_events"]:
                required_codes.add("NONTRADE_EVENTS_EXCLUDED")
            if (
                any(value is not None for value in metrics.values())
                or orders
                or payload["normalized_fills"]
                or groups
                or trips
                or payload["open_lots"]
                or payload["policy_checks"]
                or payload["behavior_findings"]
                or payload["policy_status"] != "UNCONFIGURED"
                or payload["data_quality_status"] != "UNAVAILABLE"
                or not payload["data_quality_notes"]
                or coverage["monetary_base_coverage"] != "PARTIAL"
                or coverage["realized_pl_base_coverage"] != "UNAVAILABLE"
                or any(
                    coverage[name] != 0
                    for name in (
                        "partial_fill_orders",
                        "orders_missing_fills",
                        "orphan_fill_orders",
                        "order_fill_qty_mismatches",
                        "order_status_conflicts",
                        "excluded_nontrade_events",
                    )
                )
                or set(observed_quality_codes) != required_codes
                or payload["quality_issues"] != canonical_quality_issues(required_codes)
                or payload["data_quality_notes"]
                != [item["detail"] for item in canonical_quality_issues(required_codes)]
            ):
                raise ValueError("totally unavailable source must not claim observed activity")
            if (
                payload["motive_status"] != "UNVERIFIED"
                or not payload["unverified_interpretations"]
            ):
                raise ValueError("psychological interpretation must remain explicitly unverified")
            return
        required_observed_metrics = {
            "trade_count",
            "order_count",
            "fill_count",
            "distinct_symbols",
            "notional_by_currency",
            "realized_pl_by_currency",
            "pnl_statistics_by_currency",
            "per_symbol_turnover",
            "closed_trips",
            "same_day_reversals",
            "repeated_symbol_days",
            "add_to_winner_count",
            "add_to_loser_count",
            "cost_proximity_exit_count",
        }
        if payload["data_quality_status"] == "UNAVAILABLE" or any(
            metrics[name] is None for name in required_observed_metrics
        ):
            raise ValueError("available source requires observed activity metrics")
        excluded = sum(item["event_type"] != "TRADE" for item in payload["normalized_fills"])
        if coverage["excluded_nontrade_events"] != excluded:
            raise ValueError("excluded-event coverage does not reconcile")
        if metrics["trade_count"] != len(groups):
            raise ValueError("trade count does not match order groups")
        if metrics["fill_count"] != len(fills) or metrics["fill_count"] != sum(
            item["fill_count"] for item in groups
        ):
            raise ValueError("fill count does not match normalized/grouped fills")
        group_keys = {(item["account_ref"], item["order_id"]) for item in groups}
        if len(group_keys) != len(groups):
            raise ValueError("order groups must have unique account/order identities")
        grouped_fills: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for fill in fills:
            grouped_fills.setdefault((fill["account_ref"], fill["order_id"]), []).append(fill)
        if group_keys != set(grouped_fills):
            raise ValueError("order groups must cover every trade fill exactly once")
        for group in groups:
            validation_consume(
                len(grouped_fills.get((group["account_ref"], group["order_id"]), ())) + 1
            )
            key = (group["account_ref"], group["order_id"])
            related = sorted(
                grouped_fills[key], key=lambda item: (item["executed_at"], item["deal_id"])
            )
            quantity = sum((Decimal(item["quantity"]) for item in related), Decimal(0))
            notional = sum(
                (Decimal(item["quantity"]) * Decimal(item["price"]) for item in related),
                Decimal(0),
            )
            if (
                group["group_ref"] != f"{group['account_ref']}/{group['order_id']}"
                or group["identity"] != related[0]["identity"]
                or group["side"] != related[0]["side"]
                or Decimal(group["quantity"]) != quantity
                or Decimal(group["average_price"]) != notional / quantity
                or group["fill_count"] != len(related)
                or group["effective_at"] != related[0]["executed_at"]
                or group["first_fill_at"] != related[0]["executed_at"]
                or group["last_fill_at"] != related[-1]["executed_at"]
            ):
                raise ValueError("order-group normalization does not reconcile")
        fifo_lots: dict[tuple[str, str], deque[list[Any]]] = {}
        expected_trips: list[dict[str, Any]] = []
        unmatched_sells = 0
        for fill in sorted(fills, key=lambda item: (item["executed_at"], item["deal_id"])):
            validation_consume()
            key = (fill["account_ref"], fill["identity"])
            lots = fifo_lots.setdefault(key, deque())
            quantity = Decimal(fill["quantity"])
            price = Decimal(fill["price"])
            if fill["side"] == "BUY":
                lots.append([quantity, price, fill["executed_at"], fill["currency"]])
                continue
            remaining = quantity
            while remaining and lots:
                validation_consume()
                lot = lots[0]
                matched = min(remaining, lot[0])
                if lot[3] != fill["currency"]:
                    raise ValueError("one security has conflicting FIFO currencies")
                opened = datetime.fromisoformat(lot[2].replace("Z", "+00:00"))
                closed = datetime.fromisoformat(fill["executed_at"].replace("Z", "+00:00"))
                expected_trips.append(
                    {
                        "account_ref": fill["account_ref"],
                        "identity": fill["identity"],
                        "currency": fill["currency"],
                        "quantity": matched,
                        "entry_price": lot[1],
                        "exit_price": price,
                        "opened_at": lot[2],
                        "closed_at": fill["executed_at"],
                        "realized_pl_local": (price - lot[1]) * matched,
                        "realized_pl_base": (
                            (price - lot[1]) * matched
                            if fill["currency"] == payload["base_currency"]
                            else None
                        ),
                        "holding_seconds": Decimal(str((closed - opened).total_seconds())),
                    }
                )
                lot[0] -= matched
                remaining -= matched
                if lot[0] == 0:
                    lots.popleft()
            if remaining:
                unmatched_sells += 1
        expected_open_lots = [
            {
                "account_ref": account,
                "identity": identity,
                "currency": lot[3],
                "quantity": lot[0],
                "price": lot[1],
                "opened_at": lot[2],
            }
            for (account, identity), lots in sorted(fifo_lots.items())
            for lot in lots
            if lot[0]
        ]
        if trips != _json_value(expected_trips) or payload["open_lots"] != _json_value(
            expected_open_lots
        ):
            raise ValueError("FIFO round trips or open lots do not reconcile")
        order_keys = {(item["account_ref"], item["order_id"]) for item in orders}
        fill_order_keys = {(item["account_ref"], item["order_id"]) for item in fills}
        if metrics["order_count"] != len(order_keys | fill_order_keys):
            raise ValueError("order count does not match normalized evidence")
        fills_by_order: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for fill in fills:
            fills_by_order.setdefault((fill["account_ref"], fill["order_id"]), []).append(fill)
        partial = missing = quantity_mismatch = status_conflict = 0
        for order in orders:
            key = (order["account_ref"], order["order_id"])
            related = fills_by_order.get(key, [])
            fill_quantity = sum((Decimal(item["quantity"]) for item in related), Decimal(0))
            quantity = Decimal(order["quantity"])
            dealt = Decimal(order["dealt_quantity"])
            if fill_quantity == 0 and dealt > 0:
                missing += 1
            if Decimal(0) < fill_quantity < quantity:
                partial += 1
            if fill_quantity != dealt:
                quantity_mismatch += 1
            if (
                (order["status"] == "FILLED_ALL" and not dealt == fill_quantity == quantity)
                or (
                    order["status"] in {"FILLED_PART", "CANCELLED_PART"}
                    and not Decimal(0) < dealt == fill_quantity < quantity
                )
                or (
                    order["status"]
                    in {
                        "WAITING_SUBMIT",
                        "SUBMITTING",
                        "SUBMITTED",
                        "CANCELLED_ALL",
                        "FAILED",
                        "DISABLED",
                        "DELETED",
                    }
                    and (dealt != 0 or fill_quantity != 0)
                )
                or any(
                    item["identity"] != order["identity"] or item["side"] != order["side"]
                    for item in related
                )
            ):
                status_conflict += 1
        expected_reconciliation = {
            "partial_fill_orders": partial,
            "orders_missing_fills": missing,
            "orphan_fill_orders": len(fill_order_keys - order_keys),
            "order_fill_qty_mismatches": quantity_mismatch,
            "order_status_conflicts": status_conflict,
        }
        if any(coverage[name] != value for name, value in expected_reconciliation.items()):
            raise ValueError("order/fill reconciliation coverage is inconsistent")
        identities = {item["identity"] for item in fills}
        if metrics["distinct_symbols"] != len(identities):
            raise ValueError("symbol count does not match normalized fills")
        if metrics["closed_trips"] != len(trips):
            raise ValueError("closed-trip count does not match FIFO evidence")
        holdings = [Decimal(item["holding_seconds"]) for item in trips]
        expected_average_holding = (
            None if not holdings else sum(holdings, Decimal(0)) / len(holdings)
        )
        ordered_holdings = sorted(holdings)
        expected_median_holding = (
            None
            if not ordered_holdings
            else ordered_holdings[len(ordered_holdings) // 2]
            if len(ordered_holdings) % 2
            else (
                ordered_holdings[len(ordered_holdings) // 2 - 1]
                + ordered_holdings[len(ordered_holdings) // 2]
            )
            / 2
        )
        if (
            None
            if metrics["average_holding_seconds"] is None
            else Decimal(metrics["average_holding_seconds"])
        ) != expected_average_holding or (
            None
            if metrics["median_holding_seconds"] is None
            else Decimal(metrics["median_holding_seconds"])
        ) != expected_median_holding:
            raise ValueError("holding-time metrics do not reconcile")
        expected_win_rate = (
            None
            if not trips
            else Decimal(sum(Decimal(item["realized_pl_local"]) > 0 for item in trips)) / len(trips)
        )
        observed_win_rate = None if metrics["win_rate"] is None else Decimal(metrics["win_rate"])
        if observed_win_rate != expected_win_rate:
            raise ValueError("win rate does not match closed FIFO trips")
        trade_count = metrics["trade_count"]
        chronological_groups = sorted(
            groups, key=lambda item: (item["effective_at"], item["group_ref"])
        )
        groups_by_security: dict[tuple[str, str], list[dict[str, Any]]] = {}
        activity: dict[tuple[str, str, str], int] = {}
        for group in chronological_groups:
            validation_consume()
            groups_by_security.setdefault((group["account_ref"], group["identity"]), []).append(
                group
            )
            day_key = (
                group["account_ref"],
                group["identity"],
                group_local_datetime(group).date().isoformat(),
            )
            activity[day_key] = activity.get(day_key, 0) + 1
        reversal_pairs = [
            (left, right)
            for items in groups_by_security.values()
            for left, right in zip(items, items[1:])
            if group_local_datetime(left).date() == group_local_datetime(right).date()
            and left["side"] != right["side"]
        ]
        expected_reversals = len(reversal_pairs)
        add_winner = add_loser = 0
        add_winner_refs: list[str] = []
        add_loser_refs: list[str] = []
        inventory_cost: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
        for group in chronological_groups:
            validation_consume()
            key = (group["account_ref"], group["identity"])
            quantity, cost = inventory_cost.get(key, (Decimal(0), Decimal(0)))
            group_quantity = Decimal(group["quantity"])
            group_price = Decimal(group["average_price"])
            if group["side"] == "BUY":
                if quantity > 0:
                    average = cost / quantity
                    add_winner += group_price > average
                    add_loser += group_price < average
                    if group_price > average:
                        add_winner_refs.append(group["group_ref"])
                    elif group_price < average:
                        add_loser_refs.append(group["group_ref"])
                inventory_cost[key] = (
                    quantity + group_quantity,
                    cost + group_quantity * group_price,
                )
            elif quantity > 0:
                matched = min(quantity, group_quantity)
                inventory_cost[key] = (quantity - matched, cost - cost / quantity * matched)
        if (
            metrics["same_day_reversals"] != expected_reversals
            or metrics["repeated_symbol_days"] != sum(count > 1 for count in activity.values())
            or metrics["add_to_winner_count"] != add_winner
            or metrics["add_to_loser_count"] != add_loser
        ):
            raise ValueError("observed behavior metrics do not reconcile")
        expected_reversal_rate = (
            None if not trade_count else Decimal(metrics["same_day_reversals"]) / trade_count
        )
        observed_reversal_rate = (
            None
            if metrics["same_day_reversal_rate"] is None
            else Decimal(metrics["same_day_reversal_rate"])
        )
        if expected_reversal_rate != observed_reversal_rate:
            raise ValueError("same-day reversal rate does not reconcile")
        policy = payload["policy"]
        expected_check_names: set[str] = set()
        if policy is not None:
            if (
                policy["market_open_no_trade_minutes"] is not None
                or policy["market_open_no_trade_minutes_by_market"]
            ):
                expected_check_names.add("market_open_no_trade_minutes")
            if policy["reentry_cooldown_minutes"] is not None:
                expected_check_names.add("reentry_cooldown_minutes")
            if policy["max_trades_per_day"] is not None:
                expected_check_names.add("max_trades_per_day")
            if policy["max_round_trips_per_symbol_day"] is not None:
                expected_check_names.add("max_round_trips_per_symbol_day")
            if policy["flag_add_to_loser"]:
                expected_check_names.add("flag_add_to_loser")
            if policy["flag_cost_proximity"]:
                expected_check_names.add("flag_cost_proximity_stop")
        check_names = {item["name"] for item in payload["policy_checks"]}
        if check_names != expected_check_names:
            raise ValueError("policy checks do not match the authoritative policy body")
        open_configured = "market_open_no_trade_minutes" in expected_check_names
        reentry_configured = "reentry_cooldown_minutes" in expected_check_names
        adjacent_groups = [
            pair for items in groups_by_security.values() for pair in zip(items, items[1:])
        ]
        expected_reentry_pairs = [
            pair
            for pair in adjacent_groups
            if reentry_configured
            and pair[0]["side"] == "SELL"
            and pair[1]["side"] == "BUY"
            and datetime.fromisoformat(pair[1]["effective_at"].replace("Z", "+00:00"))
            - datetime.fromisoformat(pair[0]["effective_at"].replace("Z", "+00:00"))
            <= timedelta(minutes=policy["reentry_cooldown_minutes"])
        ]
        market_opens = {
            "US": (9, 30),
            "HK": (9, 30),
            "CN": (9, 30),
            "SG": (9, 0),
            "MY": (9, 0),
            "JP": (9, 0),
            "CA": (9, 30),
            "AU": (10, 0),
        }
        expected_open_groups: list[dict[str, Any]] = []
        if open_configured:
            for group in chronological_groups:
                market = group["identity"].split(".", 1)[0]
                window = policy["market_open_no_trade_minutes_by_market"].get(
                    market, policy["market_open_no_trade_minutes"]
                )
                if window is None:
                    continue
                local = group_local_datetime(group)
                opening = market_opens[market][0] * 60 + market_opens[market][1]
                minute = local.hour * 60 + local.minute
                if opening <= minute < opening + window:
                    expected_open_groups.append(group)
        if metrics["reentries_within_cooldown"] != (
            len(expected_reentry_pairs) if reentry_configured else None
        ):
            raise ValueError("cooldown metric does not reconcile with normalized groups")
        if metrics["open_window_trades"] != (
            len(expected_open_groups) if open_configured else None
        ):
            raise ValueError("open-window metric does not reconcile with normalized groups")
        if not open_configured and metrics["open_window_trade_rate"] is not None:
            raise ValueError("open-window rate must be unavailable without policy evidence")
        if open_configured:
            expected_rate = (
                None if not trade_count else Decimal(metrics["open_window_trades"]) / trade_count
            )
            observed_rate = (
                None
                if metrics["open_window_trade_rate"] is None
                else Decimal(metrics["open_window_trade_rate"])
            )
            if expected_rate != observed_rate:
                raise ValueError("open_window_trade_rate does not reconcile")
        buckets: dict[str, dict[str, Decimal]] = {}
        for fill in fills:
            currency = fill["currency"]
            if currency == "UNKNOWN":
                continue
            bucket = buckets.setdefault(currency, {"buy": Decimal(0), "sell": Decimal(0)})
            bucket[fill["side"].lower()] += Decimal(fill["quantity"]) * Decimal(fill["price"])
        observed_buckets = {
            currency: {side: Decimal(value) for side, value in bucket.items()}
            for currency, bucket in metrics["notional_by_currency"].items()
        }
        if observed_buckets != buckets:
            raise ValueError("local-currency notional buckets do not reconcile")
        realized_buckets: dict[str, Decimal] = {}
        for trip in trips:
            if trip["currency"] != "UNKNOWN":
                realized_buckets[trip["currency"]] = realized_buckets.get(
                    trip["currency"], Decimal(0)
                ) + Decimal(trip["realized_pl_local"])
        if {
            currency: Decimal(value)
            for currency, value in metrics["realized_pl_by_currency"].items()
        } != realized_buckets:
            raise ValueError("local-currency realized P&L buckets do not reconcile")
        local_pools: dict[str, list[Decimal]] = {}
        for trip in trips:
            if trip["currency"] != "UNKNOWN":
                local_pools.setdefault(trip["currency"], []).append(
                    Decimal(trip["realized_pl_local"])
                )
        expected_local_statistics: dict[str, dict[str, Any]] = {}
        for currency, values in sorted(local_pools.items()):
            wins = [value for value in values if value > 0]
            losses = [value for value in values if value < 0]
            win_total = sum(wins, Decimal(0))
            loss_total = -sum(losses, Decimal(0))
            average_win = None if not wins else win_total / len(wins)
            average_loss = None if not losses else -loss_total / len(losses)
            expected_local_statistics[currency] = {
                "closed_trips": len(values),
                "wins": len(wins),
                "losses": len(losses),
                "gross_profit": win_total,
                "gross_loss_absolute": loss_total,
                "average_win": average_win,
                "average_loss": average_loss,
                "payoff_ratio": (
                    None
                    if average_win is None or average_loss is None
                    else average_win / -average_loss
                ),
                "profit_factor": None if loss_total == 0 else win_total / loss_total,
                "expectancy": sum(values, Decimal(0)) / len(values),
            }
        observed_local_statistics = {
            currency: {
                name: value if isinstance(value, int) else None if value is None else Decimal(value)
                for name, value in statistics.items()
            }
            for currency, statistics in metrics["pnl_statistics_by_currency"].items()
        }
        if observed_local_statistics != expected_local_statistics:
            raise ValueError("local-currency P&L statistics do not reconcile")
        base_complete = payload["coverage"]["monetary_base_coverage"] == "COMPLETE"
        pnl_fields = (
            "average_win_base",
            "average_loss_base",
            "payoff_ratio_base",
            "profit_factor_base",
            "expectancy_base",
        )
        if base_complete:
            if (
                payload["base_currency"] is None
                or coverage["source_snapshot_id"] is None
                or payload["source_snapshot_as_of"] is None
                or any(
                    fill["currency"] == "UNKNOWN" or fill["fx_to_base"] is None for fill in fills
                )
            ):
                raise ValueError("complete base coverage lacks currency/FX evidence")
            for side, name in (("BUY", "buy_notional_base"), ("SELL", "sell_notional_base")):
                expected = sum(
                    (
                        Decimal(fill["quantity"])
                        * Decimal(fill["price"])
                        * Decimal(fill["fx_to_base"])
                        for fill in fills
                        if fill["side"] == side
                    ),
                    Decimal(0),
                )
                if Decimal(metrics[name]) != expected:
                    raise ValueError(f"{name} does not reconcile")
        elif metrics["buy_notional_base"] is not None or metrics["sell_notional_base"] is not None:
            raise ValueError("base notional metrics require complete FX evidence")
        expected_pnl_coverage = (
            "UNAVAILABLE"
            if not trips
            else "COMPLETE"
            if all(item["realized_pl_base"] is not None for item in trips)
            else "PARTIAL"
        )
        if coverage["realized_pl_base_coverage"] != expected_pnl_coverage:
            raise ValueError("base realized P&L coverage does not reconcile")
        base_trip_values = [
            Decimal(item["realized_pl_base"])
            for item in trips
            if item["realized_pl_base"] is not None
        ]
        if expected_pnl_coverage == "COMPLETE":
            profits = [value for value in base_trip_values if value > 0]
            losses = [value for value in base_trip_values if value < 0]
            win_total = sum(profits, Decimal(0))
            loss_total = -sum(losses, Decimal(0))
            average_win = None if not profits else win_total / len(profits)
            average_loss = None if not losses else -loss_total / len(losses)
            expected_pnl = {
                "average_win_base": average_win,
                "average_loss_base": average_loss,
                "payoff_ratio_base": (
                    None
                    if average_win is None or average_loss is None
                    else average_win / -average_loss
                ),
                "profit_factor_base": None if loss_total == 0 else win_total / loss_total,
                "expectancy_base": sum(base_trip_values, Decimal(0)) / len(base_trip_values),
            }
        else:
            expected_pnl = {name: None for name in pnl_fields}
        if any(
            (None if metrics[name] is None else Decimal(metrics[name])) != expected
            for name, expected in expected_pnl.items()
        ):
            raise ValueError("base-currency P&L statistics do not reconcile")
        nav = (
            None
            if payload["portfolio_nav_base"] is None
            else Decimal(payload["portfolio_nav_base"])
        )
        if nav is not None and nav <= 0:
            raise ValueError("portfolio NAV must be positive")
        expected_turnover = (
            None
            if not base_complete or nav is None
            else (Decimal(metrics["buy_notional_base"]) + Decimal(metrics["sell_notional_base"]))
            / (Decimal(2) * nav)
        )
        observed_turnover = (
            None
            if metrics["portfolio_turnover"] is None
            else Decimal(metrics["portfolio_turnover"])
        )
        if observed_turnover != expected_turnover:
            raise ValueError("portfolio turnover does not reconcile")
        symbol_notional: dict[str, Decimal] = {}
        if base_complete:
            for fill in fills:
                identity = fill["identity"]
                symbol_notional[identity] = symbol_notional.get(identity, Decimal(0)) + (
                    Decimal(fill["quantity"]) * Decimal(fill["price"]) * Decimal(fill["fx_to_base"])
                )
        expected_symbol_turnover = (
            {}
            if not base_complete or nav is None
            else {
                identity: value / (Decimal(2) * nav)
                for identity, value in sorted(symbol_notional.items())
            }
        )
        if {
            identity: Decimal(value) for identity, value in metrics["per_symbol_turnover"].items()
        } != expected_symbol_turnover:
            raise ValueError("per-symbol turnover does not reconcile")
        checks = payload["policy_checks"]
        if len({item["name"] for item in checks}) != len(checks):
            raise ValueError("policy checks must have unique names")
        per_account_day: dict[tuple[str, str], int] = {}
        completed_inventory_days: dict[tuple[str, str, str], int] = {}
        policy_inventory: dict[tuple[str, str], Decimal] = {}
        for group in chronological_groups:
            day = group_local_datetime(group).date().isoformat()
            account_day = (group["account_ref"], day)
            per_account_day[account_day] = per_account_day.get(account_day, 0) + 1
            key = (group["account_ref"], group["identity"])
            current = policy_inventory.get(key, Decimal(0))
            quantity = Decimal(group["quantity"])
            if group["side"] == "BUY":
                policy_inventory[key] = current + quantity
            elif current > 0:
                remaining = current - min(current, quantity)
                policy_inventory[key] = remaining
                if remaining == 0:
                    trip_key = (group["account_ref"], group["identity"], day)
                    completed_inventory_days[trip_key] = (
                        completed_inventory_days.get(trip_key, 0) + 1
                    )
        expected_policy_observed = {
            "max_trades_per_day": max(per_account_day.values(), default=0),
            "max_round_trips_per_symbol_day": max(completed_inventory_days.values(), default=0),
            "flag_add_to_loser": add_loser,
            "flag_cost_proximity_stop": metrics["cost_proximity_exit_count"],
            "market_open_no_trade_minutes": metrics["open_window_trades"],
            "reentry_cooldown_minutes": metrics["reentries_within_cooldown"],
        }
        expected_policy_limits = {
            "max_trades_per_day": None if policy is None else policy["max_trades_per_day"],
            "max_round_trips_per_symbol_day": (
                None if policy is None else policy["max_round_trips_per_symbol_day"]
            ),
            "flag_add_to_loser": 0,
            "flag_cost_proximity_stop": 0,
            "market_open_no_trade_minutes": 0,
            "reentry_cooldown_minutes": 0,
        }
        for check in checks:
            if (
                check["observed"] != expected_policy_observed[check["name"]]
                or check["limit"] != expected_policy_limits[check["name"]]
                or check["status"] != ("BREACH" if check["observed"] > check["limit"] else "PASS")
            ):
                raise ValueError("policy observation does not reconcile")
        expected_policy = (
            "BREACH"
            if any(item["status"] == "BREACH" for item in checks)
            else "PASS"
            if checks
            else "UNCONFIGURED"
        )
        if payload["policy_status"] != expected_policy:
            raise ValueError("policy status does not match checks")
        events_by_id = {item["group_ref"]: item for item in groups}
        check_status = {item["name"]: item["status"] for item in checks}
        expected_findings: list[dict[str, Any]] = []

        def add_expected_finding(
            kind: str,
            count: int,
            event_rows: list[dict[str, Any]],
            *,
            breached: bool = False,
        ) -> None:
            if count <= 0 or not event_rows:
                return
            ordered = sorted(
                {item["group_ref"]: item for item in event_rows}.values(),
                key=lambda item: (item["effective_at"], item["group_ref"]),
            )
            expected_findings.append(
                {
                    "type": kind,
                    "severity": "MEDIUM" if breached else "INFORMATIONAL",
                    "evidence": f"Observed {count} qualifying events in normalized execution evidence.",
                    "verified": True,
                    "affected_events": [item["group_ref"] for item in ordered],
                    "affected_symbols": sorted({item["identity"] for item in ordered}),
                    "period_start": ordered[0]["effective_at"],
                    "period_end": ordered[-1]["effective_at"],
                }
            )

        add_expected_finding(
            "SAME_DAY_REVERSAL",
            len(reversal_pairs),
            [item for pair in reversal_pairs for item in pair],
        )
        observed_reentry_pairs = [
            pair
            for pair in adjacent_groups
            if pair[0]["side"] == "SELL"
            and pair[1]["side"] == "BUY"
            and group_local_datetime(pair[0]).date() == group_local_datetime(pair[1]).date()
        ]
        add_expected_finding(
            "SELL_THEN_REENTRY",
            len(observed_reentry_pairs),
            [item for pair in observed_reentry_pairs for item in pair],
        )
        observed_open_groups = []
        for group in chronological_groups:
            market = group["identity"].split(".", 1)[0]
            local = group_local_datetime(group)
            opening = market_opens[market][0] * 60 + market_opens[market][1]
            minute = local.hour * 60 + local.minute
            if opening <= minute < opening + 30:
                observed_open_groups.append(group)
        add_expected_finding(
            "MARKET_OPEN_ACTIVITY", len(observed_open_groups), observed_open_groups
        )
        add_expected_finding(
            "REENTRY_WITHIN_COOLDOWN",
            len(expected_reentry_pairs),
            [item for pair in expected_reentry_pairs for item in pair],
            breached=check_status.get("reentry_cooldown_minutes") == "BREACH",
        )
        add_expected_finding(
            "EARLY_SESSION_TRADING",
            len(expected_open_groups),
            expected_open_groups,
            breached=check_status.get("market_open_no_trade_minutes") == "BREACH",
        )
        repeated_keys = sorted(key for key, count in activity.items() if count > 1)
        groups_by_activity: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for group in chronological_groups:
            activity_key = (
                group["account_ref"],
                group["identity"],
                group_local_datetime(group).date().isoformat(),
            )
            groups_by_activity.setdefault(activity_key, []).append(group)
        for repeated_key in repeated_keys:
            repeated_rows = groups_by_activity[repeated_key]
            repeated_breach = bool(
                policy is not None
                and policy["max_trades_per_day"] is not None
                and per_account_day[(repeated_key[0], repeated_key[2])]
                > policy["max_trades_per_day"]
            )
            add_expected_finding(
                "REPEATED_SYMBOL_ACTIVITY", 1, repeated_rows, breached=repeated_breach
            )
        add_expected_finding(
            "ADD_TO_WINNER", add_winner, [events_by_id[item] for item in add_winner_refs]
        )
        add_expected_finding(
            "ADD_TO_LOSER",
            add_loser,
            [events_by_id[item] for item in add_loser_refs],
            breached=bool(policy is not None and policy["flag_add_to_loser"]),
        )
        if payload["behavior_findings"] != expected_findings:
            raise ValueError("behavior findings do not reconcile with normalized groups")
        if payload["motive_status"] != "UNVERIFIED" or not payload["unverified_interpretations"]:
            raise ValueError("psychological interpretation must remain explicitly unverified")
        required_partial = bool(
            failed_contexts
            or omitted_contexts
            or current_session
            or unmatched_sells
            or any(coverage[name] for name in expected_reconciliation)
            or coverage["corporate_action_evidence_status"] == "UNSUPPORTED_EVIDENCE"
            or (coverage["monetary_base_coverage"] == "PARTIAL" and fills)
            or (coverage["realized_pl_base_coverage"] == "PARTIAL" and trips)
        )
        required_codes: set[str] = {"SOURCE_HISTORY_ROW_CAP"}
        if policy is None or policy["reentry_cooldown_minutes"] is None:
            required_codes.add("REENTRY_POLICY_UNCONFIGURED")
        if policy is None or (
            policy["market_open_no_trade_minutes"] is None
            and not policy["market_open_no_trade_minutes_by_market"]
        ):
            required_codes.add("OPEN_WINDOW_POLICY_UNCONFIGURED")
        if coverage["source_snapshot_id"] is not None:
            required_codes.add("TURNOVER_SNAPSHOT_NAV")
        if failed_contexts:
            required_codes.add("CONTEXT_FAILED")
        if omitted_contexts:
            required_codes.add("CONTEXT_OMITTED_BUDGET")
        if current_session:
            required_codes.add("CURRENT_SESSION_INCOMPLETE")
        if coverage["corporate_action_evidence_status"] == "UNSUPPORTED_EVIDENCE":
            required_codes.add("CORPORATE_ACTION_EVIDENCE_UNSUPPORTED")
        if coverage["excluded_nontrade_events"]:
            required_codes.add("NONTRADE_EVENTS_EXCLUDED")
        for name, code in (
            ("orphan_fill_orders", "ORPHAN_FILL"),
            ("orders_missing_fills", "ORDER_MISSING_FILLS"),
            ("order_fill_qty_mismatches", "ORDER_FILL_QTY_MISMATCH"),
            ("order_status_conflicts", "ORDER_STATUS_CONFLICT"),
        ):
            if coverage[name]:
                required_codes.add(code)
        if unmatched_sells:
            required_codes.add("UNMATCHED_SELL")
        if coverage["monetary_base_coverage"] == "PARTIAL" and fills:
            required_codes.add("BASE_MONETARY_FX_INCOMPLETE")
        if coverage["realized_pl_base_coverage"] == "PARTIAL" and trips:
            required_codes.add("BASE_REALIZED_PNL_FX_INCOMPLETE")
        if trips:
            required_codes.add("FEES_UNAVAILABLE")
        if external_source_evidence:
            required_codes.add("EXTERNAL_SOURCE_NOTE")
        if set(observed_quality_codes) != required_codes:
            raise ValueError("structured quality issue codes do not match evidenced limitations")
        if payload["quality_issues"] != canonical_quality_issues(required_codes):
            raise ValueError("structured quality issue details are not canonical")
        if payload["data_quality_notes"] != [
            item["detail"] for item in canonical_quality_issues(required_codes)
        ]:
            raise ValueError("data quality notes are not canonical")
        if required_partial and not payload["data_quality_notes"]:
            raise ValueError("required source-quality limitations are missing")
        expected_quality = "PARTIAL" if payload["data_quality_notes"] else "COMPLETE"
        if payload["data_quality_status"] != expected_quality:
            raise ValueError("quality status does not match notes")
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise ReportSemanticError(f"trade review semantic validation failed: {exc}") from exc


def validate_rebalance_request_document(payload: dict[str, Any]) -> None:
    _validate(payload, "rebalance_request.schema.json")


def validate_rebalance_result_document(payload: dict[str, Any]) -> None:
    _validate(payload, "rebalance_result.schema.json")
    try:
        # Local import avoids the snapshot_io -> schema module cycle while still
        # applying the complete Phase 1 schema, identity, and financial semantics.
        from .snapshot_io import snapshot_from_document
        from .rebalance_quality import canonical_rebalance_quality_notes

        embedded_snapshot = snapshot_from_document(payload["source_snapshot"])
        snapshot_identity_body = dict(payload["source_snapshot"])
        observed_snapshot_id = snapshot_identity_body.pop("snapshot_id")
        snapshot_digest = hashlib.sha256(
            json.dumps(
                snapshot_identity_body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()[:16]
        snapshot_as_of = datetime.fromisoformat(
            payload["source_snapshot"]["as_of"].replace("Z", "+00:00")
        )
        expected_snapshot_id = (
            f"ps-{snapshot_as_of.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
            f"{payload['source_snapshot']['trd_env'].lower()}-"
            f"{payload['source_snapshot']['base_currency'].lower()}-{snapshot_digest}"
        )
        if observed_snapshot_id != expected_snapshot_id:
            raise ValueError("embedded snapshot content identity does not match complete body")
        if (
            payload["source_snapshot"]["snapshot_id"] != payload["source_snapshot_id"]
            or payload["source_snapshot"]["as_of"] != payload["as_of"]
            or payload["source_snapshot"]["base_currency"] != payload["base_currency"]
        ):
            raise ValueError("embedded source snapshot authority does not match result")
        identity_payload = dict(payload)
        observed_simulation_id = identity_payload.pop("simulation_id")
        expected_simulation_id = (
            "sim_"
            + hashlib.sha256(
                json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()[:16]
        )
        if observed_simulation_id != expected_simulation_id:
            raise ValueError("simulation identity does not match the authoritative result body")
        datetime.fromisoformat(payload["as_of"].replace("Z", "+00:00"))
        before = payload["before"]
        after = payload["after"]
        if (
            before["snapshot_id"] != payload["source_snapshot_id"]
            or after["snapshot_id"] != payload["source_snapshot_id"]
        ):
            raise ValueError("state identities must match the source snapshot")

        def decimal(raw: Any) -> Decimal:
            value = Decimal(raw)
            if not value.is_finite():
                raise ValueError("non-finite decimal")
            return value

        reconciliation_tolerance = Decimal("1e-24")

        def reconciles(actual: Decimal, expected: Decimal) -> bool:
            return (
                abs(actual - expected) <= max(Decimal(1), abs(expected)) * reconciliation_tolerance
            )

        def decimal_maps_reconcile(
            actual: dict[str, Decimal], expected: dict[str, Decimal]
        ) -> bool:
            return set(actual) == set(expected) and all(
                reconciles(actual[key], expected[key]) for key in expected
            )

        def validate_state(state: dict[str, Any]) -> None:
            nav = decimal(state["nav_base"])
            if nav <= 0 or not reconciles(
                decimal(state["cash_weight"]), decimal(state["cash_base"]) / nav
            ):
                raise ValueError("cash weight does not reconcile")
            values = {
                identity: decimal(item["market_value_base"])
                for identity, item in state["positions"].items()
            }
            if sum(values.values(), Decimal(0)) + decimal(state["cash_base"]) != nav:
                raise ValueError("cash plus positions must equal NAV")
            for identity, item in state["positions"].items():
                if not reconciles(decimal(item["weight"]), values[identity] / nav):
                    raise ValueError("position weight does not reconcile")
            long_value = sum((max(value, Decimal(0)) for value in values.values()), Decimal(0))
            short_value = sum((max(-value, Decimal(0)) for value in values.values()), Decimal(0))
            expected_monetary = {
                "long_market_value_base": long_value,
                "short_market_value_base": short_value,
                "gross_exposure_base": long_value + short_value,
                "net_exposure_base": long_value - short_value,
                "long_weight": long_value / nav,
                "short_weight": short_value / nav,
                "gross_weight": (long_value + short_value) / nav,
                "net_weight": (long_value - short_value) / nav,
            }
            if any(
                not reconciles(decimal(state[name]), value)
                for name, value in expected_monetary.items()
            ):
                raise ValueError("long/short/gross/net state values do not reconcile")
            for dimension in ("sector", "region", "theme_full", "theme_split"):
                labels = set().union(
                    *(
                        set(state["exposure_views"][view][dimension])
                        for view in ("long", "short", "gross", "net")
                    ),
                    set(state["exposures"][dimension]),
                )
                for label in labels:
                    long_weight = decimal(
                        state["exposure_views"]["long"][dimension].get(label, "0")
                    )
                    short_weight = decimal(
                        state["exposure_views"]["short"][dimension].get(label, "0")
                    )
                    gross_weight = decimal(
                        state["exposure_views"]["gross"][dimension].get(label, "0")
                    )
                    net_weight = decimal(state["exposure_views"]["net"][dimension].get(label, "0"))
                    if (
                        long_weight < 0
                        or short_weight < 0
                        or not reconciles(gross_weight, long_weight + short_weight)
                        or not reconciles(net_weight, long_weight - short_weight)
                        or not reconciles(
                            decimal(state["exposures"][dimension].get(label, "0")), net_weight
                        )
                    ):
                        raise ValueError("signed exposure views do not reconcile")
                if dimension != "theme_full":
                    expected_totals = {
                        "long": expected_monetary["long_weight"],
                        "short": expected_monetary["short_weight"],
                        "gross": expected_monetary["gross_weight"],
                        "net": expected_monetary["net_weight"],
                    }
                    if any(
                        not reconciles(
                            sum(
                                (
                                    decimal(value)
                                    for value in state["exposure_views"][view][dimension].values()
                                ),
                                Decimal(0),
                            ),
                            total,
                        )
                        for view, total in expected_totals.items()
                    ):
                        raise ValueError("additive signed exposure totals do not reconcile")
            if short_value:
                if state["concentration_status"] != "UNAVAILABLE_SIGNED" or any(
                    state["concentration"][name] is not None
                    for name in ("top_1", "top_3", "top_5", "hhi")
                ):
                    raise ValueError("signed state must not claim long-only concentration")
            else:
                weights = sorted((value / nav for value in values.values()), reverse=True)
                expected = {
                    "top_1": sum(weights[:1], Decimal(0)),
                    "top_3": sum(weights[:3], Decimal(0)),
                    "top_5": sum(weights[:5], Decimal(0)),
                    "hhi": sum((weight * weight for weight in weights), Decimal(0)),
                }
                if state["concentration_status"] != "AVAILABLE" or any(
                    not reconciles(decimal(state["concentration"][name]), value)
                    for name, value in expected.items()
                ):
                    raise ValueError("long-only concentration does not reconcile")

        validate_state(before)
        validate_state(after)
        before_values = {
            key: decimal(value["market_value_base"]) for key, value in before["positions"].items()
        }
        after_values = {
            key: decimal(value["market_value_base"]) for key, value in after["positions"].items()
        }
        before_quantities = {
            key: decimal(value["quantity"]) for key, value in before["positions"].items()
        }
        after_quantities = {
            key: decimal(value["quantity"]) for key, value in after["positions"].items()
        }
        if set(payload["evidence"]) != set(before_values) or any(
            decimal(item["unit_price_local"]) <= 0
            or decimal(item["fx_to_base"]) <= 0
            or item["fx_as_of"] != payload["as_of"]
            for item in payload["evidence"].values()
        ):
            raise ValueError("immutable price/FX evidence does not match source positions")
        for identity, evidence in payload["evidence"].items():
            unit_price = decimal(evidence["unit_price_local"])
            fx = decimal(evidence["fx_to_base"])
            legs = evidence["source_legs"]
            if len({leg["account_ref"] for leg in legs}) != len(legs):
                raise ValueError("source legs must be unique by account and identity")
            leg_quantities = [decimal(leg["quantity"]) for leg in legs]
            leg_values = [decimal(leg["market_value_base"]) for leg in legs]
            if any(
                quantity * unit_price * fx != value
                for quantity, value in zip(leg_quantities, leg_values)
            ):
                raise ValueError("account-level source legs do not reconcile with price/FX")
            nonzero_signs = {value.compare(Decimal(0)) for value in leg_values if value != 0}
            if len(nonzero_signs) > 1:
                raise ValueError("UNSUPPORTED_OFFSETTING_LEGS")
            if (
                sum(leg_quantities, Decimal(0)) != before_quantities[identity]
                or sum(leg_values, Decimal(0)) != before_values[identity]
            ):
                raise ValueError("account-level source legs do not rebuild source state")
            if before_quantities[identity] * unit_price * fx != before_values[identity]:
                raise ValueError("source value does not reconcile with immutable price/FX evidence")
        snapshot_account_refs = {
            account["account_ref"] for account in payload["source_snapshot"]["accounts"]
        }
        if len(snapshot_account_refs) != len(payload["source_snapshot"]["accounts"]):
            raise ValueError("embedded snapshot account aliases must be unique")
        authoritative_legs: dict[str, list[dict[str, Any]]] = {}
        snapshot_positions: dict[str, list[dict[str, Any]]] = {}
        for position in payload["source_snapshot"]["positions"]:
            identity = f"{position['market']}.{position['symbol']}"
            if (
                re.fullmatch(r"[A-Z]{2,4}\.[A-Z0-9]+(?:[.-][A-Z0-9]+)*", identity) is None
                or position["account_ref"] not in snapshot_account_refs
            ):
                raise ValueError("embedded snapshot position identity/account alias is invalid")
            snapshot_positions.setdefault(identity, []).append(position)
            authoritative_legs.setdefault(identity, []).append(
                {
                    "account_ref": position["account_ref"],
                    "identity": identity,
                    "quantity": position["quantity"],
                    "market_value_base": position["market_value_base"],
                    "currency": position["currency"],
                    "fx_to_base": position["fx_to_base"],
                    "sector": position["sector"],
                    "region": position["region"],
                    "themes": position["themes"],
                }
            )
        for legs in authoritative_legs.values():
            legs.sort(key=lambda item: item["account_ref"])
        if set(snapshot_positions) != set(payload["evidence"]):
            raise ValueError("embedded source positions do not match rebalance evidence")
        for identity, positions in snapshot_positions.items():
            evidence = payload["evidence"][identity]
            if evidence["source_legs"] != authoritative_legs[identity]:
                raise ValueError("source legs differ from embedded snapshot positions")
            reference = positions[0]
            if any(
                position[name] != reference[name]
                for position in positions[1:]
                for name in (
                    "sector",
                    "region",
                    "themes",
                    "last_price_local",
                    "currency",
                    "fx_to_base",
                )
            ) or any(
                evidence[name] != reference[name]
                for name in (
                    "sector",
                    "region",
                    "themes",
                    "unit_price_local",
                    "currency",
                    "fx_to_base",
                )
                if name not in {"unit_price_local"}
            ):
                raise ValueError(
                    "classification/price evidence differs from embedded source snapshot"
                )
            if evidence["unit_price_local"] != reference["last_price_local"]:
                raise ValueError("unit price evidence differs from embedded source snapshot")
        embedded_values = {
            identity: sum(
                (decimal(position["market_value_base"]) for position in positions), Decimal(0)
            )
            for identity, positions in snapshot_positions.items()
        }
        embedded_quantities = {
            identity: sum((decimal(position["quantity"]) for position in positions), Decimal(0))
            for identity, positions in snapshot_positions.items()
        }
        if (
            not reconciles(decimal(before["nav_base"]), embedded_snapshot.nav_base)
            or not reconciles(decimal(before["cash_base"]), embedded_snapshot.cash_base)
            or not reconciles(decimal(before["cash_weight"]), embedded_snapshot.cash_weight)
            or not decimal_maps_reconcile(before_values, embedded_values)
            or not decimal_maps_reconcile(before_quantities, embedded_quantities)
        ):
            raise ValueError("before state does not match the full embedded source snapshot")

        def expected_exposure_views(
            state: dict[str, Any],
        ) -> dict[str, dict[str, dict[str, Decimal]]]:
            nav = decimal(state["nav_base"])
            rebuilt = {
                view: {
                    dimension: {} for dimension in ("sector", "region", "theme_full", "theme_split")
                }
                for view in ("long", "short", "gross", "net")
            }
            for identity, item in state["positions"].items():
                value = decimal(item["market_value_base"])
                weight = value / nav
                evidence = payload["evidence"][identity]
                view_weights = {
                    "long": max(weight, Decimal(0)),
                    "short": max(-weight, Decimal(0)),
                    "gross": abs(weight),
                    "net": weight,
                }
                for dimension, label in (
                    ("sector", evidence["sector"]),
                    ("region", evidence["region"]),
                ):
                    for view, view_weight in view_weights.items():
                        if view_weight:
                            rebuilt[view][dimension][label] = (
                                rebuilt[view][dimension].get(label, Decimal(0)) + view_weight
                            )
                themes = evidence["themes"] or ["UNKNOWN"]
                for theme in themes:
                    for view, view_weight in view_weights.items():
                        if view_weight:
                            rebuilt[view]["theme_full"][theme] = (
                                rebuilt[view]["theme_full"].get(theme, Decimal(0)) + view_weight
                            )
                            rebuilt[view]["theme_split"][theme] = rebuilt[view]["theme_split"].get(
                                theme, Decimal(0)
                            ) + view_weight / len(themes)
            return rebuilt

        for state in (before, after):
            rebuilt = expected_exposure_views(state)
            for view in rebuilt:
                for dimension in rebuilt[view]:
                    observed = {
                        label: decimal(value)
                        for label, value in state["exposure_views"][view][dimension].items()
                    }
                    if not decimal_maps_reconcile(observed, rebuilt[view][dimension]):
                        raise ValueError("classification exposure evidence does not rebuild state")
        deltas: dict[str, Decimal] = {}
        quantity_deltas: dict[str, Decimal] = {}
        for change in payload["changes"]:
            symbol = change["symbol"]
            if symbol in deltas:
                raise ValueError("duplicate changes are forbidden")
            deltas[symbol] = decimal(change["amount_delta_base"])
            quantity_deltas[symbol] = decimal(change["quantity_delta"])
            evidence = payload["evidence"].get(symbol)
            if evidence is None:
                raise ValueError("change lacks immutable price/FX evidence")
            unit_price = decimal(evidence["unit_price_local"])
            fx = decimal(evidence["fx_to_base"])
            if (
                decimal(change["unit_price_local"]) != unit_price
                or decimal(change["fx_to_base"]) != fx
                or not reconciles(quantity_deltas[symbol] * unit_price * fx, deltas[symbol])
            ):
                raise ValueError("change amount/quantity formula does not reconcile")
            allocations = change["leg_allocations"]
            legs = authoritative_legs[symbol]
            if [item["account_ref"] for item in allocations] != [
                item["account_ref"] for item in legs
            ]:
                raise ValueError("operation leg allocation is not deterministic")
            if not reconciles(
                sum((decimal(item["amount_delta_base"]) for item in allocations), Decimal(0)),
                deltas[symbol],
            ) or not reconciles(
                sum((decimal(item["quantity_delta"]) for item in allocations), Decimal(0)),
                quantity_deltas[symbol],
            ):
                raise ValueError("operation leg allocations do not reconcile")
            basis = sum((abs(decimal(item["market_value_base"])) for item in legs), Decimal(0))
            for allocation, leg in zip(allocations[:-1], legs[:-1]):
                weight = (
                    abs(decimal(leg["market_value_base"])) / basis
                    if basis
                    else Decimal(1) / len(legs)
                )
                if not reconciles(
                    decimal(allocation["amount_delta_base"]), deltas[symbol] * weight
                ) or not reconciles(
                    decimal(allocation["quantity_delta"]), quantity_deltas[symbol] * weight
                ):
                    raise ValueError("operation leg allocation weights are not authoritative")
        request = payload["request"]
        if (
            request["snapshot_id"] != payload["source_snapshot_id"]
            or request["base_currency"] != payload["base_currency"]
            or decimal(request["new_cash_base"]) != decimal(payload["constraints"]["new_cash_base"])
            or any(
                request[name] != payload["constraints"][name]
                for name in ("allow_margin", "allow_short", "sell_only", "new_cash_only")
            )
            or len(request["changes"]) != len(payload["changes"])
        ):
            raise ValueError("authoritative request does not match result constraints")
        request_by_symbol = {item["symbol"]: item for item in request["changes"]}
        if len(request_by_symbol) != len(request["changes"]) or set(request_by_symbol) != set(
            deltas
        ):
            raise ValueError("authoritative request changes do not match result changes")
        final_nav = decimal(after["nav_base"])
        for change in payload["changes"]:
            source = request_by_symbol[change["symbol"]]
            if source["operation"] != change["operation"] or decimal(source["value"]) != decimal(
                change["value"]
            ):
                raise ValueError("result operation does not match authoritative request")
            current = before_values.get(change["symbol"], Decimal(0))
            value = decimal(change["value"])
            expected_delta = (
                value * final_nav - current
                if change["operation"] == "TARGET_WEIGHT"
                else value
                if change["operation"] == "AMOUNT_DELTA"
                else value * decimal(change["unit_price_local"]) * decimal(change["fx_to_base"])
            )
            if not reconciles(deltas[change["symbol"]], expected_delta):
                raise ValueError("operation-specific amount formula does not reconcile")
        for symbol in set(before_values) | set(after_values):
            if after_values.get(symbol, Decimal(0)) - before_values.get(
                symbol, Decimal(0)
            ) != deltas.get(symbol, Decimal(0)):
                raise ValueError("position deltas do not reconcile")
            if not reconciles(
                after_quantities.get(symbol, Decimal(0))
                - before_quantities.get(symbol, Decimal(0)),
                quantity_deltas.get(symbol, Decimal(0)),
            ):
                raise ValueError("quantity deltas do not reconcile")
        nav_delta = decimal(after["nav_base"]) - decimal(before["nav_base"])
        constraints = payload["constraints"]
        if nav_delta != decimal(constraints["new_cash_base"]):
            raise ValueError("NAV delta must equal explicitly supplied new cash")
        expected_cash = decimal(before["cash_base"]) + nav_delta - sum(deltas.values(), Decimal(0))
        if decimal(after["cash_base"]) != expected_cash:
            raise ValueError("cash delta does not reconcile")
        before_signed = any(value < 0 for value in before_quantities.values())
        after_signed = any(value < 0 for value in after_quantities.values())
        signed_authority = before_signed or after_signed
        if signed_authority:
            if not constraints["allow_short"]:
                raise ValueError("signed quantities require explicit short authority")
            if (
                payload["risk_changes"]["status"] != "STRUCTURAL_ONLY_SIGNED"
                or payload["risk_changes"]["concentration"] is not None
                or any(
                    payload["risk_changes"][name] is not None
                    for name in ("top_1", "top_3", "top_5", "hhi")
                )
            ):
                raise ValueError("signed risk must not claim long-only concentration")
            if not any("signed" in warning.lower() for warning in payload["warnings"]):
                raise ValueError("signed authority requires an explicit warning")
        else:
            if payload["risk_changes"]["status"] != "STRUCTURAL_ONLY":
                raise ValueError("long-only risk status is inconsistent")
            for name in ("top_1", "top_3", "top_5", "hhi"):
                expected_change = decimal(after["concentration"][name]) - decimal(
                    before["concentration"][name]
                )
                if not reconciles(decimal(payload["risk_changes"][name]), expected_change):
                    raise ValueError("risk delta does not reconcile")
                if not reconciles(
                    decimal(payload["risk_changes"]["concentration"][name]),
                    decimal(after["concentration"][name]),
                ):
                    raise ValueError("after concentration evidence does not reconcile")
        for dimension in ("sector", "region", "theme_full", "theme_split"):
            labels = set(before["exposures"][dimension]) | set(after["exposures"][dimension])
            expected_deltas = {
                label: decimal(after["exposures"][dimension].get(label, "0"))
                - decimal(before["exposures"][dimension].get(label, "0"))
                for label in labels
            }
            observed_deltas = {
                label: decimal(value) for label, value in payload["risk_changes"][dimension].items()
            }
            if not decimal_maps_reconcile(observed_deltas, expected_deltas):
                raise ValueError(f"{dimension} exposure deltas do not reconcile")
        for view in ("long", "short", "gross", "net"):
            for dimension in ("sector", "region", "theme_full", "theme_split"):
                labels = set(before["exposure_views"][view][dimension]) | set(
                    after["exposure_views"][view][dimension]
                )
                expected_deltas = {
                    label: decimal(after["exposure_views"][view][dimension].get(label, "0"))
                    - decimal(before["exposure_views"][view][dimension].get(label, "0"))
                    for label in labels
                }
                observed_deltas = {
                    label: decimal(value)
                    for label, value in payload["risk_changes"]["exposure_views"][view][
                        dimension
                    ].items()
                }
                if not decimal_maps_reconcile(observed_deltas, expected_deltas):
                    raise ValueError("signed exposure-view risk deltas do not reconcile")
        violations = (
            (decimal(after["cash_base"]) < 0 and not constraints["allow_margin"])
            or (constraints["sell_only"] and any(value > 0 for value in deltas.values()))
            or (
                constraints["new_cash_only"]
                and (
                    any(value < 0 for value in deltas.values())
                    or sum((max(value, Decimal(0)) for value in deltas.values()), Decimal(0))
                    > decimal(constraints["new_cash_base"])
                )
            )
        )
        if payload["feasible"] == violations:
            raise ValueError("feasibility does not match declared constraints")
        expected_notes = list(
            canonical_rebalance_quality_notes(embedded_snapshot.data_quality, payload["warnings"])
        )
        if payload["data_quality_notes"] != expected_notes:
            raise ValueError("quality notes do not canonically map embedded source evidence")
        if (
            payload["coverage"]["source_data_quality_status"]
            != embedded_snapshot.data_quality.status
        ):
            raise ValueError("source quality coverage does not match embedded snapshot")
        expected_quality = (
            "PARTIAL"
            if embedded_snapshot.data_quality.status != "COMPLETE" or expected_notes
            else "COMPLETE"
        )
        if payload["data_quality_status"] != expected_quality:
            raise ValueError("quality status does not match warnings")
        if (
            payload["coverage"]["source_data_quality_status"] != "COMPLETE"
            and not payload["data_quality_notes"]
        ):
            raise ValueError("partial source quality requires explicit notes")
        if payload["coverage"]["changes_applied"] != len(payload["changes"]):
            raise ValueError("change coverage does not reconcile")
        if payload["coverage"]["changes_requested"] != len(payload["changes"]):
            raise ValueError("all requested changes must be represented")
        if payload["coverage"]["source_positions"] != len(embedded_snapshot.positions) or payload[
            "coverage"
        ]["hypothetical_positions"] != len(after_values):
            raise ValueError("position population coverage does not reconcile")
        if payload["coverage"]["price_evidence"] != len(payload["evidence"]) or payload["coverage"][
            "fx_evidence"
        ] != len(payload["evidence"]):
            raise ValueError("price/FX evidence coverage does not reconcile")
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise ReportSemanticError(f"rebalance semantic validation failed: {exc}") from exc
