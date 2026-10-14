"""SDK-independent validation at the trading write boundary."""

import json
import math
from decimal import Decimal, InvalidOperation


def fail_validation(message, *, output_json=False):
    print(json.dumps({"error": message}, ensure_ascii=False) if output_json else f"错误: {message}")
    raise SystemExit(1)


def require_real_account(acc_id, trd_env, *, output_json=False):
    if str(trd_env).upper() == "REAL" and (
        isinstance(acc_id, bool) or not isinstance(acc_id, int) or acc_id <= 0
    ):
        fail_validation(
            "实盘交易必须通过 --acc-id 或 FUTU_ACC_ID 指定有效账户；不允许自动选择账户 0",
            output_json=output_json,
        )


def finite_number(value, label, *, positive=False, integer=False):
    try:
        if isinstance(value, bool):
            raise ValueError
        exact = Decimal(str(value))
        number = float(exact)
        if not math.isfinite(number) or (positive and (exact <= 0 or number <= 0)):
            raise ValueError
        if integer and exact != exact.to_integral_value():
            raise ValueError
    except (ValueError, TypeError, OverflowError, InvalidOperation):
        constraint = "有限正整数" if integer and positive else "有限正数" if positive else "有限数值"
        raise ValueError(f"{label}必须为{constraint}") from None
    return int(exact) if integer else number


def order_number(value, label, *, positive=False, integer=False, output_json=False):
    try:
        return finite_number(value, label, positive=positive, integer=integer)
    except ValueError as error:
        fail_validation(str(error), output_json=output_json)


class TradeAPIError(RuntimeError):
    def __init__(self, payload):
        self.payload = payload
        message = f"{payload['action']}失败: {payload['error']}"
        if payload.get("hint"):
            message += "\n" + payload["hint"]
        if payload.get("authority_url"):
            message += "\n" + payload["authority_url"]
        super().__init__(message)


def emit_trade_error(error, *, output_json=False):
    payload = error.payload if isinstance(error, TradeAPIError) else {"error": str(error)}
    print(json.dumps(payload, ensure_ascii=False) if output_json else f"错误: {error}")
