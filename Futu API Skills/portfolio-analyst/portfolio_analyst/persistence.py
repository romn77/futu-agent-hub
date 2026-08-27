"""Atomic, immutable snapshot persistence below the private data home."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .settings import resolve_output_path


class SnapshotAlreadyExists(FileExistsError):
    pass


class SnapshotStore:
    def __init__(self, data_home: Path) -> None:
        self.data_home = data_home.expanduser().resolve()

    def save(self, relative_path: str | Path, content: str) -> Path:
        destination = resolve_output_path(self.data_home, relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise SnapshotAlreadyExists(f"snapshot already exists: {destination.name}")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise SnapshotAlreadyExists(f"snapshot already exists: {destination.name}") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return destination
