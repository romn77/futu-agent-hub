from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from portfolio_analyst.identity import snapshot_identity
from portfolio_analyst.models import (
    Account,
    CashBalance,
    DataQuality,
    FxRateEvidence,
    NormalizedPosition,
    PortfolioSnapshot,
)
from portfolio_analyst.option_risk import analyze_option_risk
from portfolio_analyst.option_risk import OptionRiskInputError


AS_OF = datetime(2026, 1, 10, 15, tzinfo=timezone.utc)


def _position(
    symbol: str,
    *,
    account_ref: str = "acct-a",
    market: str = "US",
    security_type: str = "OPTION",
    quantity: str = "1",
    market_value: str = "100",
) -> NormalizedPosition:
    value = Decimal(market_value)
    return NormalizedPosition(
        account_ref=account_ref,
        symbol=symbol,
        name="Opaque security label",
        market=market,
        security_type=security_type,
        currency="USD",
        currency_provenance="AUDITED_SOURCE",
        quantity=Decimal(quantity),
        can_sell_quantity=max(Decimal(quantity), Decimal(0)),
        average_cost_local=abs(value),
        last_price_local=abs(value),
        market_value_local=value,
        unrealized_pl_local=Decimal("0"),
        realized_pl_local=Decimal("0"),
        fx_to_base=Decimal("1"),
        market_value_base=value,
        unrealized_pl_base=Decimal("0"),
        realized_pl_base=Decimal("0"),
        weight=Decimal("0"),
        sector="DERIVATIVES",
        industry="OPTIONS",
        region=market,
        themes=("OPTION",),
        asset_class="DERIVATIVE",
    )


def _snapshot(
    positions: tuple[NormalizedPosition, ...], *, quality: str = "COMPLETE"
) -> PortfolioSnapshot:
    cash = Decimal("1000")
    total = sum((item.market_value_base for item in positions), Decimal(0))
    nav = cash + total
    weighted = tuple(replace(item, weight=item.market_value_base / nav) for item in positions)
    long_value = sum((max(item.market_value_base, Decimal(0)) for item in weighted), Decimal(0))
    short_value = sum((max(-item.market_value_base, Decimal(0)) for item in weighted), Decimal(0))
    accounts = tuple(
        Account(ref, "REAL", "MARGIN", "NORMAL", ("US", "HK"), "ACTIVE")
        for ref in sorted({item.account_ref for item in weighted} or {"acct-a"})
    )
    exposures = {name: {} for name in ("sector", "region", "theme", "asset_class")}
    for item in weighted:
        for dimension, labels in (
            ("sector", (item.sector,)),
            ("region", (item.region,)),
            ("theme", item.themes),
            ("asset_class", (item.asset_class,)),
        ):
            for label in labels:
                exposures[dimension][label] = (
                    exposures[dimension].get(label, Decimal(0)) + item.weight
                )
    value = PortfolioSnapshot(
        schema_version="1.0",
        snapshot_id="",
        as_of=AS_OF,
        source="futuapi",
        source_version="fixture-v1",
        trd_env="REAL",
        base_currency="USD",
        fx_evidence=(FxRateEvidence("USD", "USD", Decimal("1"), "TEST", AS_OF),),
        accounts=accounts,
        cash_balances=(CashBalance("acct-a", "USD", cash, Decimal("1"), cash, cash / nav),),
        positions=weighted,
        nav_base=nav,
        cash_base=cash,
        cash_weight=cash / nav,
        long_exposure_base=long_value,
        short_exposure_base=short_value,
        gross_exposure_base=long_value + short_value,
        net_exposure_base=long_value - short_value,
        exposures=exposures,
        data_quality=DataQuality(quality),
    )
    return replace(value, snapshot_id=snapshot_identity(value))


class Provider:
    def __init__(self, rows=None, failures=None):
        self.rows = rows or {}
        self.failures = failures or {}
        self.calls = []

    def quote(self, identity: str, *, timeout: float):
        self.calls.append((identity, timeout))
        if identity in self.failures:
            raise self.failures[identity]
        row = {
            "code": identity,
            "delta": "0.5",
            "gamma": "0.1",
            "vega": "2",
            "theta": "-0.25",
            "rho": "0.75",
            "option_type": "CALL",
            "expire_time": "2026-01-16",
        }
        row.update(self.rows.get(identity, {}))
        return {"data": [row]}


class Clock:
    def __init__(self, start: datetime = AS_OF + timedelta(hours=1)):
        self.current = start

    def __call__(self):
        result = self.current
        self.current += timedelta(milliseconds=1)
        return result


class SequenceClock:
    def __init__(self, *values):
        self.values = iter(values)

    def __call__(self):
        return next(self.values)


def test_known_nonoption_population_is_not_applicable_without_provider():
    source = _snapshot((_position("AAA", security_type="STOCK"),))

    report = analyze_option_risk(source, quote_provider=None, clock=Clock()).to_dict()

    assert report["status"] == "NOT_APPLICABLE"
    assert report["coverage"]["option_legs"] == 0
    assert report["quote_evidence"] == []


