from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import subprocess

import pytest

from portfolio_analyst.identity import snapshot_identity
from portfolio_analyst.risk import analyze_risk
from portfolio_analyst.schema import ReportSemanticError, validate_technical_document
from portfolio_analyst.technical import (
    TechnicalBar,
    TechnicalInputError,
    analyze_technical,
    derive_technical_context,
    select_technical_holdings,
)
from test_risk_analysis import _position, reconciled_snapshot, snapshot as base_snapshot


def _portfolio(weights, *, security_types=None):
    security_types = security_types or {}
    positions = []
    for index, (symbol, weight) in enumerate(weights):
        position = _position(
            symbol,
            weight,
            sector="TECH" if index < 5 else "OTHER",
            region="US",
            themes=("AI",) if index < 5 else (),
        )
        position = replace(
            position,
            security_type=security_types.get(symbol, "STOCK"),
            market_value_base=Decimal(weight) * Decimal("1000"),
            market_value_local=Decimal(weight) * Decimal("1000"),
            last_price_local=Decimal("100"),
            average_cost_local=Decimal("90"),
        )
        positions.append(position)
    result = reconciled_snapshot(base_snapshot(), tuple(positions))
    return replace(result, snapshot_id=snapshot_identity(replace(result, snapshot_id="")))


def _bars(count, *, interval="1d", start=date(2026, 2, 10), zero_volume=False):
    step = 7 if interval == "1w" else 1
    bars = []
    for index in range(count):
        center = Decimal("100") + Decimal(index % 12) - Decimal("6")
        bars.append(
            TechnicalBar(
                date=start + timedelta(days=index * step),
                open=center,
                high=center + Decimal("2"),
                low=center - Decimal("2"),
                close=center + (Decimal("1") if index % 2 else Decimal("-1")),
                volume=Decimal("0") if zero_volume else Decimal(1000 + index * 10),
            )
        )
    return tuple(bars)


class Provider:
    def __init__(self, *, fail=None, daily=None, weekly=None):
        self.fail = set(fail or ())
        self.daily = daily or _bars(70)
        self.weekly = weekly or _bars(45, interval="1w", start=date(2025, 10, 1))
        self.calls = []

    def bars(self, symbol, *, market, interval, as_of, timeout_seconds):
        self.calls.append((symbol, market, interval, as_of, timeout_seconds))
        if (symbol, interval) in self.fail:
            raise TimeoutError("private detail")
        return self.daily if interval == "1d" else self.weekly


def test_selection_applies_threshold_top_five_risk_and_stable_max_eight():
    source = _portfolio(
        [("AA", ".20"), ("AB", ".15"), ("AC", ".10"), ("AD", ".08"),
         ("AE", ".07"), ("AF", ".04"), ("AG", ".03"), ("AH", ".02"), ("AI", ".01")]
    )
    risk = {
        "source_snapshot_id": source.snapshot_id,
        "top_risks": [
            {"affected_positions": ["US.AI", "US.AH"]},
            {"affected_positions": ["US.AG"]},
            {"affected_positions": ["US.AF"]},
        ]
    }

    selection = select_technical_holdings(source, risk_document=risk, explicit_symbols=("AG",))

    assert [item.identity for item in selection.selected] == [
        "US.AA", "US.AB", "US.AC", "US.AD", "US.AE", "US.AF", "US.AG", "US.AH"
    ]
    assert selection.omitted == ("US.AI",)
    assert selection.truncated is True
    assert selection.tie_breaker == "rule_priority_then_weight_desc_then_identity_asc"


def test_selection_fills_to_five_and_rejects_unheld_or_non_equity_explicit_symbols():
    source = _portfolio(
        [("AAA", ".30"), ("BBB", ".04"), ("CCC", ".03"), ("DDD", ".02"),
         ("EEE", ".01"), ("OPT", ".01")],
        security_types={"OPT": "OPTION"},
    )
    selected = select_technical_holdings(source)
    assert [item.identity for item in selected.selected] == [
        "US.AAA", "US.BBB", "US.CCC", "US.DDD", "US.EEE"
    ]
    assert selected.selected[0].reasons == ("WEIGHT_AT_LEAST_5_PERCENT_NAV",)
    with pytest.raises(TechnicalInputError, match="held US stock or ETF"):
        select_technical_holdings(source, explicit_symbols=("MISSING",))
    with pytest.raises(TechnicalInputError, match="held US stock or ETF"):
        select_technical_holdings(source, explicit_symbols=("OPT",))


