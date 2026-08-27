"""Anchored no-follow loading for configuration containing raw account IDs."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any


class PrivateConfigError(ValueError):
    pass


def load_private_json_object(data_home: Path, filename: str) -> dict[str, Any]:
    if os.name != "posix":
        return _load_fallback(data_home, filename)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(data_home.expanduser().absolute(), directory_flags)
    except OSError as exc:
        raise PrivateConfigError("private data home is unavailable") from exc
    try:
        try:
            config_fd = os.open("config", directory_flags, dir_fd=root_fd)
        except OSError as exc:
            raise PrivateConfigError("private config directory is unavailable or unsafe") from exc
        try:
            config_stat = os.fstat(config_fd)
            if not stat.S_ISDIR(config_stat.st_mode) or stat.S_IMODE(config_stat.st_mode) != 0o700:
                raise PrivateConfigError("private config directory must have mode 0700")
            try:
                file_fd = os.open(filename, file_flags, dir_fd=config_fd)
            except OSError as exc:
                raise PrivateConfigError(
                    "private account configuration is unavailable or unsafe"
                ) from exc
            try:
                file_stat = os.fstat(file_fd)
                if not stat.S_ISREG(file_stat.st_mode) or stat.S_IMODE(file_stat.st_mode) != 0o600:
                    raise PrivateConfigError("private account configuration must have mode 0600")
                raw = _read_bounded(file_fd)
                if not _same_file_anchor(config_fd, filename, file_fd, file_flags):
                    raise PrivateConfigError("private account configuration changed during read")
                if not _same_directory_anchor(root_fd, "config", config_fd, directory_flags):
                    raise PrivateConfigError("private config directory changed during read")
            finally:
                os.close(file_fd)
        finally:
            os.close(config_fd)
    finally:
        os.close(root_fd)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrivateConfigError("private account configuration is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise PrivateConfigError("private account configuration root must be an object")
    return payload


def _read_bounded(file_fd: int, limit: int = 1024 * 1024) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(file_fd, min(65536, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise PrivateConfigError("private account configuration is too large")


def _same_file_anchor(parent_fd: int, name: str, anchored_fd: int, flags: int) -> bool:
    try:
        check_fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError:
        return False
    try:
        anchored = os.fstat(anchored_fd)
        current = os.fstat(check_fd)
        return (anchored.st_dev, anchored.st_ino) == (current.st_dev, current.st_ino)
    finally:
        os.close(check_fd)


def _same_directory_anchor(parent_fd: int, name: str, anchored_fd: int, flags: int) -> bool:
    return _same_file_anchor(parent_fd, name, anchored_fd, flags)


def _load_fallback(data_home: Path, filename: str) -> dict[str, Any]:
    config = data_home.expanduser().absolute() / "config"
    path = config / filename
    if _is_reparse(config) or _is_reparse(path):
        raise PrivateConfigError("private account configuration contains a reparse point")
    try:
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise PrivateConfigError("private account configuration is unavailable") from exc
    if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != (
        after.st_dev,
        after.st_ino,
    ):
        raise PrivateConfigError("private account configuration changed during read")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrivateConfigError("private account configuration is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise PrivateConfigError("private account configuration root must be an object")
    return payload


def _is_reparse(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", lambda: False)
    return path.is_symlink() or bool(is_junction())
