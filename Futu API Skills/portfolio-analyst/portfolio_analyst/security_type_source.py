"""One bounded quote-only static-type lookup; currency comes from position_list_query.

The worker exports only code and stock_type, never account data. No type or currency is
inferred from a code/name. Exact-code USER_CONFIG remains a fallback when OpenD is unavailable.
"""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import subprocess
import sys


MAX_CODES = 200
MAX_BYTES = 256 * 1024


def _identities(values):
    if (
        not isinstance(values, (list, tuple))
        or not 1 <= len(values) <= MAX_CODES
        or any(
            not isinstance(value, str)
            or not re.fullmatch(r"(?:US|HK|SH|SZ|SG|JP|MY)\.[A-Z0-9][A-Z0-9.-]{0,63}", value)
            for value in values
        )
        or len(set(values)) != len(values)
    ):
        raise ValueError("SECURITY_METADATA_REQUEST_INVALID")
    return sorted(values)


def _parse(identities, payload):
    requested = _identities(identities)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"data"}
        or not isinstance(payload["data"], list)
    ):
        raise ValueError("SECURITY_METADATA_RESPONSE_INVALID")
    if len(payload["data"]) > len(requested):
        raise ValueError("SECURITY_METADATA_RESPONSE_INVALID")
    seen = set()
    for row in payload["data"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"code", "stock_type"}
            or not isinstance(row["code"], str)
            or row["code"] not in requested
            or row["code"] in seen
            or not isinstance(row["stock_type"], str)
            or not re.fullmatch(r"[A-Z_]{1,32}", row["stock_type"])
        ):
            raise ValueError("SECURITY_METADATA_RESPONSE_INVALID")
        seen.add(row["code"])
    return payload


def collect_security_types(identities, *, host, port, context_factory=None):
    requested = _identities(identities)
    market = "US"
    if context_factory is None:
        from futu import Market, OpenQuoteContext

        market, context_factory = Market.US, OpenQuoteContext
    context = context_factory(host=host, port=port)
    try:
        # Explicit code_list takes precedence over the market/type filters in OpenD.
        status, table = context.get_stock_basicinfo(market, code_list=requested)
        if status != 0:
            raise ValueError("SECURITY_METADATA_SOURCE_UNAVAILABLE")
        rows = table.to_dict("records")
        return _parse(
            requested,
            {
                "data": [
                    {"code": row.get("code"), "stock_type": row.get("stock_type")} for row in rows
                ]
            },
        )
    finally:
        context.close()


class FutuSecurityTypeProvider:
    def __init__(self, *, runner=None):
        from .option_metadata import _bounded_subprocess_run

        self.runner = runner or _bounded_subprocess_run

    def metadata(self, identities, *, timeout):
        from .option_metadata import validate_opend_endpoint

        requested = _identities(identities)
        try:
            host, port = validate_opend_endpoint(
                os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
                int(os.environ.get("FUTU_OPEND_PORT", "11111")),
            )
            if isinstance(timeout, bool) or not 0 < timeout <= 5:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("SECURITY_METADATA_REQUEST_INVALID") from None
        try:
            result = self.runner(
                [sys.executable, "-s", str(Path(__file__).resolve())],
                input=json.dumps({"host": host, "port": port, "identities": requested}).encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
                shell=False,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "PYTHONNOUSERSITE": "1",
                    "PYTHONIOENCODING": "utf-8",
                },
            )
        except subprocess.TimeoutExpired:
            raise ValueError("SECURITY_METADATA_TIMEOUT") from None
        except Exception:
            raise ValueError("SECURITY_METADATA_SOURCE_UNAVAILABLE") from None
        if (
            not isinstance(result.stdout, bytes)
            or not isinstance(result.stderr, bytes)
            or len(result.stdout) + len(result.stderr) > MAX_BYTES
            or result.returncode != 0
        ):
            raise ValueError("SECURITY_METADATA_SOURCE_UNAVAILABLE")
        try:
            return _parse(requested, json.loads(result.stdout))
        except (UnicodeError, ValueError):
            raise ValueError("SECURITY_METADATA_RESPONSE_INVALID") from None


def enrich_security_types(positions, configured, provider):
    from .normalization import is_unknown_sentinel

    requested = sorted(
        {
            row.code
            for row in positions
            if row.quantity != 0
            and is_unknown_sentinel(row.security_type)
            and (
                configured.resolve(row.code) is None
                or is_unknown_sentinel(configured.resolve(row.code).security_type)
            )
        }
    )
    if not requested or provider is None:
        return positions, ()
    selected = requested[:MAX_CODES]
    try:
        response = _parse(selected, provider.metadata(selected, timeout=5))
    except Exception:
        return positions, (
            "security_type OpenD static metadata unavailable; retaining unknown types",
        )
    types = {row["code"]: row["stock_type"] for row in response["data"]}
    warnings = (
        ()
        if len(types) == len(requested)
        else (
            "security_type OpenD static metadata coverage is partial; unmatched types remain unknown",
        )
    )
    return tuple(
        replace(row, security_type=types.get(row.code, row.security_type)) for row in positions
    ), warnings


def main():
    try:
        raw = sys.stdin.buffer.read(32 * 1024 + 1)
        if len(raw) > 32 * 1024:
            return 2
        request = json.loads(raw)
        if set(request) != {"host", "port", "identities"}:
            return 2
        body = collect_security_types(**request)
        encoded = json.dumps(body, allow_nan=False).encode()
        if len(encoded) > MAX_BYTES:
            return 2
        sys.stdout.buffer.write(encoded)
        return 0
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
