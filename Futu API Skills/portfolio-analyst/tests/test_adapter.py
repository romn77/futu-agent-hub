import json
from pathlib import Path
import shutil
import subprocess

import pytest

from portfolio_analyst import futu_adapter
from portfolio_analyst.futu_adapter import FutuAdapter, UnsafeFutuCommand
from portfolio_analyst.skill_trust import audited_files


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_SKILL = PROJECT_ROOT.parent / "futuapi"


def _skill(tmp_path: Path) -> Path:
    skill = tmp_path / "futuapi"
    for record in audited_files():
        destination = skill / record.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(OFFICIAL_SKILL / record.relative_path, destination)
    return skill


def test_adapter_exposes_only_hard_coded_read_only_scripts(tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))

    request = adapter.validate_request("get_accounts", ["--json"])

    assert request.command == "get_accounts"
    assert request.arguments == ("--json",)
    assert not hasattr(adapter, "command")

    with pytest.raises(UnsafeFutuCommand):
        adapter.validate_request("place_order", [])

    with pytest.raises(UnsafeFutuCommand):
        adapter.validate_request("../../tmp/custom", [])


@pytest.mark.parametrize(
    ("command", "arguments"),
    [
        ("get_accounts", ["--json", "--show-disabled"]),
        (
            "get_portfolio",
            [
                "--acc-id=12345",
                "--market",
                "HK",
                "--trd-env=REAL",
                "--currency",
                "USD",
                "--security-firm=FUTUINC",
                "--ctx-type",
                "FUTURE",
                "--show-option-strategy-view",
                "--json",
            ],
        ),
    ],
)
def test_each_command_accepts_its_exact_official_option_set(tmp_path, command, arguments):
    request = FutuAdapter(_skill(tmp_path)).validate_request(command, arguments)

    assert all("=" not in argument for argument in request.arguments)
    assert request.command == command


@pytest.mark.parametrize(
    "arguments",
    [
        ["--acc", "12345"],
        ["--unknown"],
        ["positional"],
        ["--market"],
        ["--market", "HK", "--market=US"],
        ["--json=true"],
        ["--trd-env", "LIVE"],
        ["--acc-id", "not-an-id"],
    ],
)
def test_argument_policy_rejects_abbreviated_unknown_missing_duplicate_and_malformed_options(
    tmp_path, arguments
):
    adapter = FutuAdapter(_skill(tmp_path))

    with pytest.raises(UnsafeFutuCommand):
        adapter.validate_request("get_portfolio", arguments)


def test_private_option_schema_is_immutable():
    with pytest.raises(TypeError):
        futu_adapter._COMMAND_OPTION_SCHEMA["alias"] = ()


def test_obsolete_all_portfolios_command_is_not_in_portfolio_allowlist(tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))

    with pytest.raises(UnsafeFutuCommand, match="allowlist"):
        adapter.validate_request("get_all_portfolios", ["--json"])


def test_read_only_real_query_never_accepts_a_password(tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))

    request = adapter.validate_request("get_portfolio", ["--trd-env", "REAL", "--json"])

    assert "REAL" in request.arguments
    assert all(
        "password" not in item.lower() and "pwd" not in item.lower() for item in request.arguments
    )

    with pytest.raises(UnsafeFutuCommand, match="credential"):
        adapter.validate_request("get_portfolio", ["--trd-env", "REAL", "--password", "secret"])


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


@pytest.mark.parametrize(
    "raw",
    [
        "failed for account 90000001; retry later",
        "failed for account id 90000001; retry later",
        "failed for account_id=90000001; retry later",
        "账户 90000001 查询失败",
        json.dumps({"error": "failed for account 90000001; retry later"}),
    ],
)
def test_unregistered_account_labelled_numeric_ids_are_redacted(raw):
    sanitized = FutuAdapter.sanitize_output(raw)

    assert "90000001" not in sanitized
    assert "[REDACTED]" in sanitized


@pytest.mark.parametrize(
    "message",
    [
        "processed account 2 of 5",
        "account count: 12",
        "retry after 30 seconds",
    ],
)
def test_ordinary_counts_are_not_redacted_as_account_identifiers(message):
    assert FutuAdapter.sanitize_output(message) == message


