from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import pytest

from portfolio_analyst.identity import snapshot_identity
from portfolio_analyst.account_alias import AccountAliasError, AccountAliasStore
from portfolio_analyst.futu_adapter import FutuReadError
from portfolio_analyst.models import FxRateEvidence
from portfolio_analyst.performance import (
    AttributionInputError,
    ExternalFlow,
    ExternalFlowEvidence,
    analyze_performance,
    align_price_sequences,
    select_snapshot_interval,
)
from portfolio_analyst.schema import ReportSemanticError, validate_performance_document
from portfolio_analyst.snapshot_repository import SnapshotRepository
from portfolio_analyst.snapshot_repository import SnapshotRepositoryError
from portfolio_analyst.persistence import SnapshotStore
from portfolio_analyst.trade_review import build_trade_review
from portfolio_analyst.trade_history import HistoryBudgetLimit
from portfolio_analyst.trade_review import TradeReviewInputError
from portfolio_analyst import cli
from test_risk_analysis import reconciled_snapshot, snapshot as make_snapshot


FIXTURES = Path(__file__).parent / "fixtures"


def _identified(snapshot, **updates):
    candidate = replace(snapshot, snapshot_id="", **updates)
    return replace(candidate, snapshot_id=snapshot_identity(candidate))


def _endpoint(start, *, days=1, nav="1010", realized="2", unrealized="8"):
    positions = tuple(
        replace(
            item,
            market_value_local=item.market_value_local + Decimal(nav) - start.nav_base
            if index == 0
            else item.market_value_local,
            market_value_base=item.market_value_base + Decimal(nav) - start.nav_base
            if index == 0
            else item.market_value_base,
            unrealized_pl_local=Decimal(unrealized) if index == 0 else Decimal("0"),
            unrealized_pl_base=Decimal(unrealized) if index == 0 else Decimal("0"),
            realized_pl_local=Decimal(realized) if index == 0 else Decimal("0"),
            realized_pl_base=Decimal(realized) if index == 0 else Decimal("0"),
        )
        for index, item in enumerate(start.positions)
    )
    return _identified(
        reconciled_snapshot(start, positions),
        as_of=start.as_of + timedelta(days=days),
    )


def _pnl_start():
    base = make_snapshot()
    positions = tuple(
        replace(
            item,
            industry="GENERAL",
            themes=item.themes or ("OTHER",),
            unrealized_pl_local=Decimal("0"),
            unrealized_pl_base=Decimal("0"),
            realized_pl_local=Decimal("0"),
            realized_pl_base=Decimal("0"),
        )
        for item in base.positions
    )
    snapshot = reconciled_snapshot(base, positions)
    return _identified(
        snapshot,
        data_quality=replace(
            snapshot.data_quality,
            status="COMPLETE",
            warnings=(),
            missing_fields=(),
            source_errors=(),
        ),
    )


def test_interval_selection_is_date_inclusive_precise_and_reports_gaps():
    base = _pnl_start()
    first = _identified(base, as_of=datetime(2026, 8, 27, 23, 59, 59, 999999, tzinfo=timezone.utc))
    middle = _identified(base, as_of=datetime(2026, 8, 29, 8, tzinfo=timezone.utc))
    last = _identified(base, as_of=datetime(2026, 8, 30, 0, tzinfo=timezone.utc))

    selected = select_snapshot_interval(
        (last, first, middle), start=date(2026, 8, 27), end=date(2026, 8, 29)
    )

    assert selected.snapshots == (first, middle)
    assert selected.start_snapshot_id == first.snapshot_id
    assert selected.end_snapshot_id == middle.snapshot_id
    assert selected.start_date_covered and selected.end_date_covered
    assert selected.maximum_gap_seconds == 115201
    assert selected.continuous is True


def test_interval_deduplicates_identical_snapshot_id_but_rejects_same_as_of_conflict():
    start = _pnl_start()
    selected = select_snapshot_interval(
        (start, start), start=start.as_of.date(), end=start.as_of.date()
    )
    assert selected.snapshots == (start,)

    conflicting = _identified(_endpoint(start), as_of=start.as_of)
    with pytest.raises(AttributionInputError, match="same as-of|timestamp"):
        select_snapshot_interval(
            (start, conflicting), start=start.as_of.date(), end=start.as_of.date()
        )


def test_gap_prevents_accurate_pnl_mode_even_when_endpoint_fields_are_complete():
    start = _pnl_start()
    end = _endpoint(start, days=3)

    report = analyze_performance((start, end), start=start.as_of.date(), end=end.as_of.date())

    assert report.mode == "RECONSTRUCTED"
    assert report.confidence == "LOW"
    assert any(reason["code"] == "SNAPSHOT_CADENCE_GAP" for reason in report.quality_reasons)


def test_full_interval_trajectory_retains_middle_only_identity_and_blocks_exact_mode():
    start = _pnl_start()
    middle_position = replace(
        start.positions[0],
        symbol="DDD",
        name="Middle-only holding",
        sector="OTHER",
        industry="OTHER",
        themes=("TRANSIENT",),
    )
    middle = _identified(
        reconciled_snapshot(start, (*start.positions, middle_position)),
        as_of=start.as_of + timedelta(hours=12),
    )
    end = _after_us_close(_endpoint(start))

    report = analyze_performance(
        (start, middle, end),
        start=start.as_of.date(),
        end=end.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
    )

    transient = next(item for item in report.symbol_contributions if item.identity == "US.DDD")
    assert transient.status == "UNAVAILABLE"
    assert transient.method == "TRAJECTORY_POSITION_DISCONTINUITY"
    assert [item["present"] for item in transient.evidence["trajectory"]] == [False, True, False]
    assert report.coverage["trajectory_identities"] == 4
    assert report.mode == "RECONSTRUCTED"
    assert report.confidence == "LOW"
    assert any(
        item["code"] == "TRAJECTORY_POSITION_DISCONTINUITY" for item in report.quality_reasons
    )
    validate_performance_document(report.to_dict(), source_snapshots=(start, middle, end))


def test_exact_pnl_attribution_reconciles_symbols_sector_and_theme_but_no_flow_means_no_return():
    start = _pnl_start()
    end = _after_us_close(_endpoint(start))

    report = analyze_performance(
        (start, end),
        start=start.as_of.date(),
        end=end.as_of.date(),
        flow_evidence=ExternalFlowEvidence.unavailable("cash-flow adapter not configured"),
        price_provider=_CompleteAlignedPrices(),
    )
    payload = report.to_dict()

    assert report.mode == "ACCURATE_PNL"
    assert report.confidence == "HIGH"
    assert report.portfolio_return is None
    assert report.return_status == "UNAVAILABLE"
    assert report.irr is None
    assert report.irr_status == "NOT_CALCULATED"
    assert report.total_pnl_base == Decimal("10")
    assert report.realized_pnl_base == Decimal("2")
    assert report.unrealized_pnl_base == Decimal("8")
    assert report.pnl_reconciliation["status"] == "UNAVAILABLE"
    assert report.top_contributors[0].identity == "US.AAA"
    assert report.sector_contributions == {"HEALTH": Decimal("0"), "TECH": Decimal("10")}
    assert report.theme_full_contributions == {
        "AI": Decimal("10"),
        "CLOUD": Decimal("10"),
        "OTHER": Decimal("0"),
    }
    assert report.theme_split_contributions == {
        "AI": Decimal("5"),
        "CLOUD": Decimal("5"),
        "OTHER": Decimal("0"),
    }
    assert payload["coverage"] == {
        "source_snapshots": 2,
        "source_position_rows": 6,
        "canonical_identities": 3,
        "trajectory_identities": 3,
        "available_contributions": 3,
        "unavailable_contributions": 0,
        "price_requested": 3,
        "price_analyzed": 3,
        "price_omitted": 0,
        "price_available": 3,
        "price_aligned": 3,
        "trade_orders": 0,
        "trade_fills": 0,
        "trade_round_trips": 0,
        "flow_rows": 0,
    }
    golden = json.loads((FIXTURES / "performance_attribution_golden_snippet.json").read_text())
    assert {key: payload[key] for key in golden} == golden
    validate_performance_document(payload)


