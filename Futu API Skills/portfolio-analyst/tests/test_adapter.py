import json
from pathlib import Path
import subprocess

import pytest

from portfolio_analyst.futu_adapter import FutuAdapter, UnsafeFutuCommand


def _skill(tmp_path: Path) -> Path:
    skill = tmp_path / "futuapi"
    (skill / "scripts" / "trade").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: futuapi\n---\n", encoding="utf-8")
    for name in (
        "get_accounts.py",
        "get_portfolio.py",
        "get_all_portfolios.py",
        "place_order.py",
    ):
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


def test_json_string_values_are_recursively_text_sanitized():
    raw = json.dumps(
        {
            "error": "account_id=123456789 token=secret-token",
            "messages": ["订单 ID: ORDER-9988", "safe message"],
        }
    )

    sanitized = FutuAdapter.sanitize_output(raw)

    for secret in ("123456789", "secret-token", "ORDER-9988"):
        assert secret not in sanitized
    assert "safe message" in sanitized


def test_run_masks_official_human_account_output(monkeypatch, tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))
    raw = (
        "账户 ID: 123456789\n"
        "比赛账户名称: Private Competition Display\n"
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
        "Private",
        "Competition",
        "Display",
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


def test_run_uses_utf8_decoding_with_safe_error_replacement(monkeypatch, tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))
    observed = {}

    def fake_run(args, **kwargs):
        observed.update(kwargs)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    adapter.run("get_accounts", ["--json"])

    assert observed["encoding"] == "utf-8"
    assert observed["errors"] == "replace"
    assert "text" not in observed


def test_timeout_exception_is_rebuilt_with_sanitized_metadata(monkeypatch, tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))

    def time_out(args, **_kwargs):
        raise subprocess.TimeoutExpired(
            cmd=args,
            timeout=1,
            output=b'{"error":"account_id=123456789 token=secret-token"}',
            stderr=b"order_id=ORDER-7788 credential=private-file",
        )

    monkeypatch.setattr(subprocess, "run", time_out)

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        adapter.run("get_portfolio", ["--acc-id", "123456789", "--json"], timeout=1)

    exposed = (
        f"{caught.value.cmd} {caught.value.output} {caught.value.stdout} {caught.value.stderr}"
    )
    for secret in ("123456789", "secret-token", "ORDER-7788", "private-file"):
        assert secret not in exposed
