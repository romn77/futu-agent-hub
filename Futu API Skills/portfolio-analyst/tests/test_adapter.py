from pathlib import Path
import subprocess

import pytest

from portfolio_analyst.futu_adapter import FutuAdapter, UnsafeFutuCommand


def _skill(tmp_path: Path) -> Path:
    skill = tmp_path / "futuapi"
    (skill / "scripts" / "trade").mkdir(parents=True)
    for name in ("get_accounts.py", "get_portfolio.py", "place_order.py"):
        (skill / "scripts" / "trade" / name).write_text("print('{}')\n", encoding="utf-8")
    return skill


def test_adapter_exposes_only_hard_coded_read_only_scripts(tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))

    assert adapter.command("get_accounts", ["--json"]) == [
        adapter.python_executable,
        str(adapter.skill_dir / "scripts" / "trade" / "get_accounts.py"),
        "--json",
    ]

    with pytest.raises(UnsafeFutuCommand):
        adapter.command("place_order", [])

    with pytest.raises(UnsafeFutuCommand):
        adapter.command("../../tmp/custom", [])


def test_read_only_real_query_never_accepts_a_password(tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))

    command = adapter.command("get_portfolio", ["--trd-env", "REAL", "--json"])

    assert "REAL" in command
    assert all("password" not in item.lower() and "pwd" not in item.lower() for item in command)

    with pytest.raises(UnsafeFutuCommand, match="credential"):
        adapter.command("get_portfolio", ["--trd-env", "REAL", "--password", "secret"])


def test_run_masks_sensitive_json_fields_in_stdout_and_stderr(monkeypatch, tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))
    raw_stdout = (
        '{"accounts":[{"accountId":123456789,"card_num":"998877",'
        '"competition_acc_name":"private-name"}],'
        '"order_id":"ORDER-123","nested":{"password":"trade-secret"}}'
    )
    raw_stderr = "account_id=987654321 token=secret-token"

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["python"], returncode=0, stdout=raw_stdout, stderr=raw_stderr
        ),
    )

    result = adapter.run("get_accounts", ["--json"])

    for secret in (
        "123456789",
        "998877",
        "private-name",
        "ORDER-123",
        "trade-secret",
        "987654321",
        "secret-token",
    ):
        assert secret not in result.stdout
        assert secret not in result.stderr
    assert "[REDACTED]" in result.stdout
    assert "[REDACTED]" in result.stderr


def test_run_masks_official_human_account_output(monkeypatch, tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))
    raw = (
        "账户 ID: 123456789\n"
        "比赛账户名称: private-competition\n"
        "账户 88776655 | REAL | MARGIN\n"
        "account_name: private-account\n"
        "competition_acc_name=private-api-name\n"
        "订单 ID: 99887766\n"
        "密码: should-never-escape\n"
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["python"], returncode=0, stdout=raw, stderr=""
        ),
    )

    result = adapter.run("get_accounts", [])

    for secret in (
        "123456789",
        "private-competition",
        "88776655",
        "private-account",
        "private-api-name",
        "99887766",
        "should-never-escape",
    ):
        assert secret not in result.stdout


def test_run_keeps_invocation_args_but_redacts_returned_process_args(monkeypatch, tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))
    observed = {}

    def fake_run(args, **_kwargs):
        observed["args"] = args
        exposed_args = [
            *args,
            "--order-id=ORDER-7788",
            "--api-token",
            "token-secret",
            "--credential-file",
            "/private/trading.env",
        ]
        return subprocess.CompletedProcess(args=exposed_args, returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = adapter.run("get_portfolio", ["--acc-id", "123456789", "--json"])

    assert observed["args"][-3:] == ["--acc-id", "123456789", "--json"]
    exposed = " ".join(result.args)
    for secret in ("123456789", "ORDER-7788", "token-secret", "/private/trading.env"):
        assert secret not in exposed
    assert exposed.count("[REDACTED]") == 4
