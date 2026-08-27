"""Standalone worker for SDK import and a quote-only OpenD handshake."""

from __future__ import annotations

import importlib
import json
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


def collect_environment(host: str, port: int) -> dict[str, dict[str, object]]:
    """Import the SDK, then handshake with OpenD only when import validation succeeds."""

    try:
        sdk = importlib.import_module("futu")
    except Exception as exc:
        return {
            "sdk": {
                "available": False,
                "version": None,
                "detail": f"futu SDK import failed: {type(exc).__name__}",
            },
            "opend": {
                "available": False,
                "detail": "OpenD handshake not attempted because the futu SDK is unavailable",
            },
        }
    if not callable(getattr(sdk, "OpenQuoteContext", None)) or not hasattr(sdk, "RET_OK"):
        return {
            "sdk": {
                "available": False,
                "version": None,
                "detail": "futu SDK imported but required quote APIs are missing",
            },
            "opend": {
                "available": False,
                "detail": "OpenD handshake not attempted because required SDK APIs are missing",
            },
        }
    sdk_version = getattr(sdk, "__version__", None)
    if not sdk_version:
        try:
            sdk_version = version("futu-api")
        except PackageNotFoundError:
            sdk_version = "unknown"
    opend_available, opend_detail = perform_opend_handshake(sdk, host, port)
    return {
        "sdk": {
            "available": True,
            "version": str(sdk_version),
            "detail": "futu SDK imported and required quote APIs are present",
        },
        "opend": {"available": opend_available, "detail": opend_detail},
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    if len(arguments) != 2:
        print(json.dumps({"detail": "invalid diagnostic worker arguments"}))
        return 2
    host = arguments[0]
    try:
        port = int(arguments[1])
    except ValueError:
        print(json.dumps({"detail": "invalid OpenD port"}))
        return 2
    print(json.dumps(collect_environment(host, port), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
