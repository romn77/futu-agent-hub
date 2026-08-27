from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from portfolio_analyst.rebalance import RebalanceChange, RebalanceRequest, simulate_rebalance
from portfolio_analyst.identity import snapshot_identity
from portfolio_analyst.models import DataQuality
from portfolio_analyst.schema import (
    validate_rebalance_result_document,
    validate_trade_review_document,
)
from portfolio_analyst.trade_review import TradePolicy, build_trade_review
from test_risk_analysis import reconciled_snapshot, snapshot


def _rehash_simulation(payload):
    body = dict(payload)
    body.pop("simulation_id")
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    payload["simulation_id"] = "sim_" + digest


def _rehash_trade(payload):
    body = dict(payload)
    body.pop("review_id")
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    payload["review_id"] = "tr_" + digest


def _rehash_snapshot(payload):
    body = dict(payload)
    body.pop("snapshot_id")
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    payload["snapshot_id"] = f"ps-20260827T100000Z-real-usd-{digest}"


def _deal(
    deal_id: str,
    order_id: str,
    side: str,
    qty: str,
    price: str,
    at: str,
    *,
    code: str = "US.AAA",
    currency: str = "USD",
    fx_to_base: str | None = "1",
    event_type: str = "TRADE",
) -> dict[str, object]:
    return {
        "deal_id": deal_id,
        "order_id": order_id,
        "account_ref": "acct-a",
        "code": code,
        "side": side,
        "qty": qty,
        "price": price,
        "create_time": at,
        "currency": currency,
        "fx_to_base": fx_to_base,
        "event_type": event_type,
    }


def _order(
    order_id: str,
    side: str,
    qty: str,
    dealt_qty: str,
    *,
    status: str = "FILLED_ALL",
    code: str = "US.AAA",
    currency: str = "USD",
) -> dict[str, object]:
    return {
        "order_id": order_id,
        "account_ref": "acct-a",
        "code": code,
        "side": side,
        "status": status,
        "qty": qty,
        "price": "10",
        "dealt_qty": dealt_qty,
        "create_time": "2026-08-03 10:00:00",
        "updated_time": "2026-08-03 10:01:00",
        "currency": currency,
    }


def test_mixed_currency_money_is_bucketed_and_base_aggregation_requires_complete_fx():
    report = build_trade_review(
        orders=[],
        deals=[
            _deal("d1", "o1", "BUY", "1", "10", "2026-08-03 10:00:00"),
            _deal(
                "d2",
                "o2",
                "BUY",
                "1",
                "80",
                "2026-08-03 10:01:00",
                code="HK.00001",
                currency="HKD",
                fx_to_base=None,
            ),
        ],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
        base_currency="USD",
        portfolio_nav_base=Decimal("1000"),
        source_snapshot_id="ps-fixture",
        source_snapshot_as_of=datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
    )

    assert report.metrics.notional_by_currency == {
        "HKD": {"buy": Decimal("80"), "sell": Decimal("0")},
        "USD": {"buy": Decimal("10"), "sell": Decimal("0")},
    }
    assert report.metrics.buy_notional_base is None
    assert report.metrics.sell_notional_base is None
    assert report.metrics.portfolio_turnover is None
    assert report.data_quality_status == "PARTIAL"
    assert report.coverage["monetary_base_coverage"] == "PARTIAL"


def test_complete_fx_enables_base_pnl_and_currency_qualified_statistics():
    acquired = datetime(2026, 8, 27, 16, 0, tzinfo=timezone.utc)
    snapshot_as_of = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
    report = build_trade_review(
        orders=[],
        deals=[
            _deal(
                "d1",
                "o1",
                "BUY",
                "1",
                "80",
                "2026-08-03 10:00:00",
                code="HK.00001",
                currency="HKD",
                fx_to_base="0.125",
            ),
            _deal(
                "d2",
                "o2",
                "SELL",
                "1",
                "88",
                "2026-08-04 10:00:00",
                code="HK.00001",
                currency="HKD",
                fx_to_base="0.125",
            ),
        ],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
        base_currency="USD",
        portfolio_nav_base=Decimal("1000"),
        source_snapshot_id="ps-fixture",
        acquired_at=acquired,
        source_snapshot_as_of=snapshot_as_of,
    )
    assert report.as_of == acquired
    assert report.source_snapshot_as_of == snapshot_as_of
    assert report.metrics.buy_notional_base == Decimal("10")
    assert report.metrics.sell_notional_base == Decimal("11")
    assert report.metrics.average_win_base is None
    assert report.metrics.expectancy_base is None
    assert report.round_trips[0].currency == "HKD"
    assert report.round_trips[0].realized_pl_local == Decimal("8")
    assert report.round_trips[0].realized_pl_base is None
    validate_trade_review_document(report.to_dict())