def test_partial_source_data_quality_is_embedded_and_cannot_authorize_high_confidence():
    start = _pnl_start()
    start = _identified(
        start,
        data_quality=replace(
            start.data_quality,
            status="PARTIAL",
            warnings=(*start.data_quality.warnings, "bounded warning"),
        ),
    )
    end = _after_us_close(_endpoint(start))

    report = analyze_performance(
        (start, end),
        start=start.as_of.date(),
        end=end.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
    )

    assert report.mode == "RECONSTRUCTED"
    assert report.confidence == "LOW"
    assert report.source_snapshot_quality[0] == {
        "snapshot_id": start.snapshot_id,
        "status": "PARTIAL",
        "warnings": start.data_quality.warnings,
        "missing_fields": start.data_quality.missing_fields,
        "source_errors": (),
    }
    assert any(item["code"] == "SOURCE_DATA_QUALITY_INCOMPLETE" for item in report.quality_reasons)
    validate_performance_document(report.to_dict(), source_snapshots=(start, end))


def test_cross_currency_contribution_uses_reconciled_base_pnl_without_mixing_local_values():
    from portfolio_analyst.models import FxRateEvidence

    start = _pnl_start()
    positions = tuple(
        replace(
            item,
            currency="HKD",
            fx_to_base=Decimal("0.125"),
            average_cost_local=Decimal("3200"),
            last_price_local=Decimal("3200"),
            market_value_local=Decimal("3200"),
        )
        if item.symbol == "AAA"
        else item
        for item in start.positions
    )
    start = _identified(
        reconciled_snapshot(start, positions),
        fx_evidence=start.fx_evidence
        + (FxRateEvidence("HKD", "USD", Decimal("0.125"), "TEST", start.as_of),),
    )
    end_positions = tuple(
        replace(
            item,
            last_price_local=Decimal("3280"),
            market_value_local=Decimal("3280"),
            market_value_base=Decimal("410"),
            realized_pl_local=Decimal("16"),
            realized_pl_base=Decimal("2"),
            unrealized_pl_local=Decimal("64"),
            unrealized_pl_base=Decimal("8"),
        )
        if item.symbol == "AAA"
        else item
        for item in start.positions
    )
    end_value = reconciled_snapshot(start, end_positions)
    end = _identified(end_value, as_of=start.as_of + timedelta(days=1))

    report = analyze_performance((start, end), start=start.as_of.date(), end=end.as_of.date())
    aaa = next(item for item in report.symbol_contributions if item.identity == "US.AAA")

    assert aaa.realized_pnl_base == Decimal("2")
    assert aaa.unrealized_pnl_base == Decimal("8")
    assert aaa.pnl_base == Decimal("10")


def test_verified_external_flows_enable_documented_twr_and_internal_transfer_is_excluded():
    start = _pnl_start()
    middle = _endpoint(start, nav="1110", realized="0", unrealized="10")
    end = _after_us_close(
        _identified(
            _endpoint(middle, nav="1165", realized="0", unrealized="65"),
            as_of=start.as_of + timedelta(days=2),
        )
    )
    evidence = ExternalFlowEvidence.complete(
        source="AUDITED_TEST",
        coverage_start=start.as_of,
        coverage_end=end.as_of,
        flows=(
            ExternalFlow(
                flow_id="flow-1",
                occurred_at=middle.as_of,
                flow_type="DEPOSIT",
                currency="USD",
                amount_local=Decimal("100"),
                fx_to_base=Decimal("1"),
                amount_base=Decimal("100"),
                provenance="AUDITED_TEST",
            ),
            ExternalFlow(
                flow_id="flow-2",
                occurred_at=middle.as_of,
                flow_type="INTERNAL_TRANSFER",
                currency="USD",
                amount_local=Decimal("999"),
                fx_to_base=Decimal("1"),
                amount_base=Decimal("999"),
                provenance="AUDITED_TEST",
            ),
        ),
    )

    report = analyze_performance(
        (start, middle, end),
        start=start.as_of.date(),
        end=end.as_of.date(),
        flow_evidence=evidence,
        price_provider=_CompleteAlignedPrices(),
    )

    # (1110 - 100) / 1000 - 1 = 1%; 1165 / 1110 - 1; chain the subperiod factors.
    assert report.return_status == "AVAILABLE"
    assert report.portfolio_return == Decimal("0.060045045045045045045045046")
    assert report.return_method == "TWR_END_OF_SUBPERIOD_FLOW_ADJUSTMENT"
    assert report.external_flow_base == Decimal("100")
    assert report.internal_transfer_base == Decimal("999")
    assert report.pnl_reconciliation["status"] == "MATCHED"
    assert report.mode == "ACCURATE_PNL"
    validate_performance_document(report.to_dict())


def test_complete_flows_cannot_authorize_twr_after_pnl_reconciliation_downgrade():
    start = _pnl_start()
    end = _after_us_close(_endpoint(start, nav="1110", realized="0", unrealized="10"))
    evidence = ExternalFlowEvidence.complete(
        source="AUDITED_TEST",
        coverage_start=start.as_of,
        coverage_end=end.as_of,
        flows=(),
    )

    report = analyze_performance(
        (start, end),
        start=start.as_of.date(),
        end=end.as_of.date(),
        flow_evidence=evidence,
        price_provider=_CompleteAlignedPrices(),
    )

    assert report.mode == "RECONSTRUCTED"
    assert report.pnl_reconciliation["status"] == "MISMATCH"
    assert report.return_status == "UNAVAILABLE"
    assert report.return_method is None
    assert report.portfolio_return is None
    validate_performance_document(report.to_dict(), source_snapshots=(start, end))


def test_theme_split_allocates_sorted_final_residual_for_exact_additivity():
    start = _pnl_start()
    positions = tuple(
        replace(item, themes=("ZETA", "ALPHA", "MIDDLE")) if item.symbol == "AAA" else item
        for item in start.positions
    )
    start = _identified(reconciled_snapshot(start, positions), as_of=start.as_of)
    end = _after_us_close(_endpoint(start))

    report = analyze_performance(
        (start, end),
        start=start.as_of.date(),
        end=end.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
    )

    split = report.theme_split_contributions
    assert split["ALPHA"] == split["MIDDLE"]
    assert split["ZETA"] == Decimal("10") - split["ALPHA"] - split["MIDDLE"]
    assert split["ALPHA"] + split["MIDDLE"] + split["ZETA"] == Decimal("10")
    validate_performance_document(report.to_dict(), source_snapshots=(start, end))


