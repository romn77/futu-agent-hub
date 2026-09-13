from dataclasses import replace

import pytest
from jsonschema import ValidationError

from portfolio_analyst.identity import snapshot_identity
from portfolio_analyst.models import DataQuality
from portfolio_analyst.option_risk import analyze_option_risk
from portfolio_analyst.schema import validate_option_risk_document
from test_option_risk import Clock, Provider, _position, _snapshot


def _report(*, missing=(), status="PARTIAL", coverage=None, errors=()):
    source = _snapshot((_position("AAPL260116C00150000"),))
    source = replace(
        source,
        data_quality=DataQuality(
            status,
            missing_fields=missing,
            warnings=("source detail is incomplete",),
            coverage=coverage
            if coverage is not None
            else {
                "accounts_returned": 1,
                "accounts_returned_read": 1,
                "discovery_status": "UNVERIFIED",
            },
            source_errors=errors,
        ),
    )
    source = replace(source, snapshot_id=snapshot_identity(source))
    return analyze_option_risk(source, quote_provider=Provider(), clock=Clock()).to_dict()


@pytest.mark.parametrize(
    "missing",
    [
        (),
        ("classification",),
        ("cash",),
        ("average_cost_local",),
        ("can_sell_quantity",),
        ("last_price_local",),
    ],
)
def test_non_population_gaps_do_not_erase_complete_observed_greeks(missing):
    report = _report(missing=missing)
    delta = report["greek_aggregates"]["delta"]
    assert delta["status"] == "COMPLETE"
    assert delta["net"] == "0.5"
    assert report["provenance"]["source_snapshot"]["data_quality"]["status"] == "PARTIAL"
    validate_option_risk_document(report)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"coverage": {}},
        {"coverage": {"accounts_returned": 2, "accounts_returned_read": 1}},
        {"coverage": {"accounts_returned": 2, "accounts_returned_read": 2}},
        {"coverage": {"accounts_returned": True, "accounts_returned_read": True}},
        {"missing": ("positions",)},
        {"errors": ("selected account unreadable",)},
        {"status": "STALE"},
    ],
)
def test_uncertain_population_or_source_authority_still_blocks_totals(kwargs):
    report = _report(**kwargs)
    assert report["greek_aggregates"]["delta"]["net"] is None
    if kwargs.get("coverage", {}).get("accounts_returned") is True:
        with pytest.raises(ValidationError):
            validate_option_risk_document(report)
    else:
        validate_option_risk_document(report)
