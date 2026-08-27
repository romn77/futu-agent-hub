"""Confined immutable snapshot loading and full-body identity revalidation."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .identity import snapshot_identity
from .models import (
    Account,
    CashBalance,
    DataQuality,
    FxRateEvidence,
    NormalizedPosition,
    PortfolioSnapshot,
)
from .schema import validate_snapshot_document
from .snapshot_semantics import SnapshotSemanticError, validate_snapshot_semantics


class InvalidSnapshot(ValueError):
    """Saved snapshot does not satisfy its public schema and content identity."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidSnapshot("snapshot JSON contains duplicate object keys")
        result[key] = value
    return result


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidSnapshot("snapshot timestamps must be timezone-aware")
    return parsed


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def snapshot_from_document(payload: dict[str, Any]) -> PortfolioSnapshot:
    try:
        validate_snapshot_document(payload)
        snapshot = PortfolioSnapshot(
            schema_version=payload["schema_version"],
            snapshot_id=payload["snapshot_id"],
            as_of=_datetime(payload["as_of"]),
            source=payload["source"],
            source_version=payload["source_version"],
            trd_env=payload["trd_env"],
            base_currency=payload["base_currency"],
            fx_evidence=tuple(
                FxRateEvidence(
                    item["source_currency"],
                    item["target_currency"],
                    Decimal(item["rate"]),
                    item["source"],
                    _datetime(item["effective_at"]),
                )
                for item in payload["fx_evidence"]
            ),
            accounts=tuple(Account(**item) for item in payload["accounts"]),
            cash_balances=tuple(
                CashBalance(
                    account_ref=item["account_ref"],
                    currency=item["currency"],
                    amount_local=Decimal(item["amount_local"]),
                    fx_to_base=_decimal(item["fx_to_base"]),
                    amount_base=_decimal(item["amount_base"]),
                    weight=_decimal(item["weight"]),
                    data_quality_notes=tuple(item["data_quality_notes"]),
                )
                for item in payload["cash_balances"]
            ),
            positions=tuple(
                NormalizedPosition(
                    **{
                        **item,
                        **{
                            field: _decimal(item[field])
                            for field in (
                                "quantity",
                                "can_sell_quantity",
                                "average_cost_local",
                                "last_price_local",
                                "market_value_local",
                                "unrealized_pl_local",
                                "realized_pl_local",
                                "fx_to_base",
                                "market_value_base",
                                "unrealized_pl_base",
                                "realized_pl_base",
                                "weight",
                            )
                        },
                        "themes": tuple(item["themes"]),
                        "data_quality_notes": tuple(item["data_quality_notes"]),
                    }
                )
                for item in payload["positions"]
            ),
            nav_base=Decimal(payload["nav_base"]),
            cash_base=Decimal(payload["cash_base"]),
            cash_weight=Decimal(payload["cash_weight"]),
            long_exposure_base=Decimal(payload["long_exposure_base"]),
            short_exposure_base=Decimal(payload["short_exposure_base"]),
            gross_exposure_base=Decimal(payload["gross_exposure_base"]),
            net_exposure_base=Decimal(payload["net_exposure_base"]),
            exposures={
                dimension: {label: Decimal(value) for label, value in weights.items()}
                for dimension, weights in payload["exposures"].items()
            },
            data_quality=DataQuality(
                status=payload["data_quality"]["status"],
                warnings=tuple(payload["data_quality"]["warnings"]),
                missing_fields=tuple(payload["data_quality"]["missing_fields"]),
                source_errors=tuple(payload["data_quality"]["source_errors"]),
                coverage=payload["data_quality"]["coverage"],
            ),
        )
    except InvalidSnapshot:
        raise
    except Exception as exc:
        raise InvalidSnapshot("snapshot schema or value validation failed") from exc
    if snapshot_identity(snapshot) != snapshot.snapshot_id:
        raise InvalidSnapshot("snapshot content identity does not match its complete body")
    try:
        validate_snapshot_semantics(snapshot)
    except SnapshotSemanticError as exc:
        raise InvalidSnapshot(f"snapshot financial semantics are invalid: {exc}") from exc
    return snapshot


def _validated_parts(relative_path: str | Path) -> tuple[str, ...]:
    requested = Path(relative_path)
    if (
        requested.is_absolute()
        or not requested.parts
        or any(part in {"", ".", ".."} for part in requested.parts)
    ):
        raise InvalidSnapshot("snapshot path must remain below PORTFOLIO_AGENT_HOME")
    return requested.parts


