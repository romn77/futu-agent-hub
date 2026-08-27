"""Deterministic, user-supplied security classification."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class Classification:
    sector: str = "UNKNOWN"
    industry: str = "UNKNOWN"
    region: str = "UNKNOWN"
    themes: tuple[str, ...] = ("UNKNOWN",)
    asset_class: str = "UNKNOWN"


class ClassificationMapper:
    """Resolve exact normalized codes without guessing from display names."""

    def __init__(self, mappings: Mapping[str, Classification]) -> None:
        self._mappings = MappingProxyType(
            {str(code).upper(): classification for code, classification in mappings.items()}
        )

    def classify(self, normalized_code: str, _name: str = "") -> Classification:
        return self._mappings.get(normalized_code.upper(), Classification())
