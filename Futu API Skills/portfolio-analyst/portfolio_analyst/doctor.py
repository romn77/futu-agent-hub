"""Dependency diagnostics for the read-only portfolio analyst."""

from __future__ import annotations

import importlib.util
import socket
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Callable

from .settings import discover_futuapi_skill


SdkProbe = Callable[[], tuple[bool, str | None, str]]
OpenDProbe = Callable[[str, int], tuple[bool, str]]


def probe_sdk() -> tuple[bool, str | None, str]:
    """Report SDK availability without importing account-facing code."""

    if importlib.util.find_spec("futu") is None:
        return False, None, "futu module is not installed"
    try:
        sdk_version = version("futu-api")
    except PackageNotFoundError:
        sdk_version = "unknown"
    return True, sdk_version, "futu module is importable"


def probe_opend(host: str, port: int) -> tuple[bool, str]:
    """Test only whether the configured OpenD TCP endpoint accepts a connection."""

    try:
        with socket.create_connection((host, port), timeout=0.75):
            return True, "TCP connection succeeded"
    except OSError as exc:
        return False, f"TCP connection failed: {type(exc).__name__}"


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
    sdk_available, sdk_version, sdk_detail = sdk_probe()
    opend_available, opend_detail = opend_probe(host, port)
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