def test_base_currency_fifo_pnl_is_exact_without_snapshot_fx_and_rebuilds_statistics():
    report = build_trade_review(
        orders=[],
        deals=[
            _deal("d1", "o1", "BUY", "1", "10", "2026-08-03 10:00:00"),
            _deal("d2", "o2", "SELL", "1", "12", "2026-08-04 10:00:00"),
            _deal("d3", "o3", "BUY", "1", "10", "2026-08-05 10:00:00"),
            _deal("d4", "o4", "SELL", "1", "8", "2026-08-06 10:00:00"),
        ],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
        base_currency="USD",
    )

    assert [trip.realized_pl_base for trip in report.round_trips] == [
        Decimal("2"),
        Decimal("-2"),
    ]
    assert report.coverage["realized_pl_base_coverage"] == "COMPLETE"
    assert report.metrics.average_win_base == Decimal("2")
    assert report.metrics.average_loss_base == Decimal("-2")
    assert report.metrics.payoff_ratio_base == Decimal("1")
    assert report.metrics.profit_factor_base == Decimal("1")
    assert report.metrics.expectancy_base == Decimal("0")
    assert report.metrics.pnl_statistics_by_currency["USD"] == {
        "closed_trips": 2,
        "wins": 1,
        "losses": 1,
        "gross_profit": Decimal("2"),
        "gross_loss_absolute": Decimal("2"),
        "average_win": Decimal("2"),
        "average_loss": Decimal("-2"),
        "payoff_ratio": Decimal("1"),
        "profit_factor": Decimal("1"),
        "expectancy": Decimal("0"),
    }
    validate_trade_review_document(report.to_dict())
    with pytest.raises(Exception, match="FX must equal one"):
        build_trade_review(
            orders=[],
            deals=[
                _deal(
                    "bad-fx",
                    "bad-order",
                    "BUY",
                    "1",
                    "10",
                    "2026-08-03 10:00:00",
                    fx_to_base="0.9",
                )
            ],
            start=date(2026, 8, 1),
            end=date(2026, 8, 27),
            base_currency="USD",
        )


def test_cross_currency_fifo_keeps_local_statistics_without_false_base_pnl():
    report = build_trade_review(
        orders=[],
        deals=[
            _deal(
                "d1",
                "o1",
                "BUY",
                "1",
                "80",
                "2026-08-03 10:00:00",
                code="HK.00001",
                currency="HKD",
                fx_to_base="0.125",
            ),
            _deal(
                "d2",
                "o2",
                "SELL",
                "1",
                "88",
                "2026-08-04 10:00:00",
                code="HK.00001",
                currency="HKD",
                fx_to_base="0.125",
            ),
        ],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
        base_currency="USD",
    )

    assert report.round_trips[0].realized_pl_base is None
    assert report.coverage["realized_pl_base_coverage"] == "PARTIAL"
    assert report.metrics.expectancy_base is None
    assert report.metrics.pnl_statistics_by_currency["HKD"]["average_win"] == Decimal("8")
    assert report.metrics.pnl_statistics_by_currency["HKD"]["expectancy"] == Decimal("8")
    assert report.data_quality_status == "PARTIAL"
    validate_trade_review_document(report.to_dict())


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("round_trips", 0, "realized_pl_base"), "999"),
        (("metrics", "expectancy_base"), "999"),
        (("metrics", "pnl_statistics_by_currency", "USD", "expectancy"), "999"),
        (("coverage", "realized_pl_base_coverage"), "PARTIAL"),
    ],
)
def test_base_currency_fifo_authority_rejects_rehashed_pnl_tampering(path, value):
    payload = build_trade_review(
        orders=[],
        deals=[
            _deal("d1", "o1", "BUY", "1", "10", "2026-08-03 10:00:00"),
            _deal("d2", "o2", "SELL", "1", "12", "2026-08-04 10:00:00"),
        ],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
        base_currency="USD",
    ).to_dict()
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    _rehash_trade(payload)
    with pytest.raises(Exception):
        validate_trade_review_document(payload)


def test_observed_behavior_is_structured_without_policy_but_policy_metrics_are_unavailable():
    report = build_trade_review(
        orders=[],
        deals=[
            _deal("d1", "o1", "BUY", "1", "10", "2026-08-03 09:31:00"),
            _deal("d2", "o2", "SELL", "1", "11", "2026-08-03 10:00:00"),
            _deal("d3", "o3", "BUY", "1", "12", "2026-08-03 10:10:00"),
        ],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
    )
    assert report.metrics.reentries_within_cooldown is None
    assert report.metrics.open_window_trades is None
    assert report.metrics.open_window_trade_rate is None
    assert {finding.type for finding in report.behavior_findings} >= {
        "SAME_DAY_REVERSAL",
        "SELL_THEN_REENTRY",
        "MARKET_OPEN_ACTIVITY",
        "REPEATED_SYMBOL_ACTIVITY",
    }
    assert all(finding.verified is True for finding in report.behavior_findings)
    assert all(finding.severity == "INFORMATIONAL" for finding in report.behavior_findings)
    assert report.unverified_interpretations
    payload = report.to_dict()
    validate_trade_review_document(payload)
    unavailable_as_zero = deepcopy(payload)
    unavailable_as_zero["metrics"]["reentries_within_cooldown"] = 0
    with pytest.raises(Exception):
        validate_trade_review_document(unavailable_as_zero)
    unverified_finding = deepcopy(payload)
    unverified_finding["behavior_findings"][0]["verified"] = False
    with pytest.raises(Exception):
        validate_trade_review_document(unverified_finding)


def test_order_fill_reconciliation_is_structured_and_marks_partial_quality():
    report = build_trade_review(
        orders=[
            _order("o1", "BUY", "10", "4", status="CANCELLED_PART"),
            _order("o2", "BUY", "2", "2", status="FILLED_ALL"),
        ],
        deals=[
            _deal("d1", "o1", "BUY", "4", "10", "2026-08-03 10:00:00"),
            _deal("d2", "orphan", "BUY", "1", "10", "2026-08-03 10:01:00"),
        ],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
    )
    assert len(report.normalized_orders) == 2
    assert report.coverage["partial_fill_orders"] == 1
    assert report.coverage["orders_missing_fills"] == 1
    assert report.coverage["orphan_fill_orders"] == 1
    assert report.data_quality_status == "PARTIAL"
    invalid_coverage = report.to_dict()
    invalid_coverage["coverage"]["orphan_fill_orders"] = 0
    with pytest.raises(Exception):
        validate_trade_review_document(invalid_coverage)


