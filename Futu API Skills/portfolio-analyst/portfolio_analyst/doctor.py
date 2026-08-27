"""Dependency diagnostics for the read-only portfolio analyst."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Callable, Protocol

from .settings import discover_futuapi_skill
from .skill_trust import validate_futuapi_skill


class FutuSdk(Protocol):
    RET_OK: int

    def OpenQuoteContext(self, *, host: str, port: int): ...


SdkProbe = Callable[[], tuple[FutuSdk | None, str | None, str]]
OpenDProbe = Callable[[FutuSdk, str, int], tuple[bool, str]]
OPEND_PROBE_TIMEOUT_SECONDS = 3.0


def probe_sdk() -> tuple[FutuSdk | None, str | None, str]:
    """Import the SDK and verify the API needed for the read-only handshake."""

    try:
        sdk = importlib.import_module("futu")
    except Exception as exc:
        return None, None, f"futu SDK import failed: {type(exc).__name__}"
    if not callable(getattr(sdk, "OpenQuoteContext", None)) or not hasattr(sdk, "RET_OK"):
        return None, None, "futu SDK import succeeded but required quote APIs are missing"
    sdk_version = getattr(sdk, "__version__", None)
    if not sdk_version:
        try:
            sdk_version = version("futu-api")
        except PackageNotFoundError:
            sdk_version = "unknown"
    return sdk, str(sdk_version), "futu SDK imported and required quote APIs are present"


def perform_opend_handshake(sdk: FutuSdk, host: str, port: int) -> tuple[bool, str]:
    """Perform one non-trading quote handshake; the caller must enforce a deadline."""

    available = False
    detail = "OpenD handshake failed: unexpected API response"
    context = None
    cleanup_error = None
    try:
        context = sdk.OpenQuoteContext(host=host, port=port)
        response = context.get_global_state()
        if isinstance(response, tuple) and len(response) == 2:
            ret, _data = response
            if ret == sdk.RET_OK:
                available = True
                detail = "OpenD quote API handshake succeeded"
            else:
                detail = "OpenD handshake failed: quote API rejected the request"
    except Exception as exc:
        detail = f"OpenD handshake failed: {type(exc).__name__}"
    finally:
        if context is not None:
            try:
                context.close()
            except Exception as exc:
                cleanup_error = type(exc).__name__
    if cleanup_error is not None:
        return False, f"OpenD handshake cleanup failed: {cleanup_error}"
    return available, detail


def probe_opend(_sdk: FutuSdk, host: str, port: int) -> tuple[bool, str]:
    """Run the OpenD-specific handshake in a process with a hard deadline."""

    command = [sys.executable, "-m", "portfolio_analyst.opend_probe_worker", host, str(port)]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            timeout=OPEND_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, f"OpenD handshake deadline exceeded ({OPEND_PROBE_TIMEOUT_SECONDS:.1f}s)"
    if completed.returncode != 0:
        return False, "OpenD handshake worker failed"
    for line in reversed(completed.stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("available"), bool):
            return payload["available"], str(payload.get("detail", "OpenD handshake failed"))
    return False, "OpenD handshake worker returned no valid result"


def run_doctor(
    *,
    skill_dir: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 11111,
    sdk_probe: SdkProbe = probe_sdk,
    opend_probe: OpenDProbe = probe_opend,
) -> dict[str, object]:
    """Return a structured, non-sensitive availability report."""

    if skill_dir is not None:
        skill_validation = validate_futuapi_skill(skill_dir)
        discovered = skill_validation.root
        skill_detail = skill_validation.detail
    else:
        discovered = discover_futuapi_skill()
        if discovered is None:
            skill_detail = "verified futuapi Skill not found"
        else:
            skill_detail = validate_futuapi_skill(discovered).detail
    skill_available = discovered is not None
    sdk, sdk_version, sdk_detail = sdk_probe()
    sdk_available = sdk is not None
    if sdk is None:
        opend_available = False
        opend_detail = "OpenD handshake not attempted because the futu SDK is unavailable"
    else:
        opend_available, opend_detail = opend_probe(sdk, host, port)
    checks = {
        "futuapi_skill": {
            "available": skill_available,
            "path": str(discovered) if skill_available else None,
            "detail": skill_detail,
        },
        "sdk": {
            "available": sdk_available,
            "version": sdk_version,
            "detail": sdk_detail,
        },
        "opend": {
            "available": opend_available,
            "endpoint": f"{host}:{port}",
            "detail": opend_detail,
        },
    }
    return {"ok": all(check["available"] for check in checks.values()), "checks": checks}
