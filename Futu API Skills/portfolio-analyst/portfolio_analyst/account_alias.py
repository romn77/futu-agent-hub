"""Installation-private opaque account aliases."""

from __future__ import annotations

import errno
import hashlib
import hmac
import os
import secrets
import stat
from pathlib import Path


class AccountAliasError(ValueError):
    pass


class AccountAliaser:
    def __init__(self, key: bytes, *, provenance: str = "STABLE") -> None:
        if len(key) < 32:
            raise AccountAliasError("account alias key must contain at least 32 bytes")
        if provenance not in {"STABLE", "EPHEMERAL"}:
            raise AccountAliasError("unsupported account alias provenance")
        self._key = bytes(key)
        self.provenance = provenance

    @classmethod
    def from_key(cls, key: bytes) -> "AccountAliaser":
        return cls(key, provenance="STABLE")

    @classmethod
    def ephemeral(cls) -> "AccountAliaser":
        return cls(secrets.token_bytes(32), provenance="EPHEMERAL")

    def alias(self, account_id: str) -> str:
        digest = self.scoped_digest("account", account_id)
        return f"acct-{digest}"

    def scoped_digest(self, namespace: str, value: str) -> str:
        if not namespace or not value:
            raise AccountAliasError("alias namespace and value must be non-empty")
        digest = hmac.new(
            self._key,
            f"portfolio-{namespace}:{value}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:24]
        return digest


class AccountAliasStore:
    KEY_NAME = "account_alias.key"

    def __init__(self, data_home: Path) -> None:
        self.data_home = data_home.expanduser().absolute()

    def load_or_create(self) -> AccountAliaser:
        if os.name != "posix":
            return self._load_or_create_fallback()
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            root_fd = os.open(self.data_home, flags)
        except OSError as exc:
            raise AccountAliasError("private data home must be a real directory") from exc
        try:
            try:
                os.mkdir("config", 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            try:
                config_fd = os.open("config", flags, dir_fd=root_fd)
            except OSError as exc:
                raise AccountAliasError("private config path must not be a symlink") from exc
            try:
                try:
                    key = self._read_or_create_key(config_fd)
                except OSError as exc:
                    raise AccountAliasError("account alias key state is inaccessible") from exc
                if not self._config_anchor_matches(root_fd, config_fd, flags):
                    raise AccountAliasError("private config path changed during key access")
            finally:
                os.close(config_fd)
        finally:
            os.close(root_fd)
        return AccountAliaser(key, provenance="STABLE")

    @staticmethod
    def _config_anchor_matches(root_fd: int, anchored_fd: int, flags: int) -> bool:
        try:
            check_fd = os.open("config", flags, dir_fd=root_fd)
        except OSError:
            return False
        try:
            anchored = os.fstat(anchored_fd)
            current = os.fstat(check_fd)
            return (anchored.st_dev, anchored.st_ino) == (current.st_dev, current.st_ino)
        finally:
            os.close(check_fd)

    def _read_or_create_key(self, directory_fd: int) -> bytes:
        read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.KEY_NAME, read_flags, dir_fd=directory_fd)
        except FileNotFoundError:
            temporary = f".{self.KEY_NAME}.{secrets.token_hex(8)}.tmp"
            create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            temp_fd = os.open(temporary, create_flags, 0o600, dir_fd=directory_fd)
            try:
                os.fchmod(temp_fd, 0o600)
                key = secrets.token_bytes(32)
                remaining = memoryview(key)
                while remaining:
                    written = os.write(temp_fd, remaining)
                    if written <= 0:
                        raise AccountAliasError("account alias key write failed")
                    remaining = remaining[written:]
                os.fsync(temp_fd)
            finally:
                os.close(temp_fd)
            try:
                os.link(
                    temporary,
                    self.KEY_NAME,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                pass
            finally:
                os.unlink(temporary, dir_fd=directory_fd)
                os.fsync(directory_fd)
            fd = os.open(self.KEY_NAME, read_flags, dir_fd=directory_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise AccountAliasError("account alias key must not be a symlink") from exc
            raise
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise AccountAliasError("account alias key must be a 0600 regular file")
            key = os.read(fd, 64)
            if len(key) != 32:
                raise AccountAliasError("account alias key has invalid length")
            return key
        finally:
            os.close(fd)

    def _load_or_create_fallback(self) -> AccountAliaser:
        config = self.data_home / "config"
        config.mkdir(parents=True, exist_ok=True)
        if self._is_reparse(config) or not config.is_dir():
            raise AccountAliasError("private config path must not be a reparse point")
        path = config / self.KEY_NAME
        if self._is_reparse(path):
            raise AccountAliasError("account alias key must not be a symlink")
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            descriptor = None
        if descriptor is not None:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(secrets.token_bytes(32))
                handle.flush()
                os.fsync(handle.fileno())
        if self._is_reparse(path) or not path.is_file():
            raise AccountAliasError("account alias key must be a regular file")
        return AccountAliaser(path.read_bytes(), provenance="STABLE")

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        is_junction = getattr(path, "is_junction", lambda: False)
        return path.is_symlink() or bool(is_junction())
