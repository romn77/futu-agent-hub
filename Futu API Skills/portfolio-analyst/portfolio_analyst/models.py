"""Immutable portfolio domain and snapshot models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


QUALITY_STATES = frozenset(
    {"COMPLETE", "PARTIAL", "STALE", "RECONSTRUCTED", "UNAVAILABLE", "ERROR"}
)


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
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


@dataclass(frozen=True)
class CashBalance:
    account_ref: str
    currency: str
    amount_local: Decimal
    fx_to_base: Decimal | None = None
    amount_base: Decimal | None = None
    weight: Decimal | None = None
    data_quality_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Position:
    """Source position before security, FX, and classification normalization."""

    account_ref: str
    code: str
    name: str
    security_type: str
    quantity: Decimal
    can_sell_quantity: Decimal
    average_cost_local: Decimal
    last_price_local: Decimal
    market_value_local: Decimal
    unrealized_pl_local: Decimal | None
    realized_pl_local: Decimal | None
    position_role: str | None = None
    data_quality_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class NormalizedPosition:
    account_ref: str
    symbol: str
    name: str
    market: str
    security_type: str
    currency: str
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
    weight: Decimal
    sector: str
    industry: str
    region: str
    themes: tuple[str, ...]
    asset_class: str
    position_role: str | None = None
    data_quality_notes: tuple[str, ...] = ()


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


@dataclass(frozen=True)
class PortfolioSnapshot:
    schema_version: str
    snapshot_id: str
    as_of: datetime
    source: str
    source_version: str
    trd_env: str
    base_currency: str
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

    def to_dict(self) -> dict[str, Any]:
        return _json_value(asdict(self))