def test_explicit_verified_no_flow_evidence_enables_return_without_inference():
    start = _pnl_start()
    end = _after_us_close(_endpoint(start))
    evidence = ExternalFlowEvidence.complete(
        source="AUDITED_TEST",
        coverage_start=start.as_of,
        coverage_end=end.as_of,
        flows=(),
    )

    report = analyze_performance(
        (start, end),
        start=start.as_of.date(),
        end=end.as_of.date(),
        flow_evidence=evidence,
        price_provider=_CompleteAlignedPrices(),
    )

    assert report.return_status == "AVAILABLE"
    assert report.portfolio_return == Decimal("0.01")
    assert report.external_flow_base == Decimal("0")
    assert report.pnl_reconciliation["status"] == "MATCHED"
    assert report.mode == "ACCURATE_PNL"
    validate_performance_document(report.to_dict(), source_snapshots=(start, end))


def test_opened_or_closed_position_cannot_be_silently_claimed_as_exact():
    start = _pnl_start()
    end = _endpoint(start)
    end = _identified(reconciled_snapshot(end, end.positions[:-1]), as_of=end.as_of)

    report = analyze_performance((start, end), start=start.as_of.date(), end=end.as_of.date())

    assert report.mode == "RECONSTRUCTED"
    assert report.confidence == "LOW"
    missing = next(item for item in report.symbol_contributions if item.identity == "US.CCC")
    assert missing.status == "UNAVAILABLE"
    assert missing.pnl_base is None
    assert any(
        reason["code"] == "TRAJECTORY_POSITION_DISCONTINUITY" for reason in report.quality_reasons
    )
    validate_performance_document(report.to_dict(), source_snapshots=(start, end))


def test_zero_current_holding_with_realized_pnl_is_not_silently_dropped():
    start = _pnl_start()
    template = start.positions[-1]
    closed_start = replace(
        template,
        symbol="DDD",
        quantity=Decimal("0"),
        can_sell_quantity=Decimal("0"),
        market_value_local=Decimal("0"),
        market_value_base=Decimal("0"),
        unrealized_pl_local=Decimal("0"),
        unrealized_pl_base=Decimal("0"),
        realized_pl_local=Decimal("0"),
        realized_pl_base=Decimal("0"),
        weight=Decimal("0"),
    )
    start = _identified(reconciled_snapshot(start, start.positions + (closed_start,)))
    end = _endpoint(start)
    end_positions = tuple(
        replace(item, realized_pl_local=Decimal("5"), realized_pl_base=Decimal("5"))
        if item.symbol == "DDD"
        else item
        for item in end.positions
    )
    end = _identified(reconciled_snapshot(end, end_positions), as_of=end.as_of)

    report = analyze_performance((start, end), start=start.as_of.date(), end=end.as_of.date())
    closed = next(item for item in report.symbol_contributions if item.identity == "US.DDD")

    assert closed.status == "AVAILABLE"
    assert closed.realized_pnl_base == Decimal("5")
    assert closed.unrealized_pnl_base == Decimal("0")
    validate_performance_document(report.to_dict(), source_snapshots=(start, end))


def test_intermediate_cumulative_pnl_regression_is_explicitly_unavailable():
    start = _pnl_start()
    middle_positions = tuple(
        replace(
            item,
            realized_pl_local=Decimal("5"),
            realized_pl_base=Decimal("5"),
            unrealized_pl_local=Decimal("4"),
            unrealized_pl_base=Decimal("4"),
        )
        if item.symbol == "AAA"
        else item
        for item in start.positions
    )
    middle = _identified(
        reconciled_snapshot(start, middle_positions), as_of=start.as_of + timedelta(hours=12)
    )
    end = _after_us_close(_endpoint(start, realized="2", unrealized="8"))

    report = analyze_performance(
        (start, middle, end),
        start=start.as_of.date(),
        end=end.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
    )

    aaa = next(item for item in report.symbol_contributions if item.identity == "US.AAA")
    assert aaa.status == "UNAVAILABLE"
    assert aaa.method == "TRAJECTORY_CUMULATIVE_PNL_DISCONTINUITY"
    assert report.mode == "RECONSTRUCTED"
    validate_performance_document(report.to_dict(), source_snapshots=(start, middle, end))


