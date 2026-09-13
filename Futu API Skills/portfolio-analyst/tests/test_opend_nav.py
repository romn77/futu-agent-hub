"""OpenD total_assets remains authoritative through the actual snapshot/CLI path."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO

import pytest

from portfolio_analyst import cli
from portfolio_analyst.account_alias import AccountAliaser
from portfolio_analyst.current_analysis import CliResult, run_current_analysis
from portfolio_analyst.identity import snapshot_identity
from portfolio_analyst.repository import SourceDataError
from portfolio_analyst.service import generate_snapshot
from portfolio_analyst.settings import Settings
from portfolio_analyst.snapshot import UnsafeAggregationError
from portfolio_analyst.snapshot_io import InvalidSnapshot, snapshot_from_document
from portfolio_analyst.schema import validate_rebalance_result_document, validate_risk_document
from portfolio_analyst.risk import analyze_risk
from portfolio_analyst.rebalance import RebalanceChange, RebalanceRequest, simulate_rebalance
from test_risk_analysis import snapshot as legacy_snapshot
from test_snapshot_service_cli import FixtureAdapter, _configured_home


AS_OF = datetime(2026, 8, 27, 10, tzinfo=timezone.utc)


class OpenDNavAdapter(FixtureAdapter):
    def read_private_json(self, name, arguments, *, timeout=30):
        payload = super().read_private_json(name, arguments, timeout=timeout)
        if name == "get_portfolio":
            # Deliberately differs from cash plus the listed positions. The source NAV wins.
            payload["funds"]["total_assets"] = 2000
        return payload


def _source(tmp_path, adapter=None, *, account_ids=("90000001", "90000002")):
    return generate_snapshot(
        Settings(data_home=_configured_home(tmp_path)),
        adapter=adapter or OpenDNavAdapter(),
        aliaser=AccountAliaser.from_key(b"a" * 32),
        discovery_complete=True,
        as_of=AS_OF,
        account_ids=account_ids,
    )


def test_snapshot_uses_opend_total_assets_without_reconstructing_nav(tmp_path):
    source = _source(tmp_path)

    assert source.nav_base == Decimal("4000")
    assert source.cash_base == Decimal("150")
    assert source.net_exposure_base == Decimal("1100")
    assert source.cash_weight == Decimal("150") / Decimal("4000")
    assert source.positions[0].weight == Decimal("500") / Decimal("4000")
    assert source.nav_source == "OPEND_TOTAL_ASSETS"
    assert [nav.total_assets_local for nav in source.account_navs] == [Decimal("2000")] * 2
    assert snapshot_from_document(source.to_dict()).to_dict() == source.to_dict()


@pytest.mark.parametrize("total_assets", ["1234.5678901234567", "600"])
def test_single_usd_account_uses_the_exact_source_nav(tmp_path, total_assets):
    class SingleAccountAdapter(FixtureAdapter):
        def read_private_json(self, name, arguments, *, timeout=30):
            payload = super().read_private_json(name, arguments, timeout=timeout)
            if name == "get_accounts":
                payload["accounts"] = payload["accounts"][:1]
            else:
                payload["funds"]["total_assets"] = total_assets
            return payload

    source = _source(tmp_path, SingleAccountAdapter(), account_ids=None)

    assert source.nav_base == Decimal(total_assets)
    assert source.cash_base + source.net_exposure_base == Decimal("1000")
    assert source.account_navs[0].total_assets_local == source.nav_base
    assert source.account_navs[0].fx_to_base == Decimal("1")
    assert snapshot_from_document(source.to_dict()).nav_base == Decimal(total_assets)


def test_multicurrency_account_nav_only_converts_source_totals(tmp_path):
    class MixedCurrencyAdapter(OpenDNavAdapter):
        def read_private_json(self, name, arguments, *, timeout=30):
            payload = super().read_private_json(name, arguments, timeout=timeout)
            if name == "get_portfolio" and "90000002" in arguments:
                payload["funds"].update(currency="HKD", total_assets=16000)
            return payload

    source = _source(tmp_path, MixedCurrencyAdapter())

    assert source.nav_base == Decimal("4000")
    hkd_nav = source.account_navs[1]
    assert hkd_nav.currency == "HKD"
    assert hkd_nav.total_assets_local == Decimal("16000")
    assert hkd_nav.fx_to_base == Decimal("0.125")
    assert hkd_nav.total_assets_base == Decimal("2000")
    assert source.cash_base + source.net_exposure_base != source.nav_base
    assert snapshot_from_document(source.to_dict()).to_dict() == source.to_dict()


def test_risk_and_simulation_preserve_opend_nav(tmp_path):
    source = _source(tmp_path)
    risk = analyze_risk(source).to_dict()
    validate_risk_document(risk)
    assert risk["concentration"]["top_1"] == "0.125"

    result = simulate_rebalance(
        source,
        RebalanceRequest(
            source.snapshot_id,
            "USD",
            changes=(RebalanceChange("US.AAPL", "TARGET_WEIGHT", Decimal("0.15")),),
        ),
    )
    assert result.before.nav_base == result.after.nav_base == Decimal("4000")
    assert result.after.positions["US.AAPL"]["market_value_base"] == Decimal("600")
    assert result.after.cash_base == Decimal("50")
    validate_rebalance_result_document(result.to_dict())


def test_new_cash_simulation_adds_only_the_injection_to_opend_nav(tmp_path):
    source = _source(tmp_path)
    result = simulate_rebalance(
        source,
        RebalanceRequest(
            source.snapshot_id,
            "USD",
            changes=(RebalanceChange("US.AAPL", "TARGET_WEIGHT", Decimal("0.15")),),
            new_cash=Decimal("1000"),
        ),
    )

    assert result.before.nav_base == Decimal("4000")
    assert result.after.nav_base == Decimal("5000")
    assert result.after.positions["US.AAPL"]["market_value_base"] == Decimal("750")
    assert result.after.cash_base == Decimal("900")
    validate_rebalance_result_document(result.to_dict())


@pytest.mark.parametrize("invalid", [None, "N/A", True, "NaN", "Infinity"])
def test_unavailable_opend_nav_cannot_fall_back_to_cash_and_positions(tmp_path, invalid):
    class InvalidNavAdapter(FixtureAdapter):
        def read_private_json(self, name, arguments, *, timeout=30):
            payload = super().read_private_json(name, arguments, timeout=timeout)
            if name == "get_portfolio":
                payload["funds"]["total_assets"] = invalid
            return payload

    with pytest.raises(SourceDataError):
        _source(tmp_path, InvalidNavAdapter())


@pytest.mark.parametrize("account_id", ["90000001", "90000002"])
@pytest.mark.parametrize("missing_field", ["total_assets", "currency"])
def test_one_missing_account_nav_cannot_be_silently_skipped(tmp_path, account_id, missing_field):
    class MissingNavAdapter(FixtureAdapter):
        def read_private_json(self, name, arguments, *, timeout=30):
            payload = super().read_private_json(name, arguments, timeout=timeout)
            if name == "get_portfolio" and account_id in arguments:
                payload["funds"].pop(missing_field)
            return payload

    with pytest.raises(SourceDataError, match="OpenD"):
        _source(tmp_path, MissingNavAdapter())


@pytest.mark.parametrize("total_assets", [0, -100])
def test_nonpositive_source_nav_cannot_use_positive_cash_and_positions(tmp_path, total_assets):
    class NonpositiveNavAdapter(FixtureAdapter):
        def read_private_json(self, name, arguments, *, timeout=30):
            payload = super().read_private_json(name, arguments, timeout=timeout)
            if name == "get_portfolio":
                payload["funds"]["total_assets"] = total_assets
            return payload

    with pytest.raises(UnsafeAggregationError, match="OpenD total_assets is non-positive"):
        _source(tmp_path, NonpositiveNavAdapter())


@pytest.mark.parametrize(
    "tamper", ["nav", "source_total", "fx", "converted_total", "duplicate", "missing", "unknown"]
)
def test_reidentified_snapshot_still_rejects_inconsistent_nav_evidence(tmp_path, tamper):
    source = _source(tmp_path)
    first, second = source.account_navs
    changes = {
        "nav": {"nav_base": Decimal("1250")},
        "source_total": {
            "account_navs": (replace(first, total_assets_local=Decimal("1000")), second)
        },
        "fx": {"account_navs": (replace(first, fx_to_base=Decimal("2")), second)},
        "converted_total": {
            "account_navs": (replace(first, total_assets_base=Decimal("1000")), second)
        },
        "duplicate": {"account_navs": (first, first)},
        "missing": {"account_navs": (first,)},
        "unknown": {"account_navs": (replace(first, account_ref="acct-unknown"), second)},
    }
    changed = replace(source, **changes[tamper])
    changed = replace(changed, snapshot_id=snapshot_identity(changed))

    with pytest.raises(InvalidSnapshot, match="financial semantics"):
        snapshot_from_document(changed.to_dict())


def test_legacy_history_retains_its_original_body_and_nav_validation():
    source = legacy_snapshot()
    document = source.to_dict()

    assert document["schema_version"] == "1.0"
    assert "account_navs" not in document
    assert "nav_source" not in document
    assert snapshot_from_document(document).to_dict() == document

    # A source marker cannot be used to bypass the legacy snapshot's invariants.
    changed = replace(source, nav_source="OPEND_TOTAL_ASSETS")
    changed = replace(changed, snapshot_id=snapshot_identity(changed))
    with pytest.raises(InvalidSnapshot):
        snapshot_from_document(changed.to_dict())


def test_default_current_analysis_cli_accepts_and_propagates_opend_nav(tmp_path, monkeypatch):
    source = _source(tmp_path)
    home = tmp_path / "private"
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(home))
    monkeypatch.setattr(cli, "generate_snapshot", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(cli, "discover_futuapi_skill", lambda: None)

    def invoke(arguments):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = cli.main(arguments, trusted_now=AS_OF)
        return CliResult(status, stdout.getvalue(), stderr.getvalue())

    envelope = run_current_analysis(settings=Settings(home), invoke=invoke)

    assert envelope["snapshot"]["nav_base"] == "4000"
    assert envelope["risk"]["concentration"]["top_1"] == "0.125"
    assert envelope["technical"]["source_snapshot_id"] == source.snapshot_id
    assert envelope["degradations"] == []
    assert {path.name for path in home.iterdir()} == {"config"}
