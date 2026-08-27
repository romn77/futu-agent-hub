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
    CashBalance,
    DataQuality,
    FxRateEvidence,
    NormalizedPosition,
    PortfolioSnapshot,
    Position,
)
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
        cash_balances: Iterable[CashBalance],
        positions: Iterable[Position],
        as_of: datetime,
        trd_env: str,
        source_version: str,
        data_quality: DataQuality,
    ) -> PortfolioSnapshot:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        try:
            self.converter.validate(as_of)
        except FxEvidenceError as exc:
            raise UnsafeAggregationError(str(exc)) from exc
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
        used_fx: dict[tuple[str, str], FxRateEvidence] = {}
        cash_base = Decimal("0")
        for cash in cash_input:
            evidence = self._quote(cash.currency, as_of)
            rate = evidence.rate
            used_fx[(evidence.source_currency, evidence.target_currency)] = evidence
            amount_base = cash.amount_local * rate
            cash_base += amount_base
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
            currency = metadata.currency if metadata and metadata.currency else position.currency
            if not currency:
                raise UnsafeAggregationError(
                    f"currency unavailable for security {normalized_code}; "
                    "provide exact-code security metadata"
                )
            currency = currency.upper()
            currency_provenance = (
                metadata.provenance
                if metadata and metadata.currency
                else position.currency_provenance
            )
            if currency_provenance in {"", "UNKNOWN", "UNAVAILABLE"}:
                raise UnsafeAggregationError(
                    f"currency provenance unavailable for security {normalized_code}"
                )
            security_type = (
                metadata.security_type
                if metadata and metadata.security_type
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
        nav = cash_base + net_exposure
        if nav <= 0:
            raise UnsafeAggregationError("NAV must be positive; weights are undefined")

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
            unknown_security_types |= item.security_type.strip().upper() in {
                "",
                "UNKNOWN",
                "N/A",
            }
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

        reconstructed_signs = any(
            any("sign normalized" in note for note in position.data_quality_notes)
            for position in normalized_positions
        )
        missing_fields: list[str] = []
        warnings: list[str] = []
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
            schema_version="1.0",
            snapshot_id="",
            as_of=as_of,
            source="futuapi",
            source_version=source_version,
            trd_env=trd_env,
            base_currency=self.base_currency,
            fx_evidence=tuple(used_fx[key] for key in sorted(used_fx)),
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
        return replace(snapshot, snapshot_id=snapshot_identity(snapshot))