def test_unknown_security_type_prevents_not_applicable():
    source = _snapshot((_position("MYSTERY", security_type="UNKNOWN"),), quality="PARTIAL")

    report = analyze_option_risk(source, quote_provider=None, clock=Clock()).to_dict()

    assert report["status"] == "PARTIAL"
    assert "UNKNOWN_SECURITY_TYPE" in {item["code"] for item in report["quality_reasons"]}


def test_same_contract_across_accounts_is_quoted_once_and_signed_greeks_net():
    identity = "AAPL260116C00150000"
    source = _snapshot(
        (
            _position(identity, account_ref="acct-a", quantity="2", market_value="200"),
            _position(identity, account_ref="acct-b", quantity="-1", market_value="-100"),
        )
    )
    provider = Provider()

    report = analyze_option_risk(source, quote_provider=provider, clock=Clock()).to_dict()

    assert provider.calls == [("US.AAPL260116C00150000", 5.0)]
    assert report["status"] == "COMPLETE"
    assert report["provenance"]["quote_capability"] == "AVAILABLE"
    assert len(report["provenance"]["quote_script_sha256"]) == 64
    assert report["source_population"]["net_market_value_base"] == "100"
    assert report["source_population"]["gross_market_value_base"] == "300"
    assert report["source_population"]["gross_to_nav"] == "0.2727272727272727272727272727"
    assert [leg["account_ref"] for leg in report["leg_exposures"]] == ["acct-a", "acct-b"]
    assert [leg["direction"] for leg in report["leg_exposures"]] == ["LONG", "SHORT"]
    assert report["greek_aggregates"]["delta"] == {
        "unit": "RAW_CONTRACT_UNITS_NO_MULTIPLIER",
        "status": "COMPLETE",
        "net": "0.5",
        "gross_abs": "1.5",
        "coverage": {"available_legs": 2, "total_legs": 2},
    }
    assert report["expiry_buckets"] == [
        {
            "expiry": "2026-01-16",
            "dte_by_market": [{"market": "US", "dte": 6, "expired": False}],
            "leg_count": 2,
            "identities": ["US.AAPL260116C00150000"],
            "net_market_value_base": "100",
            "gross_market_value_base": "300",
        }
    ]


def test_missing_one_greek_is_partial_and_never_publishes_partial_total():
    identity = "AAPL260116P00150000"
    source = _snapshot((_position(identity),))
    provider = Provider(rows={f"US.{identity}": {"rho": None, "option_type": "PUT"}})

    report = analyze_option_risk(source, quote_provider=provider, clock=Clock()).to_dict()

    assert report["status"] == "PARTIAL"
    assert report["greek_aggregates"]["delta"]["net"] == "0.5"
    assert report["greek_aggregates"]["rho"]["status"] == "UNAVAILABLE"
    assert report["greek_aggregates"]["rho"]["net"] is None
    assert report["leg_exposures"][0]["greeks"]["rho"] is None


def test_unsupported_market_is_partial_without_quote_and_preserves_structure():
    source = _snapshot((_position("BAD", market="SG", market_value="250"),))
    provider = Provider()

    report = analyze_option_risk(source, quote_provider=provider, clock=Clock()).to_dict()

    assert provider.calls == []
    assert report["status"] == "PARTIAL"
    assert report["source_population"]["gross_market_value_base"] == "250"
    assert report["coverage"]["unsupported_contracts"] == 1
    assert report["provenance"]["quote_capability"] == "NOT_ACCESSED"
    assert "UNSUPPORTED_MARKET" in {item["code"] for item in report["quality_reasons"]}


def test_future_snapshot_stops_before_quote():
    source = _snapshot((_position("AAPL260116C00150000"),))
    provider = Provider()

    report = analyze_option_risk(
        source, quote_provider=provider, clock=Clock(start=AS_OF - timedelta(seconds=1))
    ).to_dict()

    assert report["status"] == "FUTURE_SNAPSHOT"
    assert provider.calls == []


def test_empty_multirow_and_wrong_identity_are_contract_failures_not_fabricated_zeroes():
    identities = ("AAA260116C1", "BBB260116P1", "CCC260116C1")
    source = _snapshot(tuple(_position(identity) for identity in identities))

    class MalformedProvider:
        def quote(self, identity, *, timeout):
            del timeout
            if ".AAA" in identity:
                return {"data": []}
            if ".BBB" in identity:
                return {"data": [{"code": identity}, {"code": identity}]}
            return {"data": [{"code": "US.WRONG", "delta": "1"}]}

    report = analyze_option_risk(
        source, quote_provider=MalformedProvider(), clock=Clock()
    ).to_dict()

    assert report["status"] == "UNAVAILABLE"
    assert report["coverage"]["quoted_contracts"] == 0
    assert all(item["outcome"] == "FAILED" for item in report["quote_evidence"])
    assert all(item["net"] is None for item in report["greek_aggregates"].values())


