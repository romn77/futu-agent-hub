from __future__ import annotations

import json
from datetime import datetime, timezone

from portfolio_analyst import cli
from portfolio_analyst.persistence import SnapshotStore
from portfolio_analyst.snapshot_repository import SnapshotRepository
from portfolio_analyst.settings import Settings
from test_risk_analysis import snapshot
from test_technical_analysis import Provider


def test_technical_cli_reads_one_private_snapshot_and_emits_valid_json(monkeypatch, tmp_path, capsys):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    saved = SnapshotRepository(home).save(snapshot())
    relative = saved.relative_to(home).as_posix()
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))
    monkeypatch.setattr(cli, "discover_futuapi_skill", lambda: tmp_path / "futuapi")
    monkeypatch.setattr(cli, "FutuAdapter", lambda _path: object())
    monkeypatch.setattr(cli, "FutuTechnicalHistoryProvider", lambda _adapter: Provider())

    result = cli.main(
        ["technical", "--snapshot", relative, "--symbol", "AAA", "--json", "--no-save"],
        trusted_now=datetime(2026, 8, 27, 12, tzinfo=timezone.utc),
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source_snapshot_id"] == snapshot().snapshot_id
    assert payload["technical_report_id"].startswith("ta-")
    assert payload["selection"]["selected"][0]["identity"] == "US.AAA"
    assert not list(home.rglob("technical_report.json"))


def test_technical_cli_rejects_future_snapshot_and_unheld_symbol(monkeypatch, tmp_path, capsys):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    saved = SnapshotRepository(home).save(snapshot())
    relative = saved.relative_to(home).as_posix()
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))

    result = cli.main(
        ["technical", "--snapshot", relative, "--symbol", "MISSING", "--json", "--no-save"],
        trusted_now=datetime(2026, 8, 27, 12, tzinfo=timezone.utc),
    )
    assert result == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_TECHNICAL"

    result = cli.main(
        ["technical", "--snapshot", relative, "--json", "--no-save"],
        trusted_now=datetime(2026, 8, 27, 9, tzinfo=timezone.utc),
    )
    assert result == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "FUTURE_SNAPSHOT"


def test_technical_cli_maps_invalid_private_risk_report_to_stable_input_error(
    monkeypatch, tmp_path, capsys
):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    saved = SnapshotRepository(home).save(snapshot())
    SnapshotStore(home).save("risk.json", "{}\n")
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))

    result = cli.main(
        [
            "technical",
            "--snapshot",
            saved.relative_to(home).as_posix(),
            "--risk-report",
            "risk.json",
            "--json",
            "--no-save",
        ],
        trusted_now=datetime(2026, 8, 27, 12, tzinfo=timezone.utc),
    )

    assert result == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_TECHNICAL"
