import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock


SCRIPTS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "scripts")
)
sys.path.insert(0, SCRIPTS_DIR)

from trading_guard import (  # noqa: E402
    TRADING_ENABLED_ENV,
    TradingDisabledError,
    exit_trading_disabled,
    require_trading_enabled,
)


class TradingGuardTests(unittest.TestCase):
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

    def test_error_message_is_safe_and_actionable(self):
        with self.assertRaises(TradingDisabledError) as raised:
            require_trading_enabled({TRADING_ENABLED_ENV: "false", "PASSWORD": "secret"})
        message = str(raised.exception)
        self.assertIn("FUTU_TRADING_ENABLED=true", message)
        self.assertNotIn("secret", message)
        self.assertNotIn("FUTU_TRD_ENV", message)

    def test_cli_formatter_emits_json_and_exit_code_three(self):
        error = TradingDisabledError("disabled")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                exit_trading_disabled(error, output_json=True)
        self.assertEqual(raised.exception.code, 3)
        self.assertEqual(json.loads(stderr.getvalue()), {"error": "disabled"})


if __name__ == "__main__":
    unittest.main()