def test_ambiguous_nontrade_events_are_excluded_and_upstream_evidence_gap_is_explicit():
    report = build_trade_review(
        orders=[],
        deals=[
            _deal("d1", "o1", "BUY", "5", "10", "2026-08-03 10:00:00", event_type="TRANSFER"),
        ],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
        corporate_action_evidence_status="AVAILABLE",
    )
    assert report.metrics.fill_count == 0
    assert report.coverage["excluded_nontrade_events"] == 1
    assert report.open_lots == ()

    unsupported = build_trade_review(
        orders=[], deals=[], start=date(2026, 8, 1), end=date(2026, 8, 27)
    )
    assert unsupported.coverage["corporate_action_evidence_status"] == "UNSUPPORTED_EVIDENCE"
    assert unsupported.data_quality_status == "UNAVAILABLE"
    assert all(value is None for value in vars(unsupported.metrics).values())


def test_global_trade_and_rebalance_contracts_are_self_contained_and_reject_old_shapes():
    trade = build_trade_review(
        orders=[], deals=[], start=date(2026, 8, 1), end=date(2026, 8, 27)
    ).to_dict()
    assert {
        "as_of",
        "base_currency",
        "portfolio_nav_base",
        "normalized_orders",
        "normalized_fills",
        "behavior_findings",
        "unverified_interpretations",
    } <= set(trade)
    old_trade = dict(trade)
    old_trade.pop("as_of")
    with pytest.raises(Exception):
        validate_trade_review_document(old_trade)
    version_1_trade = deepcopy(trade)
    version_1_trade["schema_version"] = "1.0"
    with pytest.raises(Exception):
        validate_trade_review_document(version_1_trade)
    version_1_5_trade = deepcopy(trade)
    version_1_5_trade["schema_version"] = "1.5"
    version_1_5_trade.pop("external_source_evidence")
    with pytest.raises(Exception):
        validate_trade_review_document(version_1_5_trade)

    result = simulate_rebalance(
        snapshot(),
        RebalanceRequest(
            snapshot().snapshot_id,
            "USD",
            changes=(RebalanceChange("US.AAA", "AMOUNT_DELTA", Decimal("-10")),),
        ),
    ).to_dict()
    assert result["as_of"] == snapshot().to_dict()["as_of"]
    assert result["base_currency"] == "USD"
    assert result["coverage"]["changes_applied"] == 1
    assert isinstance(result["data_quality_notes"], list)
    validate_rebalance_result_document(result)
    old_result = dict(result)
    old_result.pop("base_currency")
    with pytest.raises(Exception):
        validate_rebalance_result_document(old_result)
    version_1_result = deepcopy(result)
    version_1_result["schema_version"] = "1.0"
    with pytest.raises(Exception):
        validate_rebalance_result_document(version_1_result)

    from portfolio_analyst.rebalance import request_from_document

    old_request = {
        "schema_version": "1.0",
        "snapshot_id": snapshot().snapshot_id,
        "base_currency": "USD",
        "allow_margin": False,
        "allow_short": False,
        "sell_only": False,
        "new_cash_only": False,
        "new_cash": "0",
        "changes": [],
    }
    with pytest.raises(Exception):
        request_from_document(old_request)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("changes", 0, "operation"), "SHARE_DELTA"),
        (("after", "cash_base"), "999"),
        (("after", "nav_base"), "999"),
        (("changes", 0, "unit_price_local"), "999"),
    ],
)
def test_rebalance_semantics_reject_rehashed_formula_state_and_evidence_tampering(path, value):
    payload = simulate_rebalance(
        snapshot(),
        RebalanceRequest(
            snapshot().snapshot_id,
            "USD",
            changes=(RebalanceChange("US.AAA", "AMOUNT_DELTA", Decimal("-10")),),
        ),
    ).to_dict()
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    _rehash_simulation(payload)
    with pytest.raises(Exception):
        validate_rebalance_result_document(payload)


def test_rebalance_classification_evidence_is_authoritative_and_tamper_evident():
    result = simulate_rebalance(
        snapshot(), RebalanceRequest(snapshot().snapshot_id, "USD")
    ).to_dict()
    evidence = result["evidence"]["US.AAA"]
    assert evidence["sector"] == "TECH"
    assert evidence["region"] == "US"
    assert evidence["themes"] == ["AI", "CLOUD"]
    assert evidence["theme_allocation"] == "FULL_AND_EQUAL_SPLIT"

    evidence["sector"] = "ENERGY"
    result["before"]["exposures"]["sector"] = {"ENERGY": "0.4", "TECH": "0.3", "HEALTH": "0.2"}
    result["before"]["exposure_views"]["net"]["sector"] = dict(
        result["before"]["exposures"]["sector"]
    )
    result["before"]["exposure_views"]["long"]["sector"] = dict(
        result["before"]["exposures"]["sector"]
    )
    result["before"]["exposure_views"]["gross"]["sector"] = dict(
        result["before"]["exposures"]["sector"]
    )
    _rehash_simulation(result)
    with pytest.raises(Exception):
        validate_rebalance_result_document(result)


