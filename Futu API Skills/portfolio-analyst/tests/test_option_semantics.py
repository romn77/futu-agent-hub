from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from portfolio_analyst.option_risk import _bounded_quality_reasons, analyze_option_risk
from portfolio_analyst.option_metadata import OptionMetadataProviderError
from portfolio_analyst.schema import ReportSemanticError, validate_option_risk_document
from test_option_risk import Clock, Provider, _position, _snapshot


class MetadataProvider:
    def __init__(self, rows):
        self.rows = rows

    def metadata(self, identities, *, timeout):
        assert timeout == 5.0
        return {"data": [self.rows[identity] for identity in identities]}


class FailedMetadataProvider:
    def metadata(self, identities, *, timeout):
        raise OptionMetadataProviderError("OPTION_METADATA_TIMEOUT")


def _metadata(identity, *, owner="US.AAPL", option_type="CALL", strike="150"):
    return {
        "code": identity,
        "lot_size": 100,
        "stock_owner": owner,
        "option_type": option_type,
        "strike_time": "2026-01-16",
        "strike_price": strike,
    }


def _stock(symbol="AAPL", *, account_ref="acct-a", quantity="100", price="151", fx="1"):
    value = Decimal(quantity) * Decimal(price)
    return replace(
        _position(
            symbol,
            account_ref=account_ref,
            security_type="STOCK",
            quantity=quantity,
            market_value=str(value),
        ),
        last_price_local=Decimal(price),
        average_cost_local=Decimal(price),
        fx_to_base=Decimal(fx),
        market_value_base=value * Decimal(fx),
        sector="TECHNOLOGY",
        industry="HARDWARE",
        asset_class="EQUITY",
    )


def test_static_metadata_derives_multiplier_money_delta_atm_and_covered_status():
    option = "US.AAPL260116C00150000"
    source = _snapshot(
        (
            _position(option.removeprefix("US."), quantity="-1", market_value="-200"),
            _stock(quantity="100", price="150.05"),
        )
    )

    report = analyze_option_risk(
        source,
        quote_provider=Provider(rows={option: {"strike_price": "150", "option_type": "CALL"}}),
        metadata_provider=MetadataProvider({option: _metadata(option)}),
        clock=Clock(),
    ).to_dict()
    leg = report["leg_exposures"][0]

    assert report["status"] == "COMPLETE"
    assert leg["contract_multiplier"] == "100"
    assert leg["multiplier_provenance"] == "FUTU_STATIC_INFO_LOT_SIZE"
    assert leg["underlying_identity"] == "US.AAPL"
    assert leg["contract_adjusted_delta_units"] == "-50"
    assert leg["notional_delta_local"] == "-7502.5"
    assert leg["notional_delta_base"] == "-7502.5"
    assert leg["moneyness"] == "ATM"
    assert leg["relationship"] == {"kind": "COVERED_CALL", "status": "FULL"}
    markdown = analyze_option_risk(
        source,
        quote_provider=Provider(rows={option: {"strike_price": "150", "option_type": "CALL"}}),
        metadata_provider=MetadataProvider({option: _metadata(option)}),
        clock=Clock(),
    ).to_markdown()
    assert "## Held option semantics" in markdown
    assert "FUTU_STATIC_INFO_LOT_SIZE" in markdown
    assert "COVERED_CALL / FULL" in markdown


def test_conflicting_static_and_quote_fields_are_suppressed_without_downgrading_greeks():
    option = "US.AAPL260116C00150000"
    source = _snapshot((_position(option.removeprefix("US.")), _stock()))
    quote = Provider(rows={option: {"option_type": "PUT", "strike_price": "151"}})

    report = analyze_option_risk(
        source,
        quote_provider=quote,
        metadata_provider=MetadataProvider({option: _metadata(option)}),
        clock=Clock(),
    ).to_dict()
    leg = report["leg_exposures"][0]

    assert report["status"] == "COMPLETE"
    assert leg["option_type"] is None
    assert leg["strike"] is None
    assert leg["moneyness"] == "UNKNOWN"
    assert leg["relationship"] == {"kind": None, "status": "UNKNOWN"}
    assert {item["code"] for item in report["quality_reasons"]} >= {"OPTION_METADATA_CONFLICT"}