def test_three_consecutive_source_failures_open_circuit_for_remaining_contracts():
    source = _snapshot(tuple(_position(f"O{index}") for index in range(5)))
    provider = Provider(failures={f"US.O{index}": TimeoutError() for index in range(5)})

    report = analyze_option_risk(source, quote_provider=provider, clock=Clock()).to_dict()

    assert len(provider.calls) == 3
    assert [item["outcome"] for item in report["quote_evidence"]] == [
        "FAILED",
        "FAILED",
        "FAILED",
        "CIRCUIT_OPEN",
        "CIRCUIT_OPEN",
    ]
    assert "QUOTE_CIRCUIT_OPEN" in {item["code"] for item in report["quality_reasons"]}


def test_quote_budget_selects_top_30_but_structure_covers_all_31_contracts():
    positions = tuple(
        _position(f"O{index:02d}", market_value=str(index + 1)) for index in range(31)
    )
    source = _snapshot(positions)
    provider = Provider()

    report = analyze_option_risk(source, quote_provider=provider, clock=Clock()).to_dict()

    assert len(provider.calls) == 30
    assert provider.calls[0][0] == "US.O30"
    assert provider.calls[-1][0] == "US.O01"
    assert report["coverage"]["option_legs"] == 31
    assert report["coverage"]["budget_omitted_contracts"] == 1
    assert report["source_population"]["gross_market_value_base"] == str(sum(range(1, 32)))
    assert report["greek_aggregates"]["delta"]["coverage"] == {
        "available_legs": 30,
        "total_legs": 31,
    }
    assert report["greek_aggregates"]["delta"]["net"] is None


def test_each_greek_has_independent_coverage_and_nan_does_not_erase_other_greeks():
    source = _snapshot((_position("A"), _position("B")))
    provider = Provider(
        rows={
            "US.A": {"delta": "NaN", "rho": None},
            "US.B": {"gamma": None, "rho": "0.4"},
        }
    )

    report = analyze_option_risk(source, quote_provider=provider, clock=Clock()).to_dict()

    assert report["greek_aggregates"]["delta"]["coverage"]["available_legs"] == 1
    assert report["greek_aggregates"]["gamma"]["coverage"]["available_legs"] == 1
    assert report["greek_aggregates"]["vega"]["coverage"]["available_legs"] == 2
    assert report["greek_aggregates"]["rho"]["coverage"]["available_legs"] == 1
    assert report["greek_aggregates"]["vega"]["net"] == "4"


def test_expired_bucket_uses_snapshot_market_local_calendar_date():
    source = _snapshot((_position("OLD", market="HK"),))
    provider = Provider(rows={"HK.OLD": {"expire_time": "2026-01-09", "option_type": "PUT"}})

    report = analyze_option_risk(source, quote_provider=provider, clock=Clock()).to_dict()

    assert report["expiry_buckets"][0]["dte_by_market"] == [
        {"market": "HK", "dte": -1, "expired": True}
    ]
    assert report["option_type_buckets"][0]["option_type"] == "PUT"


def test_same_expiry_bucket_keeps_market_specific_calendar_dte():
    source = _snapshot((_position("USOPT", market="US"), _position("HKOPT", market="HK")))
    late_as_of = datetime(2026, 1, 10, 23, tzinfo=timezone.utc)
    body = replace(source, as_of=late_as_of, snapshot_id="")
    source = replace(body, snapshot_id=snapshot_identity(body))
    provider = Provider(
        rows={
            "US.USOPT": {"expire_time": "2026-01-16"},
            "HK.HKOPT": {"expire_time": "2026-01-16"},
        }
    )

    report = analyze_option_risk(
        source, quote_provider=provider, clock=Clock(start=late_as_of + timedelta(hours=1))
    ).to_dict()

    assert len(report["expiry_buckets"]) == 1
    assert report["expiry_buckets"][0]["dte_by_market"] == [
        {"market": "HK", "dte": 5, "expired": False},
        {"market": "US", "dte": 6, "expired": False},
    ]


def test_source_position_hard_cap_fails_the_whole_analysis():
    source = _snapshot(())
    source = replace(source, positions=(_position("X"),) * 10_001)

    with pytest.raises(OptionRiskInputError, match="hard cap"):
        analyze_option_risk(source, quote_provider=None, clock=Clock())


def test_total_wall_budget_omits_later_quotes_and_runtime_can_rebuild_it():
    source = _snapshot((_position("A", market_value="2"), _position("B", market_value="1")))
    provider = Provider()
    analysis_now = AS_OF + timedelta(hours=1)
    clock = SequenceClock(
        analysis_now,
        analysis_now + timedelta(seconds=1),
        analysis_now + timedelta(seconds=31),
        analysis_now + timedelta(seconds=32),
    )

    report = analyze_option_risk(source, quote_provider=provider, clock=clock).to_dict()

    assert len(provider.calls) == 1
    assert [item["outcome"] for item in report["quote_evidence"]] == [
        "SUCCESS",
        "WALL_BUDGET_OMITTED",
    ]
    from portfolio_analyst.schema import validate_option_risk_document

    validate_option_risk_document(report)