def test_rebalance_rejects_rehashed_leg_and_allocation_redistribution_with_same_totals():
    source = snapshot()
    second_account = replace(source.accounts[0], account_ref="acct-second")
    second_leg = replace(
        source.positions[0],
        account_ref="acct-second",
        quantity=Decimal("0.5"),
        can_sell_quantity=Decimal("0.5"),
        market_value_local=Decimal("200"),
        market_value_base=Decimal("200"),
    )
    source = reconciled_snapshot(
        source,
        (source.positions[0], second_leg, *source.positions[1:]),
        accounts=(*source.accounts, second_account),
    )
    payload = simulate_rebalance(
        source,
        RebalanceRequest(
            source.snapshot_id,
            "USD",
            changes=(RebalanceChange("US.AAA", "AMOUNT_DELTA", Decimal("60")),),
        ),
    ).to_dict()

    legs = payload["evidence"]["US.AAA"]["source_legs"]
    legs[0]["quantity"] = "0.75"
    legs[0]["market_value_base"] = "300"
    legs[1]["quantity"] = "0.75"
    legs[1]["market_value_base"] = "300"
    allocations = payload["changes"][0]["leg_allocations"]
    for allocation in allocations:
        allocation["amount_delta_base"] = "30"
        allocation["quantity_delta"] = "0.075"
    _rehash_simulation(payload)

    with pytest.raises(Exception, match="source legs"):
        validate_rebalance_result_document(payload)


def test_rebalance_rejects_fully_rehashed_embedded_cash_that_does_not_match_before_state():
    source = snapshot()
    payload = simulate_rebalance(source, RebalanceRequest(source.snapshot_id, "USD")).to_dict()
    embedded = payload["source_snapshot"]
    embedded["cash_balances"][0]["amount_local"] = "999999"
    embedded["cash_balances"][0]["amount_base"] = "999999"
    embedded["cash_base"] = "999999"
    embedded["nav_base"] = "1000899"
    embedded["cash_balances"][0]["weight"] = str(Decimal("999999") / Decimal("1000899"))
    embedded["cash_weight"] = embedded["cash_balances"][0]["weight"]
    for position in embedded["positions"]:
        position["weight"] = str(
            Decimal(position["market_value_base"]) / Decimal(embedded["nav_base"])
        )
    for dimension, labels in embedded["exposures"].items():
        rebuilt = {}
        for position in embedded["positions"]:
            position_labels = (
                position["themes"]
                if dimension == "theme"
                else [position["asset_class"]]
                if dimension == "asset_class"
                else [position[dimension]]
            )
            for label in position_labels:
                rebuilt[label] = rebuilt.get(label, Decimal(0)) + Decimal(position["weight"])
        embedded["exposures"][dimension] = {key: str(value) for key, value in rebuilt.items()}
    _rehash_snapshot(embedded)
    new_snapshot_id = embedded["snapshot_id"]
    payload["source_snapshot_id"] = new_snapshot_id
    payload["request"]["snapshot_id"] = new_snapshot_id
    payload["before"]["snapshot_id"] = new_snapshot_id
    payload["after"]["snapshot_id"] = new_snapshot_id
    _rehash_simulation(payload)

    with pytest.raises(Exception, match="source snapshot|before"):
        validate_rebalance_result_document(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("after", "short_market_value_base"), "1"),
        (("after", "exposure_views", "short", "sector", "TECH"), "0.1"),
        (("risk_changes", "status"), "STRUCTURAL_ONLY"),
        (("risk_changes", "exposure_views", "gross", "sector", "TECH"), "1"),
    ],
)
def test_signed_rebalance_semantics_reject_rehashed_authority_tampering(path, value):
    source = snapshot()
    payload = simulate_rebalance(
        source,
        RebalanceRequest(
            source.snapshot_id,
            "USD",
            changes=(RebalanceChange("US.AAA", "SHARE_DELTA", Decimal("-2")),),
            allow_short=True,
        ),
    ).to_dict()
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    _rehash_simulation(payload)
    with pytest.raises(Exception):
        validate_rebalance_result_document(payload)


def test_trade_semantics_reject_rehashed_fifo_and_required_quality_tampering():
    closed = build_trade_review(
        orders=[],
        deals=[
            _deal("d1", "o1", "BUY", "2", "10", "2026-08-03 10:00:00"),
            _deal("d2", "o2", "SELL", "1", "11", "2026-08-04 10:00:00"),
        ],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
    ).to_dict()
    bad_holding = deepcopy(closed)
    bad_holding["round_trips"][0]["holding_seconds"] = "1"
    _rehash_trade(bad_holding)
    with pytest.raises(Exception):
        validate_trade_review_document(bad_holding)
    bad_open = deepcopy(closed)
    bad_open["open_lots"][0]["quantity"] = "999"
    _rehash_trade(bad_open)
    with pytest.raises(Exception):
        validate_trade_review_document(bad_open)

    unmatched = build_trade_review(
        orders=[],
        deals=[_deal("d1", "o1", "SELL", "1", "10", "2026-08-03 10:00:00")],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
    ).to_dict()
    unmatched["data_quality_notes"] = []
    unmatched["data_quality_status"] = "COMPLETE"
    _rehash_trade(unmatched)
    with pytest.raises(Exception):
        validate_trade_review_document(unmatched)