def test_metadata_failure_is_local_and_does_not_overwrite_complete_greek_status():
    source = _snapshot((_position("AAPL260116C00150000"),))

    report = analyze_option_risk(
        source,
        quote_provider=Provider(),
        metadata_provider=FailedMetadataProvider(),
        clock=Clock(),
    ).to_dict()

    assert report["status"] == "COMPLETE"
    assert report["metadata_evidence"]["reason_code"] == "OPTION_METADATA_TIMEOUT"
    assert report["leg_exposures"][0]["contract_multiplier"] is None


def test_missing_or_disagreeing_underlying_rows_keep_money_delta_unknown():
    option = "US.AAPL260116P00150000"
    first = _stock(quantity="50", price="149")
    second = _stock(quantity="50", price="150")
    source = _snapshot((_position(option.removeprefix("US.")), first, second))

    report = analyze_option_risk(
        source,
        quote_provider=Provider(rows={option: {"option_type": "PUT", "strike_price": "150"}}),
        metadata_provider=MetadataProvider({option: _metadata(option, option_type="PUT")}),
        clock=Clock(),
    ).to_dict()
    leg = report["leg_exposures"][0]

    assert leg["notional_delta_local"] is None
    assert leg["notional_delta_base"] is None
    assert leg["moneyness"] == "UNKNOWN"
    assert leg["relationship"] == {"kind": "PROTECTIVE_PUT", "status": "UNKNOWN"}


def test_relationships_aggregate_contract_requirements_once_per_account_underlying():
    call_a = "US.AAPL260116C00150000"
    call_b = "US.AAPL260220C00160000"
    source = _snapshot(
        (
            _position(call_a.removeprefix("US."), quantity="-1", market_value="-100"),
            _position(call_b.removeprefix("US."), quantity="-1", market_value="-100"),
            _stock(quantity="150"),
        )
    )
    metadata = MetadataProvider(
        {call_a: _metadata(call_a), call_b: _metadata(call_b, strike="160")}
    )

    report = analyze_option_risk(
        source, quote_provider=Provider(), metadata_provider=metadata, clock=Clock()
    ).to_dict()

    assert report["underlying_relationships"] == [
        {
            "account_ref": "acct-a",
            "underlying_identity": "US.AAPL",
            "kind": "COVERED_CALL",
            "option_identities": [call_a, call_b],
            "required_underlying_units": "200",
            "available_underlying_units": "150",
            "status": "PARTIAL",
        }
    ]
    assert [leg["relationship"]["status"] for leg in report["leg_exposures"]] == [
        "PARTIAL",
        "PARTIAL",
    ]


def test_relationship_does_not_borrow_underlying_from_another_account():
    option = "US.AAPL260116C00150000"
    source = _snapshot(
        (
            _position(option.removeprefix("US."), quantity="-1", market_value="-100"),
            _stock(account_ref="acct-b", quantity="100"),
        )
    )
    report = analyze_option_risk(
        source,
        quote_provider=Provider(),
        metadata_provider=MetadataProvider({option: _metadata(option)}),
        clock=Clock(),
    ).to_dict()

    assert report["underlying_relationships"][0]["status"] == "NONE"
    assert report["underlying_relationships"][0]["available_underlying_units"] is None
    assert report["leg_exposures"][0]["notional_delta_base"] is None


@pytest.mark.parametrize(
    ("quantity", "option_type", "expected"),
    [("1", "CALL", "NOT_APPLICABLE"), ("-1", "PUT", "NOT_APPLICABLE")],
)
def test_long_call_and_short_put_relationships_are_not_applicable(quantity, option_type, expected):
    option = f"US.AAPL260116{option_type[0]}00150000"
    source = _snapshot(
        (
            _position(option.removeprefix("US."), quantity=quantity, market_value=quantity),
            _stock(),
        )
    )
    report = analyze_option_risk(
        source,
        quote_provider=Provider(rows={option: {"option_type": option_type}}),
        metadata_provider=MetadataProvider({option: _metadata(option, option_type=option_type)}),
        clock=Clock(),
    ).to_dict()

    assert report["leg_exposures"][0]["relationship"]["status"] == expected
    assert report["underlying_relationships"] == []


