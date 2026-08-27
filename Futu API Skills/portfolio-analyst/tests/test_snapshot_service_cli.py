import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_analyst import cli, service as snapshot_service
from portfolio_analyst.account_alias import AccountAliaser
from portfolio_analyst.classification import Classification, ClassificationMapper
from portfolio_analyst.fx import IdentityOnlyCurrencyConverter, StaticCurrencyConverter
from portfolio_analyst.futu_adapter import FutuAdapter, FutuReadError
from portfolio_analyst.repository import FutuPortfolioRepository, SourceDataError
from portfolio_analyst.service import (
    ConfigError,
    generate_snapshot,
    load_account_currencies,
    load_currency_converter,
)
from portfolio_analyst.settings import Settings
from portfolio_analyst.security_metadata import SecurityMetadata, SecurityMetadataMapper


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


class BaseCurrencyOnlyFixtureAdapter(FixtureAdapter):
    def read_private_json(self, name, arguments, *, timeout=30):
        payload = super().read_private_json(name, arguments, timeout=timeout)
        if name == "get_portfolio":
            payload["positions"] = [
                position for position in payload["positions"] if position["code"].startswith("US.")
            ]
        return payload


def _configured_home(tmp_path):
    home = tmp_path / "private"
    config = home / "config"
    config.mkdir(parents=True)
    home.chmod(0o700)
    shutil.copy2(FIXTURES / "fx_rates.json", config / "fx_rates.json")
    shutil.copy2(FIXTURES / "classifications.json", config / "classifications.json")
    shutil.copy2(FIXTURES / "security_metadata.json", config / "security_metadata.json")
    shutil.copy2(FIXTURES / "account_currencies.json", config / "account_currencies.json")
    config.chmod(0o700)
    (config / "account_currencies.json").chmod(0o600)
    return home