def test_trade_quality_issues_are_structured_and_fees_mark_gross_pre_fee_pnl():
    report = build_trade_review(
        orders=[],
        deals=[
            _deal("d1", "o1", "BUY", "1", "10", "2026-08-03 10:00:00"),
            _deal("d2", "o2", "SELL", "1", "12", "2026-08-04 10:00:00"),
        ],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
        base_currency="USD",
    )
    issues = {item["code"]: item["detail"] for item in report.quality_issues}
    assert "FEES_UNAVAILABLE" in issues
    assert "gross pre-fee" in issues["FEES_UNAVAILABLE"]
    assert report.provenance["realized_pl_basis"] == "GROSS_PRE_FEE"

    payload = report.to_dict()
    payload["quality_issues"] = []
    payload["data_quality_notes"] = ["all perfect"]
    _rehash_trade(payload)
    with pytest.raises(Exception):
        validate_trade_review_document(payload)

    rewritten = report.to_dict()
    rewritten["quality_issues"][0]["detail"] = "all evidence is perfect"
    _rehash_trade(rewritten)
    with pytest.raises(Exception, match="quality"):
        validate_trade_review_document(rewritten)

    rewritten_notes = report.to_dict()
    rewritten_notes["data_quality_notes"] = ["all evidence is perfect"]
    _rehash_trade(rewritten_notes)
    with pytest.raises(Exception, match="quality"):
        validate_trade_review_document(rewritten_notes)


def test_trade_analysis_budget_fails_closed_with_injected_clock():
    ticks = [0.0, 0.0, 11.0]
    with pytest.raises(Exception, match="ANALYSIS_BUDGET_EXCEEDED"):
        build_trade_review(
            orders=[],
            deals=[_deal("d1", "o1", "BUY", "1", "10", "2026-08-03 10:00:00")],
            start=date(2026, 8, 1),
            end=date(2026, 8, 27),
            max_analysis_seconds=10,
            analysis_clock=lambda: ticks.pop(0) if ticks else 11.0,
        )


def test_trade_analysis_operation_budget_fails_before_metrics_are_constructed():
    with pytest.raises(Exception, match="ANALYSIS_BUDGET_EXCEEDED"):
        build_trade_review(
            orders=[],
            deals=[_deal("d1", "o1", "BUY", "1", "10", "2026-08-03 10:00:00")],
            start=date(2026, 8, 1),
            end=date(2026, 8, 27),
            max_analysis_operations=1,
        )


def test_twenty_thousand_valid_rows_complete_within_default_analysis_budget():
    deals = [
        _deal(
            f"d{index}",
            f"o{index}",
            "BUY",
            "1",
            "10",
            "2026-08-03 10:00:00",
        )
        for index in range(20_000)
    ]
    report = build_trade_review(
        orders=[],
        deals=deals,
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
    )

    assert report.metrics.fill_count == 20_000
    assert report.metrics.trade_count == 20_000


def test_ten_thousand_symbol_sell_buy_review_self_validates_at_public_row_cap():
    deals = []
    for index in range(10_000):
        sell = _deal(
            f"sell-{index}",
            f"sell-order-{index}",
            "SELL",
            "1",
            "10",
            "2026-08-03 09:31:00",
        )
        buy = _deal(
            f"buy-{index}",
            f"buy-order-{index}",
            "BUY",
            "1",
            "9",
            "2026-08-03 10:01:00",
        )
        sell["code"] = f"US.S{index}"
        buy["code"] = f"US.S{index}"
        deals.extend((sell, buy))

    report = build_trade_review(
        orders=[],
        deals=deals,
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
        acquired_at=datetime(2026, 8, 28, 16, tzinfo=timezone.utc),
    )

    assert report.metrics.fill_count == 20_000
    assert report.metrics.trade_count == 20_000
    assert len(report.behavior_findings) == 10_003
    validate_trade_review_document(report.to_dict())


def test_direct_trade_analysis_rejects_rows_above_hard_cap_before_normalization():
    row = _deal("duplicate", "o1", "BUY", "1", "10", "2026-08-03 10:00:00")
    with pytest.raises(Exception, match="ANALYSIS_ROW_LIMIT_EXCEEDED"):
        build_trade_review(
            orders=[],
            deals=[row] * 20_001,
            start=date(2026, 8, 1),
            end=date(2026, 8, 27),
        )


def test_trade_semantic_validation_rejects_oversized_document_before_deep_validation():
    report = build_trade_review(
        orders=[],
        deals=[_deal("d1", "o1", "BUY", "1", "10", "2026-08-03 10:00:00")],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
    ).to_dict()
    report["normalized_fills"] *= 20_001
    with pytest.raises(Exception, match="VALIDATION_ROW_LIMIT_EXCEEDED"):
        validate_trade_review_document(report)


def test_trade_semantic_validation_rejects_oversized_secondary_array_in_preflight():
    report = build_trade_review(
        orders=[],
        deals=[_deal("d1", "o1", "BUY", "1", "10", "2026-08-03 10:00:00")],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
    ).to_dict()
    report["policy_checks"] = [
        {"name": "max_trades_per_day", "observed": 1, "limit": 1, "status": "PASS"}
    ] * 20_001

    with pytest.raises(Exception, match="VALIDATION_SHAPE_LIMIT_EXCEEDED"):
        validate_trade_review_document(report)


def test_trade_review_schema_publishes_business_collection_limits():
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas" / "trade_review.schema.json").read_text()
    )
    properties = schema["properties"]
    definitions = schema["$defs"]

    assert properties["normalized_orders"]["maxItems"] == 20_000
    assert properties["normalized_fills"]["maxItems"] == 20_000
    assert properties["order_groups"]["maxItems"] == 20_000
    assert properties["round_trips"]["maxItems"] == 20_000
    assert properties["open_lots"]["maxItems"] == 20_000
    assert properties["policy_checks"]["maxItems"] == 6
    assert properties["behavior_findings"]["maxItems"] == 10_007
    assert properties["unverified_interpretations"]["maxItems"] == 16
    assert properties["external_source_evidence"]["maxItems"] == 20
    assert properties["quality_issues"]["maxItems"] == 20
    assert properties["data_quality_notes"]["maxItems"] == 20
    for name in (
        "requested_contexts",
        "successful_contexts",
        "failed_contexts",
        "omitted_contexts",
    ):
        assert definitions["coverage"]["properties"][name]["maxItems"] == 20
    assert definitions["behaviorFinding"]["properties"]["affected_events"]["maxItems"] == 20_000
    assert definitions["behaviorFinding"]["properties"]["affected_symbols"]["maxItems"] == 20_000
    assert definitions["metrics"]["properties"]["notional_by_currency"]["maxProperties"] == 64
    assert definitions["metrics"]["properties"]["per_symbol_turnover"]["maxProperties"] == 20_000
    assert definitions["provenance"]["properties"]["audited_source_digests"]["maxProperties"] == 4


