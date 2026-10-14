"""Offline regressions: invalid writes never reach a trade context."""

import ast
import json
import sys
import unittest

from test_trading_entrypoints import (
    AUDIT_ENTRIES, CONTEXT_OPENED, _captured_output, _load_trade_module,
    _trading_enabled, _fake_futu_for_combo, _replace_module, SCRIPTS_DIR,
)


def common_error_checker(close):
    """Load the real diagnostics without common.py's SDK import/startup effects."""
    from trade_validation import TradeAPIError

    path = SCRIPTS_DIR / "common.py"
    functions = {
        "check_ret", "_is_no_account_error", "_is_unlock_needed_error", "_is_permission_error",
        "_detect_market_from_argv", "_get_authority_url", "_build_permission_hint",
        "_build_permission_hint_json",
    }
    constants = {"_MARKET_NAMES", "_AUTHORITY_URLS", "_NO_ACCOUNT_HINT", "_UNLOCK_NEEDED_HINT"}
    nodes = [node for node in ast.parse(path.read_text()).body if (
        isinstance(node, ast.FunctionDef) and node.name in functions
    ) or (
        isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in constants for target in node.targets)
    )]
    namespace = {"TradeAPIError": TradeAPIError, "sys": sys, "json": json, "RET_OK": 0, "safe_close": close}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["check_ret"]


class Context:
    def __init__(self, result=0, message="offline result"):
        self.result = result
        self.message = message
        self.writes = []

    def get_acc_list(self):
        return 0, [{"acc_id": 1, "acc_role": "NORMAL", "trdmarket_auth": ["US", "PREDICTION"]}]

    def place_order(self, **kwargs):
        self.writes.append(kwargs)
        return self.result, self.message

    place_combo_order = place_order

    def modify_order(self, *args, **kwargs):
        return self.place_order(**kwargs)

    def cancel_all_order(self, **kwargs):
        return self.place_order(**kwargs)


CASES = {
    "place_order": (("US.AAPL", "BUY"), {"quantity": 1, "price": 10, "trd_env": "REAL", "confirmed": True}),
    "place_combo_order": (('[{"code":"US.AAPL","trd_side":"BUY","qty_ratio":1}]',), {"quantity": 1, "price": -1, "trd_env": "REAL", "confirmed": True}),
    "place_crypto_order": (("CC.BTCUSD", "BUY"), {"quantity": "0.1", "price": 10, "confirmed": True}),
    "modify_order": (("order-1",), {"quantity": 1, "price": 10, "trd_env": "REAL"}),
    "cancel_order": (("order-1",), {"trd_env": "REAL"}),
    "cancel_crypto_order": ((), {"order_id": "order-1"}),
}


