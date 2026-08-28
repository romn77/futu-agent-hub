"""Content-addressed immutable snapshot history below the private home."""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

from .models import PortfolioSnapshot
from .identity import InvalidSnapshotIdentity, parse_snapshot_identity
from .persistence import SnapshotAlreadyExists, SnapshotStore
from .private_home import PrivateHomeError, ensure_private_home
from .schema import validate_snapshot_document
from .snapshot_io import InvalidSnapshot, snapshot_from_document


MAX_SNAPSHOT_ENTRIES = 10_000
MAX_LATEST_SNAPSHOTS = 1_000
MAX_SNAPSHOT_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_HISTORY_TOTAL_BYTES = 256 * 1024 * 1024


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
        relative = self._relative(snapshot_id)
        loaded = self.read_path(relative)
        if loaded.snapshot_id != snapshot_id:
            raise SnapshotRepositoryError("snapshot path and body identity disagree")
        return loaded

    def read_path(self, relative_path: str | Path) -> PortfolioSnapshot:
        """Read a private explicit import path, or a canonical repository path, securely."""

        relative, raw = self.read_private_bytes(relative_path)
        try:
            payload = json.loads(raw, object_pairs_hook=self._unique_object)
            loaded = snapshot_from_document(payload)
        except InvalidSnapshot as exc:
            raise SnapshotRepositoryError(str(exc)) from exc
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            raise SnapshotRepositoryError("snapshot body is invalid") from exc
        canonical = self._relative(loaded.snapshot_id)
        if len(relative.parts) == 4 and relative.parts[0] == "snapshots" and relative != canonical:
            raise SnapshotRepositoryError("canonical snapshot repository path and body disagree")
        return loaded

    def read_private_bytes(self, relative_path: str | Path) -> tuple[Path, bytes]:
        """Read one bounded private file for explicit import consumers."""

        if os.name != "posix":
            raise SnapshotRepositoryError("UNSUPPORTED_PLATFORM_SECURITY: snapshot reads disabled")
        self._revalidate_home()
        relative = Path(relative_path)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise SnapshotRepositoryError(
                "snapshot import path must remain inside the private home"
            )
        return relative, self._read_anchored(relative)

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
                if not self._leaf_anchor_matches(
                    current_fd, relative.name, file_fd, metadata
                ) or not self._parent_anchor_matches(relative.parts[:-1], current_fd, dflags):
                    raise SnapshotRepositoryError("snapshot changed during anchored read")
                return b"".join(chunks)
            finally:
                os.close(file_fd)
        except OSError as exc:
            raise SnapshotRepositoryError("snapshot path is unavailable or unsafe") from exc
        finally:
            os.close(current_fd)
            os.close(root_fd)

    @staticmethod
    def _leaf_anchor_matches(
        parent_fd: int, name: str, file_fd: int, before: os.stat_result
    ) -> bool:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            after = os.fstat(file_fd)
        except OSError:
            return False
        identity = (before.st_dev, before.st_ino, before.st_size)
        return identity == (current.st_dev, current.st_ino, current.st_size) and identity == (
            after.st_dev,
            after.st_ino,
            after.st_size,
        )

    def _parent_anchor_matches(
        self, components: tuple[str, ...], anchored_fd: int, directory_flags: int
    ) -> bool:
        try:
            check_fd = os.open(self.data_home, directory_flags)
        except OSError:
            return False
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

    def list_range(
        self,
        *,
        start: datetime,
        end: datetime,
        limit: int,
        max_bytes: int,
    ) -> tuple[PortfolioSnapshot, ...]:
        """Read a bounded range without charging unrelated history to consumer budgets."""

        if (
            start.tzinfo is None
            or start.utcoffset() is None
            or end.tzinfo is None
            or end.utcoffset() is None
            or start > end
        ):
            raise SnapshotRepositoryError("snapshot range must be ordered and timezone-aware")
        if limit < 0 or max_bytes < 0:
            raise SnapshotRepositoryError("snapshot range budgets must be non-negative")
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

        start_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        start_second, end_second = start_utc.replace(microsecond=0), end_utc.replace(microsecond=0)
        months: list[tuple[int, int]] = []
        year, month = start_utc.year, start_utc.month
        while (year, month) <= (end_utc.year, end_utc.month):
            months.append((year, month))
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)

        candidates: list[tuple[str, Path, int]] = []
        seen_ids: set[str] = set()
        total_bytes = 0
        enumerated = 0
        checked_years: set[int] = set()
        for year_value, month_value in months:
            year_directory = root / f"{year_value:04d}"
            if year_value not in checked_years and year_directory.exists():
                if year_directory.is_symlink() or not year_directory.is_dir():
                    raise SnapshotRepositoryError("snapshot range year directory is unsafe")
                self._validate_directory_descriptor(year_directory)
                checked_years.add(year_value)
            directory = year_directory / f"{month_value:02d}"
            if not directory.exists():
                continue
            if directory.is_symlink() or not directory.is_dir():
                raise SnapshotRepositoryError("snapshot range directory is unsafe")
            self._validate_directory_descriptor(directory)
            for path in sorted(directory.iterdir()):
                enumerated += 1
                if enumerated > MAX_SNAPSHOT_ENTRIES:
                    raise SnapshotRepositoryError("snapshot enumeration entry budget exceeded")
                if path.name.startswith(".") and path.name.endswith(".tmp"):
                    continue
                if path.suffix != ".json":
                    raise SnapshotRepositoryError("snapshot history contains an unexpected entry")
                try:
                    identity = parse_snapshot_identity(path.stem)
                except InvalidSnapshotIdentity as exc:
                    raise SnapshotRepositoryError(
                        "snapshot history contains a non-canonical ID"
                    ) from exc
                instant = identity.timestamp.astimezone(timezone.utc)
                if instant < start_second or instant > end_second:
                    continue
                relative = path.relative_to(self.data_home)
                if self._relative(path.stem) != relative:
                    raise SnapshotRepositoryError("matching snapshot history path is not canonical")
                if path.stem in seen_ids:
                    raise SnapshotRepositoryError("snapshot range contains a duplicate identity")
                seen_ids.add(path.stem)
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    raise SnapshotRepositoryError("snapshot range entry is not a regular file")
                candidates.append((path.stem, relative, metadata.st_size))
                if len(candidates) > limit:
                    raise SnapshotRepositoryError("snapshot range consumer limit exceeded")
                total_bytes += metadata.st_size
                if total_bytes > max_bytes:
                    raise SnapshotRepositoryError("snapshot range byte budget exceeded")

        results: list[PortfolioSnapshot] = []
        for expected_id, relative, _size in candidates:
            item = self.read_path(relative)
            if item.snapshot_id != expected_id:
                raise SnapshotRepositoryError("snapshot path and body identity disagree")
            if start <= item.as_of <= end:
                results.append(item)
        return tuple(sorted(results, key=lambda item: (item.as_of, item.snapshot_id)))

    def latest(
        self,
        *,
        limit: int = MAX_LATEST_SNAPSHOTS,
        max_total_bytes: int = MAX_HISTORY_TOTAL_BYTES,
    ) -> PortfolioSnapshot:
        items = self.list(limit=limit, max_total_bytes=max_total_bytes)
        if not items:
            raise SnapshotRepositoryError("no valid immutable snapshots are available")
        return items[-1]
