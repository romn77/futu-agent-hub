from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from portfolio_analyst.rebalance import RebalanceChange, RebalanceRequest, simulate_rebalance
from portfolio_analyst.schema import (
    validate_rebalance_result_document,
    validate_trade_review_document,
)
from portfolio_analyst.trade_review import TradePolicy, build_trade_review
from test_risk_analysis import snapshot


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
    assert report.metrics.average_win_base == Decimal("1")
    assert report.metrics.expectancy_base == Decimal("1")
    assert report.round_trips[0].currency == "HKD"
    assert report.round_trips[0].realized_pl_local == Decimal("8")
    assert report.round_trips[0].realized_pl_base == Decimal("1")
    validate_trade_review_document(report.to_dict())


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
