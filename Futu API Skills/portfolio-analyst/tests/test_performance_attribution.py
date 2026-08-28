from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import pytest

from portfolio_analyst.identity import snapshot_identity
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
            unrealized_pl_local=Decimal("0"),
            unrealized_pl_base=Decimal("0"),
            realized_pl_local=Decimal("0"),
            realized_pl_base=Decimal("0"),
        )
        for item in base.positions
    )
    return _identified(reconciled_snapshot(base, positions))


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


def test_gap_prevents_accurate_pnl_mode_even_when_endpoint_fields_are_complete():
    start = _pnl_start()
    end = _endpoint(start, days=3)

    report = analyze_performance((start, end), start=start.as_of.date(), end=end.as_of.date())

    assert report.mode == "RECONSTRUCTED"
    assert report.confidence == "LOW"
    assert any(reason["code"] == "SNAPSHOT_CADENCE_GAP" for reason in report.quality_reasons)


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
        "UNKNOWN": Decimal("0"),
    }
    assert report.theme_split_contributions == {
        "AI": Decimal("5"),
        "CLOUD": Decimal("5"),
        "UNKNOWN": Decimal("0"),
    }
    assert payload["coverage"] == {
        "source_snapshots": 2,
        "source_position_rows": 6,
        "canonical_identities": 3,
        "available_contributions": 3,
        "unavailable_contributions": 0,
        "price_requested": 3,
        "price_analyzed": 3,
        "price_omitted": 0,
        "price_available": 3,
        "price_aligned": 3,
        "flow_rows": 0,
    }
    golden = json.loads((FIXTURES / "performance_attribution_golden_snippet.json").read_text())
    assert {key: payload[key] for key in golden} == golden
    validate_performance_document(payload)


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
    assert any(reason["code"] == "POSITION_ENDPOINT_MISSING" for reason in report.quality_reasons)


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
    assert aaa.method == "ACCOUNT_LEGS_CHANGED"
    assert any(reason["code"] == "ACCOUNT_LEGS_CHANGED" for reason in report.quality_reasons)


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
            ["performance", "--from", "2026-08-27", "--to", "2026-08-28", "--json", "--no-save"]
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
            ]
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
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "UNAVAILABLE"
    assert payload["source_snapshot_ids"] == [current.snapshot_id]


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
