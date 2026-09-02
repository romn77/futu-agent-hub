"""Bounded, strict access to package-owned Futu option static metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Callable, Iterable, Mapping

from .models import _decimal_text, decimal_fits_report

try:
    import resource
except ImportError:  # pragma: no cover - non-POSIX fallback is still post-validated.
    resource = None


MAX_METADATA_CONTRACTS = 30
MAX_METADATA_RESPONSE_BYTES = 256 * 1024
METADATA_TIMEOUT_SECONDS = 5.0
METADATA_FIELDS = frozenset(
    {"code", "lot_size", "stock_owner", "option_type", "strike_time", "strike_price"}
)
_IDENTITY = re.compile(r"^(US|HK)\.[A-Z0-9][A-Z0-9.-]{0,63}$")


def _limit_metadata_output() -> None:
    if resource is not None:
        maximum = MAX_METADATA_RESPONSE_BYTES + 1
        resource.setrlimit(resource.RLIMIT_FSIZE, (maximum, maximum))


def _bounded_subprocess_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    """Spool child output outside memory and enforce one combined evidence budget."""

    supplied_input = kwargs.pop("input", None)
    kwargs.pop("stdout", None)
    kwargs.pop("stderr", None)
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        completed = subprocess.run(
            command,
            input=supplied_input,
            stdout=stdout_file,
            stderr=stderr_file,
            preexec_fn=_limit_metadata_output if resource is not None else None,
            **kwargs,
        )
        stdout_file.seek(0, 2)
        stderr_file.seek(0, 2)
        stdout_size = stdout_file.tell()
        stderr_size = stderr_file.tell()
        if stdout_size + stderr_size > MAX_METADATA_RESPONSE_BYTES:
            return subprocess.CompletedProcess(
                completed.args,
                completed.returncode,
                b"x" * (MAX_METADATA_RESPONSE_BYTES + 1),
                b"",
            )
        stdout_file.seek(0)
        stderr_file.seek(0)
        return subprocess.CompletedProcess(
            completed.args,
            completed.returncode,
            stdout_file.read(stdout_size),
            stderr_file.read(stderr_size),
        )


class OptionMetadataContractError(ValueError):
    reason_code = "OPTION_METADATA_RESPONSE_INVALID"


class OptionMetadataProviderError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def canonical_option_identities(identities: Iterable[object]) -> list[str]:
    values = list(identities)
    if (
        not values
        or len(values) > MAX_METADATA_CONTRACTS
        or any(not isinstance(item, str) or not _IDENTITY.fullmatch(item) for item in values)
        or len(set(values)) != len(values)
    ):
        raise ValueError("OPTION_METADATA_REQUEST_INVALID")
    return sorted(values)


def _decimal(value: object, *, positive: bool = False, integral: bool = False) -> Decimal:
    if value is None or isinstance(value, bool):
        raise OptionMetadataContractError("invalid decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise OptionMetadataContractError("invalid decimal") from None
    if (
        not result.is_finite()
        or not decimal_fits_report(result)
        or (positive and result <= 0)
        or (integral and result != result.to_integral_value())
        or abs(result) > Decimal("1000000000")
    ):
        raise OptionMetadataContractError("invalid decimal")
    return result


@dataclass(frozen=True)
class OptionMetadata:
    identity: str
    contract_multiplier: Decimal
    underlying_identity: str
    option_type: str
    expiry: date
    strike: Decimal

    def to_source_dict(self) -> dict[str, object]:
        return {
            "code": self.identity,
            "lot_size": int(self.contract_multiplier),
            "stock_owner": self.underlying_identity,
            "option_type": self.option_type,
            "strike_time": self.expiry.isoformat(),
            "strike_price": _decimal_text(self.strike),
        }


def parse_option_metadata_response(
    identities: Iterable[object], payload: object
) -> Mapping[str, OptionMetadata]:
    requested = canonical_option_identities(identities)
    if not isinstance(payload, dict) or set(payload) != {"data"}:
        raise OptionMetadataContractError("response must contain only data")
    rows = payload["data"]
    if not isinstance(rows, list) or len(rows) != len(requested):
        raise OptionMetadataContractError("response identity set differs")
    parsed: dict[str, OptionMetadata] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != METADATA_FIELDS:
            raise OptionMetadataContractError("response row fields differ")
        identity = row["code"]
        owner = row["stock_owner"]
        if (
            not isinstance(identity, str)
            or identity in parsed
            or not isinstance(owner, str)
            or not _IDENTITY.fullmatch(owner)
            or identity.split(".", 1)[0] != owner.split(".", 1)[0]
        ):
            raise OptionMetadataContractError("response identity invalid")
        option_type = row["option_type"]
        if option_type not in {"CALL", "PUT"}:
            raise OptionMetadataContractError("option type invalid")
        try:
            expiry = date.fromisoformat(row["strike_time"])
        except (TypeError, ValueError):
            raise OptionMetadataContractError("expiry invalid") from None
        multiplier = _decimal(row["lot_size"], positive=True, integral=True)
        strike = _decimal(row["strike_price"])
        if strike < 0:
            raise OptionMetadataContractError("strike invalid")
        parsed[identity] = OptionMetadata(
            identity, multiplier, owner, option_type, expiry, strike
        )
    if set(parsed) != set(requested):
        raise OptionMetadataContractError("response identity set differs")
    return {identity: parsed[identity] for identity in requested}


class FutuOptionMetadataProvider:
    """Run the package-owned quote-only worker with a constrained process contract."""

    def __init__(
        self,
        *,
        runner: Callable[..., object] | None = None,
        worker_path: Path | None = None,
    ) -> None:
        self._runner = runner or _bounded_subprocess_run
        self._worker_path = worker_path or Path(__file__).with_name("option_metadata_worker.py")

    def metadata(self, identities: Iterable[object], *, timeout: float) -> object:
        try:
            requested = canonical_option_identities(identities)
        except (TypeError, ValueError):
            raise OptionMetadataProviderError("OPTION_METADATA_REQUEST_INVALID") from None
        if timeout <= 0 or timeout > METADATA_TIMEOUT_SECONDS:
            raise OptionMetadataProviderError("OPTION_METADATA_REQUEST_INVALID")
        encoded = json.dumps(requested, ensure_ascii=False, separators=(",", ":")).encode()
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
        }
        try:
            completed = self._runner(
                [sys.executable, "-s", str(self._worker_path)],
                input=encoded,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                shell=False,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise OptionMetadataProviderError("OPTION_METADATA_TIMEOUT") from None
        except Exception:
            raise OptionMetadataProviderError("OPTION_METADATA_SOURCE_UNAVAILABLE") from None
        stdout = getattr(completed, "stdout", b"")
        stderr = getattr(completed, "stderr", b"")
        returncode = getattr(completed, "returncode", 1)
        if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
            raise OptionMetadataProviderError("OPTION_METADATA_RESPONSE_INVALID")
        if len(stdout) > MAX_METADATA_RESPONSE_BYTES or len(stderr) > MAX_METADATA_RESPONSE_BYTES:
            raise OptionMetadataProviderError("OPTION_METADATA_RESPONSE_TOO_LARGE")
        if returncode != 0:
            raise OptionMetadataProviderError("OPTION_METADATA_SOURCE_UNAVAILABLE")
        try:
            raw = json.loads(stdout)
            parsed = parse_option_metadata_response(requested, raw)
        except (json.JSONDecodeError, UnicodeDecodeError, OptionMetadataContractError):
            raise OptionMetadataProviderError("OPTION_METADATA_RESPONSE_INVALID") from None
        return {"data": [parsed[identity].to_source_dict() for identity in requested]}
