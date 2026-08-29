# Trading-Enabled Safety Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fail-closed `FUTU_TRADING_ENABLED` switch that blocks every bundled simulated and real trading write while preserving reads and non-executable previews.

**Architecture:** A pure-standard-library `scripts/trading_guard.py` owns value parsing, the dedicated exception, and CLI exit formatting. Each mutating script calls the guard after validation/preview and before creating a trade context; runtime tests load every entry point with a fake `common` module so no test imports the SDK or connects to OpenD.

**Tech Stack:** Python 3.13, standard-library `unittest`, importlib-based test isolation, Futu API script conventions, pytest/uv for portfolio trust regression tests.

---

## File Map

- Create `Futu API Skills/futuapi/scripts/trading_guard.py`: pure environment guard and CLI failure formatting.
- Create `Futu API Skills/futuapi/tests/test_trading_guard.py`: guard value, error, and formatting unit tests.
- Create `Futu API Skills/futuapi/tests/test_trading_entrypoints.py`: isolated runtime coverage for every bundled mutation entry point and preview.
- Modify `Futu API Skills/futuapi/scripts/trade/place_order.py`: guard regular, option, futures, and prediction-market order writes.
- Modify `Futu API Skills/futuapi/scripts/trade/place_combo_order.py`: guard combo order writes.
- Modify `Futu API Skills/futuapi/scripts/trade/place_crypto_order.py`: guard crypto order writes.
- Modify `Futu API Skills/futuapi/scripts/trade/modify_order.py`: guard order modifications.
- Modify `Futu API Skills/futuapi/scripts/trade/cancel_order.py`: guard order cancellations.
- Modify `Futu API Skills/futuapi/scripts/trade/cancel_crypto_order.py`: guard single and cancel-all crypto writes.
- Modify `Futu API Skills/futuapi/SKILL.md`: document the new hard safety constraint and environment variable.
- Modify `Futu API Skills/portfolio-analyst/portfolio_analyst/skill_trust.py`: update the pinned `SKILL.md` digest only.
- Modify `.env` locally: set `FUTU_TRADING_ENABLED=false`; the ignored file is never staged.

### Task 1: Build the pure fail-closed guard

**Files:**
- Create: `Futu API Skills/futuapi/tests/test_trading_guard.py`
- Create: `Futu API Skills/futuapi/scripts/trading_guard.py`

- [ ] **Step 1: Write the failing guard tests**

Create `Futu API Skills/futuapi/tests/test_trading_guard.py`:

```python
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from trading_guard import (  # noqa: E402
    TRADING_ENABLED_ENV,
    TradingDisabledError,
    exit_trading_disabled,
    require_trading_enabled,
)


class TradingGuardTests(unittest.TestCase):
    def test_missing_value_is_disabled(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(TradingDisabledError):
                require_trading_enabled()

    def test_false_empty_and_invalid_values_are_disabled(self):
        for value in ("", "false", "0", "yes", "enabled", " truex "):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {TRADING_ENABLED_ENV: value}, clear=True):
                    with self.assertRaises(TradingDisabledError):
                        require_trading_enabled()

    def test_true_is_case_insensitive_and_trimmed(self):
        for value in ("true", "TRUE", " True "):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {TRADING_ENABLED_ENV: value}, clear=True):
                    require_trading_enabled()

    def test_environment_is_read_on_every_call(self):
        with mock.patch.dict(os.environ, {TRADING_ENABLED_ENV: "true"}, clear=True):
            require_trading_enabled()
            os.environ[TRADING_ENABLED_ENV] = "false"
            with self.assertRaises(TradingDisabledError):
                require_trading_enabled()

    def test_disabled_message_names_only_the_enabling_switch(self):
        with mock.patch.dict(os.environ, {TRADING_ENABLED_ENV: "false"}, clear=True):
            with self.assertRaises(TradingDisabledError) as captured:
                require_trading_enabled()
        message = str(captured.exception)
        self.assertIn("FUTU_TRADING_ENABLED=true", message)
        self.assertNotIn("password", message.lower())
        self.assertNotIn("FUTU_TRD_ENV", message)

    def test_cli_formatter_uses_exit_3_and_stderr_json(self):
        error = TradingDisabledError("blocked by FUTU_TRADING_ENABLED=true")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as captured:
                exit_trading_disabled(error, output_json=True)
        self.assertEqual(captured.exception.code, 3)
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"error": "blocked by FUTU_TRADING_ENABLED=true"},
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the guard tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest discover \
  -s "Futu API Skills/futuapi/tests" \
  -p "test_trading_guard.py" -v
```

