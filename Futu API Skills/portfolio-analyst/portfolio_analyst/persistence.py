"""Atomic, immutable snapshot persistence below the private data home."""

from __future__ import annotations

import os
import secrets
import stat
import hashlib
import json
from pathlib import Path
from typing import Mapping

from .identity import parse_snapshot_identity


MAX_BUNDLE_ENTRIES = 128
MAX_BUNDLE_FILE_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_TOTAL_BYTES = 128 * 1024 * 1024
MAX_BUNDLE_MANIFEST_BYTES = 1024 * 1024


class SnapshotAlreadyExists(FileExistsError):
    pass


class IncompleteBundle(ValueError):
    pass


def bundle_manifest(
    *, source_snapshot_id: str, as_of: str, report_id: str, files: Mapping[str, str]
) -> str:
    identity = parse_snapshot_identity(source_snapshot_id)
    from datetime import datetime, timezone

    timestamp = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    if timestamp.tzinfo is None or timestamp.astimezone(timezone.utc) != identity.timestamp:
        raise ValueError("bundle as_of must match source snapshot identity")
    if not report_id or len(report_id) > 256:
        raise ValueError("bundle report_id is invalid")
    artifacts = {
        name: {
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "size_bytes": len(content.encode("utf-8")),
        }
        for name, content in sorted(files.items())
    }
    return (
        json.dumps(
            {
                "schema_version": "1.0",
                "source_snapshot_id": source_snapshot_id,
                "as_of": as_of,
                "report_id": report_id,
                "artifacts": artifacts,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


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
        if len(ordered) > MAX_BUNDLE_ENTRIES + 1:
            raise ValueError("bundle entry budget exceeded")
        aggregate_bytes = 0
        for name, content in ordered:
            if Path(name).parts != (name,) or name in {"", ".", ".."}:
                raise ValueError("bundle filenames must be simple relative names")
            if not isinstance(content, str):
                raise ValueError("bundle content must be text")
            encoded_size = len(content.encode("utf-8"))
            maximum = (
                MAX_BUNDLE_MANIFEST_BYTES if name == "manifest.json" else MAX_BUNDLE_FILE_BYTES
            )
            if encoded_size > maximum:
                raise ValueError("bundle file byte budget exceeded")
            aggregate_bytes += encoded_size
            if aggregate_bytes > MAX_BUNDLE_TOTAL_BYTES:
                raise ValueError("bundle aggregate byte budget exceeded")
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
                artifacts = tuple(item for item in ordered if item[0] != "manifest.json")
                manifest = tuple(item for item in ordered if item[0] == "manifest.json")
                for name, _content in artifacts:
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
                for name, _content in manifest:
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
                if manifest:
                    os.fsync(current_fd)
                    if not self._parent_anchor_matches(
                        directory_parts, current_fd, directory_flags
                    ):
                        raise ValueError(
                            "output directory changed during transactional persistence"
                        )
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
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            root_fd = os.open(self.data_home, directory_flags)
        except OSError as exc:
            raise IncompleteBundle("bundle is INCOMPLETE: directory unavailable") from exc
        directory_fd = os.dup(root_fd)
        try:
            self._require_private_directory(root_fd, "PORTFOLIO_AGENT_HOME")
            for component in parts:
                next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
                self._require_private_directory(next_fd, "bundle directory")
                os.close(directory_fd)
                directory_fd = next_fd
            entries = os.listdir(directory_fd)
            if len(entries) > 4096:
                raise IncompleteBundle("bundle is INCOMPLETE: entry budget exceeded")
            visible = {
                name for name in entries if not (name.startswith(".") and name.endswith(".tmp"))
            }
            if len(visible) > MAX_BUNDLE_ENTRIES + 1:
                raise IncompleteBundle("bundle is INCOMPLETE: entry budget exceeded")
            if "manifest.json" not in visible:
                raise IncompleteBundle("bundle is INCOMPLETE: manifest missing")
            manifest_bytes, _manifest_digest = self._read_anchored_bundle_file(
                directory_fd, "manifest.json", MAX_BUNDLE_MANIFEST_BYTES, retain=True
            )
            if not self._parent_anchor_matches(parts, directory_fd, directory_flags):
                raise IncompleteBundle("bundle is INCOMPLETE: directory anchor changed")
            try:
                manifest = json.loads(manifest_bytes, object_pairs_hook=self._unique_json)
                if not isinstance(manifest, dict) or set(manifest) != {
                    "schema_version",
                    "source_snapshot_id",
                    "as_of",
                    "report_id",
                    "artifacts",
                }:
                    raise ValueError
                if manifest["schema_version"] != "1.0":
                    raise ValueError
                identity = parse_snapshot_identity(manifest["source_snapshot_id"])
                from datetime import datetime, timezone

                as_of = datetime.fromisoformat(str(manifest["as_of"]).replace("Z", "+00:00"))
                if as_of.tzinfo is None or as_of.astimezone(timezone.utc) != identity.timestamp:
                    raise ValueError
                report_id = manifest["report_id"]
                if not isinstance(report_id, str) or not report_id or len(report_id) > 256:
                    raise ValueError
                expected = manifest["artifacts"]
            except Exception as exc:
                raise IncompleteBundle("bundle is INCOMPLETE: manifest invalid") from exc
            if (
                not isinstance(expected, dict)
                or len(expected) > MAX_BUNDLE_ENTRIES
                or set(expected) != visible - {"manifest.json"}
            ):
                raise IncompleteBundle("bundle is INCOMPLETE: artifact enumeration mismatch")
            result: dict[str, Path] = {}
            total_bytes = len(manifest_bytes)
            for name, evidence in sorted(expected.items()):
                if (
                    Path(name).parts != (name,)
                    or not isinstance(evidence, dict)
                    or set(evidence) != {"sha256", "size_bytes"}
                ):
                    raise IncompleteBundle("bundle is INCOMPLETE: artifact manifest invalid")
                size = evidence["size_bytes"]
                digest = evidence["sha256"]
                if (
                    not isinstance(size, int)
                    or isinstance(size, bool)
                    or size < 0
                    or size > MAX_BUNDLE_FILE_BYTES
                    or not isinstance(digest, str)
                    or len(digest) != 64
                ):
                    raise IncompleteBundle("bundle is INCOMPLETE: artifact manifest invalid")
                _unused, actual_digest, actual_size = self._read_anchored_bundle_file(
                    directory_fd, name, MAX_BUNDLE_FILE_BYTES, retain=False, include_size=True
                )
                if not self._parent_anchor_matches(parts, directory_fd, directory_flags):
                    raise IncompleteBundle("bundle is INCOMPLETE: directory anchor changed")
                total_bytes += actual_size
                if total_bytes > MAX_BUNDLE_TOTAL_BYTES:
                    raise IncompleteBundle("bundle is INCOMPLETE: aggregate byte budget exceeded")
                if actual_size != size or actual_digest != digest:
                    raise IncompleteBundle("bundle is INCOMPLETE: artifact digest or size mismatch")
                result[name] = self.data_home.joinpath(*parts, name)
            if not self._parent_anchor_matches(parts, directory_fd, directory_flags):
                raise IncompleteBundle("bundle is INCOMPLETE: directory anchor changed")
            return result
        except IncompleteBundle:
            raise
        except (OSError, ValueError) as exc:
            raise IncompleteBundle("bundle is INCOMPLETE: unsafe anchored read") from exc
        finally:
            os.close(directory_fd)
            os.close(root_fd)

    @staticmethod
    def _read_anchored_bundle_file(
        directory_fd: int,
        name: str,
        maximum: int,
        *,
        retain: bool,
        include_size: bool = False,
    ) -> tuple[bytes, str] | tuple[bytes, str, int]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(name, flags, dir_fd=directory_fd)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size > maximum
            ):
                raise IncompleteBundle("bundle is INCOMPLETE: artifact is unsafe")
            digest = hashlib.sha256()
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, min(65536, maximum + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise IncompleteBundle("bundle is INCOMPLETE: artifact size limit exceeded")
                digest.update(chunk)
                if retain:
                    chunks.append(chunk)
            after = os.fstat(fd)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            identity_before = (before.st_dev, before.st_ino, before.st_size)
            if identity_before != (
                after.st_dev,
                after.st_ino,
                after.st_size,
            ) or identity_before != (
                current.st_dev,
                current.st_ino,
                current.st_size,
            ):
                raise IncompleteBundle("bundle is INCOMPLETE: artifact changed during read")
            result = (b"".join(chunks), digest.hexdigest())
            return (*result, total) if include_size else result
        finally:
            os.close(fd)

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
