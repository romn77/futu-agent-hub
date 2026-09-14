"""Bounded dependency diagnostics for the read-only portfolio analyst."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from .child_environment import controlled_child_environment, isolated_python_command
from .settings import discover_futuapi_skill_source
from .skill_trust import validate_futuapi_skill


EnvironmentResult = dict[str, dict[str, object]]
EnvironmentProbe = Callable[[str, int], EnvironmentResult]
DIAGNOSTIC_TIMEOUT_SECONDS = 4.0
OPEND_PROBE_WORKER = Path(__file__).with_name("opend_probe_worker.py").resolve()


def _stage_payload(output: str | bytes | None, name: str) -> dict[str, object] | None:
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    for line in reversed((output or "").splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get(name), dict):
            continue
        check = payload[name]
        if name == "sdk_package":
            if (
                "installed" in check
                and (check["installed"] is None or isinstance(check["installed"], bool))
                and "version" in check
                and (check["version"] is None or isinstance(check["version"], str))
                and isinstance(check.get("status"), str)
            ):
                return check
        elif isinstance(check.get("available"), bool):
            return check
    return None


def _probe_stage(
    name: str, arguments: list[str], *, execute, timeout: float
) -> tuple[dict[str, object] | None, str | None]:
    command = isolated_python_command(OPEND_PROBE_WORKER, arguments)
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
    except subprocess.TimeoutExpired as exc:
        return _stage_payload(exc.stdout, name), "timeout"
    except OSError:
        return None, "worker_failed"
    payload = _stage_payload(completed.stdout, name)
    if completed.returncode != 0:
        return payload, "worker_failed"
    return payload, None if payload is not None else "invalid_response"


def probe_environment(
    host: str,
    port: int,
    *,
    runner=None,
    timeout: float = DIAGNOSTIC_TIMEOUT_SECONDS,
) -> EnvironmentResult:
    """Bound metadata, SDK import, and OpenD stages independently (at most 3 × timeout)."""

    execute = runner or subprocess.run
    package, package_error = _probe_stage(
        "sdk_package", ["--sdk-package"], execute=execute, timeout=timeout
    )
    package = package or {"installed": None, "version": None, "status": package_error}
    sdk, sdk_error = _probe_stage("sdk", ["--sdk-import"], execute=execute, timeout=timeout)
    if sdk is None:
        detail = (
            f"diagnostic deadline exceeded ({timeout:.1f}s); SDK import unverified"
            if sdk_error == "timeout"
            else f"SDK import unverified: {sdk_error}"
        )
        sdk = {
            "available": False,
            "imported": None,
            "quote_api_available": None,
            "version": None,
            "status": sdk_error,
            "detail": detail,
        }
    sdk.update(
        installed=package["installed"],
        package_status=package["status"],
        package_version=package["version"],
        version=package["version"] or sdk.get("version"),
    )
    if not sdk["available"]:
        return {
            "sdk": sdk,
            "opend": {
                "available": False,
                "reachable": None,
                "status": "not_attempted",
                "detail": "OpenD unverified; handshake not attempted because the SDK is unavailable",
            },
        }
    opend, opend_error = _probe_stage(
        "opend", ["--opend", host, str(port)], execute=execute, timeout=timeout
    )
    if opend_error:
        reachable = opend.get("reachable") if opend else None
        if opend_error == "timeout":
            stage = (
                "SDK preparation"
                if opend and opend.get("status") == "sdk_import_pending"
                else "quote handshake"
                if reachable
                else "connection"
            )
            status = (
                "sdk_import_timeout"
                if stage == "SDK preparation"
                else "handshake_timeout"
                if reachable
                else "connection_timeout"
            )
            detail = f"OpenD {stage} deadline exceeded ({timeout:.1f}s)"
        else:
            status, detail = opend_error, f"OpenD handshake unverified: {opend_error}"
        opend = {"available": False, "reachable": reachable, "status": status, "detail": detail}
    elif opend.get("status") in {"sdk_import_pending", "handshake_pending"}:
        opend.update(
            available=False, status="invalid_response", detail="OpenD handshake result missing"
        )
    return {"sdk": sdk, "opend": opend}


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
    return {
        "ok": all(check["available"] for check in checks.values()),
        "runtime": {
            "executable": sys.executable,
            "python_version": ".".join(map(str, sys.version_info[:3])),
            "prefix": sys.prefix,
        },
        "checks": checks,
    }
