"""Content-addressed immutable snapshot history below the private home."""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path

from .models import PortfolioSnapshot
from .identity import InvalidSnapshotIdentity, parse_snapshot_identity
from .persistence import SnapshotAlreadyExists, SnapshotStore
from .private_home import PrivateHomeError, ensure_private_home
from .schema import validate_snapshot_document
from .snapshot_io import InvalidSnapshot, snapshot_from_document


MAX_SNAPSHOT_ENTRIES = 10_000
MAX_SNAPSHOT_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024


class SnapshotRepositoryError(ValueError):
    pass


class SnapshotConflict(SnapshotRepositoryError):
    pass


class SnapshotRepository:
    """Save validated Phase-1 snapshots by UTC year/month and complete identity."""

    def __init__(self, data_home: Path) -> None:
        self.data_home = data_home.expanduser().absolute()
        if self.data_home.exists():
            try:
                self.data_home = ensure_private_home(self.data_home)
            except PrivateHomeError as exc:
                raise SnapshotRepositoryError(str(exc)) from exc

    @staticmethod
    def _relative(snapshot_id: str) -> Path:
        try:
            identity = parse_snapshot_identity(snapshot_id)
        except InvalidSnapshotIdentity as exc:
            raise SnapshotRepositoryError("snapshot ID is not canonical") from exc
        return Path(
            "snapshots",
            identity.timestamp.strftime("%Y"),
            identity.timestamp.strftime("%m"),
            f"{snapshot_id}.json",
        )

    def save(self, snapshot: PortfolioSnapshot) -> Path:
        # Reconstruction validates schema, identity and all financial semantics before persistence.
        if os.name != "posix":
            raise SnapshotRepositoryError(
                "UNSUPPORTED_PLATFORM_SECURITY: snapshot mutation disabled"
            )
        try:
            self.data_home = ensure_private_home(self.data_home, create=True)
        except PrivateHomeError as exc:
            raise SnapshotRepositoryError(str(exc)) from exc
        try:
            payload = snapshot.to_dict()
            validate_snapshot_document(payload)
            validated = snapshot_from_document(payload)
        except Exception as exc:
            raise SnapshotRepositoryError(
                "snapshot schema, identity, or semantics are invalid"
            ) from exc
        relative = self._relative(validated.snapshot_id)
        rendered = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        self._preflight_private_parents(relative)
        try:
            destination = SnapshotStore(self.data_home).save(relative, rendered)
            self._validate_private_parents(relative)
            return destination
        except SnapshotAlreadyExists:
            try:
                existing = self.read(validated.snapshot_id)
            except (InvalidSnapshot, SnapshotRepositoryError) as exc:
                raise SnapshotConflict(
                    "snapshot identity exists with invalid or conflicting content"
                ) from exc
            if existing.to_dict() != payload:
                raise SnapshotConflict("snapshot identity exists with conflicting content")
            return self.data_home / relative

    def read(self, snapshot_id: str) -> PortfolioSnapshot:
        if os.name != "posix":
            raise SnapshotRepositoryError("UNSUPPORTED_PLATFORM_SECURITY: snapshot reads disabled")
        self._revalidate_home()
        relative = self._relative(snapshot_id)
        try:
            raw = self._read_anchored(relative)
            payload = json.loads(raw, object_pairs_hook=self._unique_object)
            loaded = snapshot_from_document(payload)
        except (InvalidSnapshot, json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            raise SnapshotRepositoryError("snapshot body is invalid") from exc
        if loaded.snapshot_id != snapshot_id:
            raise SnapshotRepositoryError("snapshot path and body identity disagree")
        return loaded

    @staticmethod
    def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise SnapshotRepositoryError("snapshot JSON contains duplicate keys")
            result[key] = value
        return result

    def _revalidate_home(self) -> None:
        try:
            self.data_home = ensure_private_home(self.data_home)
        except PrivateHomeError as exc:
            raise SnapshotRepositoryError(str(exc)) from exc

    @staticmethod
    def _validate_directory_descriptor(path: Path) -> None:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
                    raise SnapshotRepositoryError("snapshot directories must have mode 0700")
            finally:
                os.close(fd)
        except OSError as exc:
            raise SnapshotRepositoryError("snapshot directory is unavailable or unsafe") from exc

    def _read_anchored(self, relative: Path) -> bytes:
        dflags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        fflags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            root_fd = os.open(self.data_home, dflags)
        except OSError as exc:
            raise SnapshotRepositoryError("snapshot path is unavailable or unsafe") from exc
        current_fd = os.dup(root_fd)
        try:
            root_metadata = os.fstat(root_fd)
            if stat.S_IMODE(root_metadata.st_mode) != 0o700:
                raise SnapshotRepositoryError("PORTFOLIO_AGENT_HOME must have mode 0700")
            for component in relative.parts[:-1]:
                next_fd = os.open(component, dflags, dir_fd=current_fd)
                metadata = os.fstat(next_fd)
                if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
                    os.close(next_fd)
                    raise SnapshotRepositoryError("snapshot directories must have mode 0700")
                os.close(current_fd)
                current_fd = next_fd
            file_fd = os.open(relative.name, fflags, dir_fd=current_fd)
            try:
                metadata = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_size > MAX_SNAPSHOT_BYTES
                ):
                    raise SnapshotRepositoryError(
                        "snapshot must be a bounded private non-linked regular file with mode 0600"
                    )
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = os.read(file_fd, min(65536, MAX_SNAPSHOT_BYTES + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > MAX_SNAPSHOT_BYTES:
                        raise SnapshotRepositoryError("snapshot file size budget exceeded")
                current = os.stat(relative.name, dir_fd=current_fd, follow_symlinks=False)
                if (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino):
                    raise SnapshotRepositoryError("snapshot changed during anchored read")
                return b"".join(chunks)
            finally:
                os.close(file_fd)
        except OSError as exc:
            raise SnapshotRepositoryError("snapshot path is unavailable or unsafe") from exc
        finally:
            os.close(current_fd)
            os.close(root_fd)

    def _validate_private_parents(self, relative: Path) -> None:
        current = self.data_home
        for component in relative.parts[:-1]:
            current = current / component
            try:
                metadata = current.lstat()
            except OSError as exc:
                raise SnapshotRepositoryError("snapshot directory is unavailable") from exc
            if not stat.S_ISDIR(metadata.st_mode) or current.is_symlink():
                raise SnapshotRepositoryError("snapshot directory is unsafe")
            if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o700:
                raise SnapshotRepositoryError("snapshot directories must have mode 0700")

    def _preflight_private_parents(self, relative: Path) -> None:
        current = self.data_home
        for component in relative.parts[:-1]:
            current = current / component
            if not current.exists():
                return
            metadata = current.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or current.is_symlink():
                raise SnapshotRepositoryError("snapshot directory is unsafe")
            if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o700:
                raise SnapshotRepositoryError("snapshot directories must have mode 0700")

    def list(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        max_total_bytes: int | None = None,
    ) -> tuple[PortfolioSnapshot, ...]:
        if limit is not None and limit < 0:
            raise SnapshotRepositoryError("snapshot list limit must be non-negative")
        byte_limit = MAX_SNAPSHOT_TOTAL_BYTES if max_total_bytes is None else max_total_bytes
        if byte_limit < 0:
            raise SnapshotRepositoryError("snapshot byte budget must be non-negative")
        if start is not None and (start.tzinfo is None or start.utcoffset() is None):
            raise SnapshotRepositoryError("snapshot range start must be timezone-aware")
        if end is not None and (end.tzinfo is None or end.utcoffset() is None):
            raise SnapshotRepositoryError("snapshot range end must be timezone-aware")
        if start is not None and end is not None and start > end:
            raise SnapshotRepositoryError("snapshot range start cannot follow end")
        if os.name != "posix":
            raise SnapshotRepositoryError(
                "UNSUPPORTED_PLATFORM_SECURITY: snapshot enumeration disabled"
            )
        self._revalidate_home()
        root = self.data_home / "snapshots"
        if not root.exists():
            return ()
        if root.is_symlink() or not root.is_dir():
            raise SnapshotRepositoryError("snapshot history root is unsafe")
        self._validate_directory_descriptor(root)
        candidates: list[tuple[Path, int]] = []
        total_bytes = 0
        enumerated_entries = 0

        def bounded_entries(directory: Path) -> list[Path]:
            nonlocal enumerated_entries
            entries: list[Path] = []
            for entry in directory.iterdir():
                enumerated_entries += 1
                if enumerated_entries > MAX_SNAPSHOT_ENTRIES:
                    raise SnapshotRepositoryError("snapshot enumeration entry budget exceeded")
                entries.append(entry)
            return sorted(entries)

        for year in bounded_entries(root):
            if year.is_symlink() or not year.is_dir() or not re.fullmatch(r"\d{4}", year.name):
                raise SnapshotRepositoryError("snapshot history contains an unsafe entry")
            self._validate_directory_descriptor(year)
            for month in bounded_entries(year):
                if (
                    month.is_symlink()
                    or not month.is_dir()
                    or not re.fullmatch(r"\d{2}", month.name)
                    or not 1 <= int(month.name) <= 12
                ):
                    raise SnapshotRepositoryError(
                        "snapshot history contains an unsafe or invalid month"
                    )
                self._validate_directory_descriptor(month)
                for path in bounded_entries(month):
                    if path.name.startswith(".") and path.name.endswith(".tmp"):
                        continue
                    if path.suffix != ".json":
                        raise SnapshotRepositoryError(
                            "snapshot history contains an unexpected entry"
                        )
                    relative = path.relative_to(self.data_home)
                    if self._relative(path.stem) != relative:
                        raise SnapshotRepositoryError("snapshot history path is not canonical")
                    try:
                        timestamp = parse_snapshot_identity(path.stem).timestamp
                    except InvalidSnapshotIdentity as exc:
                        raise SnapshotRepositoryError(
                            "snapshot history identity is invalid"
                        ) from exc
                    if start is not None and timestamp < start.astimezone(timestamp.tzinfo):
                        continue
                    if end is not None and timestamp > end.astimezone(timestamp.tzinfo):
                        continue
                    metadata = path.lstat()
                    candidates.append((path, metadata.st_size))
                    if limit is not None and len(candidates) > limit:
                        raise SnapshotRepositoryError("snapshot list consumer limit exceeded")
                    total_bytes += metadata.st_size
                    if total_bytes > byte_limit:
                        raise SnapshotRepositoryError("snapshot enumeration byte budget exceeded")
        results: list[PortfolioSnapshot] = []
        seen: set[str] = set()
        for path, _size in candidates:
            item = self.read(path.stem)
            if item.snapshot_id in seen:
                raise SnapshotRepositoryError("snapshot history contains a duplicate identity")
            seen.add(item.snapshot_id)
            if start is not None and item.as_of < start:
                continue
            if end is not None and item.as_of > end:
                continue
            results.append(item)
        return tuple(sorted(results, key=lambda item: (item.as_of, item.snapshot_id)))

    def latest(self) -> PortfolioSnapshot:
        items = self.list()
        if not items:
            raise SnapshotRepositoryError("no valid immutable snapshots are available")
        return items[-1]
