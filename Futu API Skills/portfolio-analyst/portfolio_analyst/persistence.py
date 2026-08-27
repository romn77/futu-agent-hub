"""Atomic, immutable snapshot persistence below the private data home."""

from __future__ import annotations

import os
import secrets
import stat
import tempfile
from pathlib import Path


class SnapshotAlreadyExists(FileExistsError):
    pass


class SnapshotStore:
    def __init__(self, data_home: Path) -> None:
        self.data_home = data_home.expanduser().absolute()

    def save(self, relative_path: str | Path, content: str) -> Path:
        parts = self._validated_parts(relative_path)
        if os.name != "posix":
            return self._save_fallback(parts, content)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            current_fd = os.open(self.data_home, directory_flags)
        except OSError as exc:
            raise ValueError("PORTFOLIO_AGENT_HOME must be a real directory") from exc
        try:
            for component in parts[:-1]:
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                try:
                    next_fd = os.open(component, directory_flags, dir_fd=current_fd)
                except OSError as exc:
                    raise ValueError("output path contains a symlink or non-directory") from exc
                os.close(current_fd)
                current_fd = next_fd
            leaf = parts[-1]
            temporary = f".{leaf}.{secrets.token_hex(8)}.tmp"
            create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, create_flags, 0o600, dir_fd=current_fd)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(
                        temporary,
                        leaf,
                        src_dir_fd=current_fd,
                        dst_dir_fd=current_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise SnapshotAlreadyExists(f"snapshot already exists: {leaf}") from exc
                os.fsync(current_fd)
                if not self._parent_anchor_matches(parts[:-1], current_fd, directory_flags):
                    os.unlink(leaf, dir_fd=current_fd)
                    os.fsync(current_fd)
                    raise ValueError("output directory changed during atomic persistence")
            finally:
                try:
                    os.unlink(temporary, dir_fd=current_fd)
                except FileNotFoundError:
                    pass
                else:
                    os.fsync(current_fd)
        finally:
            os.close(current_fd)
        return self.data_home.joinpath(*parts)

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

    @staticmethod
    def _validated_parts(relative_path: str | Path) -> tuple[str, ...]:
        candidate = Path(relative_path)
        if candidate.is_absolute() or not candidate.parts:
            raise ValueError("output path must remain inside PORTFOLIO_AGENT_HOME")
        if any(part in {"", ".", ".."} for part in candidate.parts):
            raise ValueError("output path must remain inside PORTFOLIO_AGENT_HOME")
        return candidate.parts

    def _save_fallback(self, parts: tuple[str, ...], content: str) -> Path:
        """Best-effort Windows fallback: lstat/reparse checks plus atomic no-overwrite link."""

        current = self.data_home
        if self._is_reparse(current) or not current.is_dir():
            raise ValueError("PORTFOLIO_AGENT_HOME must be a real directory")
        for component in parts[:-1]:
            current = current / component
            current.mkdir(mode=0o700, exist_ok=True)
            metadata = current.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or self._is_reparse(current):
                raise ValueError("output path contains a symlink or reparse point")
        destination = current / parts[-1]
        if destination.exists() or self._is_reparse(destination):
            raise SnapshotAlreadyExists(f"snapshot already exists: {destination.name}")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=current
        )
        temporary = Path(temporary_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError as exc:
                raise SnapshotAlreadyExists(f"snapshot already exists: {destination.name}") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        is_junction = getattr(path, "is_junction", lambda: False)
        return path.is_symlink() or bool(is_junction())
