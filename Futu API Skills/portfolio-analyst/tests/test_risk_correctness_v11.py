from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from portfolio_analyst.risk import PortfolioPolicy, analyze_risk
from portfolio_analyst.schema import ReportSemanticError, validate_risk_document
from portfolio_analyst.snapshot_io import snapshot_from_document
from test_risk_analysis import snapshot, reconciled_snapshot


def holdings(rows):
    source = snapshot()
    remaining = Decimal(1000) - sum((Decimal(w) * 1000 for _, w in rows), Decimal(0))
    source = replace(
        source,
        cash_base=remaining,
        cash_balances=(
            replace(source.cash_balances[0], amount_local=remaining, amount_base=remaining),
        ),
    )
    positions = tuple(
        replace(
            source.positions[0],
            symbol=f"S{i}",
            sector=label,
            region=label,
            themes=(label,),
            average_cost_local=Decimal(w) * 1000,
            last_price_local=Decimal(w) * 1000,
            market_value_local=Decimal(w) * 1000,
            market_value_base=Decimal(w) * 1000,
        )
        for i, (label, w) in enumerate(rows)
    )
    result = reconciled_snapshot(source, positions)
    snapshot_from_document(result.to_dict())
    return result


@pytest.mark.parametrize(
    "rows,status,lower,upper",
    [
        ([("UNKNOWN", ".9")], "UNKNOWN", "0", ".9"),
        ([("TECH", ".7"), ("UNKNOWN", ".2")], "BREACHED", ".7", ".9"),
        ([("TECH", ".4"), ("UNKNOWN", ".3")], "UNKNOWN", ".4", ".7"),
        ([("TECH", ".3"), ("HEALTH", ".2"), ("UNKNOWN", ".1")], "WITHIN_LIMIT", ".3", ".4"),
        ([("TECH", ".4"), ("UNKNOWN", ".2")], "WITHIN_LIMIT", ".4", ".6"),
    ],
)
def test_classification_bounds_do_not_treat_unknown_as_a_sector(rows, status, lower, upper):
    policy = PortfolioPolicy(sector=Decimal(".6"), region=Decimal(".6"), theme_split=Decimal(".6"))
    report = analyze_risk(holdings(rows), policy=policy).to_dict()
    assert report["schema_version"] == "1.1"
    for dimension in ("sector", "region", "theme_split"):
        evidence = report["classification_coverage"][dimension]
        assert evidence["status"] == status
        assert Decimal(evidence["lower_bound"]) == Decimal(lower)
        assert Decimal(evidence["upper_bound"]) == Decimal(upper)
        assert report["policy_checks"][dimension]["breached"] is (
            None if status == "UNKNOWN" else status == "BREACHED"
        )
    for finding in report["top_risks"]:
        if finding["type"] in {
            "SECTOR_CONCENTRATION",
            "REGION_CONCENTRATION",
            "THEME_CONCENTRATION",
        }:
            assert "UNKNOWN" not in finding["evidence"].values()
    validate_risk_document(report)


def test_missing_policy_still_reports_unknown_coverage():
    result = analyze_risk(holdings([("UNKNOWN", ".9")])).to_dict()
    assert result["classification_coverage"]["sector"]["status"] == "NOT_CONFIGURED"
    assert result["classification_coverage"]["sector"]["unknown_weight"] == "0.9"
    assert result["policy_checks"] == {}
    assert result["overall_risk"] == "UNRATED"
    validate_risk_document(result)


@pytest.mark.parametrize(
    "rows,expected,hhi",
    [
        ([("TECH", ".1")], "1", "1"),
        ([("TECH", ".25"), ("HEALTH", ".25")], "2", ".5"),
        ([("TECH", ".45"), ("HEALTH", ".45")], "2", ".5"),
        ([], None, None),
    ],
)
def test_holdings_diversification_preserves_nav_weights(rows, expected, hhi):
    source = holdings(rows)
    result = analyze_risk(source).to_dict()
    concentration = result["concentration"]
    assert concentration["effective_positions"] == expected
    assert (
        concentration["holdings_hhi"] is None
        if hhi is None
        else Decimal(concentration["holdings_hhi"]) == Decimal(hhi)
    )
    assert Decimal(concentration["top_5"]) == sum((Decimal(w) for _, w in rows), Decimal(0))
    validate_risk_document(result)