def _read_bounded(file_fd: int, limit: int = 16 * 1024 * 1024) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(file_fd, min(65536, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise InvalidSnapshot("snapshot file exceeds the bounded read limit")


def _same_file_anchor(parent_fd: int, leaf: str, anchored_fd: int, flags: int) -> bool:
    try:
        check_fd = os.open(leaf, flags, dir_fd=parent_fd)
    except OSError:
        return False
    try:
        anchored = os.fstat(anchored_fd)
        current = os.fstat(check_fd)
        return (anchored.st_dev, anchored.st_ino) == (current.st_dev, current.st_ino)
    finally:
        os.close(check_fd)


def _same_parent_anchor(
    root_fd: int, components: tuple[str, ...], anchored_fd: int, directory_flags: int
) -> bool:
    check_fd = os.dup(root_fd)
    try:
        for component in components:
            try:
                next_fd = os.open(component, directory_flags, dir_fd=check_fd)
            except OSError:
                return False
            os.close(check_fd)
            check_fd = next_fd
        anchored = os.fstat(anchored_fd)
        current = os.fstat(check_fd)
        return (anchored.st_dev, anchored.st_ino) == (current.st_dev, current.st_ino)
    finally:
        os.close(check_fd)


def _read_confined_bytes(data_home: Path, relative_path: str | Path) -> bytes:
    parts = _validated_parts(relative_path)
    if os.name != "posix":
        return _read_confined_fallback(data_home, parts)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(data_home.expanduser().absolute(), directory_flags)
    except OSError as exc:
        raise InvalidSnapshot("private data home is unavailable or unsafe") from exc
    parent_fd = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            try:
                next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            except OSError as exc:
                raise InvalidSnapshot("snapshot path contains a symlink or non-directory") from exc
            os.close(parent_fd)
            parent_fd = next_fd
        try:
            file_fd = os.open(parts[-1], file_flags, dir_fd=parent_fd)
        except OSError as exc:
            raise InvalidSnapshot("snapshot path must name a confined regular file") from exc
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise InvalidSnapshot("snapshot path must name a confined regular file")
            raw = _read_bounded(file_fd)
            if not _same_file_anchor(
                parent_fd, parts[-1], file_fd, file_flags
            ) or not _same_parent_anchor(root_fd, parts[:-1], parent_fd, directory_flags):
                raise InvalidSnapshot("snapshot changed during anchored read")
        finally:
            os.close(file_fd)
    finally:
        os.close(parent_fd)
        os.close(root_fd)
    return raw


def _read_confined_fallback(data_home: Path, parts: tuple[str, ...]) -> bytes:
    """Windows fallback: reject symlinks/reparse points and revalidate lstat identity."""

    current = data_home.expanduser().absolute()
    is_junction = getattr(current, "is_junction", lambda: False)
    if current.is_symlink() or is_junction() or not current.is_dir():
        raise InvalidSnapshot("private data home is unavailable or unsafe")
    anchors = [(current, current.lstat())]
    for component in parts[:-1]:
        current = current / component
        is_junction = getattr(current, "is_junction", lambda: False)
        metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or current.is_symlink() or is_junction():
            raise InvalidSnapshot("snapshot path contains a symlink or reparse point")
        anchors.append((current, metadata))
    source = current / parts[-1]
    before = source.lstat()
    is_junction = getattr(source, "is_junction", lambda: False)
    if not stat.S_ISREG(before.st_mode) or source.is_symlink() or is_junction():
        raise InvalidSnapshot("snapshot path must name a confined regular file")
    with source.open("rb") as handle:
        raw = handle.read(16 * 1024 * 1024 + 1)
    after = source.lstat()
    try:
        anchors_match = all(
            (before_anchor.st_dev, before_anchor.st_ino)
            == (after_anchor.st_dev, after_anchor.st_ino)
            and not path.is_symlink()
            and not getattr(path, "is_junction", lambda: False)()
            for path, before_anchor in anchors
            for after_anchor in (path.lstat(),)
        )
    except OSError:
        anchors_match = False
    if (
        len(raw) > 16 * 1024 * 1024
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or not anchors_match
    ):
        raise InvalidSnapshot("snapshot changed during anchored read")
    return raw


def load_snapshot(data_home: Path, relative_path: str | Path) -> PortfolioSnapshot:
    try:
        raw = _read_confined_bytes(data_home, relative_path)
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except InvalidSnapshot:
        raise
    except Exception as exc:
        raise InvalidSnapshot("snapshot could not be read as JSON") from exc
    if not isinstance(payload, dict):
        raise InvalidSnapshot("snapshot document must be an object")
    return snapshot_from_document(payload)


def load_latest_snapshot(data_home: Path) -> PortfolioSnapshot:
    directory = data_home.expanduser().resolve() / "snapshots"
    if not directory.is_dir() or directory.is_symlink():
        raise InvalidSnapshot("no confined snapshots directory is available")
    candidates: list[tuple[datetime, str, str, PortfolioSnapshot]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            loaded = load_snapshot(data_home, path.relative_to(data_home))
        except InvalidSnapshot:
            continue
        candidates.append((loaded.as_of, loaded.snapshot_id, path.name, loaded))
    if not candidates:
        raise InvalidSnapshot("no valid immutable snapshots are available")
    return max(candidates, key=lambda item: item[:3])[3]
