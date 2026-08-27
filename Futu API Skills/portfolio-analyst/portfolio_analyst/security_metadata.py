"""Exact-code security metadata without market- or name-based inference."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class SecurityMetadata:
    currency: str | None = None
    security_type: str | None = None
    provenance: str = "USER_CONFIG"


class SecurityMetadataMapper:
    def __init__(self, mappings: Mapping[str, SecurityMetadata]) -> None:
        self._mappings = MappingProxyType(
            {str(code).upper(): metadata for code, metadata in mappings.items()}
        )

    def resolve(self, normalized_code: str) -> SecurityMetadata | None:
        return self._mappings.get(normalized_code.upper())
