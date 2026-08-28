"""Immutable portfolio domain and snapshot models."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from .normalization import canonical_currency_provenance, canonical_unknown


QUALITY_STATES = frozenset(
    {"COMPLETE", "PARTIAL", "STALE", "RECONSTRUCTED", "UNAVAILABLE", "ERROR"}
)
REPORT_DECIMAL_TEXT_MAX_LENGTH = 512


def decimal_fits_report(value: Decimal) -> bool:
    """Return whether ``_decimal_text`` can emit a schema-bounded value."""

    if not isinstance(value, Decimal) or not value.is_finite():
        return False
    if value.is_zero():
        return True
    parts = value.as_tuple()
    digits = list(parts.digits)
    exponent = int(parts.exponent)
    while exponent < 0 and len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    sign_length = 1 if parts.sign else 0
    if exponent >= 0:
        text_length = sign_length + len(digits) + exponent
    elif len(digits) + exponent > 0:
        text_length = sign_length + len(digits) + 1
    else:
        text_length = sign_length + 2 - exponent
    return text_length <= REPORT_DECIMAL_TEXT_MAX_LENGTH


def _decimal_text(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, dict) or isinstance(value, MappingProxyType):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict) or isinstance(value, MappingProxyType):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class Account:
    account_ref: str
    trd_env: str
    account_type: str
    account_role: str
    markets: tuple[str, ...]
    status: str
    context_type: str = "SEC"
    security_firm: str = "UNKNOWN"
    alias_provenance: str = "STABLE"

    def __post_init__(self) -> None:
        object.__setattr__(self, "markets", tuple(self.markets))


@dataclass(frozen=True)
class CashBalance:
    account_ref: str
    currency: str
    amount_local: Decimal
    fx_to_base: Decimal | None = None
    amount_base: Decimal | None = None
    weight: Decimal | None = None
    data_quality_notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_quality_notes", tuple(self.data_quality_notes))


@dataclass(frozen=True)
class Position:
    """Source position before security, FX, and classification normalization."""

    account_ref: str
    code: str
    name: str
    security_type: str
    currency: str | None
    currency_provenance: str
    quantity: Decimal
    can_sell_quantity: Decimal
    average_cost_local: Decimal
    last_price_local: Decimal
    market_value_local: Decimal
    unrealized_pl_local: Decimal | None
    realized_pl_local: Decimal | None
    position_role: str | None = None
    data_quality_notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "security_type", canonical_unknown(self.security_type, uppercase=True)
        )
        object.__setattr__(
            self,
            "currency_provenance",
            canonical_currency_provenance(self.currency_provenance),
        )
        object.__setattr__(self, "data_quality_notes", tuple(self.data_quality_notes))


@dataclass(frozen=True)
class NormalizedPosition:
    account_ref: str
    symbol: str
    name: str
    market: str
    security_type: str
    currency: str
    currency_provenance: str
    quantity: Decimal
    can_sell_quantity: Decimal
    average_cost_local: Decimal
    last_price_local: Decimal
    market_value_local: Decimal
    unrealized_pl_local: Decimal | None
    realized_pl_local: Decimal | None
    fx_to_base: Decimal
    market_value_base: Decimal
    unrealized_pl_base: Decimal | None
    realized_pl_base: Decimal | None
    weight: Decimal
    sector: str
    industry: str
    region: str
    themes: tuple[str, ...]
    asset_class: str
    position_role: str | None = None
    data_quality_notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "security_type", canonical_unknown(self.security_type, uppercase=True)
        )
        object.__setattr__(
            self,
            "currency_provenance",
            canonical_currency_provenance(self.currency_provenance),
        )
        object.__setattr__(self, "themes", tuple(self.themes))
        object.__setattr__(self, "data_quality_notes", tuple(self.data_quality_notes))


@dataclass(frozen=True)
class DataQuality:
    status: str
    warnings: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    source_errors: tuple[str, ...] = ()
    coverage: dict[str, int | str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in QUALITY_STATES:
            raise ValueError(f"unsupported data-quality status: {self.status}")
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "missing_fields", tuple(self.missing_fields))
        object.__setattr__(self, "source_errors", tuple(self.source_errors))
        object.__setattr__(self, "coverage", _deep_freeze(self.coverage))


@dataclass(frozen=True)
class FxRateEvidence:
    source_currency: str
    target_currency: str
    rate: Decimal
    source: str
    effective_at: datetime


@dataclass(frozen=True)
class PortfolioSnapshot:
    schema_version: str
    snapshot_id: str
    as_of: datetime
    source: str
    source_version: str
    trd_env: str
    base_currency: str
    fx_evidence: tuple[FxRateEvidence, ...]
    accounts: tuple[Account, ...]
    cash_balances: tuple[CashBalance, ...]
    positions: tuple[NormalizedPosition, ...]
    nav_base: Decimal
    cash_base: Decimal
    cash_weight: Decimal
    long_exposure_base: Decimal
    short_exposure_base: Decimal
    gross_exposure_base: Decimal
    net_exposure_base: Decimal
    exposures: dict[str, dict[str, Decimal]]
    data_quality: DataQuality

    def __post_init__(self) -> None:
        object.__setattr__(self, "fx_evidence", tuple(self.fx_evidence))
        object.__setattr__(self, "accounts", tuple(self.accounts))
        object.__setattr__(self, "cash_balances", tuple(self.cash_balances))
        object.__setattr__(self, "positions", tuple(self.positions))
        object.__setattr__(self, "exposures", _deep_freeze(self.exposures))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)
