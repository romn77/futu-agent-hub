from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import time

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
from portfolio_analyst.futu_adapter import FutuReadError
from portfolio_analyst.option_quote import OptionQuoteProviderError
from portfolio_analyst.snapshot_semantics import SnapshotSemanticError, validate_snapshot_semantics


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


@pytest.mark.parametrize(
    "position",
    [
        replace(_position("A"), position_role="SHORT"),
        replace(_position("A", quantity="-1", market_value="-100"), position_role="LONG"),
        _position("A", quantity="-1", market_value="100"),
    ],
)
def test_snapshot_and_direct_option_analysis_reject_directional_sign_conflicts(position):
    source = _snapshot((position,))

    with pytest.raises(SnapshotSemanticError, match="position.*sign"):
        validate_snapshot_semantics(source)
    with pytest.raises(OptionRiskInputError, match="^OPTION_POSITION_SIGN_INCONSISTENT$"):
        analyze_option_risk(source, quote_provider=Provider(), clock=Clock())


@pytest.mark.parametrize("role", ["CORE", "HEDGE", None])
def test_nondirectional_lifecycle_roles_do_not_override_signed_quantity(role):
    source = _snapshot(
        (replace(_position("A", quantity="-1", market_value="-100"), position_role=role),)
    )

    validate_snapshot_semantics(source)
    report = analyze_option_risk(source, quote_provider=Provider(), clock=Clock()).to_dict()

    assert report["leg_exposures"][0]["direction"] == "SHORT"
    assert report["leg_exposures"][0]["greeks"]["delta"] == "-0.5"


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


def test_unsupported_market_is_unavailable_without_quote_and_preserves_structure():
    source = _snapshot((_position("BAD", market="SG", market_value="250"),))
    provider = Provider()

    report = analyze_option_risk(source, quote_provider=provider, clock=Clock()).to_dict()

    assert provider.calls == []
    assert report["status"] == "UNAVAILABLE"
    assert report["source_population"]["gross_market_value_base"] == "250"
    assert report["coverage"]["unsupported_contracts"] == 1
    assert report["provenance"]["quote_capability"] == "NOT_ACCESSED"
    assert "UNSUPPORTED_MARKET" in {item["code"] for item in report["quality_reasons"]}


def test_future_snapshot_is_rejected_before_quote():
    source = _snapshot((_position("AAPL260116C00150000"),))
    provider = Provider()

    with pytest.raises(OptionRiskInputError, match="^FUTURE_SNAPSHOT$"):
        analyze_option_risk(
            source, quote_provider=provider, clock=Clock(start=AS_OF - timedelta(seconds=1))
        )
    assert provider.calls == []


def test_quote_budget_starts_at_phase6_acquisition_after_earlier_analysis_delay():
    analysis_now = AS_OF + timedelta(hours=1)
    acquisition = analysis_now + timedelta(seconds=31)
    provider = Provider()

    report = analyze_option_risk(
        _snapshot((_position("A"),)),
        quote_provider=provider,
        analysis_now=analysis_now,
        clock=SequenceClock(acquisition, acquisition + timedelta(milliseconds=1)),
    ).to_dict()

    assert provider.calls == [("US.A", 5.0)]
    assert report["quote_acquisition_started_at"] == acquisition.isoformat().replace("+00:00", "Z")
    assert report["quote_evidence"][0]["status"] == "SUCCESS"


def test_no_option_uses_analysis_now_as_acquisition_anchor_without_reading_clock():
    analysis_now = AS_OF + timedelta(hours=1)

    def forbidden_clock():
        raise AssertionError("no option acquisition must not read the clock")

    report = analyze_option_risk(
        _snapshot((_position("A", security_type="STOCK"),)),
        quote_provider=None,
        analysis_now=analysis_now,
        clock=forbidden_clock,
    ).to_dict()

    assert report["quote_acquisition_started_at"] == analysis_now.isoformat().replace("+00:00", "Z")


def test_unavailable_provider_records_the_real_acquisition_anchor():
    analysis_now = AS_OF + timedelta(hours=1)
    acquisition = analysis_now + timedelta(seconds=31)

    report = analyze_option_risk(
        _snapshot((_position("A"),)),
        quote_provider=None,
        analysis_now=analysis_now,
        clock=SequenceClock(acquisition),
    ).to_dict()

    assert report["quote_acquisition_started_at"] == acquisition.isoformat().replace("+00:00", "Z")
    assert (
        report["quote_evidence"][0]["request_started_at"] == report["quote_acquisition_started_at"]
    )


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
    assert all(item["status"] == "FAILURE" for item in report["quote_evidence"])
    assert all(
        item["reason_code"] == "QUOTE_RESPONSE_CONTRACT_INVALID"
        for item in report["quote_evidence"]
    )
    assert all(item["net"] is None for item in report["greek_aggregates"].values())


