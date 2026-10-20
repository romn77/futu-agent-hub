"""Descriptor-anchored, private, bounded files with atomic publication."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import secrets
import stat
import time

from .types import CacheError, canonical, unique_json


DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
BUCKETS = {"objects", "entries", "refs", "state", "leases", "access"}


def _private(metadata, *, directory=False):
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != (0o700 if directory else 0o600)
            or metadata.st_uid != os.getuid() or (not directory and metadata.st_nlink != 1)):
        raise CacheError("UNSAFE_PATH")


class Disk:
    def __init__(self, root: Path, scope: str):
        self.root, self.scope = root, scope
        self.fd = -1

    @contextmanager
    def locked(self, *, create=False):
        if os.name != "posix":
            raise CacheError("UNSUPPORTED_BACKEND")
        import fcntl

        if any((parent / ".git").exists() for parent in (self.root, *self.root.parents)):
            raise CacheError("UNSAFE_PATH")
        current = os.open("/", DIR_FLAGS)
        lock_fd = None
        try:
            for part in self.root.parts[1:]:
                try:
                    child = os.open(part, DIR_FLAGS, dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, 0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                    child = os.open(part, DIR_FLAGS, dir_fd=current)
                os.close(current)
                current = child
            _private(os.fstat(current), directory=True)
            self.fd = current
            try:
                try:
                    lock_fd = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                                      0o600, dir_fd=current)
                except FileNotFoundError:
                    # Concurrent initial creation can surface ENOENT on macOS even
                    # once the name exists. Reopen that exact anchored name only.
                    lock_fd = os.open(".lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=current)
                _private(os.fstat(lock_fd))
                until = time.monotonic() + 10
                while True:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= until:
                            raise CacheError("LEASE_BUSY")
                        time.sleep(0.02)
                marker = {"format": "portfolio-cache-v1", "scope": self.scope}
                try:
                    found = unique_json(self._read(current, "marker.json", 1024))
                    if found != marker:
                        raise CacheError("UNSAFE_PATH")
                except FileNotFoundError:
                    if not create:
                        raise
                    if set(os.listdir(current)) - {".lock"}:
                        raise CacheError("UNSAFE_PATH")
                    self._write(current, "marker.json", canonical(marker))
                yield self
                check = self.root.lstat()
                anchored = os.fstat(current)
                if self.root.is_symlink() or (check.st_dev, check.st_ino) != (anchored.st_dev, anchored.st_ino):
                    raise CacheError("UNSAFE_PATH")
            finally:
                if lock_fd is not None:
                    os.close(lock_fd)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise CacheError("UNSAFE_PATH") from exc
        finally:
            self.fd = -1
            os.close(current)

    @contextmanager
    def bucket(self, name, *, create=False):
        if name not in BUCKETS:
            raise CacheError("INVALID_INPUT")
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=self.fd)
            except FileExistsError:
                pass
        fd = os.open(name, DIR_FLAGS, dir_fd=self.fd)
        try:
            _private(os.fstat(fd), directory=True)
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def filename(key):
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise CacheError("INVALID_INPUT")
        return key + ".json"

    @staticmethod
    def _read(fd, name, limit):
        handle = os.open(name, FILE_FLAGS, dir_fd=fd)
        with os.fdopen(handle, "rb") as stream:
            _private(os.fstat(stream.fileno()))
            if os.fstat(stream.fileno()).st_size > limit:
                raise CacheError("TOO_LARGE")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise CacheError("TOO_LARGE")
            return data

    @staticmethod
    def _write(fd, name, data):
        temporary = ".pending-" + secrets.token_hex(16)
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=fd)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass

    def read(self, bucket, key, *, limit=2 * 1024 * 1024):
        with self.bucket(bucket) as fd:
            return unique_json(self._read(fd, self.filename(key), limit))

    def write(self, bucket, key, value):
        with self.bucket(bucket, create=True) as fd:
            self._write(fd, self.filename(key), canonical(value))

    def remove(self, bucket, key):
        with self.bucket(bucket) as fd:
            name = self.filename(key)
            metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
            _private(metadata)
            os.unlink(name, dir_fd=fd)

    def keys(self, bucket):
        try:
            with self.bucket(bucket) as fd:
                return sorted(name[:-5] for name in os.listdir(fd)
                              if re.fullmatch(r"[a-f0-9]{64}\.json", name))
        except FileNotFoundError:
            return []

    def total_bytes(self):
        total = 0
        for bucket in BUCKETS:
            try:
                with self.bucket(bucket) as fd:
                    for name in os.listdir(fd):
                        metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        _private(metadata)
                        total += metadata.st_size
            except FileNotFoundError:
                continue
        for name in ("marker.json", ".lock"):
            total += os.stat(name, dir_fd=self.fd, follow_symlinks=False).st_size
        return total
