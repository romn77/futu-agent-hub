import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


GUARD_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), os.pardir, "scripts", "trading_guard.py"
    )
)
SKILL_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SKILL_DIR.parents[1]


def load_guard_module(module_name):
    spec = importlib.util.spec_from_file_location(module_name, GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_guard = load_guard_module("_futuapi_trading_guard_test")
TRADING_ENABLED_ENV = _guard.TRADING_ENABLED_ENV
TradingDisabledError = _guard.TradingDisabledError
exit_trading_disabled = _guard.exit_trading_disabled
require_trading_enabled = _guard.require_trading_enabled


class TradingGuardTests(unittest.TestCase):
    def test_skill_documents_trading_switch_and_env_is_ignored(self):
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("`FUTU_TRADING_ENABLED`", skill)
        self.assertIn("退出码 3", skill)
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", ".env"],
            cwd=REPOSITORY_ROOT,
            check=False,
        )
        self.assertEqual(ignored.returncode, 0)

    def test_missing_environment_variable_raises(self):
        with self.assertRaises(TradingDisabledError):
            require_trading_enabled({})

    def test_empty_and_falsey_values_are_rejected(self):
        for value in ("", "false", "0", "yes", "enabled", " truex "):
            with self.subTest(value=value), self.assertRaises(TradingDisabledError):
                require_trading_enabled({TRADING_ENABLED_ENV: value})

    def test_true_values_are_accepted(self):
        for value in ("true", "TRUE", " True "):
            with self.subTest(value=value):
                self.assertIsNone(require_trading_enabled({TRADING_ENABLED_ENV: value}))

    def test_environment_is_read_on_every_call(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(TradingDisabledError):
                require_trading_enabled()
            os.environ[TRADING_ENABLED_ENV] = "true"
            self.assertIsNone(require_trading_enabled())

    def test_loading_by_path_does_not_change_process_module_state(self):
        original_path = list(sys.path)
        original_modules = dict(sys.modules)
        module_name = "_futuapi_trading_guard_isolation_unique"
        self.assertNotIn(module_name, sys.modules)

        load_guard_module(module_name)

        self.assertEqual(sys.path, original_path)
        self.assertEqual(sys.modules, original_modules)
        self.assertNotIn(module_name, sys.modules)

    def test_error_message_is_safe_and_actionable(self):
        with self.assertRaises(TradingDisabledError) as raised:
            require_trading_enabled({TRADING_ENABLED_ENV: "false", "PASSWORD": "secret"})
        message = str(raised.exception)
        self.assertIn("FUTU_TRADING_ENABLED=true", message)
        self.assertNotIn("secret", message)
        self.assertNotIn("FUTU_TRD_ENV", message)

    def test_cli_formatter_emits_json_and_exit_code_three(self):
        error = TradingDisabledError("blocked by FUTU_TRADING_ENABLED=true")
        json_stderr = io.StringIO()
        with contextlib.redirect_stderr(json_stderr):
            with self.assertRaises(SystemExit) as raised:
                exit_trading_disabled(error, output_json=True)
        self.assertEqual(raised.exception.code, 3)
        self.assertEqual(
            json.loads(json_stderr.getvalue()),
            {"error": "blocked by FUTU_TRADING_ENABLED=true"},
        )

        human_stderr = io.StringIO()
        with contextlib.redirect_stderr(human_stderr):
            with self.assertRaises(SystemExit) as raised:
                exit_trading_disabled(error, output_json=False)
        self.assertEqual(raised.exception.code, 3)
        self.assertIn(
            "错误: blocked by FUTU_TRADING_ENABLED=true",
            human_stderr.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