def test_schema_replay_rejects_tampered_derived_fields_and_metadata_evidence():
    option = "US.AAPL260116C00150000"
    report = analyze_option_risk(
        _snapshot((_position(option.removeprefix("US.")), _stock())),
        quote_provider=Provider(),
        metadata_provider=MetadataProvider({option: _metadata(option)}),
        clock=Clock(),
    ).to_dict()
    validate_option_risk_document(report)

    for mutator in (
        lambda value: value["leg_exposures"][0].__setitem__("contract_multiplier", "200"),
        lambda value: value["metadata_evidence"]["response"]["data"][0].__setitem__(
            "lot_size", 200
        ),
    ):
        tampered = deepcopy(report)
        mutator(tampered)
        with pytest.raises(ReportSemanticError):
            validate_option_risk_document(tampered)


def test_conflict_reasons_are_stably_deduplicated_and_schema_bounded_for_129_accounts():
    option = "US.AAPL260116C00150000"
    positions = tuple(
        _position(
            option.removeprefix("US."),
            account_ref="acct-a" if index == 0 else f"acct-{index:03d}",
            quantity="1",
            market_value="1",
        )
        for index in range(129)
    )
    report = analyze_option_risk(
        _snapshot(positions),
        quote_provider=Provider(rows={option: {"option_type": "PUT"}}),
        metadata_provider=MetadataProvider({option: _metadata(option)}),
        clock=Clock(),
    ).to_dict()

    conflicts = [
        reason
        for reason in report["quality_reasons"]
        if reason["code"] == "OPTION_METADATA_CONFLICT"
    ]
    assert conflicts == [
        {
            "code": "OPTION_METADATA_CONFLICT",
            "scope": option,
            "detail": "quote and static metadata disagree for option_type",
        }
    ]
    assert len(report["quality_reasons"]) <= 128
    validate_option_risk_document(report)


def test_quality_reason_unique_bound_has_deterministic_truncation_marker():
    reasons = [
        {"code": "TEST_REASON", "scope": f"scope-{index:03d}", "detail": "detail"}
        for index in range(200)
    ]

    forward = _bounded_quality_reasons(reasons + reasons[:3])
    reverse = _bounded_quality_reasons(list(reversed(reasons)))

    assert forward == reverse
    assert len(forward) == 128
    marker = next(item for item in forward if item["code"] == "QUALITY_REASONS_TRUNCATED")
    assert marker["detail"] == "73 additional unique quality reasons omitted by deterministic bound"


def test_underlying_binding_builds_one_linear_index(monkeypatch):
    import portfolio_analyst.option_risk as module

    identities = [f"US.OPT{index:02d}" for index in range(30)]
    options = tuple(
        _position(identity.removeprefix("US."), quantity="-1", market_value="-1")
        for identity in identities
    )
    stocks = tuple(_stock(quantity="100", price="150") for _ in range(100))
    source = _snapshot(options + stocks)
    metadata = MetadataProvider({identity: _metadata(identity) for identity in identities})
    original = module._identity
    calls = 0

    def counted(position):
        nonlocal calls
        calls += 1
        return original(position)

    monkeypatch.setattr(module, "_identity", counted)
    report = analyze_option_risk(
        source,
        quote_provider=Provider(),
        metadata_provider=metadata,
        clock=Clock(),
    ).to_dict()

    assert report["underlying_relationships"][0]["status"] == "FULL"
    assert calls <= len(source.positions)


def test_new_reports_are_v11_and_legacy_v10_reports_remain_replayable():
    source = _snapshot((_position("AAPL260116C00150000"),))
    current = analyze_option_risk(source, quote_provider=Provider(), clock=Clock()).to_dict()
    legacy_report = analyze_option_risk(
        source,
        quote_provider=Provider(),
        clock=Clock(),
        report_schema_version="1.0",
    )
    legacy = legacy_report.to_dict()

    assert current["schema_version"] == "1.1"
    assert legacy["schema_version"] == "1.0"
    assert "metadata_evidence" not in legacy
    assert "contract_multiplier" not in legacy["leg_exposures"][0]
    validate_option_risk_document(legacy)
    assert "## Held option semantics" not in legacy_report.to_markdown()