def test_embedded_trajectory_reconstructs_without_source_objects_after_report_rehash():
    start = _pnl_start()
    middle = _identified(start, as_of=start.as_of + timedelta(hours=12))
    end = _after_us_close(_endpoint(start))
    payload = analyze_performance(
        (start, middle, end),
        start=start.as_of.date(),
        end=end.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
    ).to_dict()
    aaa = next(item for item in payload["symbol_contributions"] if item["identity"] == "US.AAA")
    aaa["evidence"]["trajectory"][1]["legs"][0]["cumulative_realized_pnl_base"] = "999"
    payload["attribution_id"] = (
        "pa-"
        + hashlib.sha256(
            json.dumps(
                {key: value for key, value in payload.items() if key != "attribution_id"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:20]
    )

    with pytest.raises(ReportSemanticError, match="trajectory|performance"):
        validate_performance_document(payload)


def test_cross_account_leg_change_is_not_netted_into_exact_identity_pnl():
    start = _pnl_start()
    from portfolio_analyst.models import Account

    accounts = start.accounts + (
        Account("acct-other", "REAL", "MARGIN", "NORMAL", ("US",), "ACTIVE"),
    )
    start = _identified(start, accounts=accounts)
    end = _endpoint(start)
    positions = tuple(
        replace(item, account_ref="acct-other") if item.symbol == "AAA" else item
        for item in end.positions
    )
    end = _identified(reconciled_snapshot(end, positions, accounts=accounts), as_of=end.as_of)

    report = analyze_performance((start, end), start=start.as_of.date(), end=end.as_of.date())

    aaa = next(item for item in report.symbol_contributions if item.identity == "US.AAA")
    assert aaa.status == "UNAVAILABLE"
    assert aaa.method == "TRAJECTORY_ACCOUNT_LEGS_CHANGED"
    assert any(
        reason["code"] == "TRAJECTORY_ACCOUNT_LEGS_CHANGED" for reason in report.quality_reasons
    )


def test_schema_semantics_reject_tampered_totals_rankings_and_return_authority():
    start = _pnl_start()
    end = _endpoint(start)
    payload = analyze_performance(
        (start, end), start=start.as_of.date(), end=end.as_of.date()
    ).to_dict()
    payload["total_pnl_base"] = "999"
    with pytest.raises(ReportSemanticError, match="performance"):
        validate_performance_document(payload)

    payload = analyze_performance(
        (start, end), start=start.as_of.date(), end=end.as_of.date()
    ).to_dict()
    payload["symbol_contributions"][0]["realized_pnl_base"] = "3"
    payload["symbol_contributions"][0]["unrealized_pnl_base"] = "7"
    payload["attribution_id"] = (
        "pa-"
        + hashlib.sha256(
            json.dumps(
                {key: value for key, value in payload.items() if key != "attribution_id"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:20]
    )
    with pytest.raises(ReportSemanticError, match="source"):
        validate_performance_document(payload, source_snapshots=(start, end))

    payload = analyze_performance(
        (start, end), start=start.as_of.date(), end=end.as_of.date()
    ).to_dict()
    payload["portfolio_return"] = "0.1"
    with pytest.raises(ReportSemanticError, match="performance"):
        validate_performance_document(payload)


def test_rejects_duplicate_flow_ids_incomplete_coverage_and_inconsistent_accounts():
    start = _pnl_start()
    end = _endpoint(start)
    flow = ExternalFlow(
        "same", end.as_of, "WITHDRAWAL", "USD", Decimal("-1"), Decimal("1"), Decimal("-1"), "TEST"
    )
    with pytest.raises(AttributionInputError, match="duplicate"):
        ExternalFlowEvidence.complete(
            source="TEST", coverage_start=start.as_of, coverage_end=end.as_of, flows=(flow, flow)
        )

    wrong = _identified(
        end,
        accounts=tuple(replace(item, account_ref="acct-other") for item in end.accounts),
        cash_balances=tuple(replace(item, account_ref="acct-other") for item in end.cash_balances),
        positions=tuple(replace(item, account_ref="acct-other") for item in end.positions),
    )
    with pytest.raises(AttributionInputError, match="account identities"):
        analyze_performance((start, wrong), start=start.as_of.date(), end=wrong.as_of.date())


class _LookaheadPrices:
    def history(self, symbol, *, market, lookback, as_of, timeout_seconds=5):
        from portfolio_analyst.risk import PricePoint

        del symbol, market, lookback, timeout_seconds
        return (
            PricePoint(as_of - timedelta(days=1), Decimal("100")),
            PricePoint(as_of, Decimal("101")),
        )


class _CompleteAlignedPrices:
    def history(self, symbol, *, market, lookback, as_of, timeout_seconds=5):
        from portfolio_analyst.risk import PricePoint

        del symbol, market, lookback, timeout_seconds
        return (
            PricePoint(as_of - timedelta(days=1), Decimal("100")),
            PricePoint(as_of, Decimal("110")),
        )


def _after_us_close(snapshot):
    return _identified(snapshot, as_of=snapshot.as_of.replace(hour=22))


def _authoritative_closed_trade_review(
    current,
    *,
    code="US.DDD",
    currency="USD",
    buy_price="10",
    sell_price="12",
    event_type="TRADE",
    include_sell=True,
    corporate_action_evidence_status="AVAILABLE",
):
    account_ref = current.accounts[0].account_ref
    executions = [("BUY", buy_price, "2026-08-27 10:00:00")]
    if include_sell:
        executions.append(("SELL", sell_price, "2026-08-27 11:00:00"))
    orders = [
        {
            "order_id": f"order-{side.lower()}",
            "account_ref": account_ref,
            "code": code,
            "side": side,
            "status": "FILLED_ALL",
            "qty": "1",
            "price": price,
            "dealt_qty": "1",
            "create_time": at,
            "updated_time": at,
            "currency": currency,
        }
        for side, price, at in executions
    ]
    deals = [
        {
            "deal_id": f"fill-{side.lower()}",
            "order_id": f"order-{side.lower()}",
            "account_ref": account_ref,
            "code": code,
            "side": side,
            "qty": "1",
            "price": price,
            "create_time": at,
            "currency": currency,
            "fx_to_base": "1" if currency == current.base_currency else "0.125",
            "event_type": event_type,
        }
        for side, price, at in executions
    ]
    if include_sell:
        for index, position in enumerate(current.positions):
            open_id = f"current-{position.market}-{position.symbol}-{index}"
            opened_at = f"2026-08-27 09:00:{index:02d}"
            identity = f"{position.market}.{position.symbol}"
            orders.append(
                {
                    "order_id": f"order-{open_id}",
                    "account_ref": position.account_ref,
                    "code": identity,
                    "side": "BUY",
                    "status": "FILLED_ALL",
                    "qty": str(position.quantity),
                    "price": "109",
                    "dealt_qty": str(position.quantity),
                    "create_time": opened_at,
                    "updated_time": opened_at,
                    "currency": current.base_currency,
                }
            )
            deals.append(
                {
                    "deal_id": f"fill-{open_id}",
                    "order_id": f"order-{open_id}",
                    "account_ref": position.account_ref,
                    "code": identity,
                    "side": "BUY",
                    "qty": str(position.quantity),
                    "price": "109",
                    "create_time": opened_at,
                    "currency": current.base_currency,
                    "fx_to_base": "1",
                    "event_type": "TRADE",
                }
            )
    return build_trade_review(
        orders=orders,
        deals=deals,
        start=current.as_of.date() - timedelta(days=1),
        end=current.as_of.date(),
        base_currency=current.base_currency,
        portfolio_nav_base=current.nav_base,
        source_snapshot_id=current.snapshot_id,
        source_snapshot_as_of=current.as_of,
        acquired_at=current.as_of,
        corporate_action_evidence_status=corporate_action_evidence_status,
    )


def test_one_current_snapshot_with_aligned_prices_reconstructs_gross_unrealized_only():
    current = _after_us_close(_pnl_start())

    report = analyze_performance(
        (current,),
        start=current.as_of.date() - timedelta(days=1),
        end=current.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
    )

    assert report.mode == "RECONSTRUCTED"
    assert report.confidence == "LOW"
    assert report.return_status == "UNAVAILABLE"
    assert report.portfolio_return is None
    assert report.total_pnl_base == Decimal("30")
    assert all(
        item.method == "PRICE_QUANTITY_GROSS_UNREALIZED_ESTIMATE"
        for item in report.symbol_contributions
    )
    assert all(item.realized_pnl_base is None for item in report.symbol_contributions)
    assert all(item.unrealized_pnl_base == Decimal("10") for item in report.symbol_contributions)
    assert all(item.evidence["price_digest"] for item in report.symbol_contributions)
    assert any(
        reason["code"] == "RECONSTRUCTED_REALIZED_AND_EXITED_UNAVAILABLE"
        for reason in report.quality_reasons
    )
    validate_performance_document(report.to_dict(), source_snapshots=(current,))


def test_single_snapshot_flow_reconciliation_is_unavailable_even_with_complete_flow_coverage():
    current = _after_us_close(_pnl_start())
    flows = ExternalFlowEvidence.complete(
        source="FIXTURE",
        coverage_start=current.as_of - timedelta(days=1),
        coverage_end=current.as_of + timedelta(days=1),
    )

    report = analyze_performance(
        (current,),
        start=current.as_of.date() - timedelta(days=1),
        end=current.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
        flow_evidence=flows,
    )

    assert report.pnl_reconciliation["status"] == "UNAVAILABLE"
    assert report.pnl_reconciliation["expected_net_pnl_base"] is None
    assert report.return_status == "UNAVAILABLE"
    validate_performance_document(report.to_dict(), source_snapshots=(current,))


def test_authoritative_fifo_adds_closed_symbol_realized_gross_without_double_counting():
    current = _after_us_close(_pnl_start())
    trade_history = _authoritative_closed_trade_review(current)

    report = analyze_performance(
        (current,),
        start=current.as_of.date() - timedelta(days=1),
        end=current.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
        trade_history=trade_history,
    )

    closed = next(item for item in report.symbol_contributions if item.identity == "US.DDD")
    assert closed.method == "TRADE_FIFO_REALIZED_GROSS_PRE_FEE"
    assert closed.realized_pnl_base == Decimal("2")
    assert closed.unrealized_pnl_base == Decimal("0")
    assert report.total_pnl_base == Decimal("5")
    assert report.realized_pnl_base == Decimal("2")
    assert report.unrealized_pnl_base == Decimal("3")
    assert report.trade_evidence.status == "COMPLETE"
    assert report.trade_evidence.review_digest
    validate_performance_document(report.to_dict(), source_snapshots=(current,))


def test_authoritative_fifo_open_lot_uses_lot_cost_not_period_price_change():
    full = _after_us_close(_pnl_start())
    current = _identified(reconciled_snapshot(full, (full.positions[0],)), as_of=full.as_of)
    trade_history = _authoritative_closed_trade_review(
        current,
        code="US.AAA",
        buy_price="109",
        include_sell=False,
    )

    report = analyze_performance(
        (current,),
        start=current.as_of.date() - timedelta(days=1),
        end=current.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
        trade_history=trade_history,
    )

    contribution = report.symbol_contributions[0]
    assert contribution.identity == "US.AAA"
    assert contribution.method == "TRADE_FIFO_OPEN_LOT_UNREALIZED_GROSS_PRE_FEE"
    assert contribution.pnl_base == Decimal("1")
    assert contribution.realized_pnl_base == Decimal("0")
    assert contribution.unrealized_pnl_base == Decimal("1")
    assert contribution.evidence["open_lot_indexes"] == (0,)
    validate_performance_document(report.to_dict(), source_snapshots=(current,))


def test_cross_currency_fifo_retains_local_bucket_without_false_base_realized_total():
    current = _after_us_close(_pnl_start())
    trade_history = _authoritative_closed_trade_review(
        current,
        code="HK.00001",
        currency="HKD",
        buy_price="80",
        sell_price="88",
    )

    report = analyze_performance(
        (current,),
        start=current.as_of.date() - timedelta(days=1),
        end=current.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
        trade_history=trade_history,
    )

    closed = next(item for item in report.symbol_contributions if item.identity == "HK.00001")
    assert closed.method == "TRADE_FIFO_BASE_FX_UNAVAILABLE"
    assert closed.pnl_base is None
    assert closed.evidence["realized_local_by_currency"] == {"HKD": Decimal("8")}
    assert report.total_pnl_base == Decimal("3")
    assert report.realized_pnl_base is None
    assert report.contribution_status == "PARTIAL"
    validate_performance_document(report.to_dict(), source_snapshots=(current,))


def test_nontrade_or_conflicting_trade_history_fails_closed_without_losing_price_reconstruction():
    current = _after_us_close(_pnl_start())
    unsafe_history = _authoritative_closed_trade_review(current, event_type="TRANSFER")

    report = analyze_performance(
        (current,),
        start=current.as_of.date() - timedelta(days=1),
        end=current.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
        trade_history=unsafe_history,
    )

    assert report.trade_evidence.status == "UNAVAILABLE"
    assert report.trade_evidence.used_for_contributions is False
    assert report.total_pnl_base == Decimal("30")
    assert all(item.identity != "US.DDD" for item in report.symbol_contributions)
    assert any(item["code"] == "TRADE_HISTORY_UNAVAILABLE" for item in report.quality_reasons)
    validate_performance_document(report.to_dict(), source_snapshots=(current,))

    forged = report.to_dict()
    forged["trade_evidence"]["status"] = "COMPLETE"
    forged["trade_evidence"]["reason"] = None
    canonical = dict(forged)
    canonical.pop("attribution_id")
    forged["attribution_id"] = (
        "pa-"
        + hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:20]
    )
    with pytest.raises(ReportSemanticError, match="trade evidence authority"):
        validate_performance_document(forged, source_snapshots=(current,))


def test_unsupported_corporate_action_coverage_cannot_authorize_fifo():
    current = _after_us_close(_pnl_start())
    incomplete_history = _authoritative_closed_trade_review(
        current, corporate_action_evidence_status="UNSUPPORTED_EVIDENCE"
    )

    report = analyze_performance(
        (current,),
        start=current.as_of.date() - timedelta(days=1),
        end=current.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
        trade_history=incomplete_history,
    )

    assert report.trade_evidence.status == "UNAVAILABLE"
    assert all(item.identity != "US.DDD" for item in report.symbol_contributions)
    assert any(item["code"] == "TRADE_HISTORY_UNAVAILABLE" for item in report.quality_reasons)
    validate_performance_document(report.to_dict(), source_snapshots=(current,))


def test_trade_open_lot_missing_from_current_snapshot_fails_closed():
    current = _after_us_close(_pnl_start())
    unsafe_history = _authoritative_closed_trade_review(current, include_sell=False)

    report = analyze_performance(
        (current,),
        start=current.as_of.date() - timedelta(days=1),
        end=current.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
        trade_history=unsafe_history,
    )

    assert report.trade_evidence.status == "UNAVAILABLE"
    assert report.trade_evidence.used_for_contributions is False
    assert all(item.identity != "US.DDD" for item in report.symbol_contributions)
    validate_performance_document(report.to_dict(), source_snapshots=(current,))


def test_snapshot_cumulative_pnl_path_never_double_counts_fifo_trade_evidence():
    start = _pnl_start()
    end = _after_us_close(_endpoint(start))
    trade_history = _authoritative_closed_trade_review(end)

    report = analyze_performance(
        (start, end),
        start=start.as_of.date(),
        end=end.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
        trade_history=trade_history,
    )

    assert report.mode == "ACCURATE_PNL"
    assert report.total_pnl_base == Decimal("10")
    assert all(item.identity != "US.DDD" for item in report.symbol_contributions)
    assert report.trade_evidence.status == "COMPLETE"
    assert report.trade_evidence.used_for_contributions is False
    validate_performance_document(report.to_dict(), source_snapshots=(start, end))


def test_trade_source_timeout_does_not_invalidate_exact_snapshot_pnl_authority():
    start = _pnl_start()
    end = _after_us_close(_endpoint(start))

    report = analyze_performance(
        (start, end),
        start=start.as_of.date(),
        end=end.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
        trade_unavailable_reason="TRADE_SOURCE_TIMEOUT",
    )

    assert report.mode == "ACCURATE_PNL"
    assert report.confidence == "HIGH"
    assert report.quality_status == "PARTIAL"
    assert report.return_status == "UNAVAILABLE"
    assert report.portfolio_return is None
    assert report.trade_evidence.reason == "TRADE_SOURCE_TIMEOUT"
    assert any(item["code"] == "TRADE_HISTORY_UNAVAILABLE" for item in report.quality_reasons)
    validate_performance_document(report.to_dict(), source_snapshots=(start, end))


def test_embedded_normalized_trade_evidence_is_semantically_bound_after_rehash():
    current = _after_us_close(_pnl_start())
    payload = analyze_performance(
        (current,),
        start=current.as_of.date() - timedelta(days=1),
        end=current.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
        trade_history=_authoritative_closed_trade_review(current),
    ).to_dict()
    review = payload["trade_evidence"]["review"]
    review["normalized_fills"][1]["price"] = "999"
    trade_body = dict(review)
    trade_body.pop("review_id")
    review["review_id"] = (
        "tr_"
        + hashlib.sha256(
            json.dumps(trade_body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
    )
    review_digest = hashlib.sha256(
        json.dumps(review, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload["trade_evidence"]["review_digest"] = review_digest
    for item in payload["symbol_contributions"]:
        if "trade_review_digest" in item["evidence"]:
            item["evidence"]["trade_review_id"] = review["review_id"]
            item["evidence"]["trade_review_digest"] = review_digest
    canonical = dict(payload)
    canonical.pop("attribution_id")
    payload["attribution_id"] = (
        "pa-"
        + hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:20]
    )

    with pytest.raises(ReportSemanticError, match="performance|trade"):
        validate_performance_document(payload, source_snapshots=(current,))


def test_fifo_contribution_account_alias_is_bound_to_trade_and_snapshot_evidence():
    current = _after_us_close(_pnl_start())
    payload = analyze_performance(
        (current,),
        start=current.as_of.date() - timedelta(days=1),
        end=current.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
        trade_history=_authoritative_closed_trade_review(current),
    ).to_dict()
    closed = next(item for item in payload["symbol_contributions"] if item["identity"] == "US.DDD")
    closed["account_refs"] = ["acc_forged"]
    canonical = dict(payload)
    canonical.pop("attribution_id")
    payload["attribution_id"] = (
        "pa-"
        + hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:20]
    )

    with pytest.raises(ReportSemanticError, match="performance|source|account"):
        validate_performance_document(payload, source_snapshots=(current,))


def test_one_snapshot_without_any_usable_price_evidence_is_unavailable_not_an_input_error():
    current = _after_us_close(_pnl_start())

    report = analyze_performance(
        (current,),
        start=current.as_of.date() - timedelta(days=1),
        end=current.as_of.date(),
    )

    assert report.mode == "UNAVAILABLE"
    assert report.confidence == "NONE"
    assert report.total_pnl_base is None
    assert report.return_status == "UNAVAILABLE"
    validate_performance_document(report.to_dict(), source_snapshots=(current,))


def test_reconstruction_refuses_cross_currency_without_transaction_period_fx():
    current = _after_us_close(_pnl_start())
    positions = tuple(
        replace(
            item,
            currency="CAD",
            currency_provenance="USER_CONFIG",
            fx_to_base=Decimal("2"),
            average_cost_local=item.average_cost_local / Decimal("2"),
            last_price_local=item.last_price_local / Decimal("2"),
            market_value_local=item.market_value_local / Decimal("2"),
            unrealized_pl_local=Decimal("0"),
            realized_pl_local=Decimal("0"),
        )
        if item.symbol == "AAA"
        else item
        for item in current.positions
    )
    current = _identified(
        replace(
            current,
            positions=positions,
            fx_evidence=current.fx_evidence
            + (FxRateEvidence("CAD", "USD", Decimal("2"), "TEST", current.as_of),),
        )
    )

    report = analyze_performance(
        (current,),
        start=current.as_of.date() - timedelta(days=1),
        end=current.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
    )

    aaa = next(item for item in report.symbol_contributions if item.identity == "US.AAA")
    assert aaa.status == "UNAVAILABLE"
    assert aaa.pnl_base is None
    assert report.total_pnl_base == Decimal("20")
    assert any(
        reason["code"] == "RECONSTRUCTED_TRANSACTION_PERIOD_FX_UNAVAILABLE"
        and reason["scope"] == "US.AAA"
        for reason in report.quality_reasons
    )
    validate_performance_document(report.to_dict(), source_snapshots=(current,))


def test_two_snapshot_exact_pnl_requires_complete_common_price_intervals_for_high_confidence():
    start = _pnl_start()
    end = _after_us_close(_endpoint(start))

    without_prices = analyze_performance(
        (start, end), start=start.as_of.date(), end=end.as_of.date()
    )
    assert without_prices.mode == "RECONSTRUCTED"
    assert without_prices.confidence == "LOW"

    complete = analyze_performance(
        (start, end),
        start=start.as_of.date(),
        end=end.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
    )
    assert complete.mode == "ACCURATE_PNL"
    assert complete.confidence == "HIGH"
    assert complete.coverage["price_aligned"] == 3
    assert all(len(item.aligned_points) == 2 for item in complete.price_evidence)
    validate_performance_document(complete.to_dict(), source_snapshots=(start, end))


def test_reconstructed_aligned_price_evidence_is_bound_against_tampering():
    current = _after_us_close(_pnl_start())
    payload = analyze_performance(
        (current,),
        start=current.as_of.date() - timedelta(days=1),
        end=current.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
    ).to_dict()
    payload["price_evidence"][0]["aligned_points"][0]["adjusted_close"] = "99"
    payload["attribution_id"] = (
        "pa-"
        + hashlib.sha256(
            json.dumps(
                {key: value for key, value in payload.items() if key != "attribution_id"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:20]
    )

    with pytest.raises(ReportSemanticError, match="performance"):
        validate_performance_document(payload, source_snapshots=(current,))


def test_analysis_now_is_bound_and_rejects_future_snapshot_after_report_rehash():
    start = _pnl_start()
    end = _after_us_close(_endpoint(start))
    trusted_now = end.as_of + timedelta(hours=1)
    payload = analyze_performance(
        (start, end),
        start=start.as_of.date(),
        end=end.as_of.date(),
        price_provider=_CompleteAlignedPrices(),
        analysis_now=trusted_now,
    ).to_dict()
    assert payload["analysis_now"] == trusted_now.isoformat().replace("+00:00", "Z")

    payload["analysis_now"] = (end.as_of - timedelta(microseconds=1)).isoformat()
    payload["attribution_id"] = (
        "pa-"
        + hashlib.sha256(
            json.dumps(
                {key: value for key, value in payload.items() if key != "attribution_id"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:20]
    )
    with pytest.raises(ReportSemanticError, match="future snapshot|analysis_now"):
        validate_performance_document(payload, source_snapshots=(start, end))


def test_price_evidence_is_bounded_to_requested_dates_and_digest_traceable():
    start = _pnl_start()
    end = _endpoint(start, days=2)
    report = analyze_performance(
        (start, end),
        start=start.as_of.date(),
        end=end.as_of.date(),
        price_provider=_LookaheadPrices(),
    )
    assert len(report.price_evidence) == 3
    for evidence in report.price_evidence:
        assert evidence.first_date == date(2026, 8, 27)
        assert evidence.last_date == date(2026, 8, 28)
        assert evidence.rows == 2
        assert [(item.date, item.adjusted_close) for item in evidence.points] == [
            (date(2026, 8, 27), Decimal("100")),
            (date(2026, 8, 28), Decimal("101")),
        ]
        assert evidence.digest == hashlib.sha256(b"2026-08-27:100|2026-08-28:101").hexdigest()
    payload = report.to_dict()
    payload["price_evidence"][0]["points"][0]["adjusted_close"] = "999"
    payload["attribution_id"] = (
        "pa-"
        + hashlib.sha256(
            json.dumps(
                {key: value for key, value in payload.items() if key != "attribution_id"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:20]
    )
    with pytest.raises(ReportSemanticError, match="performance"):
        validate_performance_document(payload)


def test_price_series_with_post_cutoff_row_fails_closed_without_lookahead():
    class FutureRowProvider(_LookaheadPrices):
        def history(self, symbol, *, market, lookback, as_of, timeout_seconds=5):
            from portfolio_analyst.risk import PricePoint

            rows = super().history(
                symbol,
                market=market,
                lookback=lookback,
                as_of=as_of,
                timeout_seconds=timeout_seconds,
            )
            return rows + (PricePoint(as_of + timedelta(days=1), Decimal("999")),)

    start = _pnl_start()
    end = _endpoint(start, days=2)
    report = analyze_performance(
        (start, end),
        start=start.as_of.date(),
        end=end.as_of.date(),
        price_provider=FutureRowProvider(),
    )
    assert {item.status for item in report.price_evidence} == {"UNAVAILABLE"}
    assert all(item.rows == 0 and item.points == () for item in report.price_evidence)
    assert any(reason["code"] == "PRICE_DATA_PARTIAL" for reason in report.quality_reasons)


def test_price_request_budget_and_source_circuit_breaker_are_explicit(monkeypatch):
    import portfolio_analyst.performance as performance_module

    class FailedSource:
        def __init__(self):
            self.calls = 0

        def history(self, symbol, *, market, lookback, as_of, timeout_seconds=5):
            del symbol, market, lookback, as_of, timeout_seconds
            self.calls += 1
            raise RuntimeError("source unavailable")

    monkeypatch.setattr(performance_module, "MAX_PRICE_REQUESTS", 2)
    monkeypatch.setattr(performance_module, "MAX_CONSECUTIVE_PRICE_FAILURES", 1)
    provider = FailedSource()
    start = _pnl_start()
    end = _endpoint(start)
    report = analyze_performance(
        (start, end),
        start=start.as_of.date(),
        end=end.as_of.date(),
        price_provider=provider,
    )

    assert provider.calls == 1
    assert report.coverage["price_analyzed"] == 2
    assert report.coverage["price_omitted"] == 1
    assert {item.status for item in report.price_evidence} == {"UNAVAILABLE"}
    assert {reason["code"] for reason in report.quality_reasons} >= {
        "PRICE_BUDGET_LIMIT",
        "PRICE_DATA_PARTIAL",
    }
    validate_performance_document(report.to_dict())


def test_price_alignment_intersects_closes_before_any_return_interval_is_formed():
    from portfolio_analyst.risk import PricePoint

    aligned = align_price_sequences(
        {
            "US.AAA": (
                PricePoint(date(2026, 8, 25), Decimal("10")),
                PricePoint(date(2026, 8, 26), Decimal("11")),
                PricePoint(date(2026, 8, 28), Decimal("12")),
            ),
            "US.BBB": (
                PricePoint(date(2026, 8, 25), Decimal("20")),
                PricePoint(date(2026, 8, 27), Decimal("21")),
                PricePoint(date(2026, 8, 28), Decimal("22")),
            ),
        },
        start=date(2026, 8, 25),
        end=date(2026, 8, 28),
    )
    assert [item.date for item in aligned["US.AAA"]] == [
        date(2026, 8, 25),
        date(2026, 8, 28),
    ]
    assert [item.date for item in aligned["US.BBB"]] == [
        date(2026, 8, 25),
        date(2026, 8, 28),
    ]


def test_snapshot_range_budget_applies_after_canonical_timestamp_prefilter(tmp_path):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    repository = SnapshotRepository(home)
    base = _pnl_start()
    items = []
    for offset in range(370):
        item = _identified(base, as_of=base.as_of + timedelta(minutes=offset))
        repository.save(item)
        items.append(item)
    target = items[200]

    selected = repository.list_range(
        start=target.as_of,
        end=target.as_of.replace(microsecond=999999),
        limit=1,
        max_bytes=1024 * 1024,
    )

    assert selected == (target,)


def test_snapshot_range_ignores_outside_corrupt_body_but_fails_inside_and_misplaced(tmp_path):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    repository = SnapshotRepository(home)
    base = _pnl_start()
    outside = _identified(base, as_of=datetime(2026, 9, 1, tzinfo=timezone.utc))
    inside = _identified(base, as_of=datetime(2026, 10, 27, 12, tzinfo=timezone.utc))
    outside_path = repository.save(outside)
    inside_path = repository.save(inside)
    inside_bytes = inside_path.read_bytes()
    outside_path.write_text("{broken", encoding="utf-8")
    outside_path.chmod(0o600)

    assert repository.list_range(
        start=inside.as_of,
        end=inside.as_of.replace(microsecond=999999),
        limit=1,
        max_bytes=1024 * 1024,
    ) == (inside,)

    misplaced = outside_path.parent / inside_path.name
    misplaced.write_bytes(inside_bytes)
    misplaced.chmod(0o600)
    with pytest.raises(SnapshotRepositoryError, match="matching snapshot history path"):
        repository.list_range(
            start=outside.as_of,
            end=inside.as_of.replace(microsecond=999999),
            limit=3,
            max_bytes=1024 * 1024,
        )
    misplaced.unlink()

    inside_path.write_text("{broken", encoding="utf-8")
    inside_path.chmod(0o600)
    with pytest.raises(SnapshotRepositoryError, match="invalid"):
        repository.list_range(
            start=inside.as_of,
            end=inside.as_of.replace(microsecond=999999),
            limit=1,
            max_bytes=1024 * 1024,
        )


def test_range_repository_recharges_actual_opened_bytes_after_descriptor_preflight(
    monkeypatch, tmp_path
):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    repository = SnapshotRepository(home)
    snapshot = _pnl_start()
    path = repository.save(snapshot)
    initial_size = path.stat().st_size
    original = repository._read_anchored
    expanded = False

    def expand_during_open(relative, *args, **kwargs):
        nonlocal expanded
        if not expanded:
            path.write_bytes(path.read_bytes() + b" " * 128)
            path.chmod(0o600)
            expanded = True
        return original(relative, *args, **kwargs)

    monkeypatch.setattr(repository, "_read_anchored", expand_during_open)
    with pytest.raises(SnapshotRepositoryError, match="actual.*byte budget|changed"):
        repository.list_range(
            start=snapshot.as_of,
            end=snapshot.as_of,
            limit=1,
            max_bytes=initial_size + 64,
        )


def test_performance_cli_no_save_and_transactional_private_bundle(monkeypatch, tmp_path, capsys):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    start = _pnl_start()
    end = _endpoint(start)
    repository = SnapshotRepository(home)
    repository.save(start)
    repository.save(end)
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(home))
    monkeypatch.setattr(cli, "discover_futuapi_skill", lambda: None)

    assert (
        cli.main(
            ["performance", "--from", "2026-08-27", "--to", "2026-08-28", "--json", "--no-save"],
            trusted_now=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["return_status"] == "UNAVAILABLE"
    assert not (home / "reports").exists()

    assert (
        cli.main(
            [
                "performance",
                "--from",
                "2026-08-27",
                "--to",
                "2026-08-28",
                "--output",
                "reports/perf",
                "--json",
            ],
            trusted_now=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        == 0
    )
    capsys.readouterr()
    bundle = SnapshotStore(home).validate_bundle("reports/perf")
    assert set(bundle) == {"performance_attribution.json", "performance_attribution.md"}
    assert (home / "reports" / "perf" / "manifest.json").is_file()
    assert (
        oct((home / "reports" / "perf" / "performance_attribution.json").stat().st_mode & 0o777)
        == "0o600"
    )


def test_performance_cli_accepts_one_snapshot_and_reports_unavailable_without_prices(
    monkeypatch, tmp_path, capsys
):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    current = _pnl_start()
    SnapshotRepository(home).save(current)
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(home))
    monkeypatch.setattr(cli, "discover_futuapi_skill", lambda: None)

    assert (
        cli.main(
            [
                "performance",
                "--from",
                current.as_of.date().isoformat(),
                "--to",
                current.as_of.date().isoformat(),
                "--json",
                "--no-save",
            ],
            trusted_now=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "UNAVAILABLE"
    assert payload["source_snapshot_ids"] == [current.snapshot_id]


def test_performance_cli_rejects_future_snapshot_on_same_utc_date_with_structured_code(
    monkeypatch, tmp_path, capsys
):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    current = _after_us_close(_pnl_start())
    SnapshotRepository(home).save(current)
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(home))
    monkeypatch.setattr(cli, "discover_futuapi_skill", lambda: None)

    assert (
        cli.main(
            [
                "performance",
                "--from",
                current.as_of.date().isoformat(),
                "--to",
                current.as_of.date().isoformat(),
                "--json",
                "--no-save",
            ],
            trusted_now=current.as_of - timedelta(microseconds=1),
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "FUTURE_SNAPSHOT"


def test_performance_cli_collects_pinned_trade_history_with_existing_stable_alias_key(
    monkeypatch, tmp_path, capsys
):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    current = _after_us_close(_pnl_start())
    SnapshotRepository(home).save(current)
    AccountAliasStore(home).load_or_create()
    expected_trade_history = _authoritative_closed_trade_review(current)

    class FakeAdapter:
        def read_private_json(self, name, arguments, *, timeout=30):
            del timeout
            assert name == "get_kline"
            return {
                "code": arguments[0],
                "ktype": "1d",
                "source": "history",
                "data": [
                    {"time": "2026-08-26 00:00:00", "close": "100"},
                    {"time": "2026-08-27 00:00:00", "close": "110"},
                ],
            }

    class FakeTradeService:
        def __init__(self, adapter, *, aliaser):
            assert isinstance(adapter, FakeAdapter)
            assert aliaser.provenance == "STABLE"

        def review(self, **kwargs):
            assert kwargs["portfolio_snapshot"].snapshot_id == current.snapshot_id
            return expected_trade_history

    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(home))
    monkeypatch.setattr(cli, "discover_futuapi_skill", lambda: Path("/pinned/futuapi"))
    monkeypatch.setattr(cli, "FutuAdapter", lambda _path: FakeAdapter())
    monkeypatch.setattr(cli, "FutuTradeHistoryService", FakeTradeService)

    assert (
        cli.main(
            [
                "performance",
                "--from",
                "2026-08-26",
                "--to",
                "2026-08-27",
                "--json",
                "--no-save",
            ],
            trusted_now=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["trade_evidence"]["status"] == "COMPLETE"
    assert payload["trade_evidence"]["used_for_contributions"] is True
    assert any(item["identity"] == "US.DDD" for item in payload["symbol_contributions"])


@pytest.mark.parametrize(
    ("source_error", "expected_reason"),
    [
        (TimeoutError("secret timeout detail"), "TRADE_SOURCE_TIMEOUT"),
        (HistoryBudgetLimit("secret budget detail"), "TRADE_SOURCE_BUDGET_EXCEEDED"),
        (FutuReadError("secret adapter detail"), "TRADE_SOURCE_READ_FAILED"),
        (TradeReviewInputError("secret contract detail"), "TRADE_SOURCE_CONTRACT_INVALID"),
        (AccountAliasError("secret alias detail"), "TRADE_ALIAS_AUTHORITY_UNAVAILABLE"),
    ],
)
def test_performance_cli_structured_trade_acquisition_errors_degrade_without_leaking_detail(
    monkeypatch, tmp_path, capsys, source_error, expected_reason
):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    current = _after_us_close(_pnl_start())
    SnapshotRepository(home).save(current)
    AccountAliasStore(home).load_or_create()

    class FakeAdapter:
        def read_private_json(self, name, arguments, *, timeout=30):
            del timeout
            assert name == "get_kline"
            return {
                "code": arguments[0],
                "ktype": "1d",
                "source": "history",
                "data": [
                    {"time": "2026-08-26 00:00:00", "close": "100"},
                    {"time": "2026-08-27 00:00:00", "close": "110"},
                ],
            }

    class FailingTradeService:
        def __init__(self, adapter, *, aliaser):
            del adapter, aliaser

        def review(self, **kwargs):
            del kwargs
            raise source_error

    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(home))
    monkeypatch.setattr(cli, "discover_futuapi_skill", lambda: Path("/pinned/futuapi"))
    monkeypatch.setattr(cli, "FutuAdapter", lambda _path: FakeAdapter())
    monkeypatch.setattr(cli, "FutuTradeHistoryService", FailingTradeService)

    assert (
        cli.main(
            [
                "performance",
                "--from",
                "2026-08-26",
                "--to",
                "2026-08-27",
                "--json",
                "--no-save",
            ],
            trusted_now=datetime(2026, 8, 29, tzinfo=timezone.utc),
        )
        == 0
    )
    output = capsys.readouterr()
    assert "INTERNAL_ERROR" not in output.err
    assert "secret" not in output.out and "secret" not in output.err
    payload = json.loads(output.out)
    assert payload["trade_evidence"] == {
        "status": "UNAVAILABLE",
        "reason": expected_reason,
        "review_digest": None,
        "review": None,
        "used_for_contributions": False,
    }
    assert payload["mode"] == "RECONSTRUCTED"
    assert payload["confidence"] == "LOW"
    assert payload["quality_status"] == "PARTIAL"
    assert payload["return_status"] == "UNAVAILABLE"
    assert any(
        item
        == {
            "code": "TRADE_HISTORY_UNAVAILABLE",
            "scope": "trade_evidence",
            "detail": expected_reason,
        }
        for item in payload["quality_reasons"]
    )
    validate_performance_document(payload, source_snapshots=(current,))


def test_markdown_and_terminal_renderers_escape_untrusted_classification():
    start = _pnl_start()
    positions = tuple(
        replace(item, sector="[bad](javascript:alert(1))\x1b[31m\nnext") if index == 0 else item
        for index, item in enumerate(start.positions)
    )
    # Keep endpoint classification identical; snapshot schema permits bounded text.
    start = _identified(reconciled_snapshot(start, positions), as_of=start.as_of)
    end = _endpoint(start)
    report = analyze_performance((start, end), start=start.as_of.date(), end=end.as_of.date())
    human = report.to_human()
    markdown = report.to_markdown()
    assert "\x1b" not in human and "\x1b" not in markdown
    assert "javascript:alert" not in markdown or "\\(" in markdown


def test_performance_validator_operation_and_injected_wall_budgets_fail_closed():
    start = _pnl_start()
    end = _endpoint(start)
    payload = analyze_performance(
        (start, end), start=start.as_of.date(), end=end.as_of.date()
    ).to_dict()

    with pytest.raises(ReportSemanticError, match="performance validation budget exceeded"):
        validate_performance_document(payload, max_validation_operations=1)

    ticks = iter((0.0, 0.0, 11.0))
    with pytest.raises(ReportSemanticError, match="performance validation budget exceeded"):
        validate_performance_document(
            payload,
            source_snapshots=(start, end),
            max_validation_seconds=10.0,
            validation_clock=lambda: next(ticks, 11.0),
        )