def _private_account_config(tmp_path):
    home = tmp_path / "private-config"
    config = home / "config"
    config.mkdir(parents=True, mode=0o700)
    config.chmod(0o700)
    path = config / "account_currencies.json"
    path.write_text(
        json.dumps({"provenance": "USER_CONFIG", "accounts": {"90000001": "USD"}}),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return home, config, path


@pytest.mark.skipif(os.name != "posix", reason="POSIX private-config permission contract")
def test_private_account_currency_config_requires_anchored_0700_and_0600(tmp_path):
    home, config, path = _private_account_config(tmp_path)
    assert load_account_currencies(home) == {"90000001": "USD"}

    path.chmod(0o640)
    with pytest.raises(ConfigError, match="0600"):
        load_account_currencies(home)
    path.chmod(0o600)
    config.chmod(0o750)
    with pytest.raises(ConfigError, match="0700"):
        load_account_currencies(home)


@pytest.mark.skipif(os.name != "posix", reason="POSIX private-config CLI contract")
def test_insecure_private_account_config_is_structured_cli_config_error(
    monkeypatch, tmp_path, capsys
):
    home, _config, path = _private_account_config(tmp_path)
    path.chmod(0o644)
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(home))
    monkeypatch.setattr(
        cli,
        "generate_snapshot",
        lambda settings, **_kwargs: load_account_currencies(settings.data_home),
    )

    assert cli.main(["snapshot", "--json", "--no-save"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "CONFIG_ERROR"
    assert "90000001" not in json.dumps(payload)


@pytest.mark.skipif(os.name != "posix", reason="POSIX private-config no-follow contract")
def test_private_account_currency_config_refuses_symlinks_without_leaking_ids(tmp_path):
    home, _config, path = _private_account_config(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    outside.chmod(0o600)
    path.unlink()
    path.symlink_to(outside)

    with pytest.raises(ConfigError) as caught:
        load_account_currencies(home)
    assert "90000001" not in str(caught.value)


@pytest.mark.parametrize("swap_target", ["file", "directory"])
@pytest.mark.skipif(os.name != "posix", reason="POSIX anchored swap contract")
def test_private_account_currency_config_detects_concurrent_swaps(
    tmp_path, monkeypatch, swap_target
):
    from portfolio_analyst import secure_config

    home, config, path = _private_account_config(tmp_path)
    original_read = secure_config.os.read
    swapped = False

    def read_then_swap(fd, size):
        nonlocal swapped
        chunk = original_read(fd, size)
        if chunk and not swapped:
            swapped = True
            if swap_target == "file":
                path.rename(config / "old.json")
                path.write_bytes(chunk)
                path.chmod(0o600)
            else:
                moved = home / "moved-config"
                config.rename(moved)
                outside = tmp_path / "outside-config"
                outside.mkdir(mode=0o700)
                (outside / path.name).write_bytes(chunk)
                (outside / path.name).chmod(0o600)
                config.symlink_to(outside, target_is_directory=True)
        return chunk

    monkeypatch.setattr(secure_config.os, "read", read_then_swap)
    with pytest.raises(ConfigError, match="changed") as caught:
        load_account_currencies(home)
    assert "90000001" not in str(caught.value)


def test_service_generates_schema_snapshot_from_sanitized_fixtures(tmp_path):
    home = _configured_home(tmp_path)
    snapshot = generate_snapshot(
        Settings(data_home=home),
        trd_env="REAL",
        base_currency="USD",
        adapter=FixtureAdapter(),
        aliaser=AccountAliaser.from_key(b"a" * 32),
        discovery_complete=True,
        as_of=datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc),
    )

    assert snapshot.schema_version == "1.0"
    assert snapshot.nav_base == 1250
    assert snapshot.cash_base == 150
    assert snapshot.gross_exposure_base == 1100
    assert snapshot.data_quality.status == "COMPLETE"
    assert snapshot.data_quality.coverage == {
        "accounts_returned": 2,
        "accounts_returned_read": 2,
        "discovery_status": "COMPLETE",
        "alias_provenance": "STABLE",
    }
    assert snapshot.positions[1].currency == "HKD"
    assert snapshot.positions[1].currency_provenance == "USER_CONFIG"
    assert snapshot.positions[1].realized_pl_base == 10
    assert snapshot.fx_evidence[0].source == "SANITIZED_TEST_FIXTURE"


def test_snapshot_with_production_discovery_contract_never_overclaims_complete(tmp_path):
    home = _configured_home(tmp_path)
    snapshot = generate_snapshot(
        Settings(data_home=home),
        adapter=FixtureAdapter(),
        aliaser=AccountAliaser.from_key(b"a" * 32),
        discovery_complete=False,
        as_of=datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc),
    )

    assert snapshot.data_quality.status == "PARTIAL"
    assert snapshot.data_quality.coverage["discovery_status"] == "UNVERIFIED"


def test_persisting_snapshot_creates_and_reuses_stable_private_alias_key(tmp_path):
    home = _configured_home(tmp_path)
    first = generate_snapshot(
        Settings(data_home=home),
        adapter=FixtureAdapter(),
        discovery_complete=True,
        persist_aliases=True,
        as_of=datetime(2026, 8, 27, 10, tzinfo=timezone.utc),
    )
    second = generate_snapshot(
        Settings(data_home=home),
        adapter=FixtureAdapter(),
        discovery_complete=True,
        persist_aliases=True,
        as_of=datetime(2026, 8, 27, 10, tzinfo=timezone.utc),
    )

    key = home / "config" / "account_alias.key"
    assert key.is_file()
    assert [account.account_ref for account in first.accounts] == [
        account.account_ref for account in second.accounts
    ]
    assert {account.alias_provenance for account in first.accounts} == {"STABLE"}


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


def test_missing_fx_config_loads_identity_only_converter(tmp_path):
    converter = load_currency_converter(tmp_path)

    assert isinstance(converter, IdentityOnlyCurrencyConverter)
    as_of = datetime(2026, 8, 27, tzinfo=timezone.utc)
    evidence = converter.quote("USD", "USD", as_of)
    assert evidence is not None
    assert evidence.rate == 1
    assert evidence.source == "BASE_CURRENCY_IDENTITY"
    assert converter.quote("HKD", "USD", as_of) is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-follow FX config contract")
def test_present_symlinked_fx_config_is_rejected_instead_of_treated_as_absent(tmp_path):
    home = tmp_path / "private"
    config = home / "config"
    config.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text((FIXTURES / "fx_rates.json").read_text(encoding="utf-8"), encoding="utf-8")
    (config / "fx_rates.json").symlink_to(outside)

    with pytest.raises(ConfigError, match="unsafe"):
        load_currency_converter(home)


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-follow FX config CLI contract")
def test_present_symlinked_fx_config_is_structured_cli_config_error(monkeypatch, tmp_path, capsys):
    home = tmp_path / "private"
    config = home / "config"
    config.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text((FIXTURES / "fx_rates.json").read_text(encoding="utf-8"), encoding="utf-8")
    (config / "fx_rates.json").symlink_to(outside)
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(home))
    monkeypatch.setattr(
        cli,
        "generate_snapshot",
        lambda *_args, **_kwargs: load_currency_converter(home),
    )

    assert cli.main(["snapshot", "--json", "--no-save"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "CONFIG_ERROR"


@pytest.mark.parametrize(
    "payload",
    [
        {"effective_at": "2026-08-27T00:00:00+00:00", "max_age_hours": 96, "rates": {}},
        {"source": "TEST", "max_age_hours": 96, "rates": {}},
        {
            "source": "TEST",
            "effective_at": "2026-08-27T00:00:00+00:00",
            "rates": {},
        },
    ],
)
def test_fx_config_requires_source_timestamp_and_freshness_policy(tmp_path, payload):
    config = tmp_path / "config"
    config.mkdir()
    (config / "fx_rates.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError):
        load_currency_converter(tmp_path)


@pytest.mark.parametrize("max_age_hours", [True, False, -1, 0, 97, 10**999])
def test_fx_max_age_range_and_overflow_are_config_errors(tmp_path, max_age_hours):
    config = tmp_path / "config"
    config.mkdir()
    payload = {
        "source": "TEST",
        "effective_at": "2026-08-27T00:00:00+00:00",
        "max_age_hours": max_age_hours,
        "rates": {"HKD/USD": "0.125"},
    }
    (config / "fx_rates.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match="max_age_hours"):
        load_currency_converter(tmp_path)


@pytest.mark.parametrize("source", [" ", "unknown", "N/A", "none", "NULL"])
def test_fx_config_rejects_unknown_source_sentinels(tmp_path, source):
    config = tmp_path / "config"
    config.mkdir()
    payload = {
        "source": source,
        "effective_at": "2026-08-27T00:00:00+00:00",
        "max_age_hours": 96,
        "rates": {},
    }
    (config / "fx_rates.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match="source provenance"):
        load_currency_converter(tmp_path)


def test_huge_fx_max_age_is_structured_cli_config_error(monkeypatch, tmp_path, capsys):
    home = tmp_path / "private"
    config = home / "config"
    config.mkdir(parents=True)
    payload = {
        "source": "TEST",
        "effective_at": "2026-08-27T00:00:00+00:00",
        "max_age_hours": 10**999,
        "rates": {},
    }
    (config / "fx_rates.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(home))
    monkeypatch.setattr(
        cli,
        "generate_snapshot",
        lambda *_args, **_kwargs: load_currency_converter(home),
    )

    assert cli.main(["snapshot", "--json", "--no-save"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["error"]["code"] == "CONFIG_ERROR"


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
            aliaser=AccountAliaser.from_key(b"a" * 32),
            discovery_complete=True,
            as_of=datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc),
        ),
    )

    exit_code = cli.main(["snapshot", "--trd-env", "REAL", "--base-currency", "USD", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["schema_version"] == "1.0"
    assert not (home / "snapshots").exists()


def test_absent_fx_file_base_only_cli_succeeds_without_writes_and_uses_identity_only(
    monkeypatch, tmp_path, capsys
):
    home = tmp_path / "never-created"
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(home))
    monkeypatch.setattr(
        snapshot_service,
        "load_security_metadata",
        lambda _home: SecurityMetadataMapper(
            {
                "US.AAPL": SecurityMetadata("USD", "STOCK", "USER_CONFIG"),
                "US.MSFT": SecurityMetadata("USD", "STOCK", "USER_CONFIG"),
            }
        ),
    )

    def generate_base_only(settings, **kwargs):
        return snapshot_service.generate_snapshot(
            settings,
            adapter=BaseCurrencyOnlyFixtureAdapter(),
            discovery_complete=True,
            as_of=datetime(2026, 8, 27, 10, tzinfo=timezone.utc),
            account_currency_loader=lambda _home: {
                "90000001": "USD",
                "90000002": "USD",
            },
            **kwargs,
        )

    monkeypatch.setattr(cli, "generate_snapshot", generate_base_only)

    assert cli.main(["snapshot", "--json", "--no-save"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert not home.exists()
    assert payload["fx_evidence"] == [
        {
            "source_currency": "USD",
            "target_currency": "USD",
            "rate": "1",
            "source": "BASE_CURRENCY_IDENTITY",
            "effective_at": "2026-08-27T10:00:00Z",
        }
    ]


def test_absent_fx_file_cross_currency_cli_fails_closed_structurally(monkeypatch, tmp_path, capsys):
    home = tmp_path / "never-created"
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(home))
    monkeypatch.setattr(
        snapshot_service,
        "load_security_metadata",
        lambda _home: SecurityMetadataMapper(
            {
                "US.AAPL": SecurityMetadata("USD", "STOCK", "USER_CONFIG"),
                "HK.00700": SecurityMetadata("HKD", "STOCK", "USER_CONFIG"),
                "US.MSFT": SecurityMetadata("USD", "STOCK", "USER_CONFIG"),
            }
        ),
    )

    def generate_cross_currency(settings, **kwargs):
        return snapshot_service.generate_snapshot(
            settings,
            adapter=FixtureAdapter(),
            discovery_complete=True,
            as_of=datetime(2026, 8, 27, 10, tzinfo=timezone.utc),
            account_currency_loader=lambda _home: {
                "90000001": "USD",
                "90000002": "USD",
            },
            **kwargs,
        )

    monkeypatch.setattr(cli, "generate_snapshot", generate_cross_currency)

    assert cli.main(["snapshot", "--json", "--no-save"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "UNSAFE_AGGREGATION"
    assert "HKD" in payload["error"]["message"]
    assert "USD" in payload["error"]["message"]
    assert not home.exists()


@pytest.mark.parametrize("argv", [["snapshot", "--json"], ["snapshot", "--json", "--no-save"]])
def test_first_successful_nonpersisting_snapshot_creates_no_filesystem_state(
    monkeypatch, tmp_path, capsys, argv
):
    home = tmp_path / "never-created"
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(home))
    monkeypatch.setattr(
        snapshot_service,
        "load_currency_converter",
        lambda _home: StaticCurrencyConverter(
            {("HKD", "USD"): Decimal("0.125")},
            source="TEST",
            effective_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
        ),
    )
    classifications = {
        "US.AAPL": Classification("TECH", "HARDWARE", "US", ("LARGE",), "EQUITY"),
        "HK.00700": Classification("TECH", "INTERNET", "CN", ("LARGE",), "EQUITY"),
        "US.MSFT": Classification("TECH", "SOFTWARE", "US", ("LARGE",), "EQUITY"),
    }
    metadata = {
        "US.AAPL": SecurityMetadata("USD", "STOCK", "USER_CONFIG"),
        "HK.00700": SecurityMetadata("HKD", "STOCK", "USER_CONFIG"),
        "US.MSFT": SecurityMetadata("USD", "STOCK", "USER_CONFIG"),
    }
    monkeypatch.setattr(
        snapshot_service,
        "load_classifications",
        lambda _home: ClassificationMapper(classifications),
    )
    monkeypatch.setattr(
        snapshot_service,
        "load_security_metadata",
        lambda _home: SecurityMetadataMapper(metadata),
    )

    def ephemeral_snapshot(settings, **kwargs):
        return snapshot_service.generate_snapshot(
            settings,
            adapter=FixtureAdapter(),
            discovery_complete=True,
            as_of=datetime(2026, 8, 27, 10, tzinfo=timezone.utc),
            account_currency_loader=lambda _home: {
                "90000001": "USD",
                "90000002": "USD",
            },
            **kwargs,
        )

    monkeypatch.setattr(cli, "generate_snapshot", ephemeral_snapshot)

    assert cli.main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert not home.exists()
    assert {account["alias_provenance"] for account in payload["accounts"]} == {"EPHEMERAL"}
    assert payload["data_quality"]["coverage"]["alias_provenance"] == "EPHEMERAL"
    assert any("EPHEMERAL" in item for item in payload["data_quality"]["warnings"])


def test_cli_threads_saved_intent_before_alias_creation(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(tmp_path / "private"))
    observed = {}

    def stop_after_intent(_settings, **kwargs):
        observed["persist_aliases"] = kwargs["persist_aliases"]
        raise ConfigError("expected stop")

    monkeypatch.setattr(cli, "generate_snapshot", stop_after_intent)
    assert cli.main(["snapshot", "--json", "--save"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "CONFIG_ERROR"
    assert observed["persist_aliases"] is True


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
            aliaser=AccountAliaser.from_key(b"a" * 32),
            discovery_complete=True,
            as_of=datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc),
        ),
    )

    assert cli.main(["snapshot", "--save", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    destination = home / "snapshots" / "2026" / "08" / f"{payload['snapshot_id']}.json"
    assert json.loads(destination.read_text(encoding="utf-8")) == payload
    assert not (home / "snapshots" / f"{payload['snapshot_id']}.json").exists()

    # Exact content-addressed duplicates are explicit idempotent saves, never replacements.
    assert cli.main(["snapshot", "--save", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["snapshot_id"] == payload["snapshot_id"]


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


@pytest.mark.parametrize(
    "source_error",
    [
        "get_accounts failed for account 90000001",
        '{"error":"get_accounts failed for account 90000001"}',
    ],
)
def test_snapshot_cli_source_error_redacts_account_id_before_discovery(
    monkeypatch, tmp_path, capsys, source_error
):
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(tmp_path / "private"))
    adapter = object.__new__(FutuAdapter)
    adapter._protected_sensitive_values = set()
    raw = subprocess.CompletedProcess(
        args=["python", "get_accounts.py"], returncode=1, stdout=source_error, stderr=""
    )
    monkeypatch.setattr(adapter, "_run_unsanitized", lambda *_args, **_kwargs: raw)
    monkeypatch.setattr(
        cli,
        "generate_snapshot",
        lambda *_args, **_kwargs: FutuPortfolioRepository(
            adapter,
            aliaser=AccountAliaser.from_key(b"a" * 32),
            account_currencies={},
        ).read(trd_env="REAL"),
    )

    assert cli.main(["snapshot", "--json", "--no-save"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "SOURCE_UNAVAILABLE"
    assert "90000001" not in json.dumps(payload)


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
            Settings(data_home=home),
            adapter=FixtureAdapter(),
            aliaser=AccountAliaser.from_key(b"a" * 32),
            discovery_complete=True,
            as_of=datetime.now(timezone.utc),
        ),
    )

    assert cli.main(["snapshot", "--json", "--output", "../escape.json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "INVALID_OUTPUT"
    assert not (tmp_path / "escape.json").exists()


def test_snapshot_custom_output_is_rejected_instead_of_creating_legacy_flat_history(
    monkeypatch, tmp_path, capsys
):
    home = _configured_home(tmp_path)
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(home))
    called = False

    def unexpected(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError

    monkeypatch.setattr(cli, "generate_snapshot", unexpected)
    assert cli.main(["snapshot", "--json", "--output", "snapshots/custom.json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_OUTPUT"
    assert called is False
    assert not (home / "snapshots").exists()


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


@pytest.mark.parametrize("as_json", [True, False])
def test_invalid_runtime_settings_are_dedicated_config_errors(monkeypatch, capsys, as_json):
    monkeypatch.setenv("FUTU_OPEND_PORT", "not-a-port")
    argv = ["snapshot", "--no-save"] + (["--json"] if as_json else [])

    assert cli.main(argv) == 2
    captured = capsys.readouterr()
    if as_json:
        payload = json.loads(captured.out)
        assert payload["error"]["code"] == "CONFIG_ERROR"
        assert captured.err == ""
    else:
        assert "Error [CONFIG_ERROR]" in captured.err
        assert captured.out == ""
