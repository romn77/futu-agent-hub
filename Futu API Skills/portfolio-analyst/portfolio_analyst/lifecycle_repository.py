"""Strict private YAML repository for user-authored position lifecycle facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import stat
import time
from typing import Any

import yaml

from .futu_adapter import FutuAdapter
from .models import _decimal_text
from .private_home import PrivateHomeError, ensure_private_home
from .persistence import SnapshotAlreadyExists, SnapshotStore
from .rendering import terminal_text


SYMBOL = re.compile(r"^(?:US|HK|CN|SG|MY|JP|CA|AU)\.[A-Z0-9]+(?:[.-][A-Z0-9]+)*$")
ROLES = frozenset(
    {"CORE", "SATELLITE", "TRADING", "DEFENSIVE", "HEDGE", "CASH_ALTERNATIVE", "UNCLASSIFIED"}
)
STATUSES = frozenset({"ACTIVE", "EXITED"})
_FIELDS = frozenset(
    {
        "schema_version",
        "symbol",
        "role",
        "status",
        "opened_at",
        "holding_horizon",
        "target_weight",
        "original_thesis",
        "invalidation_conditions",
        "review_triggers",
        "notes",
        "last_reviewed_at",
    }
)
MAX_LIFECYCLE_RECORDS = 10_000
MAX_LIFECYCLE_TOTAL_BYTES = 640 * 1024 * 1024


class LifecycleConfigError(ValueError):
    pass


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


def _mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise LifecycleConfigError("lifecycle YAML contains duplicate keys")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _safe_text(value: object, *, max_bytes: int = 8192, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise LifecycleConfigError("lifecycle text fields must be strings")
    result = terminal_text(FutuAdapter.sanitize_output(value))
    result = re.sub(r"(?<!\d)\d{8,}(?!\d)", "[REDACTED_EXTERNAL_IDENTIFIER]", result)
    if (not result and not allow_empty) or len(result.encode("utf-8")) > max_bytes:
        raise LifecycleConfigError("lifecycle text field is empty or exceeds its bound")
    return result


def _bounded_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 32:
        raise LifecycleConfigError(f"{field} must be a list with at most 32 items")
    return tuple(_safe_text(item, max_bytes=2048) for item in value)


def _bounded_sequence(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 32:
        raise LifecycleConfigError(f"{field} must contain at most 32 text items")
    return tuple(_safe_text(item, max_bytes=2048) for item in value)


def _weight(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise LifecycleConfigError(f"{field} is invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise LifecycleConfigError(f"{field} is invalid") from exc
    if not result.is_finite() or not Decimal(0) <= result <= Decimal(1):
        raise LifecycleConfigError(f"{field} must be between 0 and 1")
    return result


def _date(value: object, field: str) -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise LifecycleConfigError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise LifecycleConfigError(f"{field} must be an ISO date") from exc


@dataclass(frozen=True)
class TargetWeight:
    minimum: Decimal
    maximum: Decimal

    def __post_init__(self) -> None:
        minimum = _weight(self.minimum, "target_weight.min")
        maximum = _weight(self.maximum, "target_weight.max")
        if minimum > maximum:
            raise LifecycleConfigError("target_weight min cannot exceed max")
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)

    def to_dict(self) -> dict[str, str]:
        return {"min": _decimal_text(self.minimum), "max": _decimal_text(self.maximum)}


@dataclass(frozen=True)
class LifecycleRecord:
    schema_version: str
    symbol: str
    role: str
    status: str
    opened_at: date
    holding_horizon: str
    target_weight: TargetWeight
    original_thesis: str
    invalidation_conditions: tuple[str, ...]
    review_triggers: tuple[str, ...]
    notes: tuple[str, ...]
    last_reviewed_at: date

    def __post_init__(self) -> None:
        if self.schema_version != "1.0":
            raise LifecycleConfigError("unsupported lifecycle schema version")
        if not isinstance(self.symbol, str):
            raise LifecycleConfigError("lifecycle symbol must be text")
        symbol = self.symbol.strip().upper()
        role, status = str(self.role).upper(), str(self.status).upper()
        if SYMBOL.fullmatch(symbol) is None:
            raise LifecycleConfigError("lifecycle symbol must be canonical and market-qualified")
        if role not in ROLES or status not in STATUSES:
            raise LifecycleConfigError("lifecycle role or position status is unsupported")
        if not isinstance(self.target_weight, TargetWeight):
            raise LifecycleConfigError("target_weight must use the strict target model")
        opened = _date(self.opened_at, "opened_at")
        reviewed = _date(self.last_reviewed_at, "last_reviewed_at")
        if reviewed < opened:
            raise LifecycleConfigError("last_reviewed_at cannot precede opened_at")
        horizon = _safe_text(self.holding_horizon, max_bytes=128).lower()
        parsed_horizon = re.fullmatch(
            r"(\d+)(?:\s*-\s*(\d+))?\s*(day|days|week|weeks|month|months|year|years)",
            horizon,
        )
        if (
            parsed_horizon is None
            or not 1 <= int(parsed_horizon.group(2) or parsed_horizon.group(1)) <= 1200
        ):
            raise LifecycleConfigError("holding_horizon is unsupported or outside its bound")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "opened_at", opened)
        object.__setattr__(self, "holding_horizon", horizon)
        object.__setattr__(self, "original_thesis", _safe_text(self.original_thesis))
        object.__setattr__(
            self,
            "invalidation_conditions",
            _bounded_sequence(self.invalidation_conditions, "invalidation_conditions"),
        )
        object.__setattr__(
            self, "review_triggers", _bounded_sequence(self.review_triggers, "review_triggers")
        )
        object.__setattr__(self, "notes", _bounded_sequence(self.notes, "notes"))
        object.__setattr__(self, "last_reviewed_at", reviewed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "role": self.role,
            "status": self.status,
            "opened_at": self.opened_at.isoformat(),
            "holding_horizon": self.holding_horizon,
            "target_weight": self.target_weight.to_dict(),
            "original_thesis": self.original_thesis,
            "invalidation_conditions": list(self.invalidation_conditions),
            "review_triggers": list(self.review_triggers),
            "notes": list(self.notes),
            "last_reviewed_at": self.last_reviewed_at.isoformat(),
        }


def record_from_document(payload: object) -> LifecycleRecord:
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise LifecycleConfigError("lifecycle record fields do not match schema 1.0")
    if payload["schema_version"] != "1.0":
        raise LifecycleConfigError("unsupported lifecycle schema version")
    symbol = str(payload["symbol"]).strip().upper()
    if SYMBOL.fullmatch(symbol) is None:
        raise LifecycleConfigError("lifecycle symbol must be canonical and market-qualified")
    role, status = str(payload["role"]).upper(), str(payload["status"]).upper()
    if role not in ROLES or status not in STATUSES:
        raise LifecycleConfigError("lifecycle role or position status is unsupported")
    target = payload["target_weight"]
    if not isinstance(target, dict) or set(target) != {"min", "max"}:
        raise LifecycleConfigError("target_weight must contain exactly min and max")
    minimum, maximum = (
        _weight(target["min"], "target_weight.min"),
        _weight(target["max"], "target_weight.max"),
    )
    if minimum > maximum:
        raise LifecycleConfigError("target_weight min cannot exceed max")
    opened, reviewed = (
        _date(payload["opened_at"], "opened_at"),
        _date(payload["last_reviewed_at"], "last_reviewed_at"),
    )
    if reviewed < opened:
        raise LifecycleConfigError("last_reviewed_at cannot precede opened_at")
    record = LifecycleRecord(
        schema_version="1.0",
        symbol=symbol,
        role=role,
        status=status,
        opened_at=opened,
        holding_horizon=_safe_text(payload["holding_horizon"], max_bytes=128).lower(),
        target_weight=TargetWeight(minimum, maximum),
        original_thesis=_safe_text(payload["original_thesis"]),
        invalidation_conditions=_bounded_list(
            payload["invalidation_conditions"], "invalidation_conditions"
        ),
        review_triggers=_bounded_list(payload["review_triggers"], "review_triggers"),
        notes=_bounded_list(payload["notes"], "notes"),
        last_reviewed_at=reviewed,
    )
    from .schema import validate_lifecycle_record_document

    try:
        validate_lifecycle_record_document(record.to_dict())
    except Exception as exc:
        raise LifecycleConfigError("lifecycle record violates its published schema") from exc
    return record


class LifecycleRepository:
    def __init__(self, data_home: Path) -> None:
        self.data_home = data_home.expanduser().absolute()
        if self.data_home.exists():
            try:
                self.data_home = ensure_private_home(self.data_home)
            except PrivateHomeError as exc:
                raise LifecycleConfigError(str(exc)) from exc

    def list(
        self,
        *,
        limit: int = MAX_LIFECYCLE_RECORDS,
        max_total_bytes: int = MAX_LIFECYCLE_TOTAL_BYTES,
        max_operations: int = 100_000,
        max_seconds: float = 10.0,
        clock: Any = time.monotonic,
    ) -> tuple[LifecycleRecord, ...]:
        if limit < 0 or max_total_bytes < 0 or max_operations <= 0 or max_seconds <= 0:
            raise LifecycleConfigError("lifecycle repository budget is invalid")
        started = clock()
        operations = 0

        def consume(amount: int = 1) -> None:
            nonlocal operations
            operations += amount
            if operations > max_operations or clock() - started > max_seconds:
                raise LifecycleConfigError(
                    "lifecycle repository operation or wall-time budget exceeded"
                )

        if os.name != "posix":
            raise LifecycleConfigError("UNSUPPORTED_PLATFORM_SECURITY: lifecycle reads disabled")
        if not self.data_home.exists():
            return ()
        try:
            self.data_home = ensure_private_home(self.data_home)
        except PrivateHomeError as exc:
            raise LifecycleConfigError(str(exc)) from exc
        directory = self.data_home / "positions"
        if not directory.exists():
            return ()
        if (
            directory.is_symlink()
            or getattr(directory, "is_junction", lambda: False)()
            or not directory.is_dir()
        ):
            raise LifecycleConfigError("positions directory is unsafe")
        if os.name == "posix" and stat.S_IMODE(directory.stat().st_mode) != 0o700:
            raise LifecycleConfigError("positions directory must have mode 0700")
        self._validate_directory_descriptors()
        paths: list[Path] = []
        total_bytes = 0
        for path in directory.iterdir():
            consume()
            paths.append(path)
            if len(paths) > min(limit, MAX_LIFECYCLE_RECORDS) or path.suffix != ".yaml":
                raise LifecycleConfigError(
                    "positions directory contains unexpected or excessive entries"
                )
            total_bytes += path.lstat().st_size
            if total_bytes > min(max_total_bytes, MAX_LIFECYCLE_TOTAL_BYTES):
                raise LifecycleConfigError(
                    "positions directory contains unexpected or excessive entries"
                )
        paths.sort()
        records = []
        for path in paths:
            consume(2)
            records.append(self.read(path.stem))
            consume()
        if len({item.symbol for item in records}) != len(records):
            raise LifecycleConfigError("duplicate lifecycle symbols are forbidden")
        consume()
        return tuple(sorted(records, key=lambda item: item.symbol))

    def create(self, value: LifecycleRecord | dict[str, Any]) -> Path:
        """Explicitly create one current record; existing records are never overwritten."""

        if os.name != "posix":
            raise LifecycleConfigError("UNSUPPORTED_PLATFORM_SECURITY: lifecycle mutation disabled")
        record = record_from_document(
            value.to_dict() if isinstance(value, LifecycleRecord) else value
        )
        try:
            self.data_home = ensure_private_home(self.data_home, create=True)
        except PrivateHomeError as exc:
            raise LifecycleConfigError(str(exc)) from exc
        directory = self.data_home / "positions"
        if directory.exists():
            metadata = directory.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or directory.is_symlink():
                raise LifecycleConfigError("positions directory is unsafe")
            if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o700:
                raise LifecycleConfigError("positions directory must have mode 0700")
        rendered = yaml.safe_dump(record.to_dict(), allow_unicode=True, sort_keys=False)
        try:
            return SnapshotStore(self.data_home).save(
                Path("positions", f"{record.symbol}.yaml"), rendered
            )
        except SnapshotAlreadyExists as exc:
            raise LifecycleConfigError(
                "lifecycle record already exists; overwrite is forbidden"
            ) from exc

    def read(self, symbol: str) -> LifecycleRecord:
        if os.name != "posix":
            raise LifecycleConfigError("UNSUPPORTED_PLATFORM_SECURITY: lifecycle reads disabled")
        try:
            self.data_home = ensure_private_home(self.data_home)
        except PrivateHomeError as exc:
            raise LifecycleConfigError(str(exc)) from exc
        canonical = str(symbol).strip().upper()
        if SYMBOL.fullmatch(canonical) is None:
            raise LifecycleConfigError("lifecycle symbol must be canonical and market-qualified")
        raw = self._read_anchored(f"{canonical}.yaml")
        try:
            payload = yaml.load(raw.decode("utf-8"), Loader=_UniqueSafeLoader)
        except LifecycleConfigError:
            raise
        except Exception as exc:
            raise LifecycleConfigError("lifecycle YAML is invalid") from exc
        record = record_from_document(payload)
        if record.symbol != canonical:
            raise LifecycleConfigError("lifecycle filename and body symbol disagree")
        return record

    def _read_anchored(self, filename: str) -> bytes:
        if os.name != "posix":
            raise LifecycleConfigError("UNSUPPORTED_PLATFORM_SECURITY: lifecycle reads disabled")
        dflags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        fflags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            root_fd = os.open(self.data_home, dflags)
        except OSError as exc:
            raise LifecycleConfigError("lifecycle record is unavailable or unsafe") from exc
        try:
            root_metadata = os.fstat(root_fd)
            if stat.S_IMODE(root_metadata.st_mode) != 0o700:
                raise LifecycleConfigError("PORTFOLIO_AGENT_HOME must have mode 0700")
            directory_fd = os.open("positions", dflags, dir_fd=root_fd)
            try:
                directory_metadata = os.fstat(directory_fd)
                if (
                    not stat.S_ISDIR(directory_metadata.st_mode)
                    or stat.S_IMODE(directory_metadata.st_mode) != 0o700
                ):
                    raise LifecycleConfigError("positions directory must have mode 0700")
                fd = os.open(filename, fflags, dir_fd=directory_fd)
                try:
                    metadata = os.fstat(fd)
                    if not stat.S_ISREG(metadata.st_mode):
                        raise LifecycleConfigError("lifecycle record must be a regular file")
                    if metadata.st_nlink != 1:
                        raise LifecycleConfigError("lifecycle record must not be a hard link")
                    if stat.S_IMODE(metadata.st_mode) != 0o600:
                        raise LifecycleConfigError("lifecycle record must have mode 0600")
                    if metadata.st_size > 64 * 1024:
                        raise LifecycleConfigError("lifecycle record exceeds the size limit")
                    chunks: list[bytes] = []
                    total = 0
                    while True:
                        chunk = os.read(fd, min(8192, 64 * 1024 + 1 - total))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > 64 * 1024:
                            raise LifecycleConfigError("lifecycle record exceeds the size limit")
                    raw = b"".join(chunks)
                    check = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
                    if (metadata.st_dev, metadata.st_ino) != (check.st_dev, check.st_ino):
                        raise LifecycleConfigError("lifecycle record changed during read")
                    return raw
                finally:
                    os.close(fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise LifecycleConfigError("lifecycle record is unavailable or unsafe") from exc
        finally:
            os.close(root_fd)

    def _validate_directory_descriptors(self) -> None:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            root_fd = os.open(self.data_home, flags)
            try:
                root_metadata = os.fstat(root_fd)
                if stat.S_IMODE(root_metadata.st_mode) != 0o700:
                    raise LifecycleConfigError("PORTFOLIO_AGENT_HOME must have mode 0700")
                directory_fd = os.open("positions", flags, dir_fd=root_fd)
                try:
                    metadata = os.fstat(directory_fd)
                    if (
                        not stat.S_ISDIR(metadata.st_mode)
                        or stat.S_IMODE(metadata.st_mode) != 0o700
                    ):
                        raise LifecycleConfigError("positions directory must have mode 0700")
                finally:
                    os.close(directory_fd)
            finally:
                os.close(root_fd)
        except OSError as exc:
            raise LifecycleConfigError("positions directory is unavailable or unsafe") from exc
