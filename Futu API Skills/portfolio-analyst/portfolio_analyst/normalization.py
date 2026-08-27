"""Shared narrow normalization for explicit unknown sentinels."""

from __future__ import annotations

from typing import Any


UNKNOWN_SENTINELS = frozenset({"", "UNKNOWN", "N/A", "NONE", "NULL"})


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
