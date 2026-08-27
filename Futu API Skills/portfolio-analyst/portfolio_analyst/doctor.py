"""Bounded dependency diagnostics for the read-only portfolio analyst."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from .child_environment import controlled_child_environment, isolated_python_command
from .settings import discover_futuapi_skill_source
from .skill_trust import validate_futuapi_skill


EnvironmentResult = dict[str, dict[str, object]]
EnvironmentProbe = Callable[[str, int], EnvironmentResult]
DIAGNOSTIC_TIMEOUT_SECONDS = 4.0
OPEND_PROBE_WORKER = Path(__file__).with_name("opend_probe_worker.py").resolve()


def _unavailable_environment(sdk_detail: str, opend_detail: str) -> EnvironmentResult:
    return {
        "sdk": {"available": False, "version": None, "detail": sdk_detail},
        "opend": {"available": False, "detail": opend_detail},
    }


def _valid_environment_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    for name in ("sdk", "opend"):
        check = payload.get(name)
        if not isinstance(check, dict) or not isinstance(check.get("available"), bool):
            return False
    return True


def probe_environment(
    host: str,
    port: int,
    *,
    runner=None,
    timeout: float = DIAGNOSTIC_TIMEOUT_SECONDS,
) -> EnvironmentResult:
    """Probe SDK import and OpenD handshake together in one killable worker."""

    execute = runner or subprocess.run
    command = isolated_python_command(OPEND_PROBE_WORKER, [host, str(port)])
    try:
        completed = execute(
            command,
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            env=controlled_child_environment(purpose="doctor"),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _unavailable_environment(
            f"diagnostic deadline exceeded ({timeout:.1f}s); SDK import unverified",
            "OpenD unverified because the diagnostic worker exceeded its deadline",
        )
    if completed.returncode != 0:
        return _unavailable_environment(
            "SDK unverified because the diagnostic worker failed",
            "OpenD unverified because the diagnostic worker failed",
        )
    for line in reversed(completed.stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _valid_environment_payload(payload):
            return payload
    return _unavailable_environment(
        "SDK unverified because the diagnostic worker returned no valid result",
        "OpenD unverified because the diagnostic worker returned no valid result",
    )


def run_doctor(
    *,
    skill_dir: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 11111,
    environment_probe: EnvironmentProbe = probe_environment,
) -> dict[str, object]:
    """Return structured, non-sensitive Skill, SDK, and OpenD status."""

    if skill_dir is not None:
        validation = validate_futuapi_skill(skill_dir)
        discovered = validation.root
        skill_detail = (
            "audited-compatible futuapi Skill supplied directly by caller"
            if validation.valid
            else validation.detail
        )
    else:
        source = discover_futuapi_skill_source()
        discovered = source.path
        skill_detail = source.detail
    environment = environment_probe(host, port)
    sdk_check = dict(environment["sdk"])
    opend_check = dict(environment["opend"])
    opend_check["endpoint"] = f"{host}:{port}"
    checks = {
        "futuapi_skill": {
            "available": discovered is not None,
            "path": str(discovered) if discovered is not None else None,
            "detail": skill_detail,
        },
        "sdk": sdk_check,
        "opend": opend_check,
    }
    return {"ok": all(check["available"] for check in checks.values()), "checks": checks}
