import json

from portfolio_analyst.doctor import run_doctor
from portfolio_analyst.settings import discover_futuapi_skill


def test_skill_discovery_prefers_explicit_environment(monkeypatch, tmp_path):
    explicit = tmp_path / "futuapi"
    explicit.mkdir()
    (explicit / "SKILL.md").write_text("---\nname: futuapi\n---\n", encoding="utf-8")
    monkeypatch.setenv("FUTUAPI_SKILL_DIR", str(explicit))

    assert discover_futuapi_skill(repo_root=tmp_path) == explicit.resolve()


def test_doctor_reports_missing_sdk_and_opend_without_faking_success(tmp_path):
    skill = tmp_path / "futuapi"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: futuapi\n---\n", encoding="utf-8")

    report = run_doctor(
        skill_dir=skill,
        sdk_probe=lambda: (False, None, "futu module is not installed"),
        opend_probe=lambda _host, _port: (False, "connection refused"),
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
