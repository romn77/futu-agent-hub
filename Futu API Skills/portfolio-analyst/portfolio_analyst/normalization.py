"""Shared narrow normalization for explicit unknown sentinels."""

from __future__ import annotations

from typing import Any


UNKNOWN_SENTINELS = frozenset({"", "UNKNOWN", "N/A", "NONE", "NULL"})
CURRENCY_PROVENANCE_VALUES = frozenset({"AUDITED_SOURCE", "USER_CONFIG"})


def normalized_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def is_unknown_sentinel(value: Any) -> bool:
    return normalized_text(value).upper() in UNKNOWN_SENTINELS


def canonical_unknown(value: Any, *, uppercase: bool = False) -> str:
    text = normalized_text(value)
    if is_unknown_sentinel(text):
        return "UNKNOWN"
    return text.upper() if uppercase else text


def canonical_currency_provenance(value: Any) -> str:
    token = normalized_text(value).upper().replace("-", "_").replace(" ", "_")
    while "__" in token:
        token = token.replace("__", "_")
    return token if token in CURRENCY_PROVENANCE_VALUES else "UNKNOWN"


def is_known_currency_provenance(value: Any) -> bool:
    return canonical_currency_provenance(value) in CURRENCY_PROVENANCE_VALUES
