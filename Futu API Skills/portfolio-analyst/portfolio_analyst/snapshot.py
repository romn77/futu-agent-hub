"""Portfolio normalization, valuation, and snapshot construction."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from .classification import Classification, ClassificationMapper
from .fx import CurrencyConverter, FxEvidenceError
from .identity import snapshot_identity
from .models import (
    Account,
    AccountNAV,
    CashBalance,
    DataQuality,
    FxRateEvidence,
    NormalizedPosition,
    PortfolioSnapshot,
    Position,
)
from .normalization import is_known_currency_provenance, is_unknown_sentinel
from .quality import compose_quality
from .security_metadata import SecurityMetadataMapper


MARKET_ALIASES = {"SH": "CN", "SZ": "CN", "HKCC": "HK"}
SUPPORTED_MARKETS = frozenset({"US", "HK", "CN", "SG", "MY", "JP", "CA", "AU"})


class UnsafeAggregationError(ValueError):
    """Raised before values in incompatible currencies can be combined."""


@dataclass(frozen=True)
class _StagedPosition:
    source: Position
    symbol: str
    market: str
    currency: str
    currency_provenance: str
    security_type: str
    rate: Decimal
    classification: Classification
    market_value_base: Decimal
    unrealized_base: Decimal | None
    realized_base: Decimal | None


def normalize_security(code: str) -> tuple[str, str]:
    market_raw, separator, symbol = str(code).strip().upper().partition(".")
    if not separator or not symbol:
        raise ValueError(f"security code requires a market prefix: {code!r}")
    market = MARKET_ALIASES.get(market_raw, market_raw)
    if market not in SUPPORTED_MARKETS:
        raise ValueError(f"unsupported security market: {market_raw}")
    return symbol, market


class PortfolioBuilder:
    WEIGHT_TOLERANCE = Decimal("0.00000001")

    def __init__(
        self,
        *,
        base_currency: str,
        converter: CurrencyConverter,
        classifications: ClassificationMapper,
        security_metadata: SecurityMetadataMapper | None = None,
    ) -> None:
        self.base_currency = base_currency.upper()
        self.converter = converter
        self.classifications = classifications
        self.security_metadata = security_metadata or SecurityMetadataMapper({})

    def _quote(self, currency: str, as_of: datetime) -> FxRateEvidence:
        try:
            evidence = self.converter.quote(currency.upper(), self.base_currency, as_of)
        except FxEvidenceError as exc:
            raise UnsafeAggregationError(str(exc)) from exc
        if evidence is None or evidence.rate <= 0:
            raise UnsafeAggregationError(
                f"missing positive FX rate for {currency.upper()} to {self.base_currency}"
            )
        return evidence

    def build(
        self,
        *,
        accounts: Iterable[Account],
        account_navs: Iterable[AccountNAV],
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
        nav_input = tuple(account_navs)
        account_refs = {account.account_ref for account in accounts_tuple}
        if len(account_refs) != len(accounts_tuple):
            raise UnsafeAggregationError("duplicate masked account references are unsafe")
        if any(account.trd_env != trd_env for account in accounts_tuple):
            raise UnsafeAggregationError("account trading environment does not match snapshot")
        orphan_refs = {
            item.account_ref
            for item in (*cash_input, *position_input, *nav_input)
            if item.account_ref not in account_refs
        }
        if orphan_refs:
            raise UnsafeAggregationError("source record references an unknown masked account")

        converted_cash: list[CashBalance] = []
        used_fx: dict[tuple[str, str], FxRateEvidence] = {}
        converted_navs: list[AccountNAV] = []
        nav_refs = {item.account_ref for item in nav_input}
        if not nav_input or len(nav_refs) != len(nav_input):
            raise UnsafeAggregationError("unique OpenD account NAV evidence is required")
        if any(item.account_ref not in nav_refs for item in (*cash_input, *position_input)):
            raise UnsafeAggregationError("cash or positions lack OpenD account NAV evidence")
        nav = Decimal("0")
        for account_nav in nav_input:
            if not account_nav.total_assets_local.is_finite():
                raise UnsafeAggregationError("OpenD total_assets must be finite")
            evidence = self._quote(account_nav.currency, as_of)
            used_fx[(evidence.source_currency, evidence.target_currency)] = evidence
            total_assets_base = account_nav.total_assets_local * evidence.rate
            nav += total_assets_base
            converted_navs.append(
                replace(
                    account_nav,
                    currency=account_nav.currency.upper(),
                    fx_to_base=evidence.rate,
                    total_assets_base=total_assets_base,
                )
            )
        if nav <= 0:
            raise UnsafeAggregationError("NAV must be positive; OpenD total_assets is non-positive")
        cash_total = Decimal("0")
        cash_complete = nav_refs <= {cash.account_ref for cash in cash_input}
        for cash in cash_input:
            evidence = self._quote(cash.currency, as_of)
            rate = evidence.rate
            used_fx[(evidence.source_currency, evidence.target_currency)] = evidence
            amount_base = cash.amount_local * rate if cash.amount_local is not None else None
            if amount_base is None:
                cash_complete = False
            else:
                cash_total += amount_base
            converted_cash.append(
                replace(
                    cash, currency=cash.currency.upper(), fx_to_base=rate, amount_base=amount_base
                )
            )

        staged: list[_StagedPosition] = []
        for position in position_input:
            symbol, market = normalize_security(position.code)
            normalized_code = f"{market}.{symbol}"
            source_code = position.code.strip().upper()
            metadata = self.security_metadata.resolve(source_code)
            if (
                position.currency
                and metadata
                and metadata.currency
                and position.currency.upper() != metadata.currency.upper()
            ):
                raise UnsafeAggregationError(
                    f"currency source/config conflict for {normalized_code}"
                )
            currency = position.currency or (metadata.currency if metadata else None)
            if not currency:
                raise UnsafeAggregationError(
                    f"currency unavailable for security {normalized_code}; "
                    "provide exact-code security metadata"
                )
            currency = currency.upper()
            currency_provenance = (
                position.currency_provenance
                if position.currency
                else metadata.provenance
                if metadata
                else "UNAVAILABLE"
            )
            if not is_known_currency_provenance(currency_provenance):
                raise UnsafeAggregationError(
                    f"currency provenance unavailable for security {normalized_code}"
                )
            security_type = (
                metadata.security_type
                if metadata and not is_unknown_sentinel(metadata.security_type)
                else position.security_type
            )
            evidence = self._quote(currency, as_of)
            rate = evidence.rate
            used_fx[(evidence.source_currency, evidence.target_currency)] = evidence
            classification = self.classifications.classify(source_code, position.name)
            market_value_base = position.market_value_local * rate
            unrealized_base = (
                position.unrealized_pl_local * rate
                if position.unrealized_pl_local is not None
                else None
            )
            realized_base = (
                position.realized_pl_local * rate
                if position.realized_pl_local is not None
                else None
            )
            staged.append(
                _StagedPosition(
                    source=position,
                    symbol=symbol,
                    market=market,
                    currency=currency,
                    currency_provenance=currency_provenance,
                    security_type=security_type or "UNKNOWN",
                    rate=rate,
                    classification=classification,
                    market_value_base=market_value_base,
                    unrealized_base=unrealized_base,
                    realized_base=realized_base,
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
            source_code = position.code.strip().upper()
            classification = item.classification
            value = item.market_value_base
            weight = value / nav
            normalized_positions.append(
                NormalizedPosition(
                    account_ref=position.account_ref,
                    symbol=item.symbol,
                    name=position.name,
                    market=item.market,
                    security_type=item.security_type,
                    currency=item.currency,
                    currency_provenance=item.currency_provenance,
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
                    realized_pl_base=item.realized_base,
                    weight=weight,
                    sector=classification.sector,
                    industry=classification.industry,
                    region=classification.region,
                    themes=classification.themes,
                    asset_class=classification.asset_class,
                    position_role=position.position_role,
                    data_quality_notes=position.data_quality_notes,
                    # CN is a reporting market, not a routable Futu quote exchange.
                    source_code=source_code if source_code.startswith(("SH.", "SZ.")) else None,
                )
            )
            unknown_classifications |= any(
                label.upper() == "UNKNOWN"
                for label in (
                    classification.sector,
                    classification.industry,
                    classification.region,
                    classification.asset_class,
                    *classification.themes,
                )
            )
            unknown_security_types |= is_unknown_sentinel(item.security_type)
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

        cash_base = cash_total if cash_complete else None
        cash_weight = cash_base / nav if cash_base is not None else None
        converted_cash = [
            replace(cash, weight=cash.amount_base / nav if cash.amount_base is not None else None)
            for cash in converted_cash
        ]

        reconstructed_signs = any(
            any("sign normalized" in note for note in position.data_quality_notes)
            for position in normalized_positions
        )
        missing_fields: list[str] = []
        warnings: list[str] = list(self.classifications.warnings)
        if not cash_complete:
            missing_fields.append("cash")
            warnings.append(
                "cash is unavailable for one or more accounts; cash totals/weight are unknown"
            )
        for field in ("can_sell_quantity", "average_cost_local", "last_price_local"):
            if any(getattr(position, field) is None for position in normalized_positions):
                missing_fields.append(field)
                warnings.append(
                    f"{field} is unavailable for one or more positions; retained as unknown"
                )
        if unknown_classifications:
            missing_fields.append("classification")
            warnings.append("one or more classifications are UNKNOWN")
        if unknown_security_types:
            missing_fields.append("security_type")
            warnings.append("one or more security types are UNKNOWN")
        if reconstructed_signs:
            warnings.append("short market-value signs were reconstructed")
        quality = compose_quality(
            data_quality,
            reconstructed=reconstructed_signs,
            missing_fields=missing_fields,
            warnings=warnings,
        )

        snapshot = PortfolioSnapshot(
            schema_version="1.1",
            snapshot_id="",
            as_of=as_of,
            source="futuapi",
            source_version=source_version,
            trd_env=trd_env,
            base_currency=self.base_currency,
            fx_evidence=tuple(used_fx[key] for key in sorted(used_fx)),
            accounts=accounts_tuple,
            account_navs=tuple(converted_navs),
            nav_source="OPEND_TOTAL_ASSETS",
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
        return replace(snapshot, snapshot_id=snapshot_identity(snapshot))
