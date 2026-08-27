"""Runtime validation against the published snapshot schema."""

from __future__ import annotations

import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NoReturn

import jsonschema
from referencing import Registry, Resource


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
    registry = Registry().with_resource(definitions["$id"], Resource.from_contents(definitions))
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
        if abs(coefficient) >= high:
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
        magnitude = abs(coefficient)
        expected_band = "HIGH" if magnitude >= high else "MEDIUM" if magnitude >= medium else None
        if pair["band"] != expected_band:
            _semantic_error("pair band must match configured thresholds")
        observed_pairs[key] = pair

    expected_pair_keys = {
        key for key, coefficient in coefficients.items() if abs(coefficient) >= medium
    }
    if set(observed_pairs) != expected_pair_keys:
        _semantic_error("pairs must cover every matrix coefficient at or above medium threshold")

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
    if payload["coverage"]["price_histories"] != correlation["coverage"]:
        _semantic_error("report price-history coverage must match correlation coverage")
    if payload["coverage"]["positions"] != correlation["coverage"]["requested"]:
        _semantic_error("requested histories must match economic-position coverage")


def validate_risk_document(payload: dict[str, Any]) -> None:
    _validate(payload, "risk_report.schema.json")
    _validate_report_semantics(payload)


def validate_portfolio_report_document(payload: dict[str, Any]) -> None:
    _validate(payload, "portfolio_report.schema.json")
    _validate_report_semantics(payload)
