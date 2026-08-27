import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from portfolio_analyst import cli
from portfolio_analyst.futu_adapter import FutuReadError
from portfolio_analyst.repository import SourceDataError
from portfolio_analyst.service import ConfigError, generate_snapshot, load_currency_converter
from portfolio_analyst.settings import Settings


FIXTURES = Path(__file__).parent / "fixtures"


class FixtureAdapter:
    def protect_sensitive_values(self, values):
        self.protected = tuple(values)

    def read_private_json(self, name, arguments, *, timeout=30):
        del timeout
        if name == "get_accounts":
            return json.loads((FIXTURES / "accounts.json").read_text(encoding="utf-8"))
        account_id = arguments[arguments.index("--acc-id") + 1]
        number = {"90000001": 1, "90000002": 2}[account_id]
        return json.loads(
            (FIXTURES / f"portfolio_account_{number}.json").read_text(encoding="utf-8")
        )


def _configured_home(tmp_path):
    home = tmp_path / "private"
    config = home / "config"
    config.mkdir(parents=True)
    shutil.copy2(FIXTURES / "fx_rates.json", config / "fx_rates.json")
    shutil.copy2(FIXTURES / "classifications.json", config / "classifications.json")
    shutil.copy2(FIXTURES / "security_metadata.json", config / "security_metadata.json")
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
    assert snapshot.nav_base == 1250
    assert snapshot.cash_base == 150
    assert snapshot.gross_exposure_base == 1100
    assert snapshot.data_quality.status == "COMPLETE"
    assert snapshot.data_quality.coverage == {"accounts_expected": 2, "accounts_read": 2}
    assert snapshot.positions[1].currency == "HKD"
    assert snapshot.positions[1].currency_provenance == "USER_CONFIG"
    assert snapshot.positions[1].realized_pl_base == 10


def test_service_stops_when_no_discovered_account_portfolio_was_read(tmp_path):
    home = _configured_home(tmp_path)

    class UnavailableAdapter(FixtureAdapter):
        def read_private_json(self, name, arguments, *, timeout=30):
            if name == "get_portfolio":
                raise FutuReadError("unavailable")
            return super().read_private_json(name, arguments, timeout=timeout)

    with pytest.raises(SourceDataError, match="no account portfolio data"):
        generate_snapshot(
            Settings(data_home=home),
            adapter=UnavailableAdapter(),
            as_of=datetime.now(timezone.utc),
        )


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


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "snapshot", "--trd-env", "LIVE"],
        ["snapshot", "--trd-env", "LIVE", "--json"],
        ["snapshot", "--base-currency", "--json"],
        ["snapshot", "--unknown", "90000001", "--json"],
    ],
)
def test_json_requested_parse_failures_emit_only_structured_json(argv, capsys):
    assert cli.main(argv) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload == {
        "ok": False,
        "error": {"code": "CLI_USAGE", "message": "invalid command-line arguments"},
    }
    assert captured.err == ""
    assert "90000001" not in captured.out


@pytest.mark.parametrize(
    "argv",
    [["--json", "snapshot", "--no-save"], ["snapshot", "--json", "--no-save"]],
)
def test_json_option_is_accepted_before_or_after_subcommand(monkeypatch, tmp_path, capsys, argv):
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(tmp_path / "private"))
    monkeypatch.setattr(
        cli,
        "generate_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConfigError("expected test stop")),
    )

    assert cli.main(argv) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "CONFIG_ERROR"