Expected: FAIL during import with `ModuleNotFoundError: No module named 'trading_guard'`.

- [ ] **Step 3: Implement the minimal pure guard**

Create `Futu API Skills/futuapi/scripts/trading_guard.py`:

```python
#!/usr/bin/env python3
"""Fail-closed authorization for bundled Futu trading-write scripts."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from typing import NoReturn


TRADING_ENABLED_ENV = "FUTU_TRADING_ENABLED"


class TradingDisabledError(RuntimeError):
    """Raised before a bundled script can reach an SDK trading-write call."""


def require_trading_enabled(environment: Mapping[str, str] | None = None) -> None:
    """Allow trading writes only when FUTU_TRADING_ENABLED is explicitly true."""

    source = os.environ if environment is None else environment
    value = source.get(TRADING_ENABLED_ENV, "")
    if value.strip().lower() != "true":
        raise TradingDisabledError(
            "交易写操作已禁用；如需启用，请显式设置 FUTU_TRADING_ENABLED=true"
        )


def exit_trading_disabled(
    error: TradingDisabledError, *, output_json: bool = False
) -> NoReturn:
    """Render the CLI failure without a traceback and exit with the reserved code 3."""

    if output_json:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
    else:
        print(f"错误: {error}", file=sys.stderr)
    raise SystemExit(3)
```

- [ ] **Step 4: Run the guard tests and verify GREEN**

Run the Step 2 command again.

Expected: 6 tests pass and the process exits `0`.

- [ ] **Step 5: Commit the guard core**

```bash
git add \
  "Futu API Skills/futuapi/scripts/trading_guard.py" \
  "Futu API Skills/futuapi/tests/test_trading_guard.py"
git commit -m "feat(futuapi): add fail-closed trading guard"
```

### Task 2: Exercise and protect every mutation entry point

**Files:**
- Create: `Futu API Skills/futuapi/tests/test_trading_entrypoints.py`
- Modify: `Futu API Skills/futuapi/scripts/trade/place_order.py`
- Modify: `Futu API Skills/futuapi/scripts/trade/place_combo_order.py`
- Modify: `Futu API Skills/futuapi/scripts/trade/place_crypto_order.py`
- Modify: `Futu API Skills/futuapi/scripts/trade/modify_order.py`
- Modify: `Futu API Skills/futuapi/scripts/trade/cancel_order.py`
- Modify: `Futu API Skills/futuapi/scripts/trade/cancel_crypto_order.py`

- [ ] **Step 1: Write the isolated entry-point regression tests**

Create `Futu API Skills/futuapi/tests/test_trading_entrypoints.py`. The fake `common` module supplies validation-only values and makes every context factory fail the test if reached:

```python
from __future__ import annotations

import importlib.util
import io
import os
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
TRADE_DIR = SCRIPTS_DIR / "trade"
sys.path.insert(0, str(SCRIPTS_DIR))

from trading_guard import TradingDisabledError  # noqa: E402


class _EnumValues:
    def __getattr__(self, name):
        return name


def _context_must_not_open(*_args, **_kwargs):
    raise AssertionError("trade context opened while trading was disabled")


def _safe_get(value, *names, default=None):
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _fake_common():
    module = types.ModuleType("common")
    enum_values = _EnumValues()
    values = {
        "create_trade_context": _context_must_not_open,
        "create_future_trade_context": _context_must_not_open,
        "create_crypto_trade_context": _context_must_not_open,
        "create_sec_or_future_trade_context": _context_must_not_open,
        "is_event_contract_code": lambda code: str(code).startswith("EC."),
        "is_crypto_code": lambda code: str(code).startswith("CC."),
        "parse_trd_env": lambda value: value,
        "parse_trd_side": lambda value: value,
        "parse_security_firm": lambda _value: None,
        "normalize_trade_ctx_type": lambda value: value,
        "get_default_acc_id": lambda: 12345,
        "get_default_trd_env": lambda: "SIMULATE",
        "infer_market_from_code": lambda _code: "US",
        "parse_qty": lambda value, **_kwargs: float(value),
        "check_ret": lambda *_args, **_kwargs: None,
        "safe_close": lambda *_args, **_kwargs: None,
        "format_enum": lambda value: str(value),
        "safe_get": _safe_get,
        "safe_float": lambda value: float(value),
        "safe_int": lambda value: int(value),
        "is_empty": lambda value: not value,
        "OrderType": enum_values,
        "Session": enum_values,
        "TimeInForce": enum_values,
        "PredSide": enum_values,
        "TrdEnv": enum_values,
        "ModifyOrderOp": enum_values,
        "RET_OK": 0,
        "TRD_MARKET_CLI_CHOICES": ("US", "HK"),
        "TRADE_CTX_TYPE_CHOICES": ("SEC", "FUTURE"),
    }
    for name, value in values.items():
        setattr(module, name, value)
    return module


def _load_entrypoint(filename):
    path = TRADE_DIR / filename
    name = f"_trading_guard_test_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get("common")
    sys.modules["common"] = _fake_common()
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop("common", None)
        else:
            sys.modules["common"] = previous
    return module


def _call_combo(module, *, confirmed, trd_env="SIMULATE"):
    module._parse_combo_legs = lambda _raw: ([{"code": "US.AAPL"}], False)
    module._resolve_order_type = lambda _name: "NORMAL"
    module._resolve_time_in_force = lambda _name: "DAY"
    return module.place_combo_order(
        '[{"code":"US.AAPL"}]', 1.0, 1, trd_env=trd_env, confirmed=confirmed
    )


class TradingEntrypointTests(unittest.TestCase):
    def test_every_write_entrypoint_is_blocked_before_context_creation(self):
        cases = (
            ("place_order.py", lambda m: m.place_order("US.AAPL", "BUY", 1, 1.0, confirmed=True)),
            ("place_order.py", lambda m: m.place_order("US.AAPL", "BUY", 1, 1.0, trd_env="REAL", confirmed=True)),
            ("place_combo_order.py", lambda m: _call_combo(m, confirmed=True)),
            ("place_crypto_order.py", lambda m: m.place_crypto_order("CC.BTCUSD", "BUY", 0.1, 1.0, confirmed=True)),
            ("modify_order.py", lambda m: m.modify_order("1", price=1.0, quantity=1)),
            ("cancel_order.py", lambda m: m.cancel_order("1")),
            ("cancel_crypto_order.py", lambda m: m.cancel_crypto_order(order_id="1")),
            ("cancel_crypto_order.py", lambda m: m.cancel_crypto_order(cancel_all=True, confirmed=True)),
        )
        with mock.patch.dict(os.environ, {"FUTU_TRADING_ENABLED": "false"}, clear=False):
            for filename, invoke in cases:
                with self.subTest(filename=filename, invoke=invoke):
                    module = _load_entrypoint(filename)
                    with self.assertRaises(TradingDisabledError):
                        invoke(module)

    def test_non_executable_previews_remain_available_while_disabled(self):
        cases = (
            ("place_order.py", lambda m: m.place_order("US.AAPL", "BUY", 1, 1.0, trd_env="REAL")),
            ("place_combo_order.py", lambda m: _call_combo(m, confirmed=False, trd_env="REAL")),
            ("place_crypto_order.py", lambda m: m.place_crypto_order("CC.BTCUSD", "BUY", 0.1, 1.0)),
            ("cancel_crypto_order.py", lambda m: m.cancel_crypto_order(cancel_all=True)),
        )
        with mock.patch.dict(os.environ, {"FUTU_TRADING_ENABLED": "false"}, clear=False):
            for filename, invoke in cases:
                with self.subTest(filename=filename):
                    module = _load_entrypoint(filename)
                    with redirect_stdout(io.StringIO()):
                        with self.assertRaises(SystemExit) as captured:
                            invoke(module)
                    self.assertEqual(captured.exception.code, 2)

    def test_enabled_switch_reaches_existing_flow_but_does_not_remove_real_preview(self):
        with mock.patch.dict(os.environ, {"FUTU_TRADING_ENABLED": "true"}, clear=False):
            cancel_module = _load_entrypoint("cancel_order.py")
            with self.assertRaisesRegex(AssertionError, "trade context opened"):
                cancel_module.cancel_order("1")

            place_module = _load_entrypoint("place_order.py")
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as captured:
                    place_module.place_order(
                        "US.AAPL", "BUY", 1, 1.0, trd_env="REAL", confirmed=False
                    )
            self.assertEqual(captured.exception.code, 2)

    def test_representative_read_only_scripts_do_not_use_the_write_guard(self):
        read_only_paths = (
            TRADE_DIR / "get_accounts.py",
            TRADE_DIR / "get_portfolio.py",
            SCRIPTS_DIR / "quote" / "get_kline.py",
        )
        for path in read_only_paths:
            with self.subTest(path=path.name):
                self.assertNotIn(
                    "require_trading_enabled", path.read_text(encoding="utf-8")
                )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the entry-point tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest discover \
  -s "Futu API Skills/futuapi/tests" \
  -p "test_trading_entrypoints.py" -v
```

