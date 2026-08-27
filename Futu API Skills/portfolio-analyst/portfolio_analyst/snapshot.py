"""Portfolio normalization, valuation, and snapshot construction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from .classification import Classification, ClassificationMapper
from .fx import CurrencyConverter
from .models import (
    Account,
    CashBalance,
    DataQuality,
    NormalizedPosition,
    PortfolioSnapshot,
    Position,
)


MARKET_ALIASES = {"SH": "CN", "SZ": "CN", "HKCC": "HK"}
MARKET_CURRENCIES = {
    "US": "USD",
    "HK": "HKD",
    "CN": "CNH",
    "SG": "SGD",
    "MY": "MYR",
    "JP": "JPY",
    "CA": "CAD",
    "AU": "AUD",
}


class UnsafeAggregationError(ValueError):
    """Raised before values in incompatible currencies can be combined."""


@dataclass(frozen=True)
class _StagedPosition:
    source: Position
    symbol: str
    market: str
    currency: str
    rate: Decimal
    classification: Classification
    market_value_base: Decimal
    unrealized_base: Decimal | None


def normalize_security(code: str) -> tuple[str, str, str]:
    market_raw, separator, symbol = str(code).strip().upper().partition(".")
    if not separator or not symbol:
        raise ValueError(f"security code requires a market prefix: {code!r}")
    market = MARKET_ALIASES.get(market_raw, market_raw)
    currency = MARKET_CURRENCIES.get(market)
    if currency is None:
        raise ValueError(f"unsupported security market: {market_raw}")
    return symbol, market, currency


def _snapshot_id(as_of: datetime, trd_env: str, base_currency: str, payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    timestamp = as_of.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"ps-{timestamp}-{trd_env.lower()}-{base_currency.lower()}-{digest}"


class PortfolioBuilder:
    WEIGHT_TOLERANCE = Decimal("0.00000001")

    def __init__(
        self,
        *,
        base_currency: str,
        converter: CurrencyConverter,
        classifications: ClassificationMapper,
    ) -> None:
        self.base_currency = base_currency.upper()
        self.converter = converter
        self.classifications = classifications

    def _rate(self, currency: str, as_of: datetime) -> Decimal:
        rate = self.converter.rate(currency.upper(), self.base_currency, as_of)
        if rate is None or rate <= 0:
            raise UnsafeAggregationError(
                f"missing positive FX rate for {currency.upper()} to {self.base_currency}"
            )
        return rate

    def build(
        self,
        *,
        accounts: Iterable[Account],
        cash_balances: Iterable[CashBalance],
        positions: Iterable[Position],
        as_of: datetime,
        trd_env: str,
        source_version: str,
        data_quality: DataQuality,
    ) -> PortfolioSnapshot:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        accounts_tuple = tuple(accounts)
        cash_input = tuple(cash_balances)
        position_input = tuple(positions)
        account_refs = {account.account_ref for account in accounts_tuple}
        if len(account_refs) != len(accounts_tuple):
            raise UnsafeAggregationError("duplicate masked account references are unsafe")
        if any(account.trd_env != trd_env for account in accounts_tuple):
            raise UnsafeAggregationError("account trading environment does not match snapshot")
        orphan_refs = {
            item.account_ref
            for item in (*cash_input, *position_input)
            if item.account_ref not in account_refs
        }
        if orphan_refs:
            raise UnsafeAggregationError("source record references an unknown masked account")

        converted_cash: list[CashBalance] = []
        cash_base = Decimal("0")
        for cash in cash_input:
            rate = self._rate(cash.currency, as_of)
            amount_base = cash.amount_local * rate
            cash_base += amount_base
            converted_cash.append(
                replace(
                    cash, currency=cash.currency.upper(), fx_to_base=rate, amount_base=amount_base
                )
            )

        staged: list[_StagedPosition] = []
        for position in position_input:
            symbol, market, currency = normalize_security(position.code)
            rate = self._rate(currency, as_of)
            classification = self.classifications.classify(f"{market}.{symbol}", position.name)
            market_value_base = position.market_value_local * rate
            unrealized_base = (
                position.unrealized_pl_local * rate
                if position.unrealized_pl_local is not None
                else None
            )
            staged.append(
                _StagedPosition(
                    source=position,
                    symbol=symbol,
                    market=market,
                    currency=currency,
                    rate=rate,
                    classification=classification,
                    market_value_base=market_value_base,
                    unrealized_base=unrealized_base,
                )
            )

        long_exposure = sum(
            (item.market_value_base for item in staged if item.market_value_base >= 0),
            Decimal("0"),
        )
        short_exposure = sum(
            (-item.market_value_base for item in staged if item.market_value_base < 0),
            Decimal("0"),
        )
        gross_exposure = long_exposure + short_exposure
        net_exposure = long_exposure - short_exposure
        nav = cash_base + net_exposure
        if nav == 0:
            raise UnsafeAggregationError("NAV is zero; weights are undefined")

        normalized_positions: list[NormalizedPosition] = []
        exposure_maps: dict[str, dict[str, Decimal]] = {
            "sector": {},
            "region": {},
            "theme": {},
            "asset_class": {},
        }
        unknown_classifications = False
        unknown_security_types = False
        for item in staged:
            position = item.source
            classification = item.classification
            value = item.market_value_base
            weight = value / nav
            normalized_positions.append(
                NormalizedPosition(
                    account_ref=position.account_ref,
                    symbol=item.symbol,
                    name=position.name,
                    market=item.market,
                    security_type=position.security_type or "UNKNOWN",
                    currency=item.currency,
                    quantity=position.quantity,
                    can_sell_quantity=position.can_sell_quantity,
                    average_cost_local=position.average_cost_local,
                    last_price_local=position.last_price_local,
                    market_value_local=position.market_value_local,
                    unrealized_pl_local=position.unrealized_pl_local,
                    realized_pl_local=position.realized_pl_local,
                    fx_to_base=item.rate,
                    market_value_base=value,
                    unrealized_pl_base=item.unrealized_base,
                    weight=weight,
                    sector=classification.sector,
                    industry=classification.industry,
                    region=classification.region,
                    themes=classification.themes,
                    asset_class=classification.asset_class,
                    position_role=position.position_role,
                    data_quality_notes=position.data_quality_notes,
                )
            )
            unknown_classifications |= "UNKNOWN" in {
                classification.sector,
                classification.industry,
                classification.region,
                classification.asset_class,
            }
            unknown_security_types |= position.security_type in {"", "UNKNOWN", "N/A"}
            for dimension, labels in (
                ("sector", (classification.sector,)),
                ("region", (classification.region,)),
                ("theme", classification.themes),
                ("asset_class", (classification.asset_class,)),
            ):
                for label in labels:
                    exposure_maps[dimension][label] = (
                        exposure_maps[dimension].get(label, Decimal("0")) + weight
                    )

        cash_weight = cash_base / nav
        converted_cash = [replace(cash, weight=cash.amount_base / nav) for cash in converted_cash]
        if not short_exposure:
            weight_total = sum((position.weight for position in normalized_positions), Decimal("0"))
            if abs(weight_total + cash_weight - Decimal("1")) > self.WEIGHT_TOLERANCE:
                raise UnsafeAggregationError("long-only portfolio weights do not reconcile to NAV")

        quality = data_quality
        reconstructed_signs = any(
            any("sign normalized" in note for note in position.data_quality_notes)
            for position in normalized_positions
        )
        if reconstructed_signs:
            warnings = tuple(
                dict.fromkeys((*quality.warnings, "short market-value signs were reconstructed"))
            )
            quality = replace(
                quality,
                status="RECONSTRUCTED" if quality.status == "COMPLETE" else quality.status,
                warnings=warnings,
            )
        if unknown_classifications:
            missing = tuple(dict.fromkeys((*quality.missing_fields, "classification")))
            warnings = tuple(
                dict.fromkeys((*quality.warnings, "one or more classifications are UNKNOWN"))
            )
            quality = replace(
                quality,
                status="PARTIAL" if quality.status == "COMPLETE" else quality.status,
                missing_fields=missing,
                warnings=warnings,
            )
        if unknown_security_types:
            missing = tuple(dict.fromkeys((*quality.missing_fields, "security_type")))
            warnings = tuple(
                dict.fromkeys((*quality.warnings, "one or more security types are UNKNOWN"))
            )
            quality = replace(
                quality,
                status="PARTIAL" if quality.status == "COMPLETE" else quality.status,
                missing_fields=missing,
                warnings=warnings,
            )

        identity_payload = {
            "as_of": as_of.isoformat(),
            "accounts": [account.account_ref for account in accounts_tuple],
            "cash": [
                (cash.account_ref, cash.currency, str(cash.amount_local)) for cash in cash_input
            ],
            "positions": [
                (
                    position.account_ref,
                    position.code,
                    str(position.quantity),
                    str(position.market_value_local),
                )
                for position in position_input
            ],
        }
        return PortfolioSnapshot(
            schema_version="1.0",
            snapshot_id=_snapshot_id(as_of, trd_env, self.base_currency, identity_payload),
            as_of=as_of,
            source="futuapi",
            source_version=source_version,
            trd_env=trd_env,
            base_currency=self.base_currency,
            accounts=accounts_tuple,
            cash_balances=tuple(converted_cash),
            positions=tuple(normalized_positions),
            nav_base=nav,
            cash_base=cash_base,
            cash_weight=cash_weight,
            long_exposure_base=long_exposure,
            short_exposure_base=short_exposure,
            gross_exposure_base=gross_exposure,
            net_exposure_base=net_exposure,
            exposures=exposure_maps,
            data_quality=quality,
        )
