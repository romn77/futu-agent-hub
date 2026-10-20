"""Small, JSON-only cache contract shared by the local adapters."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


class CacheError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise CacheError("INVALID_INPUT") from exc


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def utc(value: datetime | str) -> datetime:
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError) as exc:
        raise CacheError("CLOCK_INVALID") from exc


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def unique_json(body: bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise CacheError("CORRUPT")
            result[key] = value
        return result

    try:
        return json.loads(body, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(CacheError("CORRUPT")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CacheError("CORRUPT") from exc
