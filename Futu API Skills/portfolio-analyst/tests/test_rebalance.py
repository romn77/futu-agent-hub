from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from portfolio_analyst.rebalance import (
    RebalanceChange,
    RebalanceError,
    RebalanceRequest,
    simulate_rebalance,
)
from test_risk_analysis import snapshot


def test_target_amount_share_and_new_cash_are_deterministic_without_mutating_snapshot():
    source = snapshot()
    before = source.to_dict()
    request = RebalanceRequest(
        snapshot_id=source.snapshot_id,
        base_currency="USD",
        new_cash=Decimal("100"),
        changes=(
            RebalanceChange("US.AAA", "TARGET_WEIGHT", Decimal("0.30")),
            RebalanceChange("US.BBB", "AMOUNT_DELTA", Decimal("20")),
            RebalanceChange("US.CCC", "SHARE_DELTA", Decimal("-0.1")),
        ),
    )

    first = simulate_rebalance(source, request)
    second = simulate_rebalance(source, request)

    assert first.to_dict() == second.to_dict()
    assert source.to_dict() == before
    assert first.status == "HYPOTHETICAL"
    assert first.execution_disabled is True
    assert first.constraints["allow_margin"] is False
    assert first.constraints["allow_short"] is False
    assert first.constraints["new_cash_base"] == Decimal("100")
    assert first.feasible is True
    assert first.after.nav_base == Decimal("1100")
    assert first.after.cash_base >= 0
    assert "quantity" in first.after.positions["US.AAA"]
    assert first.before.snapshot_id == source.snapshot_id
    assert first.risk_changes["top_1"] != Decimal("0")
    with pytest.raises(TypeError):
        first.request["changes"][0]["value"] = Decimal("999")
    with pytest.raises(TypeError):
        first.evidence["US.AAA"]["fx_to_base"] = Decimal("999")


def test_no_margin_and_no_short_constraints_fail_closed_but_explicit_modes_are_permitted():
    source = snapshot()
    cash_hungry = RebalanceRequest(
        snapshot_id=source.snapshot_id,
        base_currency="USD",
        changes=(RebalanceChange("US.AAA", "AMOUNT_DELTA", Decimal("200")),),
    )
    shorting = RebalanceRequest(
        snapshot_id=source.snapshot_id,
        base_currency="USD",
        changes=(RebalanceChange("US.AAA", "SHARE_DELTA", Decimal("-2")),),
    )
    assert simulate_rebalance(source, cash_hungry).feasible is False
    with pytest.raises(RebalanceError, match="UNSUPPORTED_PORTFOLIO"):
        simulate_rebalance(source, shorting)
    assert simulate_rebalance(source, replace(cash_hungry, allow_margin=True)).feasible is True
    with pytest.raises(RebalanceError, match="UNSUPPORTED_PORTFOLIO"):
        simulate_rebalance(source, replace(shorting, allow_short=True))


def test_signed_source_portfolio_fails_closed_before_simulation():
    source = snapshot()
    signed_source = replace(
        source,
        positions=(replace(source.positions[0], quantity=Decimal("-1")), *source.positions[1:]),
    )
    request = RebalanceRequest(signed_source.snapshot_id, signed_source.base_currency)

    with pytest.raises(RebalanceError, match="UNSUPPORTED_PORTFOLIO"):
        simulate_rebalance(signed_source, request)


def test_duplicate_conflicting_changes_missing_identity_and_currency_are_rejected():
    source = snapshot()
    with pytest.raises(RebalanceError, match="duplicate"):
        RebalanceRequest(
            snapshot_id=source.snapshot_id,
            base_currency="USD",
            changes=(
                RebalanceChange("US.AAA", "TARGET_WEIGHT", Decimal("0.2")),
                RebalanceChange("US.AAA", "AMOUNT_DELTA", Decimal("1")),
            ),
        )
    with pytest.raises(RebalanceError, match="market-qualified"):
        RebalanceChange("AAA", "AMOUNT_DELTA", Decimal("1"))
    with pytest.raises(RebalanceError, match="currency"):
        simulate_rebalance(
            source,
            RebalanceRequest(source.snapshot_id, "HKD", changes=()),
        )


def test_sell_only_and_new_cash_only_constraints_are_enforced():
    source = snapshot()
    buy = RebalanceChange("US.AAA", "AMOUNT_DELTA", Decimal("1"))
    assert (
        simulate_rebalance(
            source,
            RebalanceRequest(source.snapshot_id, "USD", changes=(buy,), sell_only=True),
        ).feasible
        is False
    )
    assert (
        simulate_rebalance(
            source,
            RebalanceRequest(
                source.snapshot_id,
                "USD",
                new_cash=Decimal("10"),
                changes=(RebalanceChange("US.AAA", "AMOUNT_DELTA", Decimal("11")),),
                new_cash_only=True,
            ),
        ).feasible
        is False
    )


def test_missing_or_invalid_price_fails_and_output_recalculates_exposures():
    source = snapshot()
    bad = replace(source.positions[0], last_price_local=Decimal("0"))
    invalid_source = replace(source, positions=(bad, *source.positions[1:]))
    with pytest.raises(RebalanceError, match="price"):
        simulate_rebalance(
            invalid_source,
            RebalanceRequest(
                invalid_source.snapshot_id,
                "USD",
                changes=(RebalanceChange("US.AAA", "SHARE_DELTA", Decimal("1")),),
            ),
        )
    result = simulate_rebalance(
        source,
        RebalanceRequest(
            source.snapshot_id,
            "USD",
            changes=(RebalanceChange("US.CCC", "AMOUNT_DELTA", Decimal("-100")),),
        ),
    )
    assert result.after.exposures["sector"]["HEALTH"] < result.before.exposures["sector"]["HEALTH"]
    assert "concentration" in result.risk_changes
    assert {"sector", "region", "theme_full", "theme_split"} <= set(result.risk_changes)