def test_legacy_risk_report_retains_its_original_definition():
    legacy = json.loads((Path(__file__).parent / "fixtures/risk_report_v1_legacy.json").read_text())
    assert legacy["schema_version"] == "1.0"
    assert Decimal(legacy["concentration"]["effective_positions"]) > 3
    validate_risk_document(legacy)


@pytest.mark.parametrize("change", ["effective", "coverage", "status", "downgrade"])
def test_current_report_rejects_tampered_metric_and_classification_evidence(change):
    report = analyze_risk(
        holdings([("UNKNOWN", ".9")]), policy=PortfolioPolicy(sector=Decimal(".6"))
    ).to_dict()
    bad = deepcopy(report)
    if change == "effective":
        bad["concentration"]["effective_positions"] = "100"
    elif change == "coverage":
        bad["classification_coverage"]["sector"]["unknown_weight"] = "0"
    elif change == "status":
        bad["policy_checks"]["sector"]["breached"] = False
    else:
        bad["schema_version"] = "1.0"
    with pytest.raises((ValidationError, ReportSemanticError)):
        validate_risk_document(bad)


def test_new_report_cannot_strip_new_fields_and_claim_legacy_metrics():
    report = analyze_risk(holdings([("TECH", ".1")])).to_dict()
    report["schema_version"] = "1.0"
    del report["classification_coverage"]
    del report["concentration"]["holdings_hhi"]
    with pytest.raises(ReportSemanticError, match="legacy"):
        validate_risk_document(report)


def test_legacy_and_current_portfolio_reports_show_metric_basis():
    from portfolio_analyst.reporting import PortfolioReport, build_portfolio_report
    from portfolio_analyst.schema import validate_portfolio_report_document

    legacy = json.loads(
        (Path(__file__).parent / "fixtures/portfolio_report_v1_legacy.json").read_text()
    )
    validate_portfolio_report_document(legacy)
    assert "legacy NAV inverse-HHI" in PortfolioReport(legacy).to_markdown()
    source = holdings([("UNKNOWN", ".9")])
    current = build_portfolio_report(
        source, analyze_risk(source, policy=PortfolioPolicy(sector=Decimal(".6")))
    )
    validate_portfolio_report_document(current.to_dict())
    assert "holdings weights" in current.to_markdown()
    assert "UNKNOWN" in current.to_markdown()
    assert "classification_coverage" in current.to_dict()


@pytest.mark.parametrize(
    "dimension", ["single_position", "top_3", "top_5", "sector", "region", "theme_split"]
)
def test_cash_only_portfolio_with_configured_limits_is_valid(dimension):
    from portfolio_analyst.reporting import build_portfolio_report
    from portfolio_analyst.schema import validate_portfolio_report_document

    source = holdings([])
    risk = analyze_risk(source, policy=PortfolioPolicy(**{dimension: Decimal(".6")}))
    assert risk.overall_risk == "UNRATED"
    validate_risk_document(risk.to_dict())
    validate_portfolio_report_document(build_portfolio_report(source, risk).to_dict())


@pytest.mark.parametrize("count", [500, 1000])
def test_large_equal_weight_portfolios_validate_without_precision_false_failure(count):
    source = snapshot()
    positions = tuple(
        replace(
            source.positions[0],
            symbol=f"X{i}",
            average_cost_local=Decimal(1),
            last_price_local=Decimal(1),
            market_value_local=Decimal(1),
            market_value_base=Decimal(1),
        )
        for i in range(count)
    )
    source = reconciled_snapshot(source, positions)
    snapshot_from_document(source.to_dict())
    result = analyze_risk(source).to_dict()
    assert abs(Decimal(result["concentration"]["effective_positions"]) - count) < Decimal("1e-24")
    validate_risk_document(result)