def test_trade_review_schema_bounds_every_array_and_map_shape():
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas" / "trade_review.schema.json").read_text()
    )
    pending = [("$", schema)]
    while pending:
        path, node = pending.pop()
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        types = (
            set(node_type)
            if isinstance(node_type, list)
            else {node_type}
            if isinstance(node_type, str)
            else set()
        )
        if "array" in types:
            assert "maxItems" in node, path
        if "object" in types:
            assert "maxProperties" in node, path
        pending.extend(
            (f"{path}.{key}", child) for key, child in node.items() if isinstance(child, dict)
        )
        pending.extend(
            (f"{path}.{key}[]", child)
            for key, children in node.items()
            if isinstance(children, list)
            for child in children
            if isinstance(child, dict)
        )


def test_trade_semantic_validation_preflights_oversized_nested_currency_map():
    report = build_trade_review(
        orders=[],
        deals=[_deal("d1", "o1", "BUY", "1", "10", "2026-08-03 10:00:00")],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
    ).to_dict()
    report["metrics"]["per_symbol_turnover"] = {f"US.S{index}": "0" for index in range(20_001)}

    with pytest.raises(Exception, match="VALIDATION_SHAPE_LIMIT_EXCEEDED"):
        validate_trade_review_document(report)


def test_trade_semantic_validation_preflights_oversized_currency_bucket_body():
    report = build_trade_review(
        orders=[],
        deals=[_deal("d1", "o1", "BUY", "1", "10", "2026-08-03 10:00:00")],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
    ).to_dict()
    report["metrics"]["notional_by_currency"]["USD"]["unexpected"] = "0"

    with pytest.raises(Exception, match="VALIDATION_SHAPE_LIMIT_EXCEEDED"):
        validate_trade_review_document(report)


def test_trade_semantic_validation_preflights_nested_behavior_event_array():
    report = build_trade_review(
        orders=[],
        deals=[_deal("d1", "o1", "BUY", "1", "10", "2026-08-03 10:00:00")],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
    ).to_dict()
    report["behavior_findings"] = [
        {
            "type": "SAME_DAY_REVERSAL",
            "severity": "INFORMATIONAL",
            "evidence": "observed",
            "verified": True,
            "affected_events": ["acct/order"] * 20_001,
            "affected_symbols": ["US.AAPL"],
            "period_start": "2026-08-03T14:00:00Z",
            "period_end": "2026-08-03T14:00:00Z",
        }
    ]

    with pytest.raises(Exception, match="VALIDATION_RELATIONSHIP_LIMIT_EXCEEDED"):
        validate_trade_review_document(report)


def test_trade_semantic_preflight_rejects_cartesian_behavior_evidence_explosion():
    deals = []
    for index in range(8):
        deal = _deal(
            f"d{index}",
            f"o{index}",
            "BUY",
            "1",
            "10",
            "2026-08-03 10:00:00",
        )
        deal["code"] = f"US.S{index}"
        deals.append(deal)
    report = build_trade_review(
        orders=[],
        deals=deals,
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
    ).to_dict()
    event_refs = [group["group_ref"] for group in report["order_groups"]]
    symbol_refs = [group["identity"] for group in report["order_groups"]]
    oversized_finding = {
        "type": "SAME_DAY_REVERSAL",
        "severity": "INFORMATIONAL",
        "evidence": "observed",
        "verified": True,
        "affected_events": event_refs,
        "affected_symbols": symbol_refs,
        "period_start": "2026-08-03T14:00:00Z",
        "period_end": "2026-08-03T14:00:00Z",
    }
    report["behavior_findings"] = [oversized_finding] * 11

    with pytest.raises(Exception, match="VALIDATION_RELATIONSHIP_LIMIT_EXCEEDED"):
        validate_trade_review_document(report)


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda report: report["coverage"].__setitem__(
                "requested_contexts", [{"account_ref": "acct", "market": "US"}] * 21
            ),
            "VALIDATION_SHAPE_LIMIT_EXCEEDED",
        ),
        (
            lambda report: report.__setitem__("quality_issues", [report["quality_issues"][0]] * 21),
            "VALIDATION_SHAPE_LIMIT_EXCEEDED",
        ),
        (
            lambda report: report.__setitem__(
                "data_quality_notes", [report["data_quality_notes"][0]] * 21
            ),
            "VALIDATION_SHAPE_LIMIT_EXCEEDED",
        ),
        (
            lambda report: report.__setitem__(
                "unverified_interpretations", ["motive unverified"] * 17
            ),
            "VALIDATION_SHAPE_LIMIT_EXCEEDED",
        ),
        (
            lambda report: report["metrics"].__setitem__(
                "notional_by_currency",
                {
                    f"{chr(65 + index // 26)}{chr(65 + index % 26)}A": {
                        "buy": "0",
                        "sell": "0",
                    }
                    for index in range(65)
                },
            ),
            "VALIDATION_SHAPE_LIMIT_EXCEEDED",
        ),
        (
            lambda report: report["provenance"].__setitem__(
                "audited_source_digests", {f"source-{index}": "0" * 64 for index in range(5)}
            ),
            "VALIDATION_SHAPE_LIMIT_EXCEEDED",
        ),
        (
            lambda report: report["provenance"].__setitem__(
                "audited_source_version", "x" * (1024 * 1024 + 1)
            ),
            "VALIDATION_DOCUMENT_SIZE_EXCEEDED",
        ),
        (
            lambda report: report["provenance"].__setitem__(
                "audited_source_version", [None] * 120_001
            ),
            "VALIDATION_SHAPE_LIMIT_EXCEEDED",
        ),
    ],
)
def test_trade_semantic_preflight_closes_secondary_shape_bypasses(mutate, expected_code):
    report = build_trade_review(
        orders=[],
        deals=[_deal("d1", "o1", "BUY", "1", "10", "2026-08-03 10:00:00")],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
    ).to_dict()
    mutate(report)

    with pytest.raises(Exception, match=expected_code):
        validate_trade_review_document(report)