def test_preserved_environment_account_id_is_masked_if_a_script_echoes_it():
    sanitized = FutuAdapter.sanitize_output('{"FUTU_ACC_ID":"123456789"}')

    assert "123456789" not in sanitized


@pytest.mark.parametrize("invalid", ["abc", "0", "1234", " 12345", "12345x"])
def test_invalid_environment_account_id_fails_before_subprocess(monkeypatch, tmp_path, invalid):
    adapter = FutuAdapter(_skill(tmp_path))
    monkeypatch.setenv("FUTU_ACC_ID", invalid)

    def must_not_run(*_args, **_kwargs):
        pytest.fail("subprocess must not run for an invalid account ID")

    monkeypatch.setattr(subprocess, "run", must_not_run)

    with pytest.raises(ValueError) as caught:
        adapter.run("get_portfolio", ["--json"])

    assert invalid not in str(caught.value)


def test_known_environment_account_id_is_redacted_when_unlabeled(monkeypatch, tmp_path):
    account_id = "281756475296104250"
    adapter = FutuAdapter(_skill(tmp_path))
    monkeypatch.setenv("FUTU_ACC_ID", account_id)

    def fake_run(args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[*args, f"trace-{account_id}"],
            returncode=1,
            stdout=f"selected {account_id}\n",
            stderr=json.dumps({"message": f"failed for {account_id}"}),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = adapter.run("get_portfolio", ["--json"])

    assert account_id not in result.stdout
    assert account_id not in result.stderr
    assert account_id not in " ".join(result.args)
    assert "[REDACTED]" in f"{result.stdout} {result.stderr} {result.args}"


def test_explicit_equals_account_id_and_environment_id_are_both_redacted(monkeypatch, tmp_path):
    environment_id = "281756475296104250"
    explicit_id = "9492210"
    adapter = FutuAdapter(_skill(tmp_path))
    monkeypatch.setenv("FUTU_ACC_ID", environment_id)

    def fake_run(args, **_kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout=f"selected {explicit_id}; fallback {environment_id}",
            stderr=json.dumps({"message": f"{explicit_id} {environment_id}"}),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = adapter.run("get_portfolio", [f"--acc-id={explicit_id}", "--json"])

    assert explicit_id not in f"{result.stdout} {result.stderr} {result.args}"
    assert environment_id not in f"{result.stdout} {result.stderr} {result.args}"


@pytest.mark.parametrize(
    ("label", "value", "fragments"),
    [
        ("比赛账户名称", "Private, Competition Display", ("Private", "Competition", "Display")),
        ("api_key", "Alpha, Key Material", ("Alpha", "Key Material")),
        ("api-key", "hyphen, key material", ("hyphen", "key material")),
        ("secret", "deep, private value", ("deep", "private value")),
        ("access_token", "Gamma, Delta Value", ("Gamma", "Delta Value")),
        ("password_hash", "Omega, Sigma Value", ("Omega", "Sigma Value")),
        ("client_secret", "Theta, Private Value", ("Theta", "Private Value")),
        ("trading-password", "Lambda, Hidden Value", ("Lambda", "Hidden Value")),
    ],
)
def test_plain_text_sensitive_labels_over_redact_to_end_of_line(label, value, fragments):
    sanitized = FutuAdapter.sanitize_output(f"{label}: {value}\nstatus: safe")

    assert all(fragment not in sanitized for fragment in fragments)
    assert "status: safe" in sanitized


@pytest.mark.parametrize(
    "label",
    [
        "api_key",
        "api-key",
        "secret",
        "client_secret",
        "refresh-token",
        "auth_token",
        "password_file",
        "trading-password",
    ],
)
def test_json_scalar_sensitive_labels_are_fully_redacted(label):
    raw = json.dumps({"error": f"{label}=Alpha, Beta Credential"})

    sanitized = FutuAdapter.sanitize_output(raw)

    assert "Alpha" not in sanitized
    assert "Beta Credential" not in sanitized


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
        observed["args"] = args
        observed.update(kwargs)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    adapter.run("get_accounts", ["--json"])

    assert observed["encoding"] == "utf-8"
    assert observed["errors"] == "replace"
    assert "text" not in observed


def test_run_uses_controlled_environment_without_credentials_or_python_injection(
    monkeypatch, tmp_path
):
    adapter = FutuAdapter(_skill(tmp_path))
    safe_parent = {
        "PATH": "/runtime/bin",
        "HOME": "/runtime/home",
        "LANG": "C.UTF-8",
        "TMPDIR": "/runtime/tmp",
        "SYSTEMROOT": "C:\\Windows",
        "FUTU_OPEND_HOST": "192.0.2.10",
        "FUTU_OPEND_PORT": "22222",
        "FUTU_TRD_ENV": "REAL",
        "FUTU_DEFAULT_MARKET": "HK",
        "FUTU_SECURITY_FIRM": "FUTUSECURITIES",
        "FUTU_ACC_ID": "123456789",
    }
    unsafe_parent = {
        "FUTU_LOGIN_ACCOUNT": "private-login",
        "FUTU_LOGIN_PWD": "trade-password",
        "PASSWORD": "generic-password",
        "API_KEY": "generic-api-key",
        "ACCESS_TOKEN": "generic-token",
        "CLIENT_SECRET": "generic-secret",
        "PYTHONPATH": "/tmp/injected-code",
        "PYTHONHOME": "/tmp/injected-runtime",
        "LD_PRELOAD": "/tmp/injected.so",
    }
    for key, value in {**safe_parent, **unsafe_parent}.items():
        monkeypatch.setenv(key, value)
    observed = {}

    def fake_run(args, **kwargs):
        observed["args"] = args
        observed.update(kwargs)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    adapter.run("get_accounts", ["--json"])

    child_env = observed["env"]
    for key, value in safe_parent.items():
        assert child_env[key] == value
    assert child_env["PYTHONIOENCODING"] == "utf-8"
    assert child_env["PYTHONUTF8"] == "1"
    assert child_env["PYTHONNOUSERSITE"] == "1"
    assert unsafe_parent.keys().isdisjoint(child_env)
    assert observed["args"][1] == "-s"


def test_timeout_exception_is_rebuilt_with_sanitized_metadata(monkeypatch, tmp_path):
    adapter = FutuAdapter(_skill(tmp_path))
    monkeypatch.setenv("FUTU_LOGIN_PWD", "parent-trade-password")
    monkeypatch.setenv("FUTU_ACC_ID", "281756475296104250")
    observed = {}

    def time_out(args, **kwargs):
        observed.update(kwargs)
        raise subprocess.TimeoutExpired(
            cmd=args,
            timeout=1,
            output=b'{"error":"account_id=123456789 token=secret-token"}',
            stderr=b"order_id=ORDER-7788 credential=private-file account 281756475296104250",
        )

    monkeypatch.setattr(subprocess, "run", time_out)

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        adapter.run("get_portfolio", ["--acc-id", "123456789", "--json"], timeout=1)

    exposed = (
        f"{caught.value.cmd} {caught.value.output} {caught.value.stdout} {caught.value.stderr}"
    )
    for secret in (
        "123456789",
        "281756475296104250",
        "secret-token",
        "ORDER-7788",
        "private-file",
    ):
        assert secret not in exposed
    assert "FUTU_LOGIN_PWD" not in observed["env"]
    assert "parent-trade-password" not in exposed


def test_explicit_account_id_is_redacted_from_unlabeled_timeout_buffers(monkeypatch, tmp_path):
    explicit_id = "9492210"
    adapter = FutuAdapter(_skill(tmp_path))

    def time_out(args, **_kwargs):
        raise subprocess.TimeoutExpired(
            cmd=[*args, f"trace-{explicit_id}"],
            timeout=1,
            output=f"waiting for {explicit_id}",
            stderr=json.dumps({"message": explicit_id}),
        )

    monkeypatch.setattr(subprocess, "run", time_out)

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        adapter.run("get_portfolio", ["--acc-id", explicit_id, "--json"], timeout=1)

    exposed = f"{caught.value.cmd} {caught.value.output} {caught.value.stderr}"
    assert explicit_id not in exposed


def test_adapter_revalidates_skill_before_each_command(tmp_path):
    skill = _skill(tmp_path)
    adapter = FutuAdapter(skill)
    script = skill / "scripts/trade/get_accounts.py"
    script.unlink()
    outside = tmp_path / "swapped.py"
    outside.write_text("print('swapped')\n", encoding="utf-8")
    script.symlink_to(outside)

    with pytest.raises(ValueError, match="escapes Skill directory"):
        adapter.run("get_accounts", ["--json"])
