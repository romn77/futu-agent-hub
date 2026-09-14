"""Standalone, read-only SDK and OpenD diagnostic stages."""

from __future__ import annotations

import importlib
import json
import socket
import sys
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version


def perform_opend_handshake(sdk, host: str, port: int) -> tuple[bool, str]:
    """Perform one non-trading quote handshake inside the bounded worker."""

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


def collect_sdk_package() -> dict[str, object]:
    """Read distribution metadata without importing the SDK or opening a connection."""

    try:
        sdk_version = version("futu-api")
    except PackageNotFoundError:
        return {"installed": False, "version": None, "status": "not_installed"}
    except Exception as exc:
        return {
            "installed": None,
            "version": None,
            "status": "metadata_failed",
            "detail": f"futu-api package metadata unavailable: {type(exc).__name__}",
        }
    return {"installed": True, "version": sdk_version, "status": "installed"}


def collect_sdk_import() -> tuple[dict[str, object], object | None]:
    """Check import and required quote APIs without constructing any SDK context."""

    result: dict[str, object] = {
        "available": False,
        "imported": False,
        "quote_api_available": None,
        "version": None,
    }
    try:
        sdk = importlib.import_module("futu")
    except Exception as exc:
        status = "import_failed"
        detail = f"futu SDK import failed: {type(exc).__name__}"
        if isinstance(exc, ModuleNotFoundError):
            status = "missing_sdk" if exc.name == "futu" else "missing_dependency"
            detail += (
                "; futu module missing" if status == "missing_sdk" else "; SDK dependency missing"
            )
        elif isinstance(exc, PermissionError):
            status = "permission_denied"
            detail += "; check SDK log-directory write permissions for this interpreter"
        result.update(status=status, detail=detail)
        return result, None
    sdk_version = getattr(sdk, "__version__", None)
    result.update(imported=True, version=str(sdk_version) if sdk_version else None)
    quote_context = getattr(sdk, "OpenQuoteContext", None)
    api_available = (
        callable(quote_context)
        and hasattr(sdk, "RET_OK")
        and callable(getattr(quote_context, "get_global_state", None))
        and callable(getattr(quote_context, "close", None))
    )
    result.update(
        available=api_available,
        quote_api_available=api_available,
        status="ready" if api_available else "missing_quote_api",
        detail=(
            "futu SDK imported and required quote APIs are present"
            if api_available
            else "futu SDK imported but required quote APIs are missing"
        ),
    )
    return result, sdk


def collect_environment(host: str, port: int) -> dict[str, dict[str, object]]:
    """Compatibility helper; production diagnostics bound each stage separately."""

    package = collect_sdk_package()
    sdk_check, sdk = collect_sdk_import()
    sdk_check.update(
        installed=package["installed"],
        package_status=package["status"],
        package_version=package["version"],
        version=package["version"] or sdk_check["version"],
    )
    if not sdk_check["available"]:
        return {
            "sdk": sdk_check,
            "opend": {
                "available": False,
                "status": "not_attempted",
                "detail": "OpenD handshake not attempted because the futu SDK is unavailable",
            },
        }
    available, detail = perform_opend_handshake(sdk, host, port)
    return {
        "sdk": sdk_check,
        "opend": {
            "available": available,
            "status": "ready" if available else "handshake_failed",
            "detail": detail,
        },
    }


def collect_opend(host: str, port: int) -> dict[str, object]:
    """Check TCP separately, then flush reachability before the quote handshake."""

    result: dict[str, object] = {"available": False, "reachable": False}
    try:
        # The parent bounds this entire stage, including DNS and SDK startup.
        with socket.create_connection((host, port), timeout=2.0):
            pass
    except Exception as exc:
        status = "connection_failed"
        if isinstance(exc, ConnectionRefusedError):
            status = "connection_refused"
        elif isinstance(exc, TimeoutError):
            status = "connection_timeout"
        elif isinstance(exc, PermissionError):
            status = "connection_permission_denied"
        result.update(status=status, detail=f"OpenD TCP connection failed: {type(exc).__name__}")
        return result
    result.update(
        reachable=True,
        status="sdk_import_pending",
        detail="OpenD TCP connection succeeded; preparing the quote handshake",
    )
    print(json.dumps({"opend": result}), flush=True)
    sdk_check, sdk = collect_sdk_import()
    if not sdk_check["available"]:
        result.update(status="sdk_import_failed", detail=sdk_check["detail"])
        return result
    result.update(status="handshake_pending", detail="OpenD quote handshake in progress")
    print(json.dumps({"opend": result}), flush=True)
    available, detail = perform_opend_handshake(sdk, host, port)
    result.update(
        available=available, status="ready" if available else "handshake_failed", detail=detail
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    if arguments == ["--sdk-package"]:
        payload = {"sdk_package": collect_sdk_package()}
    elif arguments == ["--sdk-import"]:
        sdk_check, _sdk = collect_sdk_import()
        payload = {"sdk": sdk_check}
    else:
        opend_only = len(arguments) == 3 and arguments[0] == "--opend"
        if opend_only:
            arguments = arguments[1:]
        if len(arguments) != 2:
            print(json.dumps({"detail": "invalid diagnostic worker arguments"}))
            return 2
        host = arguments[0]
        try:
            port = int(arguments[1])
        except ValueError:
            print(json.dumps({"detail": "invalid OpenD port"}))
            return 2
        payload = (
            {"opend": collect_opend(host, port)} if opend_only else collect_environment(host, port)
        )
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
