"""Phase 1 financial invariants revalidated at every immutable snapshot trust boundary."""

from __future__ import annotations

from datetime import timezone
from decimal import Decimal

from .models import PortfolioSnapshot
from .normalization import is_unknown_sentinel


class SnapshotSemanticError(ValueError):
    pass


_TOLERANCE = Decimal("1e-18")


def _finite(value: Decimal, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise SnapshotSemanticError(f"{field} must be finite")
    return value


def _close(actual: Decimal, expected: Decimal, field: str) -> None:
    _finite(actual, field)
    _finite(expected, field)
    tolerance = max(Decimal("1"), abs(expected)) * _TOLERANCE
    if abs(actual - expected) > tolerance:
        raise SnapshotSemanticError(f"{field} does not reconcile")


def _require_unknown_quality(snapshot: PortfolioSnapshot, field: str) -> None:
    if (
        snapshot.schema_version != "1.1"
        or snapshot.data_quality.status == "COMPLETE"
        or field not in snapshot.data_quality.missing_fields
        or not any(field in warning for warning in snapshot.data_quality.warnings)
    ):
        raise SnapshotSemanticError(f"unknown {field} requires explicit partial quality")


def validate_snapshot_semantics(snapshot: PortfolioSnapshot) -> None:
    if snapshot.as_of.tzinfo is None or snapshot.as_of.utcoffset() is None:
        raise SnapshotSemanticError("as_of must be timezone-aware")
    nav = _finite(snapshot.nav_base, "nav_base")
    if nav <= 0:
        raise SnapshotSemanticError("nav_base must be positive")

    account_refs = [account.account_ref for account in snapshot.accounts]
    if len(account_refs) != len(set(account_refs)):
        raise SnapshotSemanticError("account aliases must be unique")
    known_accounts = set(account_refs)
    if any(not ref or ref.isdecimal() for ref in known_accounts):
        raise SnapshotSemanticError("account aliases must be masked")
    if any(
        item.account_ref not in known_accounts
        for item in (*snapshot.cash_balances, *snapshot.positions)
    ):
        raise SnapshotSemanticError("cash and position account aliases must reference accounts")

    alias_provenance = {account.alias_provenance for account in snapshot.accounts}
    if len(alias_provenance) > 1:
        raise SnapshotSemanticError("account alias provenance must be consistent")
    if alias_provenance:
        expected_alias = next(iter(alias_provenance))
        coverage_alias = snapshot.data_quality.coverage.get("alias_provenance")
        if coverage_alias is not None and coverage_alias != expected_alias:
            raise SnapshotSemanticError("quality alias provenance does not match accounts")
        if expected_alias == "EPHEMERAL" and not any(
            "EPHEMERAL" in warning for warning in snapshot.data_quality.warnings
        ):
            raise SnapshotSemanticError("ephemeral aliases require an explicit quality warning")
    unknown_classification = any(
        not position.themes
        or any(
            is_unknown_sentinel(label)
            for label in (
                position.sector,
                position.industry,
                position.region,
                position.asset_class,
                *position.themes,
            )
        )
        for position in snapshot.positions
    )
    unknown_security_type = any(
        is_unknown_sentinel(position.security_type) for position in snapshot.positions
    )
    if unknown_classification and (
        snapshot.data_quality.status == "COMPLETE"
        or "classification" not in snapshot.data_quality.missing_fields
        or not any("UNKNOWN" in warning for warning in snapshot.data_quality.warnings)
    ):
        raise SnapshotSemanticError("unknown classifications require explicit partial quality")
    if unknown_security_type and (
        snapshot.data_quality.status == "COMPLETE"
        or "security_type" not in snapshot.data_quality.missing_fields
    ):
        raise SnapshotSemanticError("unknown security types require explicit partial quality")
    if snapshot.data_quality.source_errors and snapshot.data_quality.status == "COMPLETE":
        raise SnapshotSemanticError("source errors cannot have COMPLETE data quality")

    fx_rates: dict[tuple[str, str], Decimal] = {}
    for evidence in snapshot.fx_evidence:
        rate = _finite(evidence.rate, "fx evidence rate")
        if (
            rate <= 0
            or evidence.target_currency != snapshot.base_currency
            or is_unknown_sentinel(evidence.source)
        ):
            raise SnapshotSemanticError("FX evidence must positively target the base currency")
        if evidence.source_currency == snapshot.base_currency and rate != Decimal("1"):
            raise SnapshotSemanticError("base-currency FX evidence must have rate 1")
        if evidence.effective_at.tzinfo is None or evidence.effective_at.utcoffset() is None:
            raise SnapshotSemanticError("FX evidence timestamps must be timezone-aware")
        if evidence.effective_at.astimezone(timezone.utc) > snapshot.as_of.astimezone(timezone.utc):
            raise SnapshotSemanticError("FX evidence cannot look ahead of the snapshot")
        key = (evidence.source_currency, evidence.target_currency)
        if key in fx_rates and fx_rates[key] != rate:
            raise SnapshotSemanticError("FX evidence contains conflicting rates")
        fx_rates[key] = rate

    if snapshot.schema_version == "1.1":
        if snapshot.nav_source != "OPEND_TOTAL_ASSETS" or not snapshot.account_navs:
            raise SnapshotSemanticError("OpenD account NAV evidence is required")
        nav_accounts = [item.account_ref for item in snapshot.account_navs]
        nav_account_refs = set(nav_accounts)
        if len(nav_accounts) != len(nav_account_refs) or not nav_account_refs <= known_accounts:
            raise SnapshotSemanticError("OpenD NAV account aliases must be unique known accounts")
        if any(
            item.account_ref not in nav_account_refs
            for item in (*snapshot.cash_balances, *snapshot.positions)
        ):
            raise SnapshotSemanticError("cash or positions lack OpenD account NAV evidence")
        accounts_read = snapshot.data_quality.coverage.get("accounts_returned_read")
        if accounts_read is not None and accounts_read != len(nav_accounts):
            raise SnapshotSemanticError("OpenD NAV coverage does not match accounts read")
        if len(nav_accounts) < len(known_accounts) and snapshot.data_quality.status == "COMPLETE":
            raise SnapshotSemanticError("incomplete account NAV coverage cannot be COMPLETE")
        total_nav = Decimal("0")
        for account_nav in snapshot.account_navs:
            assets = _finite(account_nav.total_assets_local, "OpenD total_assets")
            rate = fx_rates.get((account_nav.currency, snapshot.base_currency))
            if rate is None:
                raise SnapshotSemanticError("OpenD NAV conversion lacks FX evidence")
            _close(account_nav.fx_to_base, rate, "OpenD NAV fx_to_base")
            _close(account_nav.total_assets_base, assets * rate, "OpenD NAV total_assets_base")
            total_nav += account_nav.total_assets_base
        _close(nav, total_nav, "OpenD account NAV total")
    elif (
        snapshot.schema_version != "1.0" or snapshot.account_navs or snapshot.nav_source is not None
    ):
        raise SnapshotSemanticError("snapshot NAV source/version is invalid")

    cash_total = Decimal("0")
    cash_complete = {item.account_ref for item in snapshot.account_navs} <= {
        cash.account_ref for cash in snapshot.cash_balances
    }
    for cash in snapshot.cash_balances:
        rate = fx_rates.get((cash.currency, snapshot.base_currency))
        if rate is None:
            raise SnapshotSemanticError("cash conversion lacks FX evidence")
        _close(cash.fx_to_base, rate, "cash fx_to_base")
        if cash.amount_local is None:
            cash_complete = False
            _require_unknown_quality(snapshot, "cash")
            if cash.amount_base is not None or cash.weight is not None:
                raise SnapshotSemanticError("unknown cash cannot have an amount or weight")
            continue
        if cash.amount_base is None or cash.weight is None:
            raise SnapshotSemanticError("known cash requires converted amounts and weight")
        _close(cash.amount_base, cash.amount_local * rate, "cash amount_base")
        _close(cash.weight, cash.amount_base / nav, "cash weight")
        cash_total += cash.amount_base
    if cash_complete:
        _close(snapshot.cash_base, cash_total, "cash_base")
        _close(snapshot.cash_weight, snapshot.cash_base / nav, "cash_weight")
    else:
        _require_unknown_quality(snapshot, "cash")
        if snapshot.cash_base is not None or snapshot.cash_weight is not None:
            raise SnapshotSemanticError(
                "incomplete cash coverage cannot have aggregate cash values"
            )

    long_exposure = Decimal("0")
    short_exposure = Decimal("0")
    exposure_maps: dict[str, dict[str, Decimal]] = {
        "sector": {},
        "region": {},
        "theme": {},
        "asset_class": {},
    }
    economic_legs = [
        (position.account_ref, position.market, position.symbol) for position in snapshot.positions
    ]
    if len(economic_legs) != len(set(economic_legs)):
        raise SnapshotSemanticError(
            "duplicate account and canonical identity economic position leg"
        )
    for position in snapshot.positions:
        for field in ("can_sell_quantity", "average_cost_local", "last_price_local"):
            value = getattr(position, field)
            if value is None:
                _require_unknown_quality(snapshot, field)
            else:
                _finite(value, field)
        quantity = _finite(position.quantity, "position quantity")
        role = (position.position_role or "").upper()
        if quantity and ((role == "LONG" and quantity < 0) or (role == "SHORT" and quantity > 0)):
            raise SnapshotSemanticError("position directional role and quantity signs conflict")
        if quantity:
            for market_value in (position.market_value_local, position.market_value_base):
                if market_value and (quantity > 0) != (market_value > 0):
                    raise SnapshotSemanticError("position quantity and market value signs conflict")
        rate = fx_rates.get((position.currency, snapshot.base_currency))
        if rate is None:
            raise SnapshotSemanticError("position conversion lacks FX evidence")
        _close(position.fx_to_base, rate, "position fx_to_base")
        _close(
            position.market_value_base,
            position.market_value_local * rate,
            "position market_value_base",
        )
        for local, base, field in (
            (position.unrealized_pl_local, position.unrealized_pl_base, "unrealized P&L"),
            (position.realized_pl_local, position.realized_pl_base, "realized P&L"),
        ):
            if (local is None) != (base is None):
                raise SnapshotSemanticError(f"{field} local/base availability differs")
            if local is not None and base is not None:
                _close(base, local * rate, field)
        _close(position.weight, position.market_value_base / nav, "position weight")
        if position.market_value_base >= 0:
            long_exposure += position.market_value_base
        else:
            short_exposure -= position.market_value_base
        for dimension, labels in (
            ("sector", (position.sector,)),
            ("region", (position.region,)),
            ("theme", position.themes),
            ("asset_class", (position.asset_class,)),
        ):
            for label in labels:
                exposure_maps[dimension][label] = (
                    exposure_maps[dimension].get(label, Decimal("0")) + position.weight
                )

    _close(snapshot.long_exposure_base, long_exposure, "long_exposure_base")
    _close(snapshot.short_exposure_base, short_exposure, "short_exposure_base")
    _close(snapshot.gross_exposure_base, long_exposure + short_exposure, "gross_exposure_base")
    _close(snapshot.net_exposure_base, long_exposure - short_exposure, "net_exposure_base")
    # Only legacy 1.0 history used reconstructed NAV. New snapshots preserve OpenD's NAV
    # even when the exposed cash/position population does not account for every asset.
    if snapshot.schema_version == "1.0":
        _close(snapshot.nav_base, snapshot.cash_base + snapshot.net_exposure_base, "NAV identity")
        if not short_exposure:
            _close(
                sum((position.weight for position in snapshot.positions), Decimal("0"))
                + snapshot.cash_weight,
                Decimal("1"),
                "long-only weights",
            )

    if set(snapshot.exposures) != set(exposure_maps):
        raise SnapshotSemanticError("snapshot exposure dimensions are incomplete")
    for dimension, expected in exposure_maps.items():
        actual = snapshot.exposures[dimension]
        if set(actual) != set(expected):
            raise SnapshotSemanticError(f"{dimension} exposure labels do not match positions")
        for label, weight in expected.items():
            _close(actual[label], weight, f"{dimension} exposure {label}")
