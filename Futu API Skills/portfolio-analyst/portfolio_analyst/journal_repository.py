"""Explicit append-only, hash-chained portfolio journal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

from .futu_adapter import FutuAdapter
from .models import _deep_freeze, _json_value
from .private_home import PrivateHomeError, ensure_private_home
from .rendering import terminal_text


MAX_RECORD_BYTES = 64 * 1024
MAX_JOURNAL_BYTES = 64 * 1024 * 1024
MAX_METADATA_ITEMS = 64
_ZERO_HASH = "0" * 64
_SNAPSHOT_ID = re.compile(r"^ps-\d{8}T\d{6}Z-(?:real|simulate)-[a-z]{3}-[0-9a-f]{16}$")


class JournalError(ValueError):
    pass


def _time_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise JournalError("journal timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_text(value: object, *, limit: int = 4096, mask_numeric_ids: bool = True) -> str:
    text = terminal_text(FutuAdapter.sanitize_output(str(value)))
    if mask_numeric_ids:
        text = re.sub(r"(?<!\d)\d{8,}(?!\d)", "[REDACTED_EXTERNAL_IDENTIFIER]", text)
    if not text or len(text.encode("utf-8")) > limit:
        raise JournalError("journal text is empty or too long")
    return text


def _safe_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    if len(value) > MAX_METADATA_ITEMS:
        raise JournalError("journal metadata count exceeds the limit")
    result: dict[str, Any] = {}
    for key, item in sorted(value.items()):
        safe_key = _safe_text(key, limit=128)
        if safe_key in result:
            raise JournalError("journal metadata keys collide after sanitization")
        if isinstance(item, bool) or item is None:
            result[safe_key] = item
        elif isinstance(item, int):
            result[safe_key] = (
                "[REDACTED_EXTERNAL_IDENTIFIER]" if len(str(abs(item))) >= 8 else item
            )
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise JournalError("journal metadata numbers must be finite")
            result[safe_key] = item
        elif isinstance(item, str):
            result[safe_key] = _safe_text(item, limit=1024)
        else:
            raise JournalError("journal metadata values must be bounded JSON scalars")
    return result


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


@dataclass(frozen=True)
class JournalRecord:
    schema_version: str
    event_id: str
    record_hash: str
    previous_hash: str
    timestamp: str
    event_type: str
    source_snapshot_id: str
    artifact_path: str | None
    summary: str
    decision: str | None
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _deep_freeze(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)


class JournalRepository:
    """Journal writes only occur through explicit append methods."""

    def __init__(self, data_home: Path) -> None:
        self.data_home = data_home.expanduser().absolute()
        if self.data_home.exists():
            try:
                self.data_home = ensure_private_home(self.data_home)
            except PrivateHomeError as exc:
                raise JournalError(str(exc)) from exc

    def append_decision(
        self,
        *,
        timestamp: datetime,
        source_snapshot_id: str,
        summary: str,
        decision: str,
        metadata: Mapping[str, Any],
    ) -> JournalRecord:
        normalized = decision.upper()
        if normalized not in {"ACCEPTED", "REJECTED", "DEFERRED", "SUPERSEDED"}:
            raise JournalError("unsupported journal decision")
        return self._append(
            "decisions.jsonl",
            timestamp=timestamp,
            event_type="USER_DECISION",
            source_snapshot_id=source_snapshot_id,
            artifact_path=None,
            summary=summary,
            decision=normalized,
            metadata=metadata,
        )

    def append_analysis(
        self,
        *,
        timestamp: datetime,
        source_snapshot_id: str,
        artifact_path: str,
        summary: str,
        metadata: Mapping[str, Any],
    ) -> JournalRecord:
        path = Path(artifact_path)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise JournalError("artifact path must remain below PORTFOLIO_AGENT_HOME")
        safe_artifact = "/".join(_safe_text(part, limit=255) for part in path.parts)
        return self._append(
            "analyses.jsonl",
            timestamp=timestamp,
            event_type="PORTFOLIO_ANALYSIS",
            source_snapshot_id=source_snapshot_id,
            artifact_path=safe_artifact,
            summary=summary,
            decision=None,
            metadata=metadata,
        )

    def list_decisions(self) -> tuple[JournalRecord, ...]:
        return self._read("decisions.jsonl")

    def list_analyses(self) -> tuple[JournalRecord, ...]:
        return self._read("analyses.jsonl")

    def _append(self, filename: str, **fields: Any) -> JournalRecord:
        if _SNAPSHOT_ID.fullmatch(str(fields["source_snapshot_id"])) is None:
            raise JournalError("source_snapshot_id is not canonical")
        try:
            self.data_home = ensure_private_home(self.data_home, create=True)
        except PrivateHomeError as exc:
            raise JournalError(str(exc)) from exc
        prepared = {
            "schema_version": "1.0",
            "timestamp": _time_text(fields["timestamp"]),
            "event_type": fields["event_type"],
            "source_snapshot_id": _safe_text(
                fields["source_snapshot_id"], limit=256, mask_numeric_ids=False
            ),
            "artifact_path": fields["artifact_path"],
            "summary": _safe_text(fields["summary"]),
            "decision": fields["decision"],
            "metadata": _safe_metadata(fields["metadata"]),
        }
        if os.name != "posix":
            return self._append_fallback(filename, prepared)
        import fcntl

        journal_fd = self._journal_directory_fd(create=True)
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            file_fd = os.open(filename, flags, 0o600, dir_fd=journal_fd)
            try:
                os.fchmod(file_fd, 0o600)
                metadata = os.fstat(file_fd)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise JournalError("journal must be a non-linked regular file")
                fcntl.flock(file_fd, fcntl.LOCK_EX)
                existing = self._read_fd(file_fd)
                previous = existing[-1].record_hash if existing else _ZERO_HASH
                record = self._build_record(prepared, previous)
                from .schema import validate_journal_document

                validate_journal_document(record.to_dict())
                line = _canonical(record.to_dict()) + b"\n"
                if len(line) > MAX_RECORD_BYTES:
                    raise JournalError("journal record size exceeds the limit")
                view = memoryview(line)
                while view:
                    written = os.write(file_fd, view)
                    if written <= 0:
                        raise JournalError("journal append did not complete")
                    view = view[written:]
                os.fsync(file_fd)
                os.fsync(journal_fd)
                return record
            finally:
                os.close(file_fd)
        finally:
            os.close(journal_fd)

    def _build_record(self, prepared: dict[str, Any], previous: str) -> JournalRecord:
        body = {**prepared, "previous_hash": previous}
        digest = hashlib.sha256(_canonical(body)).hexdigest()
        event_id = f"evt-{digest[:24]}"
        record_hash = hashlib.sha256(_canonical({**body, "event_id": event_id})).hexdigest()
        return JournalRecord(event_id=event_id, record_hash=record_hash, **body)

    def _journal_directory_fd(self, *, create: bool) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        root_fd = os.open(self.data_home, flags)
        try:
            if create:
                try:
                    os.mkdir("journal", 0o700, dir_fd=root_fd)
                except FileExistsError:
                    pass
            directory_fd = os.open("journal", flags, dir_fd=root_fd)
            if stat.S_IMODE(os.fstat(directory_fd).st_mode) != 0o700:
                os.close(directory_fd)
                raise JournalError("journal directory must have mode 0700")
            return directory_fd
        finally:
            os.close(root_fd)

    def _read(self, filename: str) -> tuple[JournalRecord, ...]:
        if not self.data_home.exists():
            return ()
        if os.name != "posix":
            path = self.data_home / "journal" / filename
            if not path.exists():
                return ()
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise JournalError("journal path contains a reparse point")
            with path.open("rb") as handle:
                raw = handle.read(MAX_JOURNAL_BYTES + 1)
                if len(raw) > MAX_JOURNAL_BYTES:
                    raise JournalError("journal count or size exceeds the limit")
                return self._parse(raw)
        try:
            journal_fd = self._journal_directory_fd(create=False)
        except FileNotFoundError:
            return ()
        try:
            try:
                fd = os.open(
                    filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=journal_fd
                )
            except FileNotFoundError:
                return ()
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise JournalError("journal must be a non-linked regular file")
                if stat.S_IMODE(metadata.st_mode) != 0o600:
                    raise JournalError("journal file must have mode 0600")
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_SH)
                result = self._read_fd(fd)
                current = os.stat(filename, dir_fd=journal_fd, follow_symlinks=False)
                if (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino):
                    raise JournalError("journal changed during anchored read")
                return result
            finally:
                os.close(fd)
        finally:
            os.close(journal_fd)

    def _read_fd(self, fd: int) -> tuple[JournalRecord, ...]:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_JOURNAL_BYTES:
                raise JournalError("journal count or size exceeds the limit")
            chunks.append(chunk)
        return self._parse(b"".join(chunks))

    def _parse(self, raw: bytes) -> tuple[JournalRecord, ...]:
        if not raw:
            return ()
        if not raw.endswith(b"\n"):
            raise JournalError("journal contains a truncated record")
        records: list[JournalRecord] = []
        previous = _ZERO_HASH
        seen: set[str] = set()
        for line in raw.splitlines():
            if len(records) >= 10_000:
                raise JournalError("journal record count exceeds the limit")
            if len(line) > MAX_RECORD_BYTES:
                raise JournalError("journal record size exceeds the limit")
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict) or set(payload) != {
                    "schema_version",
                    "event_id",
                    "record_hash",
                    "previous_hash",
                    "timestamp",
                    "event_type",
                    "source_snapshot_id",
                    "artifact_path",
                    "summary",
                    "decision",
                    "metadata",
                }:
                    raise ValueError
                record = JournalRecord(**payload)
                from .schema import validate_journal_document

                validate_journal_document(record.to_dict())
            except Exception as exc:
                raise JournalError("journal contains an invalid record") from exc
            if record.schema_version != "1.0" or record.previous_hash != previous:
                raise JournalError("journal hash chain is invalid")
            rebuilt = self._build_record(
                {
                    "schema_version": record.schema_version,
                    "timestamp": record.timestamp,
                    "event_type": record.event_type,
                    "source_snapshot_id": record.source_snapshot_id,
                    "artifact_path": record.artifact_path,
                    "summary": record.summary,
                    "decision": record.decision,
                    "metadata": record.to_dict()["metadata"],
                },
                previous,
            )
            if rebuilt.event_id != record.event_id or rebuilt.record_hash != record.record_hash:
                raise JournalError("journal record identity is invalid")
            if record.event_id in seen:
                raise JournalError("journal contains a duplicate record")
            seen.add(record.event_id)
            previous = record.record_hash
            records.append(record)
        return tuple(records)

    def _append_fallback(self, filename: str, prepared: dict[str, Any]) -> JournalRecord:
        """Windows fallback: reparse checks and a process-local atomic append lock are best effort."""
        import threading

        lock = getattr(self, "_fallback_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._fallback_lock = lock
        with lock:
            directory = self.data_home / "journal"
            directory.mkdir(mode=0o700, exist_ok=True)
            if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
                raise JournalError("journal directory contains a reparse point")
            path = directory / filename
            records = self._read(filename)
            record = self._build_record(
                prepared, records[-1].record_hash if records else _ZERO_HASH
            )
            line = _canonical(record.to_dict()) + b"\n"
            if len(line) > MAX_RECORD_BYTES:
                raise JournalError("journal record size exceeds the limit")
            with path.open("ab") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            return record
