import importlib.util
import io
import json
import os
import runpy
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
TRADE_DIR = SCRIPTS_DIR / "trade"
GUARD_PATH = SCRIPTS_DIR / "trading_guard.py"
_PREVIOUS_TRADING_GUARD = sys.modules.get("trading_guard")
_GUARD_SPEC = importlib.util.spec_from_file_location("trading_guard", GUARD_PATH)
_GUARD_MODULE = importlib.util.module_from_spec(_GUARD_SPEC)
sys.modules["trading_guard"] = _GUARD_MODULE
_GUARD_SPEC.loader.exec_module(_GUARD_MODULE)
TradingDisabledError = _GUARD_MODULE.TradingDisabledError


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
AUDIT_ENTRIES = []


def _context_factory(*args, **kwargs):
    CONTEXT_OPENED.append((args, kwargs))
    raise AssertionError("trade context opened")


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
    module.safe_get = (
        lambda row, key, default=None, *args: row.get(key, default)
        if isinstance(row, dict) else getattr(row, key, default)
    )
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


def _fake_futu_for_combo():
    module = types.ModuleType("futu")

    class ComboLeg:
        pass

    module.ComboLeg = ComboLeg
    module.OrderType = _Enum
    module.TimeInForce = _Enum
    return module


@contextmanager
def _replace_module(name, module):
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        yield
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