def test_explicit_symbol_input_is_bounded_before_selection():
    source = _portfolio([(f"A{index}", ".01") for index in range(10)])
    with pytest.raises(TechnicalInputError, match="at most eight"):
        select_technical_holdings(source, explicit_symbols=tuple(f"A{index}" for index in range(9)))


def test_risk_selection_requires_exact_snapshot_binding():
    source = _portfolio([("AAA", ".30")])
    with pytest.raises(TechnicalInputError, match="does not bind"):
        select_technical_holdings(source, risk_document={"top_risks": []})


def test_nonzero_held_position_with_zero_valuation_remains_eligible_for_local_degradation():
    source = _portfolio([("AAA", ".30")])
    source = replace(
        source,
        positions=(
            replace(
                source.positions[0],
                market_value_base=Decimal("0"),
                market_value_local=Decimal("0"),
                weight=Decimal("0"),
            ),
        ),
    )

    selection = select_technical_holdings(source, explicit_symbols=("AAA",))

    assert selection.selected[0].identity == "US.AAA"
    assert selection.selected[0].weight == 0


def test_derivation_is_deterministic_bounded_and_never_crosses_current_price():
    daily = list(_bars(70))
    daily.reverse()  # input order is not authoritative
    daily.append(daily[-1])  # exact duplicates are harmless
    context = derive_technical_context(
        identity="US.AAA",
        current_price=Decimal("100"),
        daily=daily,
        weekly=_bars(45, interval="1w", start=date(2025, 10, 1)),
        cutoff=date(2026, 8, 27),
    )

    assert context["moving_averages"]["daily_20"] is not None
    assert context["moving_averages"]["daily_50"] is not None
    assert context["moving_averages"]["weekly_20"] is not None
    assert context["moving_averages"]["weekly_40"] is not None
    assert len(context["support_zones"]) <= 2
    assert len(context["resistance_zones"]) <= 2
    assert all(Decimal(zone["upper"]) <= 100 for zone in context["support_zones"])
    assert all(Decimal(zone["lower"]) >= 100 for zone in context["resistance_zones"])
    assert all(zone["kind"] == "SUPPORT" for zone in context["support_zones"])
    assert all(zone["kind"] == "RESISTANCE" for zone in context["resistance_zones"])
    assert "score" not in str(context).lower()


@pytest.mark.parametrize(
    ("places", "quantum"),
    [(0, "0.01"), (2, "0.01"), (4, "0.0001")],
)
def test_all_derived_price_evidence_uses_input_bounded_quantum(places, quantum):
    unit = Decimal(1).scaleb(-places)
    daily = tuple(
        TechnicalBar(
            date=date(2026, 2, 10) + timedelta(days=index),
            open=(Decimal("100") + Decimal(index % 7)).quantize(unit),
            high=(Decimal("102") + Decimal(index % 7)).quantize(unit),
            low=(Decimal("98") + Decimal(index % 7)).quantize(unit),
            close=(Decimal("100.3333") + Decimal(index % 7)).quantize(unit),
            volume=Decimal(1000 + index),
        )
        for index in range(70)
    )
    weekly = daily[:45]

    context = derive_technical_context(
        identity="US.AAA",
        current_price=Decimal("103.3333").quantize(unit),
        daily=daily,
        weekly=weekly,
        cutoff=date(2026, 8, 27),
    )

    assert context["price_quantum"] == quantum
    price_texts = [
        value
        for value in context["moving_averages"].values()
        if value is not None
    ]
    if context["atr14"] is not None:
        price_texts.append(context["atr14"])
    price_texts.extend(item["price"] for item in context["swing_pivots"])
    price_texts.extend(item["price"] for item in context["volume_congestion"]["nodes"])
    for zone in (*context["support_zones"], *context["resistance_zones"]):
        price_texts.extend((zone["lower"], zone["upper"]))
    max_places = len(quantum.partition(".")[2])
    assert all(len(text.partition(".")[2]) <= max_places for text in price_texts)
    reversed_context = derive_technical_context(
        identity="US.AAA",
        current_price=Decimal("103.3333").quantize(unit),
        daily=tuple(reversed(daily)),
        weekly=tuple(reversed(weekly)),
        cutoff=date(2026, 8, 27),
    )
    assert reversed_context == context


def test_sparse_and_zero_volume_series_are_explicitly_unknown_without_prediction():
    context = derive_technical_context(
        identity="US.AAA",
        current_price=Decimal("100"),
        daily=_bars(5, zero_volume=True),
        weekly=_bars(3, interval="1w", zero_volume=True),
        cutoff=date(2026, 8, 27),
    )

    assert context["moving_averages"] == {
        "daily_20": None, "daily_50": None, "weekly_20": None, "weekly_40": None
    }
    assert context["volume_congestion"]["status"] == "UNAVAILABLE"
    assert context["support_zones"] == []
    assert context["resistance_zones"] == []
    assert "INSUFFICIENT_HISTORY" in context["quality_reasons"]


