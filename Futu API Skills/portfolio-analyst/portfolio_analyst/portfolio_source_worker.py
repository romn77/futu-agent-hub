"""Preserve missing numeric facts while invoking the unchanged, pinned portfolio reader.

The parent supplies its verified temporary source copy, allowlisted arguments, controlled
environment, timeout, and output budget. This worker adds no SDK calls or query fallbacks.
Only JSON field extraction changes; the official context/query/close path remains intact.
"""

from __future__ import annotations

import argparse
import json
import math
import runpy
from types import SimpleNamespace
from typing import Callable


_SOURCE_NUMBERS = frozenset(
    {
        "total_assets",
        "cash",
        "qty",
        "market_val",
        "can_sell_qty",
        "average_cost",
        "nominal_price",
        "unrealized_pl",
        "realized_pl",
    }
)


class _SourceNumber:
    def __init__(self, value: object):
        self.value = value


def preserve_missing_numbers(reader: Callable) -> None:
    """Preserve numeric absence and source position currency in this reader only."""
    namespace = reader.__globals__
    original_get, original_float = namespace["safe_get"], namespace["safe_float"]
    original_dumps = namespace["json"].dumps
    position_currencies = []

    def source_get(row, *keys, default=""):
        if keys == ("code",):
            currency = original_get(row, "currency", default=None)
            if currency is not None:
                currency = namespace["format_enum"](currency)
            position_currencies.append((original_get(row, "code", default=""), currency))
        if len(keys) == 1 and keys[0] in _SOURCE_NUMBERS:
            return _SourceNumber(original_get(row, *keys, default=None))
        return original_get(row, *keys, default=default)

    def source_float(value, default=0.0):
        if not isinstance(value, _SourceNumber):
            return original_float(value, default=default)
        raw = value.value
        scalar = getattr(raw, "item", None)
        if callable(scalar):
            raw = scalar()
        if raw is None or isinstance(raw, bool):
            return None
        try:
            number = float(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    namespace["safe_get"] = source_get
    namespace["safe_float"] = source_float

    def source_dumps(payload, *args, **kwargs):
        if isinstance(payload, dict) and set(payload) == {"funds", "positions"}:
            positions = payload["positions"]
            if len(positions) != len(position_currencies):
                raise ValueError("source position currency population differs")
            for position, (code, currency) in zip(positions, position_currencies):
                if position["code"] != code:
                    raise ValueError("source position currency identity differs")
                position["currency"] = currency
        return original_dumps(payload, *args, **kwargs)

    namespace["json"] = SimpleNamespace(dumps=source_dumps)


def main() -> int:
    parser = argparse.ArgumentParser(description="Internal pinned portfolio JSON reader")
    parser.add_argument("source_script")
    parser.add_argument("--acc-id", type=int)
    parser.add_argument("--market")
    parser.add_argument("--trd-env")
    parser.add_argument("--currency")
    parser.add_argument("--security-firm")
    parser.add_argument("--ctx-type", default="SEC")
    parser.add_argument("--show-option-strategy-view", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        if not args.json:
            raise ValueError("JSON mode is required")
        namespace = runpy.run_path(args.source_script, run_name="pinned_portfolio_source")
        reader = namespace["get_portfolio"]
        preserve_missing_numbers(reader)
        reader(
            acc_id=args.acc_id,
            market=args.market,
            trd_env=args.trd_env,
            currency=args.currency,
            security_firm=args.security_firm,
            ctx_type=args.ctx_type,
            show_option_strategy_view=args.show_option_strategy_view,
            output_json=True,
        )
        return 0
    except Exception:
        print(json.dumps({"error": "PORTFOLIO_SOURCE_UNAVAILABLE"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
