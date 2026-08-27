from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_analyst import cli, skill_trust
from portfolio_analyst.futu_adapter import FutuAdapter, UnsafeFutuCommand
from portfolio_analyst.persistence import SnapshotStore
from portfolio_analyst.rebalance import RebalanceChange, RebalanceRequest, simulate_rebalance
from portfolio_analyst.schema import (
    ReportSemanticError,
    validate_rebalance_result_document,
    validate_trade_review_document,
)
from portfolio_analyst.settings import Settings
from portfolio_analyst.trade_review import (
    TradePolicyConfigError,
    build_trade_review,
    load_trade_policy,
)
from test_risk_analysis import snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_SKILL = PROJECT_ROOT.parent / "futuapi"


def _audited_copy(tmp_path: Path) -> Path:
    target = tmp_path / "futuapi"
    for record in skill_trust.audited_files():
        destination = target / record.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(OFFICIAL_SKILL / record.relative_path, destination)
    return target


def test_history_commands_are_pinned_and_have_exact_date_account_contract(tmp_path):
    assert {
        "get_history_orders",
        "get_history_order_fill_list",
    } <= skill_trust.audited_command_names()
    adapter = FutuAdapter(_audited_copy(tmp_path))
    common = [
        "--acc-id",
        "12345",
        "--market",
        "US",
        "--trd-env",
        "REAL",
        "--start",
        "2026-08-01",
        "--end",
        "2026-08-27",
        "--security-firm",
        "FUTUSECURITIES",
        "--ctx-type",
        "SEC",
        "--json",
    ]
    assert adapter.validate_request("get_history_order_fill_list", common).arguments == tuple(
        common
    )
    order_args = [*common[:-1], "--limit", "200", "--json"]
    assert adapter.validate_request("get_history_orders", order_args).arguments == tuple(order_args)
    for bad in (
        [*common, "--refresh-cache"],
        [*common[:-3], "--ctx-type", "FUTURE", "--json"],
        [*common[:-3], "--end", "2027-01-01", "--json"],
    ):
        with pytest.raises(UnsafeFutuCommand):
            adapter.validate_request("get_history_order_fill_list", bad)


def test_trade_policy_yaml_is_private_strict_and_unknown_keys_fail(tmp_path):
    home = tmp_path / "home"
    config = home / "config"
    config.mkdir(parents=True, mode=0o700)
    config.chmod(0o700)
    policy = config / "trade-policy.yaml"
    policy.write_text(
        "schema_version: '1.0'\nmarket_open_rules:\n  US:\n"
        "    no_trade_minutes_after_open: 15\nreentry:\n  cooldown_minutes: 30\n"
        "frequency:\n  max_round_trips_per_symbol_per_day: 2\n  max_trades_per_day: 8\n"
        "position_management:\n  flag_add_to_loser: true\n  flag_cost_proximity_stop: false\n",
        encoding="utf-8",
    )
    policy.chmod(0o600)
    loaded = load_trade_policy(home)
    assert loaded.market_open_no_trade_minutes_by_market["US"] == 15
    assert loaded.flag_add_to_loser is True
    policy.write_text("schema_version: '1.0'\nsecret_default: 1\n", encoding="utf-8")
    with pytest.raises(TradePolicyConfigError, match="unknown"):
        load_trade_policy(home)


def test_phase3_documents_pass_strict_runtime_schemas():
    review = build_trade_review(orders=[], deals=[], start=date(2026, 8, 1), end=date(2026, 8, 27))
    validate_trade_review_document(review.to_dict())
    tampered_review = review.to_dict()
    tampered_review["metrics"]["fill_count"] = 1
    with pytest.raises(ReportSemanticError, match="trade review"):
        validate_trade_review_document(tampered_review)
    result = simulate_rebalance(
        snapshot(),
        RebalanceRequest(
            snapshot().snapshot_id,
            "USD",
            changes=(RebalanceChange("US.AAA", "AMOUNT_DELTA", Decimal("-10")),),
        ),
    )
    validate_rebalance_result_document(result.to_dict())
    tampered = result.to_dict()
    tampered["execution_disabled"] = False
    with pytest.raises(Exception):
        validate_rebalance_result_document(tampered)
    tampered = result.to_dict()
    tampered["after"]["positions"]["US.AAA"]["weight"] = "0.99"
    with pytest.raises(ReportSemanticError, match="rebalance"):
        validate_rebalance_result_document(tampered)


def test_simulate_cli_reads_confined_request_and_transactionally_saves_bundle(
    monkeypatch, tmp_path, capsys
):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    SnapshotStore(home).save("snapshots/source.json", json.dumps(snapshot().to_dict()))
    request = {
        "schema_version": "1.1",
        "snapshot_id": snapshot().snapshot_id,
        "base_currency": "USD",
        "allow_margin": False,
        "allow_short": False,
        "sell_only": False,
        "new_cash_only": False,
        "new_cash_base": "0",
        "changes": [{"symbol": "US.AAA", "operation": "AMOUNT_DELTA", "value": "-10"}],
    }
    SnapshotStore(home).save("requests/rebalance.json", json.dumps(request))
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))

    code = cli.main(
        [
            "simulate",
            "--snapshot",
            "snapshots/source.json",
            "--request",
            "requests/rebalance.json",
            "--output",
            "reports/sim-1",
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "HYPOTHETICAL"
    assert sorted(path.name for path in (home / "reports/sim-1").iterdir()) == [
        "manifest.json",
        "request.json",
        "result.json",
        "summary.md",
    ]
    result_path = home / "reports/sim-1/result.json"
    summary = (home / "reports/sim-1/summary.md").read_text()
    assert "HYPOTHETICAL" in summary
    assert "Authoritative JSON: `result.json`" in summary
    assert hashlib.sha256(result_path.read_bytes()).hexdigest() in summary


def test_trades_cli_is_no_save_by_default_and_uses_fixture_service(monkeypatch, tmp_path, capsys):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))
    monkeypatch.setattr(cli, "discover_futuapi_skill", lambda: OFFICIAL_SKILL)

    class Service:
        def __init__(self, adapter, *, aliaser):
            del adapter, aliaser

        def review(self, **kwargs):
            return build_trade_review(orders=[], deals=[], start=kwargs["start"], end=kwargs["end"])

    monkeypatch.setattr(cli, "FutuTradeHistoryService", Service)
    assert cli.main(["trades", "--from", "2026-08-01", "--to", "2026-08-27", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["motive_status"] == "UNVERIFIED"
    assert list(home.iterdir()) == []


def test_source_has_no_write_api_or_execution_path():
    source = "\n".join(
        (PROJECT_ROOT / "portfolio_analyst" / name).read_text(encoding="utf-8")
        for name in ("trade_review.py", "rebalance.py")
    )
    for forbidden in ("place_order", "modify_order", "unlock_trade", "execution_enabled"):
        assert forbidden not in source
    cli_source = (PROJECT_ROOT / "portfolio_analyst/cli.py").read_text(encoding="utf-8")
    assert 'add_parser("order"' not in cli_source