def test_trending_series_is_explainable_and_does_not_invent_opposite_side_zones():
    daily = tuple(
        TechnicalBar(
            date=date(2026, 1, 1) + timedelta(days=index),
            open=Decimal(50 + index),
            high=Decimal(51 + index),
            low=Decimal(49 + index),
            close=Decimal(50 + index),
            volume=Decimal(1000 + index),
        )
        for index in range(70)
    )
    context = derive_technical_context(
        identity="US.AAA",
        current_price=Decimal("120"),
        daily=daily,
        weekly=_bars(45, interval="1w", start=date(2025, 10, 1)),
        cutoff=date(2026, 8, 27),
    )
    assert all(Decimal(zone["upper"]) <= 120 for zone in context["support_zones"])
    assert context["resistance_zones"] == []


@pytest.mark.parametrize("bad", ["future", "conflict", "extreme"])
def test_invalid_future_conflicting_duplicate_and_extreme_bars_fail_closed(bad):
    bars = list(_bars(25))
    if bad == "future":
        bars.append(replace(bars[-1], date=date(2026, 9, 1)))
    elif bad == "conflict":
        bars.append(replace(bars[-1], close=Decimal("999")))
    else:
        bars.append(replace(bars[-1], close=Decimal("1e1000"), high=Decimal("1e1000")))
    with pytest.raises(TechnicalInputError):
        derive_technical_context(
            identity="US.AAA",
            current_price=Decimal("100"),
            daily=bars,
            weekly=_bars(20, interval="1w"),
            cutoff=date(2026, 8, 27),
        )


def test_provider_rows_before_requested_window_are_rejected_locally():
    source = _portfolio([("AAA", ".30")])
    too_old = _bars(25, start=date(2025, 1, 1))
    report = analyze_technical(source, provider=Provider(daily=too_old)).to_dict()
    assert report["instruments"][0]["evidence"]["daily"]["status"] == "UNAVAILABLE"


def test_analysis_keeps_symbol_and_timeframe_failures_local_and_caps_raw_evidence():
    source = _portfolio([("AAA", ".30"), ("BBB", ".20")])
    provider = Provider(fail={("AAA", "1w"), ("BBB", "1d")})

    report = analyze_technical(source, provider=provider).to_dict()

    assert report["source_snapshot_id"] == source.snapshot_id
    assert [item["identity"] for item in report["instruments"]] == ["US.AAA", "US.BBB"]
    assert [item["status"] for item in report["instruments"]] == ["PARTIAL", "PARTIAL"]
    assert len(report["instruments"][0]["evidence"]["daily"]["bars"]) <= 160
    assert report["instruments"][0]["evidence"]["weekly"]["status"] == "UNAVAILABLE"
    assert all(call[-1] == 5 for call in provider.calls)


def test_subprocess_timeout_is_a_local_stable_gap_without_command_disclosure():
    source = _portfolio([("AAA", ".30")])

    class SubprocessTimeoutProvider:
        def bars(self, symbol, *, market, interval, as_of, timeout_seconds):
            raise subprocess.TimeoutExpired(
                cmd=["python", "/private/secret.py", "--token", "secret-value"],
                timeout=timeout_seconds,
                output="secret-value",
            )

    report = analyze_technical(source, provider=SubprocessTimeoutProvider()).to_dict()
    item = report["instruments"][0]

    assert item["status"] == "UNAVAILABLE"
    assert item["evidence"]["daily"]["reason"] == "SOURCE_TIMEOUT"
    assert item["evidence"]["weekly"]["reason"] == "SOURCE_TIMEOUT"
    assert "secret" not in str(report).lower()


def test_empty_source_series_is_unavailable_not_successful_coverage():
    source = _portfolio([("AAA", ".30")])

    class EmptyProvider:
        def bars(self, symbol, *, market, interval, as_of, timeout_seconds):
            return ()

    report = analyze_technical(source, provider=EmptyProvider()).to_dict()
    item = report["instruments"][0]
    assert item["status"] == "UNAVAILABLE"
    assert item["evidence"]["daily"]["status"] == "UNAVAILABLE"
    assert item["latest_bar"] is None


