from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import jsonschema
import pytest

from portfolio_analyst.reporting import build_portfolio_report
from portfolio_analyst.risk import PricePoint, analyze_risk
from portfolio_analyst.schema import (
    validate_portfolio_report_document,
    validate_risk_document,
)

from test_risk_analysis import snapshot


def _risk():
    return analyze_risk(snapshot()).to_dict()


def _portfolio():
    source = snapshot()
    risk = analyze_risk(source)
    return build_portfolio_report(source, risk).to_dict()


class _CompletePrices:
    def history(self, symbol, *, market, lookback, as_of):
        del symbol, market, lookback, as_of
        level = Decimal("100")
        points = []
        for offset in range(70):
            level *= Decimal("1.01") if offset % 2 else Decimal("0.99")
            points.append(PricePoint(date(2026, 1, 1) + timedelta(days=offset), level))
        return tuple(points)


def _complete_risk():
    return analyze_risk(snapshot(), price_provider=_CompletePrices()).to_dict()


def _complete_portfolio():
    source = snapshot()
    risk = analyze_risk(source, price_provider=_CompletePrices())
    return build_portfolio_report(source, risk).to_dict()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["correlation"].update({"pairs": [42]}),
        lambda value: value["correlation"].update({"matrix": {"US.AAA": {"US.BBB": "bad"}}}),
        lambda value: value["correlation"].update({"high_threshold": "9"}),
        lambda value: value["correlation"].update(
            {
                "pairs": [
                    {
                        "symbols": ["US.AAA", "US.BBB"],
                        "coefficient": "2",
                        "sample_count": 60,
                        "band": "HIGH",
                    }
                ]
            }
        ),
        lambda value: value["coverage"].update({"positions": "3"}),
        lambda value: value["coverage"].update({"fake": True}),
        lambda value: value["correlation"]["coverage"].update({"fake": 1}),
        lambda value: value.update(
            {
                "policy_status": "CONFIGURED",
                "policy_checks": {
                    "single_position": {
                        "measured": "fake",
                        "threshold": "0.3",
                        "breached": True,
                    }
                },
            }
        ),
        lambda value: value["top_risks"][0].update({"evidence": {"fake": True}}),
        lambda value: value["top_risks"][0].update({"extra": "forbidden"}),
    ],
)
def test_risk_schema_rejects_malformed_nested_structures(mutate):
    original = _risk()
    validate_risk_document(original)
    payload = deepcopy(original)
    mutate(payload)
    with pytest.raises(jsonschema.ValidationError):
        validate_risk_document(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["summary"].update({"fake": True}),
        lambda value: value["summary"].update({"position_count": "3"}),
        lambda value: value["summary"].update({"top_holdings": [{"fake": 1}]}),
        lambda value: value.update({"largest_risks": [42, 42, 42]}),
        lambda value: value.update({"concentration": {"fake": 1}}),
        lambda value: value["correlation"].update({"pairs": [42]}),
        lambda value: value["coverage"].update({"fake": 1}),
        lambda value: value["lifecycle"].update({"extra": True}),
    ],
)
def test_portfolio_schema_rejects_malformed_embedded_structures(mutate):
    original = _portfolio()
    validate_portfolio_report_document(original)
    payload = deepcopy(original)
    mutate(payload)
    with pytest.raises(jsonschema.ValidationError):
        validate_portfolio_report_document(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda correlation: correlation.update(
            {
                "status": "COMPLETE",
                "coverage": {"requested": 3, "available": 0},
                "matrix": {},
                "pairs": [],
                "clusters": [],
            }
        ),
        lambda correlation: correlation.update(
            {"medium_threshold": "0.8", "high_threshold": "0.7"}
        ),
        lambda correlation: correlation.update({"minimum_observations": correlation["lookback"]}),
        lambda correlation: correlation["coverage"].update({"available": 4}),
        lambda correlation: correlation["matrix"]["US.AAA"].update({"US.BBB": "0.5"}),
        lambda correlation: correlation["matrix"]["US.AAA"].update({"US.AAA": "0.9"}),
        lambda correlation: correlation["pairs"][0].update({"band": "MEDIUM"}),
        lambda correlation: correlation["pairs"][0].update({"sample_count": 120}),
        lambda correlation: correlation["pairs"][0].update({"coefficient": "0.8"}),
        lambda correlation: correlation["pairs"][0].update({"symbols": ["US.AAA", "US.AAA"]}),
        lambda correlation: correlation.update({"clusters": [["US.AAA", "US.BBB"]]}),
        lambda correlation: correlation.update(
            {"clusters": [correlation["clusters"][0], list(reversed(correlation["clusters"][0]))]}
        ),
    ],
)
def test_risk_runtime_validation_rejects_semantically_fake_correlation(mutate):
    payload = _complete_risk()
    validate_risk_document(payload)
    mutate(payload["correlation"])

    with pytest.raises(ValueError, match="correlation semantic"):
        validate_risk_document(payload)


