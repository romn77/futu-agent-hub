"""Bounded-process worker for a quote-only OpenD handshake."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence

from .doctor import perform_opend_handshake, probe_sdk


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    if len(arguments) != 2:
        print(json.dumps({"available": False, "detail": "invalid handshake worker arguments"}))
        return 2
    host = arguments[0]
    try:
        port = int(arguments[1])
    except ValueError:
        print(json.dumps({"available": False, "detail": "invalid OpenD port"}))
        return 2

    sdk, _version, sdk_detail = probe_sdk()
    if sdk is None:
        available, detail = False, sdk_detail
    else:
        available, detail = perform_opend_handshake(sdk, host, port)
    print(json.dumps({"available": available, "detail": detail}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
