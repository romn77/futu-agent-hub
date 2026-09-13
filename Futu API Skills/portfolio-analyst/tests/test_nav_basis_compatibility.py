from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from portfolio_analyst.models import AccountNAV
from portfolio_analyst.performance import AttributionInputError, analyze_performance
from portfolio_analyst.snapshot_io import snapshot_from_document
from test_performance_attribution import _identified, _pnl_start


def _opend(source, *, days=1, nav="1100"):
    value = Decimal(nav)
    return _identified(
        source,
        schema_version="1.1",
        as_of=source.as_of + timedelta(days=days),
        nav_source="OPEND_TOTAL_ASSETS",
        nav_base=value,
        account_navs=(AccountNAV(source.accounts[0].account_ref, "USD", value, Decimal(1), value),),
        cash_weight=source.cash_base / value,
        cash_balances=tuple(
            replace(row, weight=row.amount_base / value) for row in source.cash_balances
        ),
        positions=tuple(
            replace(row, weight=row.market_value_base / value) for row in source.positions
        ),
        exposures={
            key: {label: amount * source.nav_base / value for label, amount in labels.items()}
            for key, labels in source.exposures.items()
        },
    )


@pytest.mark.parametrize("nav", ["1000", "1100"])
def test_mixed_nav_basis_is_rejected_even_if_totals_coincide(nav):
    before = _pnl_start()
    original = before.to_dict()
    after = _opend(before, nav=nav)
    for source in (before, after):
        snapshot_from_document(source.to_dict())
    with pytest.raises(AttributionInputError, match="NAV_BASIS_MISMATCH"):
        analyze_performance(
            (before, after),
            start=before.as_of.date(),
            end=after.as_of.date(),
            analysis_now=after.as_of,
        )
    assert before.to_dict() == original


def test_unselected_legacy_history_does_not_block_opend_period():
    legacy = _pnl_start()
    first = _opend(legacy, days=1)
    last = _opend(legacy, days=2)
    report = analyze_performance(
        (legacy, first, last),
        start=first.as_of.date(),
        end=last.as_of.date(),
        analysis_now=last.as_of,
    )
    assert report.pnl_reconciliation["nav_change_base"] == 0
