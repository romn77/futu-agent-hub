from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portfolio_analyst.trade_review import (
    TradePolicy,
    TradeReviewInputError,
    build_trade_review,
)


def _deal(
    deal_id: str,
    order_id: str,
    account_ref: str,
    side: str,
    qty: str,
    price: str,
    at: str,
    symbol: str = "US.AAA",
) -> dict[str, str]:
    return {
        "deal_id": deal_id,
        "order_id": order_id,
        "account_ref": account_ref,
        "code": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "create_time": at,
    }


def test_partial_fills_merge_and_fifo_round_trips_stay_account_isolated():
    deals = [
        _deal("d1", "buy-1", "acct-a", "BUY", "4", "10", "2026-08-03 10:00:00"),
        _deal("d2", "buy-1", "acct-a", "BUY", "6", "12", "2026-08-03 10:01:00"),
        _deal("d3", "sell-1", "acct-a", "SELL", "5", "15", "2026-08-04 11:00:00"),
        _deal("d4", "buy-2", "acct-b", "BUY", "5", "100", "2026-08-03 10:00:00"),
        _deal("d5", "sell-2", "acct-b", "SELL", "5", "90", "2026-08-04 11:00:00"),
    ]
    report = build_trade_review(
        orders=[], deals=deals, start=date(2026, 8, 1), end=date(2026, 8, 27)
    )

    assert report.metrics.order_count == 4
    assert report.metrics.fill_count == 5
    assert report.metrics.closed_trips == 3
    assert report.metrics.win_rate == Decimal("2") / Decimal("3")
    assert report.round_trips[-1].account_ref == "acct-b"
    assert report.round_trips[-1].realized_pl_local == Decimal("-50")
    assert report.open_lots[0].account_ref == "acct-a"
    assert report.open_lots[0].quantity == Decimal("5")
    assert report.order_groups[0].fill_count == 2
    assert report.order_groups[0].average_price == Decimal("11.2")


def test_exchange_timezone_open_window_reversal_reentry_and_observed_language():
    deals = [
        _deal("d1", "o1", "acct-a", "BUY", "1", "10", "2026-03-09 09:31:00"),
        _deal("d2", "o2", "acct-a", "SELL", "1", "11", "2026-03-09 10:00:00"),
        _deal("d3", "o3", "acct-a", "BUY", "1", "12", "2026-03-09 10:10:00"),
    ]
    report = build_trade_review(
        orders=[],
        deals=deals,
        start=date(2026, 3, 9),
        end=date(2026, 3, 9),
        policy=TradePolicy(market_open_no_trade_minutes=5, reentry_cooldown_minutes=30),
    )

    assert report.metrics.open_window_trades == 1
    assert report.metrics.same_day_reversals == 2
    assert report.metrics.same_day_reversal_rate == Decimal("2") / Decimal("3")
    assert report.metrics.reentries_within_cooldown == 1
    assert report.metrics.open_window_trade_rate == Decimal("1") / Decimal("3")
    assert report.policy_status == "BREACH"
    assert all("motive" not in finding.evidence.lower() for finding in report.behavior_findings)
    assert report.motive_status == "UNVERIFIED"


def test_open_lots_do_not_enter_closed_trip_win_rate_and_ambiguous_events_are_partial():
    report = build_trade_review(
        orders=[],
        deals=[_deal("d1", "o1", "acct-a", "BUY", "2", "10", "2026-08-03 10:00:00")],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
        source_notes=["transfer/corporate action evidence is ambiguous"],
    )
    assert report.metrics.closed_trips == 0
    assert report.metrics.win_rate is None
    assert report.data_quality_status == "PARTIAL"
    assert report.open_lots[0].quantity == Decimal("2")


def test_history_window_that_starts_with_a_sell_is_partial_not_a_fabricated_loss():
    report = build_trade_review(
        orders=[],
        deals=[_deal("d1", "o1", "acct-a", "SELL", "2", "10", "2026-08-03 10:00:00")],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
    )
    assert report.metrics.closed_trips == 0
    assert report.metrics.win_rate is None
    assert report.data_quality_status == "PARTIAL"
    assert any("opening inventory" in note for note in report.data_quality_notes)


def test_trade_input_contract_rejects_duplicate_ids_unknown_side_and_out_of_range():
    valid = _deal("same", "o1", "acct-a", "BUY", "1", "10", "2026-08-03 10:00:00")
    with pytest.raises(TradeReviewInputError):
        build_trade_review(
            orders=[], deals=[valid, valid], start=date(2026, 8, 1), end=date(2026, 8, 27)
        )
    with pytest.raises(TradeReviewInputError):
        build_trade_review(
            orders=[],
            deals=[{**valid, "deal_id": "d2", "side": "HOLD"}],
            start=date(2026, 8, 1),
            end=date(2026, 8, 27),
        )
    with pytest.raises(TradeReviewInputError):
        build_trade_review(
            orders=[],
            deals=[{**valid, "deal_id": "d3", "create_time": "2026-07-31 10:00:00"}],
            start=date(2026, 8, 1),
            end=date(2026, 8, 27),
        )


def test_mainland_exchange_prefix_is_canonicalized_to_cn_market_identity():
    report = build_trade_review(
        orders=[],
        deals=[
            _deal(
                "d1",
                "o1",
                "acct-a",
                "BUY",
                "1",
                "10",
                "2026-08-03 09:31:00",
                symbol="SH.600519",
            )
        ],
        start=date(2026, 8, 1),
        end=date(2026, 8, 27),
    )
    assert report.open_lots[0].identity == "CN.600519"


@pytest.mark.parametrize("at", ["2026-03-08 02:30:00", "2026-11-01 01:30:00"])
def test_naive_nonexistent_or_ambiguous_exchange_dst_time_fails_closed(at):
    day = date.fromisoformat(at[:10])
    with pytest.raises(TradeReviewInputError, match="ambiguous or nonexistent"):
        build_trade_review(
            orders=[],
            deals=[_deal("d1", "o1", "acct-a", "BUY", "1", "10", at)],
            start=day,
            end=day,
        )
