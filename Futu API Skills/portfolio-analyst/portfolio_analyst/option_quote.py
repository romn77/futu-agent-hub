"""Strict normalization for the optional official option quote capability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping

from .models import _decimal_text


GREEKS = ("delta", "gamma", "vega", "theta", "rho")
RAW_GREEK_UNIT = "RAW_CONTRACT_UNITS_NO_MULTIPLIER"


class OptionQuoteError(ValueError):
    """A quote response did not prove one requested option identity."""


class FutuOptionQuoteProvider:
    """Canonical one-contract facade over the audited adapter."""

    def __init__(self, adapter: object) -> None:
        self.adapter = adapter

    def quote(self, identity: str, *, timeout: float) -> object:
        import json

        legs = json.dumps(
            [{"code": identity, "action": "BUY", "quantity": 1.0}],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return self.adapter.read_private_json(  # type: ignore[attr-defined]
            "get_option_quote", (legs, "--json"), timeout=timeout
        )


def _finite_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    parts = parsed.as_tuple()
    if len(parts.digits) > 256 or abs(parts.exponent) > 1000 or abs(parsed.adjusted()) > 1000:
        return None
    return parsed


@dataclass(frozen=True)
class OptionQuote:
    identity: str
    greeks: Mapping[str, Decimal | None]
    option_type: str | None
    expiry: date | None
    strike: Decimal | None
    implied_volatility: Decimal | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "greeks", MappingProxyType(dict(self.greeks)))

    def to_dict(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "unit": RAW_GREEK_UNIT,
            "greeks": {
                name: None if self.greeks[name] is None else _decimal_text(self.greeks[name])
                for name in GREEKS
            },
            "option_type": self.option_type,
            "expiry": None if self.expiry is None else self.expiry.isoformat(),
            "strike": None if self.strike is None else _decimal_text(self.strike),
            "implied_volatility": (
                None if self.implied_volatility is None else _decimal_text(self.implied_volatility)
            ),
        }


def parse_option_quote_response(identity: str, payload: object) -> OptionQuote:
    if not isinstance(payload, dict) or set(payload) != {"data"}:
        raise OptionQuoteError("option quote response must contain only data")
    rows = payload["data"]
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise OptionQuoteError("option quote response must contain one object")
    row: dict[str, Any] = rows[0]
    if row.get("code") != identity:
        raise OptionQuoteError("option quote response identity does not match the request")

    option_type = row.get("option_type")
    if option_type not in {"CALL", "PUT"}:
        option_type = None
    expiry = None
    if isinstance(row.get("expire_time"), str):
        try:
            expiry = date.fromisoformat(row["expire_time"])
        except ValueError:
            pass
    strike = _finite_decimal(row.get("strike_price"))
    if strike is not None and strike < 0:
        strike = None
    implied_volatility = _finite_decimal(row.get("implied_volatility"))
    if implied_volatility is not None and implied_volatility < 0:
        implied_volatility = None
    return OptionQuote(
        identity=identity,
        greeks={name: _finite_decimal(row.get(name)) for name in GREEKS},
        option_type=option_type,
        expiry=expiry,
        strike=strike,
        implied_volatility=implied_volatility,
    )
