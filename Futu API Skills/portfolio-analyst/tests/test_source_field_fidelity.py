"""Run the pinned official reader through the real adapter with only SDK I/O replaced."""

import ast
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import runpy
import subprocess
import sys
from types import ModuleType

import pytest

from portfolio_analyst.futu_adapter import FutuAdapter


OFFICIAL = Path(__file__).resolve().parents[2] / "futuapi"


def _adapter_with_sdk_rows(monkeypatch, funds, positions):
    calls = []

    class Context:
        def accinfo_query(self, **kwargs):
            calls.append(("accinfo_query", kwargs))
            return 0, funds

        def position_list_query(self, **kwargs):
            calls.append(("position_list_query", kwargs))
            return 0, positions

        def close(self):
            calls.append(("close", {}))

    common = ModuleType("common")
    # Use the audited helpers themselves, including their lossy default-zero behavior.
    tree = ast.parse((OFFICIAL / "scripts/common.py").read_text(encoding="utf-8"))
    helper_nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in {"safe_get", "safe_float", "safe_int"}
    ]
    exec(
        compile(ast.Module(body=helper_nodes, type_ignores=[]), "audited_common_helpers", "exec"),
        common.__dict__,
    )
    common.create_sec_or_future_trade_context = lambda *args, **kwargs: Context()
    for name in (
        "normalize_trade_ctx_type",
        "parse_trd_env",
        "parse_market",
        "parse_security_firm",
        "format_enum",
    ):
        setattr(common, name, lambda value: value)
    common.TRADE_CTX_TYPE_CHOICES = ("SEC", "FUTURE")
    common.TRD_MARKET_CLI_CHOICES = ("US", "HK")
    common.get_default_acc_id = lambda: None
    common.get_default_trd_env = lambda: "REAL"
    common.get_default_market = lambda: "US"
    common.check_ret = lambda *args: None
    common.safe_close = lambda ctx: ctx.close() if ctx is not None else None
    common.is_empty = lambda data: not data
    monkeypatch.setitem(sys.modules, "common", common)

    def run(command, **kwargs):
        # Execute exactly what the adapter selected, without network, SDK, or child setup.
        stdout, stderr = StringIO(), StringIO()
        with monkeypatch.context() as child:
            child.setattr(sys, "argv", command[2:])
            child.setattr(sys, "path", list(sys.path))
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    runpy.run_path(command[2], run_name="__main__")
                    status = 0
                except SystemExit as exc:
                    status = exc.code or 0
        return subprocess.CompletedProcess(command, status, stdout.getvalue(), stderr.getvalue())

    monkeypatch.setattr(subprocess, "run", run)
    return FutuAdapter(OFFICIAL), calls


@pytest.mark.parametrize(
    "field",
    [
        "cash",
        "total_assets",
        "qty",
        "market_val",
        "can_sell_qty",
        "average_cost",
        "nominal_price",
        "unrealized_pl",
        "realized_pl",
    ],
)
@pytest.mark.parametrize("missing", ["ABSENT", None, "N/A", True, "NaN", "Infinity"])
def test_source_missing_fields_survive_the_official_reader_as_null(monkeypatch, field, missing):
    funds = {"currency": "USD", "total_assets": 1000, "cash": 100}
    position = {
        "code": "US.TEST",
        "qty": 2,
        "market_val": 100,
        "acc_id": 90000001,
        "can_sell_qty": 2,
        "average_cost": 40,
        "nominal_price": 50,
        "unrealized_pl": 20,
        "realized_pl": 0,
    }
    source = funds if field in {"cash", "total_assets"} else position
    if missing == "ABSENT":
        source.pop(field)
    else:
        source[field] = missing
    adapter, calls = _adapter_with_sdk_rows(monkeypatch, funds, [position])
    result = adapter.read_private_json(
        "get_portfolio",
        [
            "--acc-id",
            "90000001",
            "--market",
            "US",
            "--trd-env",
            "REAL",
            "--currency",
            "USD",
            "--security-firm",
            "FUTUINC",
            "--ctx-type",
            "SEC",
            "--json",
        ],
    )

    observed = result["funds"] if field in {"cash", "total_assets"} else result["positions"][0]
    assert observed[field] is None
    assert [name for name, _kwargs in calls] == ["accinfo_query", "position_list_query", "close"]
    assert calls[0][1]["refresh_cache"] is True
    assert calls[0][1]["acc_id"] == calls[1][1]["acc_id"] == 90000001


def test_real_source_zero_survives_the_same_official_reader(monkeypatch):
    adapter, _calls = _adapter_with_sdk_rows(
        monkeypatch,
        {"currency": "USD", "total_assets": 0, "cash": 0},
        [
            {
                "code": "US.TEST",
                "qty": 0,
                "market_val": 0,
                "average_cost": 0,
                "can_sell_qty": 0,
                "nominal_price": 0,
            }
        ],
    )
    result = adapter.read_private_json("get_portfolio", ["--acc-id", "90000001", "--json"])
    assert result["funds"]["total_assets"] == result["funds"]["cash"] == 0
    assert result["positions"][0]["average_cost"] == result["positions"][0]["can_sell_qty"] == 0


@pytest.mark.parametrize("field", ["cash", "average_cost", "qty", "market_val"])
def test_official_raw_missing_facts_reach_the_snapshot_boundary(monkeypatch, tmp_path, field):
    from decimal import Decimal
    from portfolio_analyst.repository import SourceDataError
    from test_account_scope_and_missing_values import _source

    funds = {"currency": "USD", "total_assets": 1000, "cash": 100}
    row = {
        "code": "US.AAPL",
        "stock_name": "fixture",
        "qty": 2,
        "market_val": 100,
        "can_sell_qty": 2,
        "average_cost": 40,
        "nominal_price": 50,
        "acc_id": 90000001,
    }
    (funds if field == "cash" else row).pop(field)
    wire, calls = _adapter_with_sdk_rows(monkeypatch, funds, [row])

    class Adapter:
        def protect_sensitive_values(self, values):
            wire.protect_sensitive_values(values)

        def read_private_json(self, name, arguments, *, timeout=30):
            if name == "get_accounts":
                return {
                    "accounts": [
                        {
                            "acc_id": 90000001,
                            "trd_env": "REAL",
                            "ctx_type": "SEC",
                            "trdmarket_auth": ["US"],
                            "security_firm": "FUTUINC",
                            "acc_status": "ACTIVE",
                        }
                    ]
                }
            return wire.read_private_json(name, arguments, timeout=timeout)

    if field in {"qty", "market_val"}:
        with pytest.raises(SourceDataError, match="incomplete"):
            _source(tmp_path, Adapter())
    else:
        snapshot = _source(tmp_path, Adapter())
        assert snapshot.nav_base == Decimal("1000")
        if field == "cash":
            assert snapshot.cash_base is None
        else:
            assert snapshot.positions[0].average_cost_local is None
    assert [name for name, _kwargs in calls] == ["accinfo_query", "position_list_query", "close"]
