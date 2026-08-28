from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

import portfolio_analyst.schema as schema_module
from portfolio_analyst.reporting import build_portfolio_report, portfolio_report_identity
from portfolio_analyst.risk import analyze_risk
from portfolio_analyst.schema import (
    ReportSemanticError,
    validate_portfolio_report_document,
    validate_risk_document,
)

from test_risk_analysis import snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _portfolio() -> dict:
    source = snapshot()
    return build_portfolio_report(source, analyze_risk(source)).to_dict()


def _refresh_portfolio_identity(payload: dict) -> None:
    payload["portfolio_report_id"] = portfolio_report_identity(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["summary"].update({"nav_base": "999"}),
        lambda value: value.update({"data_gaps": ["forged but schema-valid gap"]}),
        lambda value: value.update(
            {"hypothetical_simulation_candidates": ["Hypothetical only: forged candidate"]}
        ),
    ],
)
def test_portfolio_derived_views_are_bound_to_validated_source_authority(mutate):
    payload = _portfolio()
    mutate(payload)
    _refresh_portfolio_identity(payload)

    with pytest.raises(ReportSemanticError):
        validate_portfolio_report_document(payload)


def test_portfolio_identity_is_content_addressed_and_stable():
    first = _portfolio()
    second = deepcopy(first)

    assert first["portfolio_report_id"] == second["portfolio_report_id"]
    assert first["portfolio_report_id"] == portfolio_report_identity(first)
    assert first["portfolio_report_id"].startswith("pr-")
    validate_portfolio_report_document(first)

    forged = deepcopy(first)
    forged["base_currency"] = "HKD"
    with pytest.raises(ReportSemanticError, match="identity"):
        validate_portfolio_report_document(forged)


def test_point_in_time_report_directs_performance_users_to_history_command():
    payload = _portfolio()

    assert payload["performance"] == {"status": "NOT_INCLUDED_USE_PERFORMANCE_COMMAND"}


@pytest.mark.parametrize(
    ("validator", "payload"),
    [
        (validate_risk_document, {"data_quality_notes": ["x"] * 50_001}),
        (
            validate_portfolio_report_document,
            {"data_gaps": ["x"] * 50_001, "options": {}},
        ),
        (validate_risk_document, {"data_quality_notes": ["x" * (1024 * 1024)]}),
        (
            validate_portfolio_report_document,
            {"data_gaps": ["x" * (1024 * 1024)], "options": {}},
        ),
    ],
)
def test_risk_and_portfolio_preflight_rejects_unbounded_inputs_before_schema(
    monkeypatch, validator, payload
):
    def schema_must_not_run(*_args, **_kwargs):
        raise AssertionError("JSON Schema ran before bounded preflight")

    monkeypatch.setattr(schema_module, "_validate", schema_must_not_run)
    with pytest.raises(ReportSemanticError, match="validation .* budget exceeded"):
        validator(payload)


def test_risk_preflight_accepts_published_producer_boundaries():
    payload = {
        "positions": [None] * 10_000,
        "warnings": ["x"] * 10_000,
        "missing_fields": ["x"] * 10_000,
        "source_errors": ["x"] * 10_000,
        "correlation": {
            "matrix": {f"US.S{i}": {f"US.S{j}": None for j in range(200)} for i in range(200)},
            "pairs": [None] * 19_900,
            "inverse_pairs": [None] * 19_900,
        },
    }

    schema_module._preflight_risk_shape(payload)


def test_shared_risk_schemas_bound_every_dynamic_container_and_string():
    for name in (
        "report_defs.schema.json",
        "risk_report.schema.json",
        "portfolio_report.schema.json",
    ):
        schema = json.loads((PROJECT_ROOT / "schemas" / name).read_text(encoding="utf-8"))
        stack = [schema]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert "maxProperties" in node, (name, node)
                if node.get("type") == "array":
                    assert "maxItems" in node, (name, node)
                if node.get("type") == "string" and "const" not in node and "enum" not in node:
                    assert "maxLength" in node, (name, node)
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)


def test_packaging_explicitly_limits_packages_and_exposes_cli_entrypoint():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '[project.scripts]\nportfolio-analyst = "portfolio_analyst.cli:main"' in pyproject
    assert '[tool.setuptools]\npackages = ["portfolio_analyst"]' in pyproject
