"""Canonical snapshot serialization and traceable identity."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .models import PortfolioSnapshot


_SNAPSHOT_ID = re.compile(r"^ps-(\d{8}T\d{6}Z)-(real|simulate)-([a-z]{3})-([0-9a-f]{16})$")


class InvalidSnapshotIdentity(ValueError):
    pass


@dataclass(frozen=True)
class SnapshotIdentityParts:
    timestamp: datetime
    environment: str
    currency: str
    digest: str


def parse_snapshot_identity(value: object) -> SnapshotIdentityParts:
    """Parse one canonical snapshot identity, including its real UTC calendar time."""

    if not isinstance(value, str):
        raise InvalidSnapshotIdentity("snapshot identity must be text")
    match = _SNAPSHOT_ID.fullmatch(value)
    if match is None:
        raise InvalidSnapshotIdentity("snapshot identity is not canonical")
    try:
        timestamp = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise InvalidSnapshotIdentity("snapshot identity timestamp is invalid") from exc
    return SnapshotIdentityParts(timestamp, match.group(2), match.group(3), match.group(4))


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
