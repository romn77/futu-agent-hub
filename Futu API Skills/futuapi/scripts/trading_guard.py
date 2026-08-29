import json
import os
import sys
from typing import Mapping, NoReturn


TRADING_ENABLED_ENV = "FUTU_TRADING_ENABLED"


class TradingDisabledError(RuntimeError):
    pass


def require_trading_enabled(
    environment: Mapping[str, str] | None = None,
) -> None:
    values = os.environ if environment is None else environment
    value = values.get(TRADING_ENABLED_ENV)
    if value is None or value.strip().lower() != "true":
        raise TradingDisabledError(
            "交易写操作已禁用；如需启用，请显式设置 FUTU_TRADING_ENABLED=true"
        )


def exit_trading_disabled(error: TradingDisabledError, *, output_json: bool = False) -> NoReturn:
    if output_json:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
    else:
        print(f"错误: {error}", file=sys.stderr)
    raise SystemExit(3)
