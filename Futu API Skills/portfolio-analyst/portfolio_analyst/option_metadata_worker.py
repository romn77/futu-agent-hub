"""Minimal quote-only child process for Futu option static information."""

from __future__ import annotations

from contextlib import redirect_stdout
import json
import re
import sys
from typing import Callable


_FIELDS = ("code", "lot_size", "stock_owner", "option_type", "strike_time", "strike_price")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*")


def _plain_scalar(value: object) -> object:
    item = getattr(value, "item", None)
    if callable(item):
        return item()
    return value


def _canonical_identities(values: object) -> list[str]:
    if (
        not isinstance(values, list)
        or not values
        or len(values) > 30
        or any(
            not isinstance(item, str)
            or re.fullmatch(r"^(?:US|HK)\.[A-Z0-9][A-Z0-9.-]{0,63}$", item) is None
            for item in values
        )
        or len(set(values)) != len(values)
    ):
        raise ValueError("OPTION_METADATA_REQUEST_INVALID")
    return sorted(values)


def _endpoint(host: object, port: object) -> tuple[str, int]:
    if (
        not isinstance(host, str)
        or not host
        or host != host.strip()
        or len(host.encode("utf-8")) > 253
        or _HOST.fullmatch(host) is None
        or isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
    ):
        raise ValueError("OPTION_METADATA_REQUEST_INVALID")
    return host, port


def _parse_request(raw: bytes) -> tuple[str, int, list[str]]:
    try:
        values = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("OPTION_METADATA_REQUEST_INVALID") from None
    if not isinstance(values, dict) or set(values) != {"host", "identities", "port"}:
        raise ValueError("OPTION_METADATA_REQUEST_INVALID")
    host, port = _endpoint(values["host"], values["port"])
    requested = _canonical_identities(values["identities"])
    canonical = json.dumps(
        {"host": host, "identities": requested, "port": port},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if raw != canonical:
        raise ValueError("OPTION_METADATA_REQUEST_INVALID")
    return host, port, requested


def collect_option_metadata(
    identities: object,
    *,
    context_factory: Callable[..., object] | None = None,
    option_security_type: object | None = None,
    host: str,
    port: int,
) -> dict[str, object]:
    requested = _canonical_identities(identities)
    try:
        host, port = _endpoint(host, port)
    except ValueError:
        raise ValueError("OPTION_METADATA_REQUEST_INVALID") from None
    market_values: dict[str, object] = {"US": "US", "HK": "HK"}
    if context_factory is None:
        from futu import Market, OpenQuoteContext, SecurityType

        def context_factory(*, host: str, port: int) -> object:
            return OpenQuoteContext(host=host, port=port)

        market_values = {"US": Market.US, "HK": Market.HK}
        option_security_type = SecurityType.DRVT
    if option_security_type is None:
        option_security_type = "DRVT"
    context = context_factory(host=host, port=port)
    result_rows: list[dict[str, object]] = []
    try:
        for market in ("HK", "US"):
            codes = [identity for identity in requested if identity.startswith(f"{market}.")]
            if not codes:
                continue
            status, table = context.get_stock_basicinfo(
                market_values[market], option_security_type, codes
            )
            if status != 0:
                raise RuntimeError("OPTION_METADATA_SOURCE_UNAVAILABLE")
            rows = table.to_dict("records")
            if not isinstance(rows, list):
                raise RuntimeError("OPTION_METADATA_SOURCE_UNAVAILABLE")
            for row in rows:
                if not isinstance(row, dict):
                    raise RuntimeError("OPTION_METADATA_SOURCE_UNAVAILABLE")
                result_rows.append({field: _plain_scalar(row.get(field)) for field in _FIELDS})
    finally:
        context.close()
    result_rows.sort(key=lambda row: str(row["code"]))
    return {"data": result_rows}


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(16 * 1024 + 1)
        if len(raw) > 16 * 1024:
            return 2
        host, port, identities = _parse_request(raw)
        # Futu binds its console logger during import; keep SDK logs off the JSON pipe.
        with redirect_stdout(sys.stderr):
            result = collect_option_metadata(identities, host=host, port=port)
        encoded = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        if len(encoded) > 256 * 1024:
            return 2
        sys.stdout.buffer.write(encoded)
        return 0
    except Exception:
        # The parent receives only a stable nonzero status, never raw Futu errors.
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
