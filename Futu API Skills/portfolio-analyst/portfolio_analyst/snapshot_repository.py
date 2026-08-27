"""Content-addressed immutable snapshot history below the private home."""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path

from .models import PortfolioSnapshot
from .persistence import SnapshotAlreadyExists, SnapshotStore
from .private_home import PrivateHomeError, ensure_private_home
from .schema import validate_snapshot_document
from .snapshot_io import InvalidSnapshot, load_snapshot, snapshot_from_document


_ID = re.compile(r"^ps-(\d{4})(\d{2})\d{2}T\d{6}Z-(?:real|simulate)-[a-z]{3}-[0-9a-f]{16}$")


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
        match = _ID.fullmatch(snapshot_id)
        if match is None:
            raise SnapshotRepositoryError("snapshot ID is not canonical")
        return Path("snapshots", match.group(1), match.group(2), f"{snapshot_id}.json")

    def save(self, snapshot: PortfolioSnapshot) -> Path:
        # Reconstruction validates schema, identity and all financial semantics before persistence.
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
        self._validate_private_parents(relative)
        path = self.data_home / relative
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise SnapshotRepositoryError("snapshot is unavailable") from exc
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink() or metadata.st_nlink != 1:
            raise SnapshotRepositoryError("snapshot must be a private non-linked regular file")
        if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600:
            raise SnapshotRepositoryError("snapshot file must have mode 0600")
        try:
            loaded = load_snapshot(self.data_home, relative)
        except InvalidSnapshot as exc:
            raise SnapshotRepositoryError("snapshot body is invalid") from exc
        if loaded.snapshot_id != snapshot_id:
            raise SnapshotRepositoryError("snapshot path and body identity disagree")
        return loaded

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
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> tuple[PortfolioSnapshot, ...]:
        if start is not None and (start.tzinfo is None or start.utcoffset() is None):
            raise SnapshotRepositoryError("snapshot range start must be timezone-aware")
        if end is not None and (end.tzinfo is None or end.utcoffset() is None):
            raise SnapshotRepositoryError("snapshot range end must be timezone-aware")
        if start is not None and end is not None and start > end:
            raise SnapshotRepositoryError("snapshot range start cannot follow end")
        root = self.data_home / "snapshots"
        if not root.exists():
            return ()
        if root.is_symlink() or not root.is_dir():
            raise SnapshotRepositoryError("snapshot history root is unsafe")
        results: list[PortfolioSnapshot] = []
        for year in sorted(root.iterdir()):
            if year.is_symlink() or not year.is_dir() or not re.fullmatch(r"\d{4}", year.name):
                raise SnapshotRepositoryError("snapshot history contains an unsafe entry")
            for month in sorted(year.iterdir()):
                if (
                    month.is_symlink()
                    or not month.is_dir()
                    or not re.fullmatch(r"\d{2}", month.name)
                ):
                    raise SnapshotRepositoryError("snapshot history contains an unsafe entry")
                for path in sorted(month.iterdir()):
                    if path.suffix != ".json":
                        raise SnapshotRepositoryError(
                            "snapshot history contains an unexpected entry"
                        )
                    item = self.read(path.stem)
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
