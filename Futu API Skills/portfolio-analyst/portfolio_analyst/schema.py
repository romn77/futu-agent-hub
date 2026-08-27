"""Runtime validation against the published snapshot schema."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NoReturn

import jsonschema
from referencing import Registry, Resource

from .normalization import canonical_unknown
from .risk import _latest_complete_market_close_date


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


def validate_trade_review_document(payload: dict[str, Any]) -> None:
    _validate(payload, "trade_review.schema.json")
    try:
        start = datetime.fromisoformat(payload["start"]).date()
        end = datetime.fromisoformat(payload["end"]).date()
        if start > end:
            raise ValueError("period is reversed")
        metrics = payload["metrics"]
        groups = payload["order_groups"]
        trips = payload["round_trips"]
        if metrics["trade_count"] != len(groups):
            raise ValueError("trade count does not match order groups")
        if metrics["fill_count"] != sum(item["fill_count"] for item in groups):
            raise ValueError("fill count does not match grouped fills")
        if metrics["order_count"] < len(groups):
            raise ValueError("order count is smaller than evidenced order groups")
        identities = {item["identity"] for item in groups}
        if metrics["distinct_symbols"] != len(identities):
            raise ValueError("symbol count does not match order groups")
        if metrics["closed_trips"] != len(trips):
            raise ValueError("closed-trip count does not match FIFO evidence")
        expected_win_rate = (
            None
            if not trips
            else Decimal(sum(Decimal(item["realized_pl"]) > 0 for item in trips)) / len(trips)
        )
        observed_win_rate = None if metrics["win_rate"] is None else Decimal(metrics["win_rate"])
        if observed_win_rate != expected_win_rate:
            raise ValueError("win rate does not match closed FIFO trips")
        fill_count = metrics["fill_count"]
        for count_name, rate_name in (
            ("same_day_reversals", "same_day_reversal_rate"),
            ("open_window_trades", "open_window_trade_rate"),
        ):
            expected_rate = None if not fill_count else Decimal(metrics[count_name]) / fill_count
            observed_rate = None if metrics[rate_name] is None else Decimal(metrics[rate_name])
            if expected_rate != observed_rate:
                raise ValueError(f"{rate_name} does not reconcile")
        checks = payload["policy_checks"]
        expected_policy = (
            "BREACH"
            if any(item["status"] == "BREACH" for item in checks)
            else "PASS"
            if checks
            else "UNCONFIGURED"
        )
        if payload["policy_status"] != expected_policy:
            raise ValueError("policy status does not match checks")
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

        def validate_state(state: dict[str, Any]) -> None:
            nav = decimal(state["nav_base"])
            if nav <= 0 or decimal(state["cash_weight"]) != decimal(state["cash_base"]) / nav:
                raise ValueError("cash weight does not reconcile")
            values = {
                identity: decimal(item["market_value_base"])
                for identity, item in state["positions"].items()
            }
            for identity, item in state["positions"].items():
                if decimal(item["weight"]) != values[identity] / nav:
                    raise ValueError("position weight does not reconcile")
            weights = sorted((value / nav for value in values.values()), reverse=True)
            expected = {
                "top_1": sum(weights[:1], Decimal(0)),
                "top_3": sum(weights[:3], Decimal(0)),
                "top_5": sum(weights[:5], Decimal(0)),
                "hhi": sum((weight * weight for weight in weights), Decimal(0)),
            }
            if any(
                decimal(state["concentration"][name]) != value for name, value in expected.items()
            ):
                raise ValueError("concentration does not reconcile")
            position_weight = sum((value / nav for value in values.values()), Decimal(0))
            for dimension in ("sector", "region", "theme_split"):
                if (
                    sum(
                        (decimal(value) for value in state["exposures"][dimension].values()),
                        Decimal(0),
                    )
                    != position_weight
                ):
                    raise ValueError("additive exposure does not reconcile")

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
        deltas: dict[str, Decimal] = {}
        quantity_deltas: dict[str, Decimal] = {}
        for change in payload["changes"]:
            symbol = change["symbol"]
            if symbol in deltas:
                raise ValueError("duplicate changes are forbidden")
            deltas[symbol] = decimal(change["amount_delta_base"])
            quantity_deltas[symbol] = decimal(change["quantity_delta"])
        for symbol in set(before_values) | set(after_values):
            if after_values.get(symbol, Decimal(0)) - before_values.get(
                symbol, Decimal(0)
            ) != deltas.get(symbol, Decimal(0)):
                raise ValueError("position deltas do not reconcile")
            if after_quantities.get(symbol, Decimal(0)) - before_quantities.get(
                symbol, Decimal(0)
            ) != quantity_deltas.get(symbol, Decimal(0)):
                raise ValueError("quantity deltas do not reconcile")
        nav_delta = decimal(after["nav_base"]) - decimal(before["nav_base"])
        constraints = payload["constraints"]
        if nav_delta != decimal(constraints["new_cash"]):
            raise ValueError("NAV delta must equal explicitly supplied new cash")
        expected_cash = decimal(before["cash_base"]) + nav_delta - sum(deltas.values(), Decimal(0))
        if decimal(after["cash_base"]) != expected_cash:
            raise ValueError("cash delta does not reconcile")
        for name in ("top_1", "top_3", "top_5", "hhi"):
            expected_change = decimal(after["concentration"][name]) - decimal(
                before["concentration"][name]
            )
            if decimal(payload["risk_changes"][name]) != expected_change:
                raise ValueError("risk delta does not reconcile")
            if decimal(payload["risk_changes"]["concentration"][name]) != decimal(
                after["concentration"][name]
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
            if observed_deltas != expected_deltas:
                raise ValueError(f"{dimension} exposure deltas do not reconcile")
        violations = (
            (decimal(after["cash_base"]) < 0 and not constraints["allow_margin"])
            or (
                any(value < 0 for value in after_quantities.values())
                and not constraints["allow_short"]
            )
            or (constraints["sell_only"] and any(value > 0 for value in deltas.values()))
            or (
                constraints["new_cash_only"]
                and (
                    any(value < 0 for value in deltas.values())
                    or sum((max(value, Decimal(0)) for value in deltas.values()), Decimal(0))
                    > decimal(constraints["new_cash"])
                )
            )
        )
        if payload["feasible"] == violations:
            raise ValueError("feasibility does not match declared constraints")
        expected_quality = "PARTIAL" if payload["warnings"] else "COMPLETE"
        if payload["data_quality_status"] != expected_quality:
            raise ValueError("quality status does not match warnings")
        signed = any(value < 0 for value in after_quantities.values())
        expected_risk_status = "STRUCTURAL_ONLY_SIGNED" if signed else "STRUCTURAL_ONLY"
        if payload["risk_changes"]["status"] != expected_risk_status:
            raise ValueError("risk status does not match signed-position semantics")
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise ReportSemanticError(f"rebalance semantic validation failed: {exc}") from exc
