"""Atomic, immutable snapshot persistence below the private data home."""

from __future__ import annotations

import os
import secrets
import stat
import hashlib
import json
import re
from pathlib import Path
from typing import Mapping


class SnapshotAlreadyExists(FileExistsError):
    pass


class IncompleteBundle(ValueError):
    pass


class SnapshotStore:
    def __init__(self, data_home: Path) -> None:
        self.data_home = data_home.expanduser().absolute()

    def save(self, relative_path: str | Path, content: str) -> Path:
        parts = self._validated_parts(relative_path)
        if os.name != "posix":
            raise ValueError("UNSUPPORTED_PLATFORM_SECURITY: repository mutation disabled")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            current_fd = os.open(self.data_home, directory_flags)
        except OSError as exc:
            raise ValueError("PORTFOLIO_AGENT_HOME must be a real directory") from exc
        try:
            self._require_private_directory(current_fd, "PORTFOLIO_AGENT_HOME")
            for component in parts[:-1]:
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                try:
                    next_fd = os.open(component, directory_flags, dir_fd=current_fd)
                except OSError as exc:
                    raise ValueError("output path contains a symlink or non-directory") from exc
                self._require_private_directory(next_fd, "output directory")
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
        ordered = tuple(
            sorted(files.items(), key=lambda item: (item[0] == "manifest.json", item[0]))
        )
        for name, content in ordered:
            if Path(name).parts != (name,) or name in {"", ".", ".."}:
                raise ValueError("bundle filenames must be simple relative names")
            if not isinstance(content, str):
                raise ValueError("bundle content must be text")
        if os.name != "posix":
            raise ValueError("UNSUPPORTED_PLATFORM_SECURITY: bundle mutation disabled")

        directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            root_fd = os.open(self.data_home, directory_flags)
        except OSError as exc:
            raise ValueError("PORTFOLIO_AGENT_HOME must be a real directory") from exc
        current_fd = os.dup(root_fd)
        staged: dict[str, str] = {}
        committed: list[str] = []
        try:
            self._require_private_directory(root_fd, "PORTFOLIO_AGENT_HOME")
            for component in directory_parts:
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                try:
                    next_fd = os.open(component, directory_flags, dir_fd=current_fd)
                except OSError as exc:
                    raise ValueError("output path contains a symlink or non-directory") from exc
                self._require_private_directory(next_fd, "output directory")
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

    @staticmethod
    def _require_private_directory(fd: int, label: str) -> None:
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError(f"{label} must be a private directory with mode 0700")

    def validate_bundle(self, relative_directory: str | Path) -> dict[str, Path]:
        """Fail closed unless a manifest-last bundle is complete and digest-consistent."""

        parts = self._validated_parts(relative_directory)
        if os.name != "posix":
            raise IncompleteBundle("UNSUPPORTED_PLATFORM_SECURITY: bundle reads disabled")
        directory = self.data_home.joinpath(*parts)
        for private_directory in (self.data_home, *directory.parents[::-1], directory):
            if private_directory == private_directory.parent:
                continue
            if private_directory == self.data_home or self.data_home in private_directory.parents:
                metadata = private_directory.lstat()
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or private_directory.is_symlink()
                    or stat.S_IMODE(metadata.st_mode) != 0o700
                ):
                    raise IncompleteBundle("bundle is INCOMPLETE: directory is unsafe")
        try:
            entries = sorted(directory.iterdir())
        except OSError as exc:
            raise IncompleteBundle("bundle is INCOMPLETE: directory unavailable") from exc
        visible = [
            entry
            for entry in entries
            if not (entry.name.startswith(".") and entry.name.endswith(".tmp"))
        ]
        manifest_path = directory / "manifest.json"
        if manifest_path not in visible:
            raise IncompleteBundle("bundle is INCOMPLETE: manifest missing")
        try:
            manifest_metadata = manifest_path.lstat()
            if (
                not stat.S_ISREG(manifest_metadata.st_mode)
                or manifest_metadata.st_nlink != 1
                or stat.S_IMODE(manifest_metadata.st_mode) != 0o600
                or manifest_metadata.st_size > 1024 * 1024
            ):
                raise ValueError
            manifest = json.loads(manifest_path.read_bytes(), object_pairs_hook=self._unique_json)
            if (
                not isinstance(manifest, dict)
                or set(manifest) != {"schema_version", "source_snapshot_id", "artifacts"}
                or manifest["schema_version"] != "1.0"
                or re.fullmatch(
                    r"ps-[0-9]{8}T[0-9]{6}Z-(?:real|simulate)-[a-z]{3}-[0-9a-f]{16}",
                    str(manifest["source_snapshot_id"]),
                )
                is None
            ):
                raise ValueError
            expected = manifest["artifacts"]
        except Exception as exc:
            raise IncompleteBundle("bundle is INCOMPLETE: manifest invalid") from exc
        if (
            not isinstance(expected, dict)
            or len(expected) > 128
            or set(expected) != {entry.name for entry in visible if entry.name != "manifest.json"}
        ):
            raise IncompleteBundle("bundle is INCOMPLETE: artifact enumeration mismatch")
        result: dict[str, Path] = {}
        for name, digest in sorted(expected.items()):
            path = directory / name
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise IncompleteBundle("bundle is INCOMPLETE: artifact is unsafe")
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise IncompleteBundle("bundle is INCOMPLETE: artifact digest mismatch")
            result[name] = path
        return result

    @staticmethod
    def _unique_json(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise IncompleteBundle("bundle is INCOMPLETE: duplicate manifest key")
            result[key] = value
        return result

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
        raise ValueError("UNSUPPORTED_PLATFORM_SECURITY: repository mutation disabled")

    def _save_bundle_fallback(
        self, directory_parts: tuple[str, ...], ordered: tuple[tuple[str, str], ...]
    ) -> dict[str, Path]:
        raise ValueError("UNSUPPORTED_PLATFORM_SECURITY: bundle mutation disabled")