class TradeValidationTests(unittest.TestCase):
    def setUp(self):
        AUDIT_ENTRIES.clear()
        CONTEXT_OPENED.clear()

    def test_real_writes_reject_implicit_zero_account_before_preview_or_context(self):
        for name, (args, kwargs) in CASES.items():
            with self.subTest(name=name), _trading_enabled("true"), _replace_module("futu", _fake_futu_for_combo()), _load_trade_module(name) as module:
                CONTEXT_OPENED.clear()
                module.get_default_acc_id = lambda: 0
                with _captured_output() as (out, _), self.assertRaises(SystemExit) as raised:
                    getattr(module, name)(*args, **{**kwargs, "output_json": True})
                self.assertEqual(raised.exception.code, 1)
                self.assertIn("--acc-id", json.loads(out.getvalue())["error"])
                self.assertFalse(CONTEXT_OPENED)

    def test_nonfinite_or_nonpositive_order_values_fail_before_context(self):
        cases = [
            ("place_order", ("EC.TEST", "BUY"), {"amount": -100, "price": .5, "pred_side": "YES", "trd_env": "REAL", "confirmed": True}),
            ("place_order", ("US.AAPL", "BUY"), {"quantity": 1.5, "price": 10, "trd_env": "REAL", "confirmed": True}),
        ]
        for name in ("place_order", "place_combo_order", "place_crypto_order", "modify_order"):
            args, kwargs = CASES[name]
            for invalid in (0, -1, True):
                cases.append((name, args, {**kwargs, "quantity": invalid}))
            for field in ("price", "quantity"):
                for invalid in (float("nan"), float("inf"), float("-inf")):
                    cases.append((name, args, {**kwargs, field: invalid}))
        for invalid in (0, float("nan"), float("inf"), float("-inf")):
            cases.append(("place_order", ("EC.TEST", "BUY"), {
                "amount": invalid, "price": .5, "pred_side": "YES",
                "trd_env": "REAL", "confirmed": True,
            }))
        for name, args, kwargs in cases:
            with self.subTest(name=name, kwargs=kwargs), _trading_enabled("true"), _replace_module("futu", _fake_futu_for_combo()), _load_trade_module(name) as module:
                CONTEXT_OPENED.clear()
                with _captured_output() as (out, _), self.assertRaises(SystemExit) as raised:
                    getattr(module, name)(*args, **{**kwargs, "output_json": True})
                self.assertEqual(raised.exception.code, 1)
                json.loads(out.getvalue())
                self.assertFalse(CONTEXT_OPENED)

    def test_sdk_rejections_are_audited_and_close_the_context(self):
        for name, (args, kwargs) in CASES.items():
            with self.subTest(name=name), _trading_enabled("true"), _replace_module("futu", _fake_futu_for_combo()), _load_trade_module(name) as module:
                AUDIT_ENTRIES.clear()
                context = Context(result=-1)
                closed = []
                for factory in ("create_trade_context", "create_future_trade_context", "create_crypto_trade_context", "create_sec_or_future_trade_context"):
                    setattr(module, factory, lambda *a, **k: context)
                module.safe_close = closed.append
                module.check_ret = common_error_checker(closed.append)
                with _captured_output() as (out, _), self.assertRaises(SystemExit) as raised:
                    getattr(module, name)(*args, **{**kwargs, "output_json": True})
                self.assertEqual(raised.exception.code, 1)
                self.assertEqual(len(context.writes), 1)
                self.assertEqual([row["result"] for row in AUDIT_ENTRIES], ["attempt", "error"])
                self.assertIn("offline result", AUDIT_ENTRIES[-1]["error"])
                payload = json.loads(out.getvalue())
                self.assertEqual(payload["ret"], -1)
                self.assertEqual(payload["error"], "offline result")
                self.assertTrue(payload["action"])
                self.assertEqual(closed, [context])

    def test_sdk_failure_json_retains_action_return_code_and_diagnostic_hints(self):
        for message, expected_hint in (
            ("trade not unlocked", "OpenD GUI"),
            ("No available real accounts", "security_firm"),
            ("permission denied", "行情权限"),
        ):
            with self.subTest(message=message), _trading_enabled("true"), _load_trade_module("place_order") as module:
                context = Context(-1, message)
                module.create_trade_context = lambda *a, **k: context
                module.check_ret = common_error_checker(lambda _: None)
                with _captured_output() as (out, _), self.assertRaises(SystemExit):
                    module.place_order("US.AAPL", "BUY", quantity=1, price=10, trd_env="REAL", confirmed=True, output_json=True)
                payload = json.loads(out.getvalue())
                self.assertEqual((payload["ret"], payload["action"], payload["error"]), (-1, "下单", message))
                self.assertIn(expected_hint, payload["hint"])
                if message == "permission denied":
                    self.assertIn("authority", payload["authority_url"])

    def test_explicit_accounts_reach_sdk_with_success_audit_and_credit_price_unchanged(self):
        for name, (args, kwargs) in CASES.items():
            with self.subTest(name=name), _trading_enabled("true"), _replace_module("futu", _fake_futu_for_combo()), _load_trade_module(name) as module:
                AUDIT_ENTRIES.clear()
                context = Context()
                for factory in ("create_trade_context", "create_future_trade_context", "create_crypto_trade_context", "create_sec_or_future_trade_context"):
                    setattr(module, factory, lambda *a, **k: context)
                with _captured_output() as (out, _):
                    getattr(module, name)(*args, **{**kwargs, "acc_id": 1, "output_json": True})
                json.loads(out.getvalue())
                self.assertEqual(context.writes[0]["acc_id"], 1)
                self.assertEqual([row["result"] for row in AUDIT_ENTRIES], ["attempt", "success"])
                if name == "place_combo_order":
                    self.assertEqual(context.writes[0]["price"], -1)

    def test_missing_account_never_produces_a_real_preview(self):
        for name in ("place_order", "place_combo_order", "place_crypto_order"):
            args, kwargs = CASES[name]
            with self.subTest(name=name), _replace_module("futu", _fake_futu_for_combo()), _load_trade_module(name) as module:
                module.get_default_acc_id = lambda: 0
                with _captured_output() as (out, _), self.assertRaises(SystemExit) as raised:
                    getattr(module, name)(*args, **{**kwargs, "confirmed": False, "output_json": True})
                self.assertEqual(raised.exception.code, 1)
                self.assertIn("--acc-id", json.loads(out.getvalue())["error"])
                self.assertFalse(CONTEXT_OPENED)

    def test_simulation_still_allows_sdk_default_account(self):
        with _trading_enabled("true"), _load_trade_module("place_order") as module:
            module.get_default_acc_id = lambda: 0
            context = Context()
            module.create_trade_context = lambda *a, **k: context
            with _captured_output():
                module.place_order("US.AAPL", "BUY", quantity=1, price=10, trd_env="SIMULATE")
            self.assertEqual(context.writes[0]["acc_id"], 0)

    def test_combo_leg_ratios_must_be_finite_positive(self):
        for ratio in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(ratio=ratio), _replace_module("futu", _fake_futu_for_combo()), _load_trade_module("place_combo_order") as module:
                legs = json.dumps([{"code": "US.AAPL", "trd_side": "BUY", "qty_ratio": ratio}])
                with self.assertRaises(ValueError):
                    module._parse_combo_legs(legs)

    def test_crypto_cancel_all_rejection_is_audited(self):
        with _trading_enabled("true"), _load_trade_module("cancel_crypto_order") as module:
            context = Context(-1)
            module.create_crypto_trade_context = lambda **kwargs: context
            module.check_ret = common_error_checker(lambda _: None)
            with _captured_output() as (out, _), self.assertRaises(SystemExit) as raised:
                module.cancel_crypto_order(cancel_all=True, confirmed=True, acc_id=1, output_json=True)
            self.assertEqual(raised.exception.code, 1)
            self.assertEqual(json.loads(out.getvalue())["ret"], -1)
            self.assertEqual([row["result"] for row in AUDIT_ENTRIES], ["attempt", "error"])


if __name__ == "__main__":
    unittest.main()
