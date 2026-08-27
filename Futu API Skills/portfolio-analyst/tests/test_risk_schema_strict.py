from __future__ import annotations

from copy import deepcopy

import jsonschema
import pytest

from portfolio_analyst.reporting import build_portfolio_report
from portfolio_analyst.risk import analyze_risk
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
