import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from portfolio_analyst import cli
from portfolio_analyst.service import ConfigError, generate_snapshot, load_currency_converter
from portfolio_analyst.settings import Settings


FIXTURES = Path(__file__).parent / "fixtures"


class FixtureAdapter:
    def read_private_json(self, name, arguments, *, timeout=30):
        del arguments, timeout
        fixture = "accounts.json" if name == "get_accounts" else "portfolios.json"
        return json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))


def _configured_home(tmp_path):
    home = tmp_path / "private"
    config = home / "config"
    config.mkdir(parents=True)
    shutil.copy2(FIXTURES / "fx_rates.json", config / "fx_rates.json")
    shutil.copy2(FIXTURES / "classifications.json", config / "classifications.json")
    return home


def test_service_generates_schema_snapshot_from_sanitized_fixtures(tmp_path):
    home = _configured_home(tmp_path)
    snapshot = generate_snapshot(
        Settings(data_home=home),
        trd_env="REAL",
        base_currency="USD",
        adapter=FixtureAdapter(),
        as_of=datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc),
    )

    assert snapshot.schema_version == "1.0"
    assert snapshot.nav_base == 1350
    assert snapshot.cash_base == 250
    assert snapshot.gross_exposure_base == 1100
    assert snapshot.data_quality.status == "COMPLETE"
    assert snapshot.data_quality.coverage == {"accounts_expected": 2, "accounts_read": 2}


def test_missing_fx_config_fails_closed_instead_of_inventing_rates(tmp_path):
    with pytest.raises(ConfigError, match="fx_rates.json"):
        load_currency_converter(tmp_path)


def test_snapshot_cli_json_defaults_to_no_save(monkeypatch, tmp_path, capsys):
    home = _configured_home(tmp_path)
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(home))
    monkeypatch.setattr(
        cli,
        "generate_snapshot",
        lambda *_args, **_kwargs: generate_snapshot(
            Settings(data_home=home),
            trd_env="REAL",
            base_currency="USD",
            adapter=FixtureAdapter(),
            as_of=datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc),
        ),
    )

    exit_code = cli.main(["snapshot", "--trd-env", "REAL", "--base-currency", "USD", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["schema_version"] == "1.0"
    assert not (home / "snapshots").exists()


def test_snapshot_cli_save_uses_immutable_default_name(monkeypatch, tmp_path, capsys):
    home = _configured_home(tmp_path)
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(home))
    monkeypatch.setattr(
        cli,
        "generate_snapshot",
        lambda *_args, **_kwargs: generate_snapshot(
            Settings(data_home=home),
            trd_env="REAL",
            base_currency="USD",
            adapter=FixtureAdapter(),
            as_of=datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc),
        ),
    )

    assert cli.main(["snapshot", "--save", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    destination = home / "snapshots" / f"{payload['snapshot_id']}.json"
    assert json.loads(destination.read_text(encoding="utf-8")) == payload

    assert cli.main(["snapshot", "--save", "--json"]) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["ok"] is False
    assert error["error"]["code"] == "SNAPSHOT_EXISTS"


def test_snapshot_cli_json_errors_are_structured_and_stdout_stays_machine_readable(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(tmp_path / "private"))
    monkeypatch.setattr(
        cli,
        "generate_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConfigError("FX unavailable")),
    )

    exit_code = cli.main(["snapshot", "--json", "--no-save"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload == {"ok": False, "error": {"code": "CONFIG_ERROR", "message": "FX unavailable"}}


def test_unexpected_snapshot_failure_is_generic_and_does_not_leak_source_data(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(tmp_path / "private"))
    monkeypatch.setattr(
        cli,
        "generate_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("unexpected raw account 90000001")
        ),
    )

    assert cli.main(["snapshot", "--json", "--no-save"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "ok": False,
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "snapshot failed without exposing source details",
        },
    }


def test_output_escape_is_rejected_as_structured_json(monkeypatch, tmp_path, capsys):
    home = _configured_home(tmp_path)
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(home))
    monkeypatch.setattr(
        cli,
        "generate_snapshot",
        lambda *_args, **_kwargs: generate_snapshot(
            Settings(data_home=home), adapter=FixtureAdapter(), as_of=datetime.now(timezone.utc)
        ),
    )

    assert cli.main(["snapshot", "--json", "--output", "../escape.json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "INVALID_OUTPUT"
    assert not (tmp_path / "escape.json").exists()