@contextmanager
def _load_trade_module(name):
    module_name = f"test_trading_entrypoints_{name}"
    path_before = list(sys.path)
    spec = importlib.util.spec_from_file_location(module_name, TRADE_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    with _replace_module("common", _fake_common()):
        try:
            spec.loader.exec_module(module)
            module._audit_log = lambda entry: AUDIT_ENTRIES.append(entry)
            yield module
        finally:
            sys.modules.pop(module_name, None)
            sys.path[:] = path_before


@contextmanager
def _trading_enabled(value):
    previous = os.environ.get("FUTU_TRADING_ENABLED")
    if value is None:
        os.environ.pop("FUTU_TRADING_ENABLED", None)
    else:
        os.environ["FUTU_TRADING_ENABLED"] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("FUTU_TRADING_ENABLED", None)
        else:
            os.environ["FUTU_TRADING_ENABLED"] = previous


@contextmanager
def _captured_output():
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        yield stdout, stderr


@contextmanager
def _null_context():
    yield


class TradingEntrypointsTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        if _PREVIOUS_TRADING_GUARD is None:
            sys.modules.pop("trading_guard", None)
        else:
            sys.modules["trading_guard"] = _PREVIOUS_TRADING_GUARD

    def setUp(self):
        CONTEXT_OPENED.clear()
        AUDIT_ENTRIES.clear()
        self.home_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.home_directory.cleanup)
        self.previous_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home_directory.name
        self.addCleanup(self._restore_home)

    def tearDown(self):
        self.assertFalse((Path(self.home_directory.name) / ".futu_trade_audit.jsonl").exists())

    def _restore_home(self):
        if self.previous_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.previous_home

    def _assert_disabled(self, callback):
        with self.assertRaises(TradingDisabledError):
            callback()
        self.assertFalse(CONTEXT_OPENED)
        self.assertFalse(AUDIT_ENTRIES)

    def _assert_preview(self, callback):
        with _captured_output():
            with self.assertRaises(SystemExit) as exited:
                callback()
        self.assertEqual(exited.exception.code, 2)
        self.assertFalse(CONTEXT_OPENED)
        self.assertFalse(AUDIT_ENTRIES)

    def test_disabled_confirmed_mutation_matrix_raises_before_context(self):
        with _trading_enabled("false"):
            with _load_trade_module("place_order") as module:
                self._assert_disabled(
                    lambda: module.place_order("US.AAPL", "BUY", quantity=1, price=1,
                                               trd_env="SIMULATE", confirmed=True)
                )
                self._assert_disabled(
                    lambda: module.place_order("US.AAPL", "BUY", quantity=1, price=1,
                                               trd_env="REAL", confirmed=True)
                )
                self._assert_disabled(
                    lambda: module.place_order("EC.TEST", "BUY", quantity=1, price=1,
                                               pred_side="YES", trd_env="REAL", confirmed=True)
                )
            with _load_trade_module("place_combo_order") as module:
                module._parse_combo_legs = lambda legs: ([{"code": "US.AAPL"}], False)
                module._resolve_order_type = lambda name: _Enum.NORMAL
                module._resolve_time_in_force = lambda name: _Enum.DAY
                self._assert_disabled(
                    lambda: module.place_combo_order("[]", price=1, quantity=1,
                                                      trd_env="SIMULATE", confirmed=True)
                )
                self._assert_disabled(
                    lambda: module.place_combo_order("[]", price=1, quantity=1,
                                                      trd_env="REAL", confirmed=True)
                )
                module._parse_combo_legs = lambda legs: ([{"code": "EC.TEST"}], True)
                self._assert_disabled(
                    lambda: module.place_combo_order("[]", price=1, quantity=1,
                                                      trd_env="REAL", quote_id="quote-1",
                                                      confirmed=True)
                )
            with _load_trade_module("place_crypto_order") as module:
                self._assert_disabled(
                    lambda: module.place_crypto_order("CC.BTCUSD", "BUY", quantity="0.1",
                                                      price=1, confirmed=True)
                )
            with _load_trade_module("modify_order") as module:
                self._assert_disabled(
                    lambda: module.modify_order("order-1", price=1, quantity=1,
                                                trd_env="SIMULATE", ctx_type="SEC")
                )
                self._assert_disabled(
                    lambda: module.modify_order("order-1", price=1, quantity=1,
                                                trd_env="REAL", ctx_type="FUTURE")
                )
            with _load_trade_module("cancel_order") as module:
                self._assert_disabled(
                    lambda: module.cancel_order("order-1", trd_env="SIMULATE", ctx_type="SEC")
                )
                self._assert_disabled(
                    lambda: module.cancel_order("order-1", trd_env="REAL", ctx_type="FUTURE")
                )
            with _load_trade_module("cancel_crypto_order") as module:
                self._assert_disabled(lambda: module.cancel_crypto_order(order_id="order-1"))
                self._assert_disabled(
                    lambda: module.cancel_crypto_order(cancel_all=True, confirmed=True)
                )

    def test_previews_remain_non_executable_under_both_switch_states(self):
        for switch in ("false", "true"):
            with self.subTest(switch=switch), _trading_enabled(switch):
                with _load_trade_module("place_order") as module:
                    self._assert_preview(
                        lambda: module.place_order("US.AAPL", "BUY", quantity=1, price=1,
                                                   trd_env="REAL", confirmed=False)
                    )
                with _load_trade_module("place_combo_order") as module:
                    module._parse_combo_legs = lambda legs: ([{"code": "US.AAPL"}], False)
                    module._resolve_order_type = lambda name: _Enum.NORMAL
                    module._resolve_time_in_force = lambda name: _Enum.DAY
                    self._assert_preview(
                        lambda: module.place_combo_order("[]", price=1, quantity=1,
                                                          trd_env="REAL", confirmed=False)
                    )
                with _load_trade_module("place_crypto_order") as module:
                    self._assert_preview(
                        lambda: module.place_crypto_order("CC.BTCUSD", "BUY", quantity="0.1",
                                                          price=1, confirmed=False)
                    )
                with _load_trade_module("cancel_crypto_order") as module:
                    self._assert_preview(
                        lambda: module.cancel_crypto_order(cancel_all=True, confirmed=False)
                    )

    def test_enabled_confirmed_cancel_reaches_existing_context_flow(self):
        with _trading_enabled("true"), _load_trade_module("cancel_order") as module:
            with _captured_output():
                with self.assertRaises(SystemExit) as exited:
                    module.cancel_order("order-1")
            self.assertEqual(exited.exception.code, 1)
        self.assertTrue(CONTEXT_OPENED)

    def test_every_cli_reports_disabled_guard_as_json_before_context(self):
        cli_cases = {
            "place_order": [
                "--code", "US.AAPL", "--side", "BUY", "--quantity", "1", "--price", "1",
                "--trd-env", "SIMULATE", "--json",
            ],
            "place_combo_order": [
                '[{"code":"US.AAPL","trd_side":"BUY","qty_ratio":1}]',
                "--price", "1", "--quantity", "1", "--json",
            ],
            "place_crypto_order": [
                "--code", "CC.BTCUSD", "--side", "BUY", "--quantity", "0.1", "--price", "1",
                "--confirmed", "--json",
            ],
            "modify_order": ["--order-id", "1", "--price", "1", "--quantity", "1", "--json"],
            "cancel_order": ["--order-id", "1", "--json"],
            "cancel_crypto_order": ["--order-id", "1", "--json"],
        }
        for name, arguments in cli_cases.items():
            with self.subTest(script=name):
                self._run_cli_and_assert_guard(name, arguments)

    def _run_cli_and_assert_guard(self, name, arguments):
        path_before = list(sys.path)
        argv_before = sys.argv
        with _trading_enabled(None), _replace_module("common", _fake_common()):
            fake_futu = _fake_futu_for_combo() if name == "place_combo_order" else None
            futu_context = _replace_module("futu", fake_futu) if fake_futu else _null_context()
            with futu_context, _captured_output() as (stdout, stderr):
                sys.argv = [str(TRADE_DIR / f"{name}.py"), *arguments]
                try:
                    with self.assertRaises(SystemExit) as exited:
                        runpy.run_path(str(TRADE_DIR / f"{name}.py"), run_name="__main__")
                finally:
                    sys.argv = argv_before
                    sys.path[:] = path_before
        self.assertEqual(exited.exception.code, 3)
        self.assertEqual(stdout.getvalue(), "")
        payload = json.loads(stderr.getvalue())
        self.assertIn("FUTU_TRADING_ENABLED=true", payload["error"])
        self.assertFalse(CONTEXT_OPENED)
        self.assertFalse(AUDIT_ENTRIES)

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
