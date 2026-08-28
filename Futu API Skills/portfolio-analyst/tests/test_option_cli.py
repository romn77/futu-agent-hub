import hashlib
import json
from pathlib import Path
import shutil
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from portfolio_analyst import cli
from portfolio_analyst.persistence import SnapshotStore
from portfolio_analyst.settings import Settings
from portfolio_analyst.skill_trust import audited_files

from test_option_risk import Provider, _position, _snapshot


def _future_snapshot():
    source = _snapshot((_position("A260116C1"),))
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    body = replace(
        source,
        as_of=future,
        fx_evidence=tuple(replace(item, effective_at=future) for item in source.fx_evidence),
        snapshot_id="",
    )
    from portfolio_analyst.identity import snapshot_identity

    return replace(body, snapshot_id=snapshot_identity(body))


def _save(home: Path, positions) -> None:
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    SnapshotStore(home).save(
        "snapshots/source.json", json.dumps(_snapshot(positions).to_dict(), indent=2) + "\n"
    )


def test_options_cli_default_and_no_save_create_no_files(monkeypatch, tmp_path, capsys):
    home = tmp_path / "private"
    positions = (_position("A260116C1"),)
    _save(home, positions)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))
    monkeypatch.setattr(cli, "discover_futuapi_skill", lambda: None)
    before = sorted(path.relative_to(home).as_posix() for path in home.rglob("*"))

    assert cli.main(["options", "--snapshot", "snapshots/source.json", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "UNAVAILABLE"
    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == before

    assert (
        cli.main(
            [
                "options",
                "--snapshot",
                "snapshots/source.json",
                "--output",
                "reports/ignored",
                "--no-save",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == before


def test_options_cli_saves_atomic_digest_manifest_and_never_overwrites(
    monkeypatch, tmp_path, capsys
):
    home = tmp_path / "private"
    positions = (_position("A260116C1"),)
    source = _snapshot(positions)
    _save(home, positions)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))
    monkeypatch.setattr(cli, "discover_futuapi_skill", lambda: None)
    args = [
        "options",
        "--snapshot",
        "snapshots/source.json",
        "--output",
        "reports/options-1",
        "--json",
    ]

    assert cli.main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    bundle = home / "reports/options-1"
    assert {path.name for path in bundle.iterdir()} == {
        "manifest.json",
        "option_risk.json",
        "option_risk.md",
        "portfolio_snapshot.json",
    }
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["report_id"] == payload["option_risk_id"]
    assert manifest["source_snapshot_id"] == source.snapshot_id
    for name, evidence in manifest["artifacts"].items():
        raw = (bundle / name).read_bytes()
        assert evidence == {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}

    assert cli.main(args) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "SNAPSHOT_EXISTS"
    assert SnapshotStore(home).validate_bundle("reports/options-1")


def test_analyze_embeds_unavailable_option_report_without_changing_phase2_findings(
    monkeypatch, tmp_path, capsys
):
    home = tmp_path / "private"
    _save(home, (_position("A260116C1"),))
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))
    monkeypatch.setattr(cli, "discover_futuapi_skill", lambda: None)

    assert cli.main(["analyze", "--snapshot", "snapshots/source.json", "--json", "--no-save"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["options"]["status"] == "UNAVAILABLE"
    assert len(payload["largest_risks"]) == 3
    assert payload["overall_risk"] == payload["risk_report"]["overall_risk"]
    assert not any(
        phrase in json.dumps(payload["largest_risks"], ensure_ascii=False).lower()
        for phrase in ("buy ", "sell ", "should buy", "should sell")
    )


def test_analyze_starts_independent_option_acquisition_after_prior_phase_delay(
    monkeypatch, tmp_path, capsys
):
    home = tmp_path / "private"
    _save(home, (_position("A260116C1"),))
    provider = Provider()
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))
    monkeypatch.setattr(cli, "discover_futuapi_skill", lambda: tmp_path / "audited-skill")
    monkeypatch.setattr(
        cli, "validate_option_quote_capability", lambda _path: type("Trust", (), {"valid": True})()
    )
    monkeypatch.setattr(cli, "FutuAdapter", lambda _path: object())
    monkeypatch.setattr(cli, "FutuOptionQuoteProvider", lambda _adapter: provider)
    from portfolio_analyst import price_history

    monkeypatch.setattr(price_history, "FutuAdjustedPriceProvider", lambda _adapter: None)
    frozen_now = datetime.now(timezone.utc) - timedelta(seconds=31)

    assert (
        cli.main(
            ["analyze", "--snapshot", "snapshots/source.json", "--json", "--no-save"],
            trusted_now=frozen_now,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)["options"]

    assert provider.calls == [("US.A260116C1", 5.0)]
    analysis_now = datetime.fromisoformat(payload["analysis_now"].replace("Z", "+00:00"))
    acquisition = datetime.fromisoformat(
        payload["quote_acquisition_started_at"].replace("Z", "+00:00")
    )
    assert acquisition - analysis_now >= timedelta(seconds=30)
    assert payload["quote_evidence"][0]["status"] == "SUCCESS"


def test_standalone_options_accepts_signed_legs_but_analyze_keeps_phase2_fail_closed(
    monkeypatch, tmp_path, capsys
):
    home = tmp_path / "private"
    _save(
        home,
        (
            _position("A260116C1", quantity="2", market_value="200"),
            _position("A260116C1", quantity="-1", market_value="-100", account_ref="acct-b"),
        ),
    )
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))
    monkeypatch.setattr(cli, "discover_futuapi_skill", lambda: None)

    assert cli.main(["options", "--snapshot", "snapshots/source.json", "--json"]) == 0
    assert (
        json.loads(capsys.readouterr().out)["source_population"]["gross_market_value_base"] == "300"
    )

    assert cli.main(["analyze", "--snapshot", "snapshots/source.json", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "UNSUPPORTED_PORTFOLIO"


def test_tampered_optional_quote_digest_degrades_options_without_disabling_core(
    monkeypatch, tmp_path, capsys
):
    home = tmp_path / "private"
    _save(home, (_position("A260116C1"),))
    official = Path(__file__).parents[2] / "futuapi"
    skill = tmp_path / "futuapi"
    for record in audited_files():
        destination = skill / record.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(official / record.relative_path, destination)
    relative = Path("scripts/quote/get_option_quote.py")
    destination = skill / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(official / relative, destination)
    destination.write_text(destination.read_text() + "\n# tampered\n")
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))
    monkeypatch.setattr(cli, "discover_futuapi_skill", lambda: skill)

    assert cli.main(["options", "--snapshot", "snapshots/source.json", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "UNAVAILABLE"
    assert "QUOTE_CAPABILITY_UNAVAILABLE" in {item["code"] for item in payload["quality_reasons"]}


@pytest.mark.parametrize("command", ["options", "analyze"])
def test_future_snapshot_cli_commands_fail_closed(monkeypatch, tmp_path, capsys, command):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    SnapshotStore(home).save(
        "snapshots/future.json", json.dumps(_future_snapshot().to_dict()) + "\n"
    )
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))
    monkeypatch.setattr(cli, "discover_futuapi_skill", lambda: None)

    assert cli.main([command, "--snapshot", "snapshots/future.json", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "FUTURE_SNAPSHOT"
