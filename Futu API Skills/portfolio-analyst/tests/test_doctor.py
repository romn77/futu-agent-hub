import json
import socket
from types import SimpleNamespace

import pytest

from portfolio_analyst import doctor
from portfolio_analyst.doctor import probe_opend, probe_sdk, run_doctor
from portfolio_analyst.settings import discover_futuapi_skill


def test_skill_discovery_prefers_explicit_environment(monkeypatch, tmp_path):
    explicit = tmp_path / "futuapi"
    explicit.mkdir()
    (explicit / "SKILL.md").write_text("---\nname: futuapi\n---\n", encoding="utf-8")
    monkeypatch.setenv("FUTUAPI_SKILL_DIR", str(explicit))

    assert discover_futuapi_skill(repo_root=tmp_path) == explicit.resolve()


def test_invalid_skill_override_falls_back_to_repository(monkeypatch, tmp_path):
    fallback = tmp_path / "Futu API Skills" / "futuapi"
    fallback.mkdir(parents=True)
    (fallback / "SKILL.md").write_text("---\nname: futuapi\n---\n", encoding="utf-8")
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
        available, detail = probe_opend(fake_sdk, host, port)
    finally:
        listener.close()

    assert available is False
    assert "handshake failed" in detail


def test_doctor_reports_missing_sdk_and_opend_without_faking_success(tmp_path):
    skill = tmp_path / "futuapi"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: futuapi\n---\n", encoding="utf-8")

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
    skill = tmp_path / "futuapi"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: futuapi\n---\n", encoding="utf-8")
    monkeypatch.setenv("FUTUAPI_SKILL_DIR", str(skill))
    data_home = tmp_path / "private-state"
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(data_home))

    from portfolio_analyst.cli import main

    exit_code = main(["doctor", "--json", "--output", "doctor.json", "--no-save"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code in (0, 1)
    assert set(payload["checks"]) == {"futuapi_skill", "sdk", "opend"}
    assert not (data_home / "doctor.json").exists()