Expected: the write-entrypoint test errors because a fake context factory is reached; preview cases still exit `2`.

- [ ] **Step 3: Import the guard in all six mutation scripts**

Immediately after each script adds `scripts/` to `sys.path`, add:

```python
from trading_guard import (
    TradingDisabledError,
    exit_trading_disabled,
    require_trading_enabled,
)
```

Keep existing imports from `common` unchanged.

- [ ] **Step 4: Guard each write after validation and preview**

In each public mutation function, insert this immediately before `ctx = None`:

```python
require_trading_enabled()
```

Apply it to `place_order`, `place_combo_order`, `place_crypto_order`, `modify_order`, `cancel_order`, and `cancel_crypto_order`. In `cancel_crypto_order`, it must remain after the unconfirmed cancel-all preview so exit `2` still wins.

- [ ] **Step 5: Map guard failures to CLI exit 3**

Wrap each bottom-level function invocation under `if __name__ == "__main__":` with its exact existing arguments.

For `place_order.py`:

```python
try:
    place_order(code=args.code, side=args.side, quantity=args.quantity, price=args.price,
                order_type=args.order_type, acc_id=args.acc_id,
                trd_env=args.trd_env, security_firm=args.security_firm,
                output_json=args.output_json, confirmed=args.confirmed,
                fill_outside_rth=args.fill_outside_rth, session_str=args.session,
                amount=args.amount, pred_side=args.pred_side,
                time_in_force=args.time_in_force, expire_time=args.expire_time,
                remark=args.remark)
except TradingDisabledError as error:
    exit_trading_disabled(error, output_json=args.output_json)
```

For `place_combo_order.py`:

```python
try:
    place_combo_order(
        legs_json=args.legs,
        price=args.price,
        quantity=args.quantity,
        order_type=args.order_type,
        acc_id=args.acc_id,
        trd_env=args.trd_env,
        security_firm=args.security_firm,
        remark=args.remark,
        time_in_force=args.time_in_force,
        expire_time=args.expire_time,
        confirmed=args.confirmed,
        quote_id=args.quote_id,
        output_json=args.output_json,
    )
except TradingDisabledError as error:
    exit_trading_disabled(error, output_json=args.output_json)
```

For `place_crypto_order.py`:

