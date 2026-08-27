"""Runtime validation against the published snapshot schema."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, NoReturn
from zoneinfo import ZoneInfo

import jsonschema
from referencing import Registry, Resource

from .models import _json_value
from .normalization import canonical_unknown
from .risk import _latest_complete_market_close_date
from .trade_quality import canonical_quality_issues


class ReportSemanticError(ValueError):
    """A structurally valid report violates the risk evidence contract."""


_TRADE_ARRAY_LIMITS = {
    "normalized_orders": 20_000,
    "normalized_fills": 20_000,
    "order_groups": 20_000,
    "round_trips": 20_000,
    "open_lots": 20_000,
    "policy_checks": 6,
    "behavior_findings": 10_007,
    "unverified_interpretations": 16,
    "quality_issues": 19,
    "data_quality_notes": 19,
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
    "": 26,
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
        expected_quality = "PARTIAL" if payload["data_quality_notes"] else "COMPLETE"
        if payload["data_quality_status"] != expected_quality:
            raise ValueError("quality status does not match warnings")
        if not set(payload["warnings"]) <= set(payload["data_quality_notes"]):
            raise ValueError("quality notes must preserve all simulation limitations")
        if (
            payload["coverage"]["source_data_quality_status"] != "COMPLETE"
            and not payload["data_quality_notes"]
        ):
            raise ValueError("partial source quality requires explicit notes")
        if payload["coverage"]["changes_applied"] != len(payload["changes"]):
            raise ValueError("change coverage does not reconcile")
        if payload["coverage"]["changes_requested"] != len(payload["changes"]):
            raise ValueError("all requested changes must be represented")
        if payload["coverage"]["source_positions"] <= 0:
            raise ValueError("source position coverage must be explicit")
        if payload["coverage"]["source_positions"] < len(before_values) or payload["coverage"][
            "hypothetical_positions"
        ] != len(after_values):
            raise ValueError("position population coverage does not reconcile")
        if payload["coverage"]["price_evidence"] != len(payload["evidence"]) or payload["coverage"][
            "fx_evidence"
        ] != len(payload["evidence"]):
            raise ValueError("price/FX evidence coverage does not reconcile")
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise ReportSemanticError(f"rebalance semantic validation failed: {exc}") from exc
