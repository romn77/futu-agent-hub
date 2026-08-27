"""Dependency diagnostics for the read-only portfolio analyst."""

from __future__ import annotations

import importlib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Callable, Protocol

from .settings import discover_futuapi_skill


class FutuSdk(Protocol):
    RET_OK: int
    OpenQuoteContext: object


SdkProbe = Callable[[], tuple[FutuSdk | None, str | None, str]]
OpenDProbe = Callable[[FutuSdk, str, int], tuple[bool, str]]


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


def probe_opend(sdk: FutuSdk, host: str, port: int) -> tuple[bool, str]:
    """Verify OpenD with a non-trading quote API handshake."""

    context = None
    try:
        context = sdk.OpenQuoteContext(host=host, port=port)
        response = context.get_global_state()
        if not isinstance(response, tuple) or len(response) != 2:
            return False, "OpenD handshake failed: unexpected API response"
        ret, _data = response
        if ret != sdk.RET_OK:
            return False, "OpenD handshake failed: quote API rejected the request"
        return True, "OpenD quote API handshake succeeded"
    except Exception as exc:
        return False, f"OpenD handshake failed: {type(exc).__name__}"
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


def run_doctor(
    *,
    skill_dir: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 11111,
    sdk_probe: SdkProbe = probe_sdk,
    opend_probe: OpenDProbe = probe_opend,
) -> dict[str, object]:
    """Return a structured, non-sensitive availability report."""

    discovered = skill_dir.resolve() if skill_dir else discover_futuapi_skill()
    skill_available = discovered is not None and (discovered / "SKILL.md").is_file()
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
            "detail": "official futuapi Skill found"
            if skill_available
            else "futuapi Skill not found",
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
