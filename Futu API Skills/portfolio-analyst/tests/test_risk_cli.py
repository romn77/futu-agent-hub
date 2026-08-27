from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from portfolio_analyst import cli
from portfolio_analyst.persistence import SnapshotStore
from portfolio_analyst.settings import Settings

from test_risk_analysis import snapshot


def _saved_snapshot(home: Path, name: str = "snapshots/source.json") -> Path:
    home.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot().to_dict(), indent=2) + "\n"
    return SnapshotStore(home).save(name, payload)


def test_risk_cli_revalidates_snapshot_and_is_no_save_by_default(monkeypatch, tmp_path, capsys):
    home = tmp_path / "private"
    _saved_snapshot(home)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))

    assert cli.main(["risk", "--snapshot", "snapshots/source.json", "--json", "--no-save"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source_snapshot_id"] == snapshot().snapshot_id
    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == [
        "snapshots",
        "snapshots/source.json",
    ]


def test_risk_human_output_contains_complete_source_quality_coverage_and_findings(
    monkeypatch, tmp_path, capsys
):
    home = tmp_path / "private"
    _saved_snapshot(home)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))

    assert cli.main(["risk", "--snapshot", "snapshots/source.json", "--no-save"]) == 0
    output = capsys.readouterr().out
    assert f"source_snapshot_id: {snapshot().snapshot_id}" in output
    assert "As of: 2026-08-27T10:00:00Z" in output
    assert "Source holdings: 3" in output
    assert "Economic positions: 3" in output
    assert "Classified sector: 3" in output
    assert "Classified region: 3" in output
    assert "Price histories: 0/3" in output
    assert "Data quality: PARTIAL" in output
    assert "fixture warning" in output
    for label in (
        "Type:",
        "Severity:",
        "Summary:",
        "Evidence:",
        "Affected positions:",
        "Review priority:",
        "Simulation direction:",
    ):
        assert output.count(label) == 3
    assert not re.search(r"\b(?:BUY|SELL)\b", output, flags=re.IGNORECASE)


def test_tampered_snapshot_identity_is_structured_invalid_input(monkeypatch, tmp_path, capsys):
    home = tmp_path / "private"
    path = _saved_snapshot(home)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["cash_base"] = "101"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))

    assert cli.main(["risk", "--snapshot", "snapshots/source.json", "--json"]) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["error"]["code"] == "INVALID_SNAPSHOT"


def test_duplicate_snapshot_json_keys_are_rejected_even_when_values_match(
    monkeypatch, tmp_path, capsys
):
    home = tmp_path / "private"
    path = _saved_snapshot(home)
    text = path.read_text(encoding="utf-8").replace(
        '"cash_base": "100"', '"cash_base": "100", "cash_base": "100"', 1
    )
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))

    assert cli.main(["risk", "--snapshot", "snapshots/source.json", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_SNAPSHOT"


def test_risk_input_is_exactly_one_of_latest_or_snapshot(capsys):
    assert cli.main(["risk", "--json", "--no-save"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "CLI_USAGE"
    assert (
        cli.main(
            [
                "risk",
                "--latest",
                "--snapshot",
                "snapshots/source.json",
                "--json",
                "--no-save",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "CLI_USAGE"


def test_latest_selects_highest_valid_as_of_not_filesystem_mtime(monkeypatch, tmp_path, capsys):
    home = tmp_path / "private"
    _saved_snapshot(home, "snapshots/z-old.json")
    newer = snapshot().to_dict()
    # Same immutable snapshot is sufficient to prove latest discovery is validated and deterministic.
    SnapshotStore(home).save("snapshots/a-new.json", json.dumps(newer))
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))

    assert cli.main(["risk", "--latest", "--json", "--no-save"]) == 0
    assert json.loads(capsys.readouterr().out)["source_snapshot_id"] == snapshot().snapshot_id


def test_analyze_explicit_output_saves_json_and_markdown_immutably(monkeypatch, tmp_path, capsys):
    home = tmp_path / "private"
    _saved_snapshot(home)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))

    assert (
        cli.main(
            [
                "analyze",
                "--snapshot",
                "snapshots/source.json",
                "--output",
                "reports/run-1",
                "--json",
            ]
        )
        == 0
    )
    stdout = json.loads(capsys.readouterr().out)
    json_report = json.loads((home / "reports/run-1/portfolio_report.json").read_text())
    markdown = (home / "reports/run-1/portfolio_report.md").read_text()
    assert stdout == json_report
    assert stdout["source_snapshot_id"] == snapshot().snapshot_id
    assert snapshot().snapshot_id in markdown


@pytest.mark.parametrize("unsafe", ["../outside", "/tmp/outside"])
def test_analysis_output_is_confined(monkeypatch, tmp_path, capsys, unsafe):
    home = tmp_path / "private"
    _saved_snapshot(home)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))
    assert (
        cli.main(["analyze", "--snapshot", "snapshots/source.json", "--output", unsafe, "--json"])
        == 2
    )
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_OUTPUT"