def test_trade_semantic_validation_accepts_twenty_context_business_limit():
    contexts = [{"account_ref": f"acct-{index:02d}", "market": "US"} for index in range(20)]
    report = build_trade_review(
        orders=[],
        deals=[],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
        acquired_at=datetime(2026, 8, 28, 16, tzinfo=timezone.utc),
        coverage_context={
            "accounts_requested": 20,
            "accounts_covered": 20,
            "contexts_requested": 20,
            "contexts_read": 20,
            "contexts_failed": 0,
            "contexts_omitted_budget": 0,
            "requested_contexts": contexts,
            "successful_contexts": contexts,
            "failed_contexts": [],
            "omitted_contexts": [],
        },
    )

    validate_trade_review_document(report.to_dict())


def test_trade_semantic_preflight_bounds_normalized_document_bytes_without_canonicalizing():
    report = build_trade_review(
        orders=[],
        deals=[_deal("d1", "o1", "BUY", "1", "10", "2026-08-03 10:00:00")],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
    ).to_dict()
    escaped_chunk = "\x00" * 900_000
    report["unverified_interpretations"] = [escaped_chunk] * 16

    with pytest.raises(Exception, match="VALIDATION_DOCUMENT_SIZE_EXCEEDED"):
        validate_trade_review_document(report)


def test_trade_semantic_validation_has_independent_injected_wall_budget():
    report = build_trade_review(
        orders=[],
        deals=[_deal("d1", "o1", "BUY", "1", "10", "2026-08-03 10:00:00")],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
    ).to_dict()
    ticks = [0.0, 31.0]
    with pytest.raises(Exception, match="VALIDATION_BUDGET_EXCEEDED"):
        validate_trade_review_document(
            report,
            max_validation_seconds=30,
            validation_clock=lambda: ticks.pop(0) if ticks else 31.0,
        )


def test_behavior_and_frequency_use_order_groups_not_partial_fill_rows():
    report = build_trade_review(
        orders=[],
        deals=[
            _deal("d1", "o1", "BUY", "4", "10", "2026-08-03 09:31:00"),
            _deal("d2", "o1", "BUY", "6", "12", "2026-08-03 09:32:00"),
            _deal("d3", "o2", "SELL", "10", "13", "2026-08-03 10:00:00"),
            _deal("d4", "o3", "BUY", "1", "14", "2026-08-03 10:10:00"),
        ],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
        policy=TradePolicy(
            market_open_no_trade_minutes=5,
            reentry_cooldown_minutes=30,
            max_trades_per_day=3,
            max_round_trips_per_symbol_day=1,
        ),
        acquired_at=datetime(2026, 8, 27, 16, 0, tzinfo=timezone.utc),
    )

    assert report.metrics.trade_count == 3
    assert report.metrics.fill_count == 4
    assert report.order_groups[0].quantity == Decimal("10")
    assert report.order_groups[0].average_price == Decimal("11.2")
    assert report.order_groups[0].effective_at == report.order_groups[0].first_fill_at
    assert report.metrics.same_day_reversals == 2
    assert report.metrics.same_day_reversal_rate == Decimal("2") / Decimal("3")
    assert report.metrics.reentries_within_cooldown == 1
    assert report.metrics.open_window_trades == 1
    assert report.metrics.open_window_trade_rate == Decimal("1") / Decimal("3")
    assert report.metrics.add_to_winner_count == 0
    assert report.metrics.add_to_loser_count == 0
    max_daily = next(
        check for check in report.policy_checks if check["name"] == "max_trades_per_day"
    )
    assert max_daily["observed"] == 3
    max_round_trips = next(
        check for check in report.policy_checks if check["name"] == "max_round_trips_per_symbol_day"
    )
    assert max_round_trips["observed"] == 1
    assert all(
        event.startswith("acct-a/")
        for finding in report.behavior_findings
        for event in finding.affected_events
    )

    forged_open_window = report.to_dict()
    forged_open_window["metrics"]["open_window_trades"] = 0
    forged_open_window["metrics"]["open_window_trade_rate"] = "0"
    for check in forged_open_window["policy_checks"]:
        if check["name"] == "market_open_no_trade_minutes":
            check["observed"] = 0
            check["status"] = "PASS"
    forged_open_window["behavior_findings"] = [
        finding
        for finding in forged_open_window["behavior_findings"]
        if finding["type"] != "EARLY_SESSION_TRADING"
    ]
    _rehash_trade(forged_open_window)
    with pytest.raises(Exception):
        validate_trade_review_document(forged_open_window)

    forged_behavior_type = report.to_dict()
    reversal = next(
        finding
        for finding in forged_behavior_type["behavior_findings"]
        if finding["type"] == "SAME_DAY_REVERSAL"
    )
    reversal["type"] = "SELL_THEN_REENTRY"
    _rehash_trade(forged_behavior_type)
    with pytest.raises(Exception):
        validate_trade_review_document(forged_behavior_type)


