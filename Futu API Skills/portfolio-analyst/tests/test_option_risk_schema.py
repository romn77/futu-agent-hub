from copy import deepcopy
import json
from pathlib import Path

import jsonschema
import pytest

from portfolio_analyst.option_risk import analyze_option_risk
from portfolio_analyst.reporting import build_portfolio_report
from portfolio_analyst.risk import analyze_risk
from portfolio_analyst.schema import ReportSemanticError, validate_option_risk_document

from test_option_risk import Clock, Provider, _position, _snapshot


SCHEMA = Path(__file__).parents[1] / "schemas" / "option_risk.schema.json"


def _payload():
    return analyze_option_risk(
        _snapshot(
            (_position("A260116C1"), _position("B260116P1", quantity="-2", market_value="-50"))
        ),
        quote_provider=Provider(rows={"US.B260116P1": {"option_type": "PUT"}}),
        clock=Clock(),
    ).to_dict()


def test_option_risk_schema_is_draft_202012_strict_and_runtime_valid():
    payload = _payload()
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    jsonschema.Draft202012Validator.check_schema(schema)
    validate_option_risk_document(payload)

    payload["unexpected"] = True
    with pytest.raises(jsonschema.ValidationError):
        validate_option_risk_document(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.__setitem__("option_risk_id", "0" * 20),
        lambda body: body.__setitem__("status", "PARTIAL"),
        lambda body: body["coverage"].__setitem__("option_legs", 99),
        lambda body: body["source_population"].__setitem__("gross_market_value_base", "999"),
        lambda body: body["source_population"]["contracts"][0].__setitem__("selection_rank", 2),
        lambda body: body["leg_exposures"][0]["greeks"].__setitem__("delta", "999"),
        lambda body: body["greek_aggregates"]["delta"].__setitem__("net", "999"),
        lambda body: body["expiry_buckets"][0]["dte_by_market"][0].__setitem__("dte", 999),
        lambda body: body["quote_evidence"][0]["request"].__setitem__("action", "SELL"),
        lambda body: body["quote_evidence"][0]["response"]["greeks"].__setitem__("delta", "0.9"),
        lambda body: body["provenance"].__setitem__("greek_unit", "OTHER"),
        lambda body: body["quality_reasons"].append(
            {"code": "FORGED", "scope": "x", "detail": "x"}
        ),
        lambda body: body.__setitem__("source_snapshot_digest", "0" * 64),
        lambda body: body["provenance"]["source_snapshot"].__setitem__("nav_base", "999"),
    ],
)
def test_runtime_rejects_derived_source_quote_and_identity_tampering(mutate):
    payload = deepcopy(_payload())
    mutate(payload)

    with pytest.raises((ReportSemanticError, jsonschema.ValidationError)):
        validate_option_risk_document(payload)


def test_comprehensive_report_embeds_and_binds_full_option_report():
    source = _snapshot((_position("A260116C1"),))
    option_report = analyze_option_risk(source, quote_provider=Provider(), clock=Clock())
    portfolio = build_portfolio_report(
        source,
        analyze_risk(source),
        options=option_report,
    ).to_dict()

    assert portfolio["options"] == option_report.to_dict()
    markdown = build_portfolio_report(
        source, analyze_risk(source), options=option_report
    ).to_markdown()
    assert "RAW_CONTRACT_UNITS_NO_MULTIPLIER" in markdown
    from portfolio_analyst.schema import validate_portfolio_report_document

    validate_portfolio_report_document(portfolio)

    forged = deepcopy(portfolio)
    forged["options"]["base_currency"] = "HKD"
    with pytest.raises((ReportSemanticError, jsonschema.ValidationError)):
        validate_portfolio_report_document(forged)


def test_phase6_version_and_packaged_schema_are_0_6_0():
    root = Path(__file__).parents[1]
    project_text = (root / "pyproject.toml").read_text()
    from portfolio_analyst import __version__

    assert __version__ == "0.6.0"
    assert 'version = "0.6.0"' in project_text
    assert "schemas/option_risk.schema.json" in project_text
    assert "version: 0.6.0" in (root / "SKILL.md").read_text()
