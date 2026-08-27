"""Currency conversion interfaces."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, Protocol


class CurrencyConverter(Protocol):
    def rate(self, source: str, target: str, as_of: datetime) -> Decimal | None: ...


class StaticCurrencyConverter:
    """Explicit rates suitable for config, fixtures, and deterministic runs."""

    def __init__(self, rates: Mapping[tuple[str, str], Decimal]) -> None:
        normalized = {
            (source.upper(), target.upper()): Decimal(str(rate))
            for (source, target), rate in rates.items()
        }
        self._rates = MappingProxyType(normalized)

    def rate(self, source: str, target: str, as_of: datetime) -> Decimal | None:
        del as_of
        source = source.upper()
        target = target.upper()
        if source == target:
            return Decimal("1")
        return self._rates.get((source, target))