```python
try:
    place_crypto_order(code=args.code, side=args.side, quantity=args.quantity,
                       price=args.price, order_type=args.order_type, acc_id=args.acc_id,
                       security_firm=args.security_firm, output_json=args.output_json,
                       confirmed=args.confirmed)
except TradingDisabledError as error:
    exit_trading_disabled(error, output_json=args.output_json)
```

For `modify_order.py`:

```python
try:
    modify_order(order_id=args.order_id, price=args.price, quantity=args.quantity,
                 adjust_limit=args.adjust_limit, acc_id=args.acc_id, market=args.market,
                 trd_env=args.trd_env, security_firm=args.security_firm,
                 ctx_type=args.ctx_type, output_json=args.output_json)
except TradingDisabledError as error:
    exit_trading_disabled(error, output_json=args.output_json)
```

For `cancel_order.py`:

```python
try:
    cancel_order(order_id=args.order_id, acc_id=args.acc_id, market=args.market,
                 trd_env=args.trd_env, security_firm=args.security_firm,
                 ctx_type=args.ctx_type, output_json=args.output_json)
except TradingDisabledError as error:
    exit_trading_disabled(error, output_json=args.output_json)
```

For `cancel_crypto_order.py`:

```python
try:
    cancel_crypto_order(order_id=args.order_id, cancel_all=args.cancel_all,
                        acc_id=args.acc_id, security_firm=args.security_firm,
                        output_json=args.output_json, confirmed=args.confirmed)
except TradingDisabledError as error:
    exit_trading_disabled(error, output_json=args.output_json)
```

Do not catch `TradingDisabledError` inside the public function's broad provider `except Exception`; the guard call must stay before that `try` block so programmatic callers receive the dedicated exception and CLI callers receive exit `3`.

- [ ] **Step 6: Run all Futu guard tests and verify GREEN**

Run:

```bash
.venv/bin/python -m unittest discover \
  -s "Futu API Skills/futuapi/tests" \
  -p "test_*.py" -v
```

Expected: all guard and entry-point tests pass; no OpenD connection log appears.

- [ ] **Step 7: Commit entry-point protection**

```bash
git add \
  "Futu API Skills/futuapi/scripts/trade/place_order.py" \
  "Futu API Skills/futuapi/scripts/trade/place_combo_order.py" \
  "Futu API Skills/futuapi/scripts/trade/place_crypto_order.py" \
  "Futu API Skills/futuapi/scripts/trade/modify_order.py" \
  "Futu API Skills/futuapi/scripts/trade/cancel_order.py" \
  "Futu API Skills/futuapi/scripts/trade/cancel_crypto_order.py" \
  "Futu API Skills/futuapi/tests/test_trading_entrypoints.py"
git commit -m "feat(futuapi): gate all bundled trading writes"
```

### Task 3: Document the switch and preserve portfolio trust

**Files:**
- Modify: `Futu API Skills/futuapi/tests/test_trading_guard.py`
- Modify: `Futu API Skills/futuapi/SKILL.md`
- Modify: `Futu API Skills/portfolio-analyst/portfolio_analyst/skill_trust.py`
- Modify locally only: `.env`

- [ ] **Step 1: Add a failing documentation contract test**

Add `import subprocess` with the existing imports, add these constants below `SCRIPTS_DIR`, and add the method inside `TradingGuardTests`:

```python
SKILL_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SKILL_DIR.parents[1]


class TradingGuardTests(unittest.TestCase):
    def test_skill_documents_trading_switch_and_env_is_ignored(self):
        skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("`FUTU_TRADING_ENABLED`", skill_text)
        self.assertIn("退出码 3", skill_text)
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", ".env"],
            cwd=REPOSITORY_ROOT,
            check=False,
        )
        self.assertEqual(ignored.returncode, 0)
```

The snippet shows the class context; do not create a second `TradingGuardTests` class. Insert only the method into the existing class.

- [ ] **Step 2: Run the documentation test and verify RED**

Run the Task 1 Step 2 command.

Expected: FAIL because `SKILL.md` does not yet contain the new variable or exit-code contract.

