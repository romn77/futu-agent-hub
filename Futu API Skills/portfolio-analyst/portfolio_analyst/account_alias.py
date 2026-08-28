"""Installation-private opaque account aliases."""

from __future__ import annotations

import errno
import hashlib
import hmac
import os
import secrets
import stat
from pathlib import Path

from .persistence import open_or_create_private_directory


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
        return self._load(create=True)

    def load(self) -> AccountAliaser:
        """Load the stable alias key without creating directories or files."""

        return self._load(create=False)

    def _load(self, *, create: bool) -> AccountAliaser:
        if os.name != "posix":
            raise AccountAliasError(
                "UNSUPPORTED_PLATFORM_SECURITY: account alias repository mutation disabled"
            )
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            root_fd = os.open(self.data_home, flags)
        except OSError as exc:
            raise AccountAliasError("private data home must be a real directory") from exc
        try:
            root_metadata = os.fstat(root_fd)
            if (
                not stat.S_ISDIR(root_metadata.st_mode)
                or stat.S_IMODE(root_metadata.st_mode) != 0o700
            ):
                raise AccountAliasError("private data home must have mode 0700")
            try:
                if create:
                    config_fd = open_or_create_private_directory(root_fd, "config", flags)
                else:
                    config_fd = os.open("config", flags, dir_fd=root_fd)
            except (OSError, ValueError) as exc:
                raise AccountAliasError("private config path must not be a symlink") from exc
            try:
                try:
                    key = self._read_or_create_key(config_fd, create=create)
                except OSError as exc:
                    raise AccountAliasError("account alias key state is inaccessible") from exc
                if not self._config_anchor_matches(
                    root_fd, config_fd, flags
                ) or not self._home_anchor_matches(root_fd, flags):
                    raise AccountAliasError("private config path changed during key access")
            finally:
                os.close(config_fd)
        finally:
            os.close(root_fd)
        return AccountAliaser(key, provenance="STABLE")

    def _home_anchor_matches(self, anchored_fd: int, flags: int) -> bool:
        try:
            check_fd = os.open(self.data_home, flags)
        except OSError:
            return False
        try:
            anchored = os.fstat(anchored_fd)
            current = os.fstat(check_fd)
            return (anchored.st_dev, anchored.st_ino) == (current.st_dev, current.st_ino)
        finally:
            os.close(check_fd)

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

    def _read_or_create_key(self, directory_fd: int, *, create: bool = True) -> bytes:
        read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.KEY_NAME, read_flags, dir_fd=directory_fd)
        except FileNotFoundError:
            if not create:
                raise AccountAliasError("stable account alias key is unavailable") from None
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
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size != 32
            ):
                raise AccountAliasError(
                    "account alias key must be a private non-linked 32-byte regular file"
                )
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, 33 - total)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > 32:
                    break
            key = b"".join(chunks)
            if len(key) != 32:
                raise AccountAliasError("account alias key has invalid length")
            after = os.fstat(fd)
            current = os.stat(self.KEY_NAME, dir_fd=directory_fd, follow_symlinks=False)
            identity = (metadata.st_dev, metadata.st_ino, metadata.st_size)
            if identity != (after.st_dev, after.st_ino, after.st_size) or identity != (
                current.st_dev,
                current.st_ino,
                current.st_size,
            ):
                raise AccountAliasError("account alias key changed during anchored read")
            return key
        finally:
            os.close(fd)

    def _load_or_create_fallback(self) -> AccountAliaser:
        raise AccountAliasError(
            "UNSUPPORTED_PLATFORM_SECURITY: account alias repository mutation disabled"
        )

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        is_junction = getattr(path, "is_junction", lambda: False)
        return path.is_symlink() or bool(is_junction())
