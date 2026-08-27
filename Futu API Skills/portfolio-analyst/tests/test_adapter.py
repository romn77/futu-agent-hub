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
    raw = "账户 ID: 123456789\n订单 ID: 99887766\n密码: should-never-escape\n"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["python"], returncode=0, stdout=raw, stderr=""
        ),
    )

    result = adapter.run("get_accounts", [])

    assert "123456789" not in result.stdout
    assert "99887766" not in result.stdout
    assert "should-never-escape" not in result.stdout
