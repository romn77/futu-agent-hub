"""Atomic, immutable snapshot persistence below the private data home."""

from __future__ import annotations

import os
import secrets
import stat
import tempfile
from pathlib import Path
from typing import Mapping


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

    def save_bundle(
        self, relative_directory: str | Path, files: Mapping[str, str]
    ) -> dict[str, Path]:
        directory_parts = self._validated_parts(relative_directory)
        if not files:
            raise ValueError("bundle must contain at least one file")
        ordered = tuple(sorted(files.items()))
        for name, content in ordered:
            if Path(name).parts != (name,) or name in {"", ".", ".."}:
                raise ValueError("bundle filenames must be simple relative names")
            if not isinstance(content, str):
                raise ValueError("bundle content must be text")
        if os.name != "posix":
            return self._save_bundle_fallback(directory_parts, ordered)

        directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            root_fd = os.open(self.data_home, directory_flags)
        except OSError as exc:
            raise ValueError("PORTFOLIO_AGENT_HOME must be a real directory") from exc
        current_fd = os.dup(root_fd)
        staged: dict[str, str] = {}
        committed: list[str] = []
        try:
            for component in directory_parts:
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
            for name, _content in ordered:
                try:
                    os.stat(name, dir_fd=current_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise SnapshotAlreadyExists(f"snapshot already exists: {name}")
            create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            for name, content in ordered:
                temporary = f".{name}.{secrets.token_hex(8)}.tmp"
                descriptor = os.open(temporary, create_flags, 0o600, dir_fd=current_fd)
                staged[name] = temporary
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            try:
                for name, _content in ordered:
                    try:
                        os.link(
                            staged[name],
                            name,
                            src_dir_fd=current_fd,
                            dst_dir_fd=current_fd,
                            follow_symlinks=False,
                        )
                    except FileExistsError as exc:
                        raise SnapshotAlreadyExists(
                            f"bundle destination already exists: {name}"
                        ) from exc
                    committed.append(name)
                os.fsync(current_fd)
                if not self._parent_anchor_matches(directory_parts, current_fd, directory_flags):
                    raise ValueError("output directory changed during transactional persistence")
            except BaseException:
                for name in committed:
                    try:
                        os.unlink(name, dir_fd=current_fd)
                    except FileNotFoundError:
                        pass
                os.fsync(current_fd)
                raise
        finally:
            try:
                for temporary in staged.values():
                    try:
                        os.unlink(temporary, dir_fd=current_fd)
                    except FileNotFoundError:
                        pass
                if staged:
                    os.fsync(current_fd)
            finally:
                os.close(current_fd)
                os.close(root_fd)
        return {name: self.data_home.joinpath(*directory_parts, name) for name, _content in ordered}

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

    def _save_bundle_fallback(
        self, directory_parts: tuple[str, ...], ordered: tuple[tuple[str, str], ...]
    ) -> dict[str, Path]:
        current = self.data_home
        if self._is_reparse(current) or not current.is_dir():
            raise ValueError("PORTFOLIO_AGENT_HOME must be a real directory")
        for component in directory_parts:
            current = current / component
            current.mkdir(mode=0o700, exist_ok=True)
            if self._is_reparse(current) or not current.is_dir():
                raise ValueError("output path contains a symlink or reparse point")
        destinations = {name: current / name for name, _content in ordered}
        if any(path.exists() or self._is_reparse(path) for path in destinations.values()):
            raise SnapshotAlreadyExists("bundle destination already exists")
        staged: dict[str, Path] = {}
        committed: list[Path] = []
        try:
            for name, content in ordered:
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{name}.", suffix=".tmp", dir=current
                )
                temporary = Path(temporary_name)
                staged[name] = temporary
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            for name, _content in ordered:
                try:
                    os.link(staged[name], destinations[name], follow_symlinks=False)
                except FileExistsError as exc:
                    raise SnapshotAlreadyExists(
                        f"bundle destination already exists: {name}"
                    ) from exc
                committed.append(destinations[name])
        except BaseException:
            for destination in committed:
                destination.unlink(missing_ok=True)
            raise
        finally:
            for temporary in staged.values():
                temporary.unlink(missing_ok=True)
        return destinations

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        is_junction = getattr(path, "is_junction", lambda: False)
        return path.is_symlink() or bool(is_junction())