def test_invalid_snapshot_price_suppresses_zones_as_a_local_gap():
    source = _portfolio([("AAA", ".30")])
    source = replace(
        source,
        positions=(replace(source.positions[0], last_price_local=Decimal("0")),),
        snapshot_id="",
    )
    source = replace(source, snapshot_id=snapshot_identity(source))

    item = analyze_technical(source, provider=Provider()).to_dict()["instruments"][0]

    assert item["status"] == "PARTIAL"
    assert item["current_price"] is None
    assert item["support_zones"] == []
    assert item["resistance_zones"] == []
    assert item["quality_reasons"] == ["CURRENT_PRICE_EVIDENCE_INVALID"]
    report_note = analyze_technical(source, provider=Provider()).to_dict()[
        "data_quality_notes"
    ][0]
    assert "current price" in report_note


def test_schema_replay_rejects_derived_or_raw_evidence_tampering():
    source = _portfolio([("AAA", ".30")])
    report = analyze_technical(source, provider=Provider()).to_dict()
    validate_technical_document(report)

    tampered = deepcopy(report)
    tampered["instruments"][0]["moving_averages"]["daily_20"] = "999"
    with pytest.raises(ReportSemanticError):
        validate_technical_document(tampered)


def test_analyze_and_validator_reject_semantically_invalid_embedded_risk():
    source = base_snapshot()
    valid_risk = analyze_risk(source).to_dict()
    invalid_risk = deepcopy(valid_risk)
    invalid_risk["top_risks"].reverse()

    with pytest.raises(TechnicalInputError, match="risk report is invalid"):
        analyze_technical(source, provider=Provider(), risk_document=invalid_risk)

    report = analyze_technical(
        source, provider=Provider(), risk_document=valid_risk
    ).to_dict()
    report["provenance"]["source_risk_report"]["top_risks"].reverse()
    with pytest.raises(ReportSemanticError, match="embedded risk report is invalid"):
        validate_technical_document(report)


def test_replay_preserves_explicit_requested_identity_even_when_cap_omits_it():
    source = _portfolio(
        [(f"I{index}", ".10" if index < 8 else ".01") for index in range(9)]
    )
    report = analyze_technical(
        source, provider=Provider(), explicit_symbols=("I8",)
    ).to_dict()

    assert len(report["selection"]["selected"]) == 8
    assert report["selection"]["explicit_requested"] == ["US.I8"]
    assert report["selection"]["omitted"] == ["US.I8"]
    validate_technical_document(report)

    tampered = deepcopy(report)
    tampered["selection"]["explicit_requested"] = ["US.I0"]
    with pytest.raises(ReportSemanticError):
        validate_technical_document(tampered)

    tampered = deepcopy(report)
    tampered["instruments"][0]["evidence"]["daily"]["bars"][0]["close"] = "999"
    with pytest.raises(ReportSemanticError):
        validate_technical_document(tampered)


def test_markdown_states_zones_are_observed_context_not_forecasts():
    source = _portfolio([("AAA", ".30")])
    markdown = analyze_technical(source, provider=Provider()).to_markdown()

    assert "Observed support/resistance context" in markdown
    assert "not forecasts" in markdown
    assert "Score" not in markdown
    assert "BUY" not in markdown


def test_untrusted_preflight_rejects_depth_width_strings_cycles_and_non_json_scalars():
    source = _portfolio([("AAA", ".30")])
    report = analyze_technical(source, provider=Provider()).to_dict()

    deep = {}
    cursor = deep
    for _ in range(1200):
        cursor["x"] = {}
        cursor = cursor["x"]
    hostile = deepcopy(report)
    hostile["hostile"] = deep

    wide = deepcopy(report)
    wide["hostile"] = [None] * 20_001
    huge = deepcopy(report)
    huge["hostile"] = "x" * (1024 * 1024 + 1)
    cyclic = deepcopy(report)
    cyclic["hostile"] = cyclic
    non_json = deepcopy(report)
    non_json["hostile"] = object()
    non_string_key = deepcopy(report)
    non_string_key[1] = "bad"

    for payload in (hostile, wide, huge, cyclic, non_json, non_string_key):
        with pytest.raises(ReportSemanticError, match="technical validation preflight"):
            validate_technical_document(payload)

    validate_technical_document(report)

    near_bar_budget = analyze_technical(
        _portfolio([(f"B{index}", ".10") for index in range(8)]),
        provider=Provider(
            daily=_bars(160),
            weekly=_bars(
                105,
                interval="1w",
                start=date(2024, 8, 23),
            ),
        ),
    ).to_dict()
    assert sum(
        len(series["bars"])
        for item in near_bar_budget["instruments"]
        for series in item["evidence"].values()
    ) == 8 * (160 + 105)
    validate_technical_document(near_bar_budget)
