"""Lifecycle-free current portfolio orchestration with owned temporary state."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from . import cli
from .private_home import ensure_private_home, require_secure_platform_mutation
from .settings import Settings


_ALLOWED_FILES = frozenset({"snapshot.json", "risk.json", "options.json"})
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)


class CurrentAnalysisError(RuntimeError):
    """Raised when the authoritative snapshot cannot be acquired."""


class TemporaryCleanupError(RuntimeError):
    """Raised when owned temporary state cannot be safely removed."""


@dataclass(frozen=True)
class CliResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class _Identity:
    device: int
    inode: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> "_Identity":
        return cls(metadata.st_dev, metadata.st_ino)


def _same_identity(metadata: os.stat_result, expected: _Identity) -> bool:
    return _Identity.from_stat(metadata) == expected


class OwnedTemporaryRun:
    """Own one direct private-home child and only the files created through this object."""

    def __init__(
        self,
        *,
        home: Path,
        path: Path,
        home_fd: int,
        directory_fd: int,
        home_identity: _Identity,
        directory_identity: _Identity,
    ) -> None:
        self.home = home
        self.path = path
        self._home_fd = home_fd
        self._directory_fd = directory_fd
        self._home_identity = home_identity
        self._directory_identity = directory_identity
        self._files: dict[str, _Identity] = {}
        self._closed = False

    @classmethod
    def create(cls, data_home: Path) -> "OwnedTemporaryRun":
        require_secure_platform_mutation()
        home = ensure_private_home(data_home, create=True).resolve(strict=True)
        home_fd = os.open(home, _DIRECTORY_FLAGS)
        path: Path | None = None
        directory_fd: int | None = None
        try:
            home_metadata = os.fstat(home_fd)
            path = Path(tempfile.mkdtemp(prefix=".portfolio-current.", dir=home))
            os.chmod(path, 0o700, follow_symlinks=False)
            directory_fd = os.open(path.name, _DIRECTORY_FLAGS, dir_fd=home_fd)
            directory_metadata = os.fstat(directory_fd)
            anchored_metadata = os.stat(path.name, dir_fd=home_fd, follow_symlinks=False)
            if not _same_identity(anchored_metadata, _Identity.from_stat(directory_metadata)):
                raise TemporaryCleanupError(
                    f"temporary directory identity changed during creation: {path}"
                )
            if (
                not stat.S_ISDIR(directory_metadata.st_mode)
                or stat.S_IMODE(directory_metadata.st_mode) != 0o700
            ):
                raise TemporaryCleanupError(f"temporary directory is not mode 0700: {path}")
            return cls(
                home=home,
                path=path,
                home_fd=home_fd,
                directory_fd=directory_fd,
                home_identity=_Identity.from_stat(home_metadata),
                directory_identity=_Identity.from_stat(directory_metadata),
            )
        except Exception as exc:
            if directory_fd is not None:
                os.close(directory_fd)
            os.close(home_fd)
            if path is not None:
                if isinstance(exc, TemporaryCleanupError):
                    raise
                raise TemporaryCleanupError(
                    f"temporary setup failed; cleanup refused for {path}: {exc}"
                ) from exc
            raise

    def _cleanup_error(self, detail: str) -> TemporaryCleanupError:
        return TemporaryCleanupError(f"temporary cleanup refused for {self.path}: {detail}")

    def _ensure_open(self) -> None:
        if self._closed:
            raise CurrentAnalysisError(f"temporary run is already closed: {self.path}")

    def write_json(self, name: str, payload: Any) -> Path:
        self._ensure_open()
        if name not in _ALLOWED_FILES or name in self._files:
            raise CurrentAnalysisError(f"temporary file name is not available: {self.path / name}")
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        descriptor = os.open(name, _FILE_FLAGS, 0o600, dir_fd=self._directory_fd)
        metadata = os.fstat(descriptor)
        self._files[name] = _Identity.from_stat(metadata)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            raise
        return self.path / name

    def relative_path(self, name: str) -> str:
        self._ensure_open()
        if name not in self._files:
            raise CurrentAnalysisError(f"temporary file is not owned: {self.path / name}")
        return str(Path(self.path.name) / name)

    def _validate_before_cleanup(self) -> None:
        try:
            home_path_metadata = self.home.lstat()
            home_fd_metadata = os.fstat(self._home_fd)
            path_metadata = self.path.lstat()
            anchored_metadata = os.stat(
                self.path.name, dir_fd=self._home_fd, follow_symlinks=False
            )
            directory_fd_metadata = os.fstat(self._directory_fd)
        except OSError as exc:
            raise self._cleanup_error(f"identity check failed: {exc}") from exc

        if self.home.is_symlink() or not _same_identity(
            home_path_metadata, self._home_identity
        ) or not _same_identity(home_fd_metadata, self._home_identity):
            raise self._cleanup_error("private-home device/inode identity changed")
        if self.path.is_symlink() or any(
            not _same_identity(metadata, self._directory_identity)
            for metadata in (path_metadata, anchored_metadata, directory_fd_metadata)
        ):
            raise self._cleanup_error("temporary-directory device/inode identity changed")
        if (
            not stat.S_ISDIR(directory_fd_metadata.st_mode)
            or stat.S_IMODE(directory_fd_metadata.st_mode) != 0o700
        ):
            raise self._cleanup_error("temporary directory is not a private mode-0700 directory")

        try:
            entries = set(os.listdir(self._directory_fd))
        except OSError as exc:
            raise self._cleanup_error(f"cannot enumerate owned directory: {exc}") from exc
        expected_entries = set(self._files)
        if entries != expected_entries:
            unknown = sorted(entries - expected_entries)
            missing = sorted(expected_entries - entries)
            raise self._cleanup_error(f"entry set changed; unknown={unknown}; missing={missing}")

        for name, expected_identity in self._files.items():
            try:
                metadata = os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise self._cleanup_error(f"cannot verify {name}: {exc}") from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
                or not _same_identity(metadata, expected_identity)
            ):
                raise self._cleanup_error(f"owned file identity or mode changed: {name}")

    def cleanup(self) -> None:
        if self._closed:
            return
        try:
            self._validate_before_cleanup()
            for name in sorted(self._files):
                os.unlink(name, dir_fd=self._directory_fd)
            os.close(self._directory_fd)
            self._directory_fd = -1
            try:
                final_metadata = os.stat(
                    self.path.name, dir_fd=self._home_fd, follow_symlinks=False
                )
            except OSError as exc:
                raise self._cleanup_error(f"final directory identity check failed: {exc}") from exc
            if not _same_identity(final_metadata, self._directory_identity):
                raise self._cleanup_error(
                    "temporary-directory identity changed before final removal"
                )
            os.rmdir(self.path.name, dir_fd=self._home_fd)
        except TemporaryCleanupError:
            raise
        except OSError as exc:
            raise self._cleanup_error(f"owned unlink/rmdir failed: {exc}") from exc
        finally:
            if self._directory_fd >= 0:
                os.close(self._directory_fd)
            os.close(self._home_fd)
            self._closed = True


def _invoke_cli(arguments: list[str]) -> CliResult:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        returncode = cli.main(arguments)
    return CliResult(returncode, stdout.getvalue(), stderr.getvalue())


def _parse_object(result: CliResult, component: str) -> dict[str, Any]:
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CurrentAnalysisError(f"{component} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise CurrentAnalysisError(f"{component} returned a non-object JSON body")
    return payload


def _error_evidence(result: CliResult, component: str) -> dict[str, str]:
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"code": "COMMAND_FAILED", "message": f"{component} command failed"}
    if isinstance(payload, Mapping) and isinstance(payload.get("error"), Mapping):
        error = payload["error"]
        code = error.get("code")
        message = error.get("message")
        if isinstance(code, str) and isinstance(message, str):
            return {"code": code, "message": message}
        if isinstance(code, str):
            return {"code": code, "message": f"{component} command failed"}
    return {"code": "COMMAND_FAILED", "message": f"{component} command failed"}


def _component_result(
    result: CliResult, component: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if result.returncode != 0:
        return None, {
            "component": component,
            "status": "UNAVAILABLE",
            "error": _error_evidence(result, component),
        }
    try:
        return _parse_object(result, component), None
    except CurrentAnalysisError as exc:
        return None, {
            "component": component,
            "status": "UNAVAILABLE",
            "error": {
                "code": "INVALID_COMPONENT_OUTPUT",
                "message": str(exc),
            },
        }


def _has_held_options(snapshot: Mapping[str, Any]) -> bool:
    positions = snapshot.get("positions")
    if not isinstance(positions, list):
        raise CurrentAnalysisError("snapshot positions are unavailable")
    try:
        return any(
            isinstance(position, Mapping)
            and position.get("security_type") == "OPTION"
            and Decimal(str(position.get("quantity"))) != 0
            for position in positions
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CurrentAnalysisError("snapshot option quantity is invalid") from exc


def _validate_snapshot_scope(snapshot: Mapping[str, Any]) -> None:
    if snapshot.get("trd_env") != "REAL" or snapshot.get("base_currency") != "USD":
        raise CurrentAnalysisError("snapshot is not the required REAL/USD scope")
    for field in ("snapshot_id", "as_of"):
        if not isinstance(snapshot.get(field), str) or not snapshot[field]:
            raise CurrentAnalysisError(f"snapshot {field} is unavailable")
    if not isinstance(snapshot.get("positions"), list):
        raise CurrentAnalysisError("snapshot positions are unavailable")


def run_current_analysis(
    *,
    settings: Settings | None = None,
    invoke: Callable[[list[str]], CliResult] | None = None,
) -> dict[str, Any]:
    """Acquire one snapshot; make risk/options failures explicit local degradations."""

    resolved_settings = settings or Settings.from_env()
    command = invoke or _invoke_cli
    owned = OwnedTemporaryRun.create(resolved_settings.data_home)
    try:
        snapshot_result = command(
            [
                "snapshot",
                "--trd-env",
                "REAL",
                "--base-currency",
                "USD",
                "--json",
                "--no-save",
            ]
        )
        if snapshot_result.returncode != 0:
            error = _error_evidence(snapshot_result, "snapshot")
            raise CurrentAnalysisError(f"snapshot failed: {error['code']}")
        snapshot = _parse_object(snapshot_result, "snapshot")
        _validate_snapshot_scope(snapshot)
        owned.write_json("snapshot.json", snapshot)
        snapshot_path = owned.relative_path("snapshot.json")

        degradations: list[dict[str, Any]] = []
        risk: dict[str, Any] | None = None
        risk_result = command(
            ["risk", "--snapshot", snapshot_path, "--json", "--no-save"]
        )
        risk, risk_degradation = _component_result(risk_result, "risk")
        if risk is not None:
            owned.write_json("risk.json", risk)
        elif risk_degradation is not None:
            degradations.append(risk_degradation)

        options: dict[str, Any] | None = None
        if _has_held_options(snapshot):
            options_result = command(
                ["options", "--snapshot", snapshot_path, "--json", "--no-save"]
            )
            options, options_degradation = _component_result(options_result, "options")
            if options is not None:
                owned.write_json("options.json", options)
            elif options_degradation is not None:
                degradations.append(options_degradation)

        return {
            "snapshot": snapshot,
            "risk": risk,
            "options": options,
            "degradations": degradations,
        }
    finally:
        owned.cleanup()


def main() -> int:
    try:
        envelope = run_current_analysis()
    except (CurrentAnalysisError, TemporaryCleanupError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {"code": type(exc).__name__, "message": str(exc)},
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(envelope, ensure_ascii=False))
    return 0