@pytest.mark.parametrize(
    ("status", "dealt", "fill_qty", "expected_conflict"),
    [
        ("FILLED_ALL", "10", "10", 0),
        ("FILLED_PART", "4", "4", 0),
        ("CANCELLED_PART", "4", "4", 0),
        ("WAITING_SUBMIT", "0", None, 0),
        ("SUBMITTING", "0", None, 0),
        ("SUBMITTED", "0", None, 0),
        ("CANCELLED_ALL", "0", None, 0),
        ("FAILED", "0", None, 0),
        ("DISABLED", "0", None, 0),
        ("DELETED", "0", None, 0),
        ("CANCELLED_PART", "0", None, 1),
        ("FAILED", "1", "1", 1),
        ("SUBMITTED", "1", "1", 1),
        ("FILLED_ALL", "9", "9", 1),
    ],
)
def test_order_status_reconciliation_uses_a_strict_quantity_matrix(
    status: str, dealt: str, fill_qty: str | None, expected_conflict: int
):
    deals = (
        []
        if fill_qty is None
        else [_deal("d1", "o1", "BUY", fill_qty, "10", "2026-08-03 10:00:00")]
    )
    report = build_trade_review(
        orders=[_order("o1", "BUY", "10", dealt, status=status)],
        deals=deals,
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
        acquired_at=datetime(2026, 8, 27, 16, 0, tzinfo=timezone.utc),
    )
    assert report.coverage["order_status_conflicts"] == expected_conflict
    validate_trade_review_document(report.to_dict())


@pytest.mark.parametrize("new_cash", [Decimal("0"), Decimal("100")])
def test_all_cash_snapshot_is_complete_zero_position_coverage(new_cash: Decimal):
    source = reconciled_snapshot(snapshot(), ())
    result = simulate_rebalance(
        source,
        RebalanceRequest(source.snapshot_id, source.base_currency, new_cash=new_cash),
    ).to_dict()

    assert result["coverage"]["source_positions"] == 0
    assert result["coverage"]["hypothetical_positions"] == 0
    assert result["after"]["cash_base"] == str(source.cash_base + new_cash)
    validate_rebalance_result_document(result)


def test_rebalance_quality_and_coverage_are_exactly_bound_to_embedded_snapshot():
    source = snapshot()
    source = replace(
        source,
        snapshot_id="",
        data_quality=DataQuality(
            "PARTIAL",
            warnings=("fixture warning",),
            missing_fields=("classification",),
            source_errors=("upstream timeout",),
        ),
    )
    source = replace(source, snapshot_id=snapshot_identity(source))
    result = simulate_rebalance(
        source, RebalanceRequest(source.snapshot_id, source.base_currency)
    ).to_dict()

    assert result["coverage"]["source_positions"] == len(result["source_snapshot"]["positions"])
    assert result["coverage"]["source_data_quality_status"] == "PARTIAL"
    assert any("fixture warning" in note for note in result["data_quality_notes"])
    assert any("classification" in note for note in result["data_quality_notes"])
    assert any("upstream timeout" in note for note in result["data_quality_notes"])

    forged = deepcopy(result)
    forged["coverage"]["source_positions"] = 999
    forged["coverage"]["source_data_quality_status"] = "COMPLETE"
    forged["data_quality_status"] = "COMPLETE"
    forged["data_quality_notes"] = ["all complete"]
    _rehash_simulation(forged)
    with pytest.raises(Exception):
        validate_rebalance_result_document(forged)


def test_trade_external_source_notes_are_preserved_as_unverified_escaped_evidence():
    report = build_trade_review(
        orders=[_order("o1", "BUY", "1", "1")],
        deals=[_deal("d1", "o1", "BUY", "1", "10", "2026-08-03 10:00:00")],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
        acquired_at=datetime(2026, 8, 27, 16, tzinfo=timezone.utc),
        source_notes=["<script>ambiguous transfer</script>"],
    )
    payload = report.to_dict()

    assert payload["external_source_evidence"] == [
        {
            "code": "EXTERNAL_SOURCE_NOTE",
            "detail": "<script>ambiguous transfer</script>",
            "verification_status": "UNVERIFIED_EXTERNAL",
        }
    ]
    assert "EXTERNAL_SOURCE_NOTE" in {item["code"] for item in payload["quality_issues"]}
    assert "externally provided" in report.to_markdown().lower()
    assert "&lt;script&gt;" in report.to_markdown()
    assert "externally provided" in report.to_human().lower()
    validate_trade_review_document(payload)

    forged = deepcopy(payload)
    forged["external_source_evidence"] = []
    _rehash_trade(forged)
    with pytest.raises(Exception):
        validate_trade_review_document(forged)


def test_trade_external_source_notes_are_bounded_and_sanitized():
    report = build_trade_review(
        orders=[],
        deals=[],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
        source_notes=["ambiguous\ntransfer\x1b[2J"],
    )
    assert report.external_source_evidence[0]["detail"] == "ambiguous transfer [2J"
    with pytest.raises(Exception, match="source note"):
        build_trade_review(
            orders=[],
            deals=[],
            start=date(2026, 8, 1),
            end=date(2026, 8, 27),
            source_notes=["x" * 513],
        )
