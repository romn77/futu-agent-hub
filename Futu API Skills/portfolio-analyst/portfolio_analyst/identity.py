"""Canonical snapshot serialization and traceable identity."""

from __future__ import annotations

import hashlib
import json
from datetime import timezone

from .models import PortfolioSnapshot


def canonical_snapshot_body(snapshot: PortfolioSnapshot) -> bytes:
    body = snapshot.to_dict()
    body.pop("snapshot_id", None)
    return json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def snapshot_identity(snapshot: PortfolioSnapshot) -> str:
    digest = hashlib.sha256(canonical_snapshot_body(snapshot)).hexdigest()[:16]
    timestamp = snapshot.as_of.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"ps-{timestamp}-{snapshot.trd_env.lower()}-{snapshot.base_currency.lower()}-{digest}"
