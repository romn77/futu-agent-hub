"""Currency conversion with explicit, freshness-checked evidence."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, Protocol

from .models import FxRateEvidence


class FxEvidenceError(ValueError):
    pass


class CurrencyConverter(Protocol):
    def validate(self, as_of: datetime) -> None: ...

    def quote(self, source: str, target: str, as_of: datetime) -> FxRateEvidence | None: ...


class StaticCurrencyConverter:
    """Explicit source snapshot rates with a bounded weekend-aware maximum age."""

    def __init__(
        self,
        rates: Mapping[tuple[str, str], Decimal],
        *,
        source: str,
        effective_at: datetime,
        max_age: timedelta = timedelta(hours=96),
    ) -> None:
        if not source.strip() or source.strip().upper() in {"UNKNOWN", "N/A"}:
            raise FxEvidenceError("FX source provenance is required")
        if effective_at.tzinfo is None or effective_at.utcoffset() is None:
            raise FxEvidenceError("FX effective_at must be timezone-aware")
        if max_age <= timedelta(0) or max_age > timedelta(hours=96):
            raise FxEvidenceError("FX max age must be positive and no more than 96 hours")
        normalized: dict[tuple[str, str], Decimal] = {}
        for (source_currency, target_currency), raw_rate in rates.items():
            rate = Decimal(str(raw_rate))
            if not rate.is_finite() or rate <= 0:
                raise FxEvidenceError("FX rates must be finite and positive")
            normalized[(source_currency.upper(), target_currency.upper())] = rate
        self._rates = MappingProxyType(normalized)
        self.source = source.strip()
        self.effective_at = effective_at
        self.max_age = max_age

    def validate(self, as_of: datetime) -> None:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise FxEvidenceError("valuation as_of must be timezone-aware")
        age = as_of - self.effective_at
        if age < timedelta(0):
            raise FxEvidenceError("FX evidence is from the future")
        if age > self.max_age:
            raise FxEvidenceError(
                f"FX evidence is stale (maximum age {int(self.max_age.total_seconds() // 3600)}h)"
            )

    def quote(self, source: str, target: str, as_of: datetime) -> FxRateEvidence | None:
        self.validate(as_of)
        source = source.upper()
        target = target.upper()
        if source == target:
            return FxRateEvidence(
                source_currency=source,
                target_currency=target,
                rate=Decimal("1"),
                source="BASE_CURRENCY_IDENTITY",
                effective_at=as_of,
            )
        rate = self._rates.get((source, target))
        if rate is None:
            return None
        return FxRateEvidence(source, target, rate, self.source, self.effective_at)
