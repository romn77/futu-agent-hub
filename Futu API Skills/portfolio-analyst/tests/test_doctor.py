import json
import socket
import subprocess
from types import SimpleNamespace

import pytest

from portfolio_analyst import doctor
from portfolio_analyst.doctor import probe_opend, probe_sdk, run_doctor
from portfolio_analyst.settings import discover_futuapi_skill


def _trusted_skill(path):
    (path / "scripts" / "trade").mkdir(parents=True)
    (path / "SKILL.md").write_text("---\nname: futuapi\n---\n", encoding="utf-8")
    for name in ("get_accounts.py", "get_portfolio.py", "get_all_portfolios.py"):
        (path / "scripts" / "trade" / name).write_text("print('{}')\n", encoding="utf-8")
    return path


def test_skill_discovery_prefers_explicit_environment(monkeypatch, tmp_path):
    explicit = _trusted_skill(tmp_path / "futuapi")
    monkeypatch.setenv("FUTUAPI_SKILL_DIR", str(explicit))

    assert discover_futuapi_skill(repo_root=tmp_path) == explicit.resolve()


def test_invalid_skill_override_falls_back_to_repository(monkeypatch, tmp_path):
    fallback = _trusted_skill(tmp_path / "Futu API Skills" / "futuapi")
    monkeypatch.setenv("FUTUAPI_SKILL_DIR", str(tmp_path / "missing"))

    assert discover_futuapi_skill(repo_root=tmp_path, cwd=tmp_path) == fallback.resolve()


def test_sdk_probe_rejects_broken_import(monkeypatch):
    def broken_import(_name):
        raise RuntimeError("native dependency failed")

    monkeypatch.setattr(doctor.importlib, "import_module", broken_import)

    module, sdk_version, detail = probe_sdk()

    assert module is None
    assert sdk_version is None
    assert "import failed" in detail


def test_opend_probe_rejects_unrelated_tcp_listener_when_handshake_fails():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    host, port = listener.getsockname()

    class NotOpenDContext:
        def __init__(self, host, port):
            self.connection = socket.create_connection((host, port), timeout=0.5)

        def get_global_state(self):
            return -1, "not an OpenD protocol endpoint"

        def close(self):
            self.connection.close()

    fake_sdk = SimpleNamespace(OpenQuoteContext=NotOpenDContext, RET_OK=0)
    try:
        available, detail = doctor.perform_opend_handshake(fake_sdk, host, port)
    finally:
        listener.close()

    assert available is False
    assert "handshake failed" in detail


def test_opend_probe_has_bounded_timeout(monkeypatch):
    def time_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["worker"], timeout=2)

    monkeypatch.setattr(doctor.subprocess, "run", time_out)

    available, detail = probe_opend(SimpleNamespace(), "127.0.0.1", 11111)

    assert available is False
    assert "deadline exceeded" in detail


def test_opend_probe_parses_worker_success_with_utf8_and_deadline(monkeypatch):
    observed = {}

    def completed(args, **kwargs):
        observed["args"] = args
        observed.update(kwargs)
        output = 'SDK log\n{"available": true, "detail": "verified quote handshake"}\n'
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", completed)

    available, detail = probe_opend(SimpleNamespace(), "127.0.0.1", 11111)

    assert available is True
    assert detail == "verified quote handshake"
    assert observed["args"][1:3] == ["-m", "portfolio_analyst.opend_probe_worker"]
    assert observed["timeout"] == doctor.OPEND_PROBE_TIMEOUT_SECONDS
    assert observed["encoding"] == "utf-8"
    assert observed["errors"] == "replace"


def test_successful_handshake_with_close_failure_is_unavailable():
    class Context:
        def get_global_state(self):
            return 0, {"market_hk": "OPEN"}

        def close(self):
            raise RuntimeError("close failed")

    fake_sdk = SimpleNamespace(OpenQuoteContext=lambda **_kwargs: Context(), RET_OK=0)

    available, detail = doctor.perform_opend_handshake(fake_sdk, "127.0.0.1", 11111)

    assert available is False
    assert "cleanup failed" in detail


def test_doctor_reports_missing_sdk_and_opend_without_faking_success(tmp_path):
    skill = _trusted_skill(tmp_path / "futuapi")

    report = run_doctor(
        skill_dir=skill,
        sdk_probe=lambda: (None, None, "futu module is not installed"),
        opend_probe=lambda *_args: pytest.fail("OpenD must not be claimed without a working SDK"),
    )

    assert report["ok"] is False
    assert report["checks"]["futuapi_skill"]["available"] is True
    assert report["checks"]["sdk"]["available"] is False
    assert report["checks"]["opend"]["available"] is False


def test_cli_doctor_json_supports_no_save(monkeypatch, tmp_path, capsys):
    data_home = tmp_path / "private-state"
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(data_home))

    from portfolio_analyst import cli

    expected = {
        "ok": False,
        "checks": {
            "futuapi_skill": {"available": True, "detail": "verified"},
            "sdk": {"available": False, "detail": "missing"},
            "opend": {"available": False, "detail": "not checked"},
        },
    }
    monkeypatch.setattr(cli, "run_doctor", lambda **_kwargs: expected)

    exit_code = cli.main(["doctor", "--json", "--output", "doctor.json", "--no-save"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload == expected
    assert not (data_home / "doctor.json").exists()
