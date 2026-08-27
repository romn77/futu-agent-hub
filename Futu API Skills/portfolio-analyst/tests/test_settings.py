from pathlib import Path

import pytest

from portfolio_analyst.settings import Settings, resolve_output_path


def test_data_home_defaults_outside_repository(monkeypatch, tmp_path):
    monkeypatch.delenv("PORTFOLIO_AGENT_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    settings = Settings.from_env()

    assert settings.data_home == tmp_path / ".moomoo-portfolio-agent"


def test_data_home_can_be_overridden(monkeypatch, tmp_path):
    custom_home = tmp_path / "private-state"
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(custom_home))

    assert Settings.from_env().data_home == custom_home


def test_data_home_rejects_location_inside_git_repository(monkeypatch):
    project_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(project_root / "reports"))

    with pytest.raises(ValueError, match="Git repository"):
        Settings.from_env()


def test_output_path_is_confined_to_data_home(tmp_path):
    assert resolve_output_path(tmp_path, "reports/doctor.json") == (
        tmp_path / "reports" / "doctor.json"
    )

    with pytest.raises(ValueError, match="PORTFOLIO_AGENT_HOME"):
        resolve_output_path(tmp_path, "../account.json")

    with pytest.raises(ValueError, match="PORTFOLIO_AGENT_HOME"):
        resolve_output_path(tmp_path, Path("/tmp/account.json"))