def test_portfolio_runtime_validation_rejects_semantically_fake_embedded_correlation():
    payload = _complete_portfolio()
    validate_portfolio_report_document(payload)
    payload["correlation"]["matrix"]["US.AAA"]["US.BBB"] = "0.5"

    with pytest.raises(ValueError, match="correlation semantic"):
        validate_portfolio_report_document(payload)


def test_runtime_validation_rejects_rated_finding_without_matching_policy_check():
    payload = _risk()
    payload["policy_status"] = "CONFIGURED"
    payload["top_risks"][0]["severity"] = "HIGH"
    payload["overall_risk"] = "HIGH"

    with pytest.raises(ValueError, match="policy evidence"):
        validate_risk_document(payload)


def test_runtime_validation_rejects_breached_policy_candidate_omitted_for_unrated_finding():
    source = snapshot()
    positions = tuple(replace(position, position_role="OFFENSIVE") for position in source.positions)
    payload = analyze_risk(replace(source, positions=positions)).to_dict()
    assert "THEME_CONCENTRATION" not in {finding["type"] for finding in payload["top_risks"]}
    payload["policy_status"] = "CONFIGURED"
    payload["policy_checks"] = {
        "single_position": {"measured": "0.4", "threshold": "0.3", "breached": True},
        "theme_split": {"measured": "0.5", "threshold": "0.4", "breached": True},
    }
    payload["top_risks"][0]["severity"] = "HIGH"
    payload["top_risks"][0]["evidence"]["policy_threshold_applied"] = True
    payload["overall_risk"] = "HIGH"

    with pytest.raises(ValueError, match="breached policy candidate"):
        validate_risk_document(payload)


def test_pnl_schema_and_semantics_reject_fake_totals_shares_and_partial_coverage():
    source = snapshot()
    positions = (
        replace(
            source.positions[0], unrealized_pl_base=Decimal("90"), realized_pl_base=Decimal("10")
        ),
        replace(
            source.positions[1], unrealized_pl_base=Decimal("-30"), realized_pl_base=Decimal("-20")
        ),
        replace(
            source.positions[2], unrealized_pl_base=Decimal("10"), realized_pl_base=Decimal("0")
        ),
    )
    payload = analyze_risk(replace(source, positions=positions)).to_dict()
    index = next(
        index
        for index, finding in enumerate(payload["top_risks"])
        if finding["type"] == "PNL_CONCENTRATION"
    )

    negative = deepcopy(payload)
    negative["top_risks"][index]["evidence"]["profit_total_base"] = "-1"
    with pytest.raises(jsonschema.ValidationError):
        validate_risk_document(negative)

    missing_share = deepcopy(payload)
    missing_share["top_risks"][index]["evidence"]["largest_loss_share"] = None
    with pytest.raises(ValueError, match="P&L semantic"):
        validate_risk_document(missing_share)

    partial = deepcopy(payload)
    partial["coverage"]["pnl_evidence_status"] = "PARTIAL"
    with pytest.raises(ValueError, match="P&L semantic"):
        validate_risk_document(partial)


def test_runtime_validation_rejects_partial_pnl_with_complete_top_level_quality():
    payload = _complete_risk()
    assert payload["coverage"]["pnl_evidence_status"] == "PARTIAL"
    payload["data_quality_status"] = "COMPLETE"

    with pytest.raises(ValueError, match="P&L semantic"):
        validate_risk_document(payload)


def test_runtime_validation_rejects_noncanonical_or_negative_exposure_evidence():
    sentinel = _risk()
    sentinel["exposures"]["sector"]["weights"]["N/A"] = sentinel["exposures"]["sector"][
        "weights"
    ].pop("TECH")
    with pytest.raises(ValueError, match="exposure semantic"):
        validate_risk_document(sentinel)

    negative = _risk()
    negative["exposures"]["theme_split"]["weights"]["AI"] = "-0.1"
    with pytest.raises(ValueError, match="exposure semantic"):
        validate_risk_document(negative)