- [ ] **Step 3: Document the environment variable and hard rule**

In the environment-variable table in `Futu API Skills/futuapi/SKILL.md`, insert after `FUTU_TRD_ENV`:

```markdown
| `FUTU_TRADING_ENABLED` | 是否允许交易写操作；仅 `true` 启用 | false |
```

Append response rule 15 exactly:

```markdown
15. **交易写开关（硬约束）**：所有内置下单、改单、撤单脚本在真正调用 SDK 写接口前必须确认 `FUTU_TRADING_ENABLED=true`；缺失、`false` 或非法值均以退出码 3 阻止写操作。该开关同时适用于 `SIMULATE` 和 `REAL`，且不替代实盘 `--confirmed` 与 OpenD GUI 手动解锁。
```

- [ ] **Step 4: Update the audited SKILL.md digest**

The canonical SHA-256 for the exact two documentation insertions in Step 3 is:

```text
5619a3fa58c8e36855f68b71238e39a65f140fe6822092bf5188716ad020c1cd
```

Replace only the `Path("SKILL.md")` digest in `Futu API Skills/portfolio-analyst/portfolio_analyst/skill_trust.py` with that value. Do not change the pinned `common.py` digest or the read-only command allowlist.

- [ ] **Step 5: Keep the ignored local default disabled**

Update the project-root `.env` to exactly:

```dotenv
FUTU_TRD_ENV=REAL
FUTU_TRADING_ENABLED=false
```

Do not stage `.env`.

- [ ] **Step 6: Run documentation and trust regression tests**

Run:

```bash
.venv/bin/python -m unittest discover \
  -s "Futu API Skills/futuapi/tests" \
  -p "test_*.py" -v
UV_CACHE_DIR=/tmp/uv-cache uv run \
  --project "Futu API Skills/portfolio-analyst" \
  --extra test pytest \
  "Futu API Skills/portfolio-analyst/tests/test_skill_trust.py" \
  "Futu API Skills/portfolio-analyst/tests/test_manifest_integrity.py" -q
```

Expected: all Futu guard tests and both portfolio trust test files pass.

- [ ] **Step 7: Commit documentation and trust metadata**

```bash
git add \
  "Futu API Skills/futuapi/SKILL.md" \
  "Futu API Skills/futuapi/tests/test_trading_guard.py" \
  "Futu API Skills/portfolio-analyst/portfolio_analyst/skill_trust.py"
git commit -m "docs(futuapi): document trading write switch"
```

Confirm `git status --short --ignored .env` reports `!! .env` and not a staged file.

### Task 4: Run full safety verification

**Files:**
- Verify only; no planned production edits.

- [ ] **Step 1: Run the complete Futu guard suite**

```bash
.venv/bin/python -m unittest discover \
  -s "Futu API Skills/futuapi/tests" \
  -p "test_*.py" -v
```

Expected: all tests pass with no SDK import or OpenD connection output.

- [ ] **Step 2: Run the complete portfolio-analyst suite**

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run \
  --project "Futu API Skills/portfolio-analyst" \
  --extra test pytest -q
```

Expected: the complete portfolio-analyst suite passes, including manifest, trust, and no-order-entry safety tests.

- [ ] **Step 3: Verify repository and local configuration state**

```bash
git diff --check
git status --short
git status --short --ignored .env
```

Expected: no unstaged tracked implementation changes, no whitespace errors, and `.env` appears only as `!! .env`.

- [ ] **Step 4: Verify fail-closed behavior without OpenD**

Run only the pure guard module in a subprocess; do not run a trading script:

```bash
env -u FUTU_TRADING_ENABLED .venv/bin/python -c \
  'import sys; sys.path.insert(0, "Futu API Skills/futuapi/scripts"); from trading_guard import require_trading_enabled; require_trading_enabled()'
```

Expected: `TradingDisabledError` and a non-zero exit, with no OpenD connection log. The CLI-specific exit-3 behavior is already covered without a traceback by `test_cli_formatter_uses_exit_3_and_stderr_json`.
