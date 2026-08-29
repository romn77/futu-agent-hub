import importlib.util
import os
import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
TRADE_DIR = SCRIPTS_DIR / "trade"
sys.path.insert(0, str(SCRIPTS_DIR))

from trading_guard import TradingDisabledError  # noqa: E402


class _Enum:
    NONE = "NONE"
    RTH = "RTH"
    ETH = "ETH"
    OVERNIGHT = "OVERNIGHT"
    ALL = "ALL"
    NORMAL = "NORMAL"
    MARKET = "MARKET"
    DAY = "DAY"
    IOC = "IOC"
    GTC = "GTC"
    REAL = "REAL"
    YES = "YES"
    NO = "NO"
    CANCEL = "CANCEL"


CONTEXT_OPENED = []


def _context_factory(*args, **kwargs):
    CONTEXT_OPENED.append((args, kwargs))
    raise AssertionError("trade context opened while trading was disabled")


def _fake_common():
    module = types.ModuleType("common")
    module.create_trade_context = _context_factory
    module.create_future_trade_context = _context_factory
    module.create_crypto_trade_context = _context_factory
    module.create_sec_or_future_trade_context = _context_factory
    module.is_event_contract_code = lambda code: str(code).startswith("EC.")
    module.is_crypto_code = lambda code: str(code).startswith("CC.")
    module.parse_trd_env = lambda value: str(value).upper()
    module.parse_trd_side = lambda value: str(value).upper()
    module.parse_security_firm = lambda value: value
    module.parse_qty = lambda value, **kwargs: float(value)
    module.get_default_acc_id = lambda: 1
    module.get_default_trd_env = lambda: "SIMULATE"
    module.infer_market_from_code = lambda code: "US" if code else None
    module.normalize_trade_ctx_type = lambda value: value
    module.TRADE_CTX_TYPE_CHOICES = ("SEC", "FUTURE")
    module.TRD_MARKET_CLI_CHOICES = ("US", "HK")
    module.check_ret = lambda *args, **kwargs: None
    module.safe_close = lambda ctx: None
    module.format_enum = lambda value: str(value)
    module.safe_get = lambda row, key, default=None, *args: row.get(key, default) if isinstance(row, dict) else default
    module.safe_float = lambda value: float(value)
    module.safe_int = lambda value: int(value)
    module.is_empty = lambda value: not value
    module.OrderType = _Enum
    module.Session = _Enum
    module.TimeInForce = _Enum
    module.PredSide = _Enum
    module.TrdEnv = _Enum
    module.ModifyOrderOp = _Enum
    module.RET_OK = 0
    return module


@contextmanager
def _load_trade_module(name):
    previous_common = sys.modules.get("common")
    module_name = f"test_trading_entrypoints_{name}"
    spec = importlib.util.spec_from_file_location(module_name, TRADE_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["common"] = _fake_common()
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)
        if previous_common is None:
            sys.modules.pop("common", None)
        else:
            sys.modules["common"] = previous_common


@contextmanager
def _trading_enabled(value):
    previous = os.environ.get("FUTU_TRADING_ENABLED")
    os.environ["FUTU_TRADING_ENABLED"] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("FUTU_TRADING_ENABLED", None)
        else:
            os.environ["FUTU_TRADING_ENABLED"] = previous


class TradingEntrypointsTests(unittest.TestCase):
    def setUp(self):
        CONTEXT_OPENED.clear()

    def test_disabled_mutations_raise_before_context_opens(self):
        with _trading_enabled("false"):
            with _load_trade_module("place_order") as module:
                for env in ("SIMULATE", "REAL"):
                    with self.subTest(script="place_order", environment=env):
                        with self.assertRaises(TradingDisabledError):
                            module.place_order("US.AAPL", "BUY", quantity=1, price=1,
                                               trd_env=env, confirmed=True)
            with _load_trade_module("place_combo_order") as module:
                module._parse_combo_legs = lambda legs: ([{"code": "US.AAPL"}], False)
                module._resolve_order_type = lambda name: _Enum.NORMAL
                module._resolve_time_in_force = lambda name: _Enum.DAY
                with self.assertRaises(TradingDisabledError):
                    module.place_combo_order("[]", price=1, quantity=1, confirmed=True)
            with _load_trade_module("place_crypto_order") as module:
                with self.assertRaises(TradingDisabledError):
                    module.place_crypto_order("CC.BTCUSD", "BUY", quantity="1", price=1,
                                              confirmed=True)
            with _load_trade_module("modify_order") as module:
                with self.assertRaises(TradingDisabledError):
                    module.modify_order("order-1", price=1, quantity=1)
            with _load_trade_module("cancel_order") as module:
                with self.assertRaises(TradingDisabledError):
                    module.cancel_order("order-1")
            with _load_trade_module("cancel_crypto_order") as module:
                with self.assertRaises(TradingDisabledError):
                    module.cancel_crypto_order(order_id="order-1")
                with self.assertRaises(TradingDisabledError):
                    module.cancel_crypto_order(cancel_all=True, confirmed=True)

    def test_disabled_previews_exit_without_context(self):
        with _trading_enabled("false"):
            with _load_trade_module("place_order") as module:
                with self.assertRaises(SystemExit) as exited:
                    module.place_order("US.AAPL", "BUY", quantity=1, price=1,
                                       trd_env="REAL", confirmed=False)
                self.assertEqual(exited.exception.code, 2)
            with _load_trade_module("place_combo_order") as module:
                module._parse_combo_legs = lambda legs: ([{"code": "US.AAPL"}], False)
                module._resolve_order_type = lambda name: _Enum.NORMAL
                module._resolve_time_in_force = lambda name: _Enum.DAY
                with self.assertRaises(SystemExit) as exited:
                    module.place_combo_order("[]", price=1, quantity=1,
                                             trd_env="REAL", confirmed=False)
                self.assertEqual(exited.exception.code, 2)
            with _load_trade_module("place_crypto_order") as module:
                with self.assertRaises(SystemExit) as exited:
                    module.place_crypto_order("CC.BTCUSD", "BUY", quantity="1", price=1,
                                              confirmed=False)
                self.assertEqual(exited.exception.code, 2)
            with _load_trade_module("cancel_crypto_order") as module:
                with self.assertRaises(SystemExit) as exited:
                    module.cancel_crypto_order(cancel_all=True, confirmed=False)
                self.assertEqual(exited.exception.code, 2)

    def test_enabled_guard_allows_existing_context_flow_but_keeps_previews(self):
        with _trading_enabled("true"):
            with _load_trade_module("cancel_order") as module:
                with self.assertRaises(SystemExit) as exited:
                    module.cancel_order("order-1")
                self.assertEqual(exited.exception.code, 1)
                self.assertTrue(CONTEXT_OPENED)
            with _load_trade_module("place_order") as module:
                with self.assertRaises(SystemExit) as exited:
                    module.place_order("US.AAPL", "BUY", quantity=1, price=1,
                                       trd_env="REAL", confirmed=False)
                self.assertEqual(exited.exception.code, 2)

    def test_read_only_scripts_are_not_gated(self):
        for relative_path in (
            "trade/get_accounts.py",
            "trade/get_portfolio.py",
            "quote/get_kline.py",
        ):
            with self.subTest(script=relative_path):
                content = (SCRIPTS_DIR / relative_path).read_text(encoding="utf-8")
                self.assertNotIn("require_trading_enabled", content)


if __name__ == "__main__":
    unittest.main()