def test_three_consecutive_source_failures_open_circuit_for_remaining_contracts():
    source = _snapshot(tuple(_position(f"O{index}") for index in range(5)))
    provider = Provider(failures={f"US.O{index}": TimeoutError() for index in range(5)})

    report = analyze_option_risk(source, quote_provider=provider, clock=Clock()).to_dict()

    assert len(provider.calls) == 3
    assert [item["reason_code"] for item in report["quote_evidence"]] == [
        "QUOTE_SOURCE_TIMEOUT",
        "QUOTE_SOURCE_TIMEOUT",
        "QUOTE_SOURCE_TIMEOUT",
        "QUOTE_CIRCUIT_OPEN",
        "QUOTE_CIRCUIT_OPEN",
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


def test_nonfinite_numeric_greek_does_not_erase_other_fields_and_replays():
    report = analyze_option_risk(
        _snapshot((_position("A"),)),
        quote_provider=Provider(rows={"US.A": {"delta": float("nan"), "gamma": "0.25"}}),
        clock=Clock(),
    ).to_dict()

    assert report["leg_exposures"][0]["greeks"]["delta"] is None
    assert report["leg_exposures"][0]["greeks"]["gamma"] == "0.25"
    from portfolio_analyst.schema import validate_option_risk_document

    validate_option_risk_document(report)


@pytest.mark.parametrize("value", ["1e1000", "1e-1000"])
def test_fixed_point_quote_overflow_degrades_fields_and_report_still_replays(value):
    report = analyze_option_risk(
        _snapshot((_position("A"),)),
        quote_provider=Provider(
            rows={
                "US.A": {
                    "delta": value,
                    "gamma": "0.25",
                    "strike_price": value,
                    "implied_volatility": value,
                }
            }
        ),
        clock=Clock(),
    ).to_dict()

    leg = report["leg_exposures"][0]
    assert leg["greeks"]["delta"] is None
    assert leg["greeks"]["gamma"] == "0.25"
    assert leg["strike"] is None
    assert leg["implied_volatility"] is None
    assert report["greek_aggregates"]["delta"]["coverage"] == {
        "available_legs": 0,
        "total_legs": 1,
    }
    from portfolio_analyst.schema import validate_option_risk_document

    validate_option_risk_document(report)


def test_leg_greek_product_overflow_is_field_local_and_replayable():
    report = analyze_option_risk(
        _snapshot((_position("A", quantity="9" * 300),)),
        quote_provider=Provider(rows={"US.A": {"delta": "9" * 256}}),
        clock=Clock(),
    ).to_dict()

    assert report["leg_exposures"][0]["greeks"]["delta"] is None
    assert report["greek_aggregates"]["delta"]["status"] == "UNAVAILABLE"
    assert "GREEK_DERIVED_VALUE_OUT_OF_RANGE" in {
        item["code"] for item in report["quality_reasons"]
    }
    from portfolio_analyst.schema import validate_option_risk_document

    validate_option_risk_document(report)


def test_greek_aggregate_overflow_suppresses_only_the_portfolio_total():
    report = analyze_option_risk(
        _snapshot(
            (
                _position("A", account_ref="acct-a", quantity="6" + "0" * 299),
                _position("A", account_ref="acct-b", quantity="6" + "0" * 299),
            )
        ),
        quote_provider=Provider(rows={"US.A": {"delta": "1" + "0" * 212}}),
        clock=Clock(),
    ).to_dict()

    assert all(len(item["greeks"]["delta"]) <= 512 for item in report["leg_exposures"])
    aggregate = report["greek_aggregates"]["delta"]
    assert aggregate["coverage"] == {"available_legs": 2, "total_legs": 2}
    assert aggregate["status"] == "PARTIAL"
    assert aggregate["net"] is None
    assert aggregate["gross_abs"] is None
    assert "GREEK_DERIVED_VALUE_OUT_OF_RANGE" in {
        item["code"] for item in report["quality_reasons"]
    }
    from portfolio_analyst.schema import validate_option_risk_document

    validate_option_risk_document(report)


def test_structural_decimal_overflow_fails_closed_before_report_serialization():
    source = _snapshot(
        (
            _position("A", market_value="9" * 512),
            _position("B", market_value="9" * 512),
        )
    )

    with pytest.raises(OptionRiskInputError, match="^OPTION_STRUCTURAL_DECIMAL_OUT_OF_RANGE$"):
        analyze_option_risk(source, quote_provider=None, clock=Clock())


def test_quote_evidence_never_serializes_forbidden_pricing_or_probability_fields():
    report = analyze_option_risk(
        _snapshot((_position("A"),)),
        quote_provider=Provider(
            rows={
                "US.A": {
                    "price": "9.99",
                    "bid_price": "9.98",
                    "prob_of_profit": "0.5",
                    "max_profit": "999",
                }
            }
        ),
        clock=Clock(),
    ).to_dict()

    serialized = str(report)
    assert "bid_price" not in serialized
    assert "prob_of_profit" not in serialized
    assert "max_profit" not in serialized
    assert "'price'" not in serialized


def test_implied_volatility_has_independent_nonaggregated_coverage():
    source = _snapshot((_position("A"), _position("B")))
    provider = Provider(rows={"US.A": {"implied_volatility": "0.25"}})

    report = analyze_option_risk(source, quote_provider=provider, clock=Clock()).to_dict()

    assert report["implied_volatility_coverage"] == {
        "status": "PARTIAL",
        "available_legs": 1,
        "total_legs": 2,
    }
    assert [item["implied_volatility"] for item in report["leg_exposures"]] == ["0.25", None]
    assert "IMPLIED_VOLATILITY_COVERAGE_INCOMPLETE" in {
        item["code"] for item in report["quality_reasons"]
    }
    assert any("raw vendor value" in item for item in report["limitations"])
    assert any("not aggregated" in item for item in report["limitations"])


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
    assert [item["reason_code"] for item in report["quote_evidence"]] == [
        "QUOTE_TOTAL_BUDGET_EXCEEDED",
        "QUOTE_TOTAL_BUDGET_EXCEEDED",
    ]
    from portfolio_analyst.schema import validate_option_risk_document

    validate_option_risk_document(report)


def test_exact_total_deadline_is_a_replayable_total_budget_failure(monkeypatch):
    from portfolio_analyst import option_risk

    monkeypatch.setattr(option_risk, "TOTAL_WALL_SECONDS", 1.0)
    analysis_now = AS_OF + timedelta(hours=1)
    acquisition = analysis_now
    report = analyze_option_risk(
        _snapshot((_position("A"),)),
        quote_provider=Provider(),
        analysis_now=analysis_now,
        clock=SequenceClock(acquisition, acquisition + timedelta(seconds=1)),
    ).to_dict()

    assert report["quote_evidence"][0]["status"] == "FAILURE"
    assert report["quote_evidence"][0]["reason_code"] == "QUOTE_TOTAL_BUDGET_EXCEEDED"
    from portfolio_analyst.schema import validate_option_risk_document

    validate_option_risk_document(report)


def test_timeout_ignoring_provider_is_bounded_by_daemon_worker(monkeypatch):
    from portfolio_analyst import option_risk

    monkeypatch.setattr(option_risk, "PER_CALL_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(option_risk, "TOTAL_WALL_SECONDS", 0.04)

    class IgnoringProvider:
        def quote(self, identity, *, timeout):
            del identity, timeout
            time.sleep(0.2)
            return Provider().quote("US.A", timeout=5.0)

    started = time.monotonic()
    report = analyze_option_risk(
        _snapshot((_position("A"),)),
        quote_provider=IgnoringProvider(),
        clock=lambda: datetime.now(timezone.utc),
    ).to_dict()

    assert time.monotonic() - started < 0.15
    assert report["quote_evidence"][0]["status"] == "FAILURE"
    assert report["quote_evidence"][0]["reason_code"] == "QUOTE_SOURCE_TIMEOUT"
    assert report["coverage"]["quoted_contracts"] == 0


def test_first_daemon_timeout_quarantines_provider_for_remaining_contracts(monkeypatch):
    from portfolio_analyst import option_risk

    monkeypatch.setattr(option_risk, "PER_CALL_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(option_risk, "TOTAL_WALL_SECONDS", 0.2)

    class IgnoringProvider:
        def __init__(self):
            self.calls = []

        def quote(self, identity, *, timeout):
            self.calls.append((identity, timeout))
            time.sleep(0.1)
            return Provider().quote(identity, timeout=timeout)

    provider = IgnoringProvider()
    report = analyze_option_risk(
        _snapshot(tuple(_position(name) for name in ("A", "B", "C", "D"))),
        quote_provider=provider,
        clock=lambda: datetime.now(timezone.utc),
    ).to_dict()

    assert len(provider.calls) == 1
    assert [item["reason_code"] for item in report["quote_evidence"]] == [
        "QUOTE_SOURCE_TIMEOUT",
        "QUOTE_CIRCUIT_OPEN",
        "QUOTE_CIRCUIT_OPEN",
        "QUOTE_CIRCUIT_OPEN",
    ]
    from portfolio_analyst.schema import validate_option_risk_document

    validate_option_risk_document(report)


@pytest.mark.parametrize(
    ("failure", "reason_code"),
    [
        (FutuReadError("sensitive source details"), "QUOTE_SOURCE_READ_FAILED"),
        (TimeoutError("source detail"), "QUOTE_SOURCE_TIMEOUT"),
        (RuntimeError("source detail"), "QUOTE_SOURCE_UNAVAILABLE"),
    ],
)
def test_source_failures_use_stable_codes_without_exception_text(failure, reason_code):
    report = analyze_option_risk(
        _snapshot((_position("A"),)),
        quote_provider=Provider(failures={"US.A": failure}),
        clock=Clock(),
    ).to_dict()

    evidence = report["quote_evidence"][0]
    assert evidence["status"] == "FAILURE"
    assert evidence["reason_code"] == reason_code
    assert "sensitive" not in str(evidence)
    assert "source detail" not in str(evidence)
    assert evidence["response_byte_count"] == 0


def test_single_and_total_response_byte_budgets_are_stable_failures(monkeypatch):
    from portfolio_analyst import option_risk

    single = analyze_option_risk(
        _snapshot((_position("A"),)),
        quote_provider=Provider(rows={"US.A": {"padding": "x" * (256 * 1024)}}),
        clock=Clock(),
    ).to_dict()
    assert single["quote_evidence"][0]["reason_code"] == "QUOTE_RESPONSE_TOO_LARGE"

    monkeypatch.setattr(option_risk, "MAX_TOTAL_RESPONSE_BYTES", 900)
    rows = {f"US.{name}": {"delta": "1" * 500} for name in ("A", "B", "C")}
    total = analyze_option_risk(
        _snapshot(tuple(_position(name) for name in ("A", "B", "C"))),
        quote_provider=Provider(rows=rows),
        clock=Clock(),
    ).to_dict()
    assert [item["reason_code"] for item in total["quote_evidence"]] == [
        None,
        "QUOTE_TOTAL_BUDGET_EXCEEDED",
        "QUOTE_TOTAL_BUDGET_EXCEEDED",
    ]


def test_first_normalized_total_overflow_uses_marker_and_omits_all_later_calls(monkeypatch):
    from portfolio_analyst import option_risk

    monkeypatch.setattr(option_risk, "MAX_TOTAL_RESPONSE_BYTES", 900)
    rows = {f"US.{name}": {"delta": "1" * 500} for name in ("A", "B", "C", "D")}
    provider = Provider(rows=rows)

    report = analyze_option_risk(
        _snapshot(tuple(_position(name) for name in ("A", "B", "C", "D"))),
        quote_provider=provider,
        clock=Clock(),
    ).to_dict()

    assert len(provider.calls) == 2
    evidence = report["quote_evidence"]
    assert [item["status"] for item in evidence] == [
        "SUCCESS",
        "FAILURE",
        "OMITTED",
        "OMITTED",
    ]
    assert [item["reason_code"] for item in evidence] == [
        None,
        "QUOTE_TOTAL_BUDGET_EXCEEDED",
        "QUOTE_TOTAL_BUDGET_EXCEEDED",
        "QUOTE_TOTAL_BUDGET_EXCEEDED",
    ]
    assert evidence[1]["source_response"] is None
    marker = evidence[1]["overflow_marker"]
    assert set(marker) == {"response_sha256"}
    assert len(marker["response_sha256"]) == 64
    assert (
        sum(
            len(option_risk._bounded_response_bytes(item["source_response"]))
            for item in evidence
            if item["source_response"] is not None
        )
        <= option_risk.MAX_TOTAL_RESPONSE_BYTES
    )
    from portfolio_analyst.schema import validate_option_risk_document

    validate_option_risk_document(report)


@pytest.mark.parametrize(
    "reason_code", ["QUOTE_CAPABILITY_UNAVAILABLE", "QUOTE_RESPONSE_TOO_LARGE"]
)
def test_adapter_boundary_failures_produce_runtime_replayable_reports(reason_code):
    provider = Provider(failures={"US.A": OptionQuoteProviderError(reason_code)})
    report = analyze_option_risk(
        _snapshot((_position("A"),)), quote_provider=provider, clock=Clock()
    ).to_dict()

    assert report["quote_evidence"][0]["reason_code"] == reason_code
    if reason_code == "QUOTE_CAPABILITY_UNAVAILABLE":
        assert report["provenance"]["quote_capability"] == "UNAVAILABLE"
    from portfolio_analyst.schema import validate_option_risk_document

    validate_option_risk_document(report)
