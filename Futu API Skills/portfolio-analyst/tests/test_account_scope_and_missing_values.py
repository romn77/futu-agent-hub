"""Regressions for explicit account scope and source missing-versus-zero semantics."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
import json

import pytest

from portfolio_analyst import cli
from portfolio_analyst.account_alias import AccountAliaser
from portfolio_analyst.current_analysis import CliResult, CurrentAnalysisError, run_current_analysis
from portfolio_analyst.futu_adapter import FutuReadError
from portfolio_analyst.identity import snapshot_identity
from portfolio_analyst.rebalance import (
    RebalanceChange,
    RebalanceError,
    RebalanceRequest,
    simulate_rebalance,
)
from portfolio_analyst.repository import FutuPortfolioRepository, SourceDataError
from portfolio_analyst.reporting import build_portfolio_report
from portfolio_analyst.risk import analyze_risk
from portfolio_analyst.schema import (
    validate_portfolio_report_document,
    validate_risk_document,
    validate_technical_document,
)
from portfolio_analyst.service import generate_snapshot
from portfolio_analyst.settings import Settings
from portfolio_analyst.snapshot_io import InvalidSnapshot, snapshot_from_document
from portfolio_analyst.technical import analyze_technical
from test_repository import ACCOUNT_IDS, FixtureAdapter, _repository
from test_snapshot_service_cli import _configured_home


AS_OF = datetime(2026, 8, 27, 10, tzinfo=timezone.utc)


class MissingFieldsAdapter(FixtureAdapter):
    def __init__(self, *, cash=False, position_fields=(), invalid=None, omit=True):
        super().__init__()
        self.cash = cash
        self.position_fields = position_fields
        self.invalid = invalid
        self.omit = omit

    def read_private_json(self, name, arguments, *, timeout=30):
        payload = super().read_private_json(name, arguments, timeout=timeout)
        if name == "get_portfolio" and ACCOUNT_IDS[0] in arguments:
            targets = [(payload["funds"], "cash")] if self.cash else []
            targets.extend(
                (row, field) for row in payload["positions"] for field in self.position_fields
            )
            for row, field in targets:
                if self.omit:
                    row.pop(field, None)
                else:
                    row[field] = self.invalid
        return payload


def _source(tmp_path, adapter, *, account_ids=(ACCOUNT_IDS[0],)):
    return generate_snapshot(
        Settings(_configured_home(tmp_path)),
        adapter=adapter,
        account_ids=account_ids,
        aliaser=AccountAliaser.from_key(b"a" * 32),
        discovery_complete=True,
        as_of=AS_OF,
    )


def test_multiple_accounts_require_selection_before_any_portfolio_read():
    adapter = FixtureAdapter()

    with pytest.raises(SourceDataError, match="(?i)select"):
        _repository(adapter).read()

    assert [name for name, _args, _timeout in adapter.calls] == ["get_accounts"]


@pytest.mark.parametrize(
    ("source_field", "model_field"),
    [
        ("can_sell_qty", "can_sell_quantity"),
        ("average_cost", "average_cost_local"),
        ("nominal_price", "last_price_local"),
    ],
)
def test_missing_optional_position_field_is_unknown_not_zero(source_field, model_field):
    row = {
        "code": "US.TEST",
        "qty": "2",
        "market_val": "100",
        "can_sell_qty": "0",
        "average_cost": "0",
        "nominal_price": "0",
    }
    row.pop(source_field)
    positions = []
    FutuPortfolioRepository._parse_positions([row], "acct-test", "90000001", positions)

    assert getattr(positions[0], model_field) is None


def test_missing_cash_is_unknown_not_zero():
    cash = []
    FutuPortfolioRepository._parse_cash({"currency": "USD"}, "acct-test", cash)

    assert len(cash) == 1
    assert cash[0].amount_local is None


def test_explicit_zero_cash_remains_zero():
    cash = []
    FutuPortfolioRepository._parse_cash({"currency": "USD", "cash": 0}, "acct-test", cash)

    assert cash[0].amount_local == Decimal("0")


def test_one_eligible_account_is_selected_automatically():
    class SingleAccountAdapter(FixtureAdapter):
        def read_private_json(self, name, arguments, *, timeout=30):
            payload = super().read_private_json(name, arguments, timeout=timeout)
            if name == "get_accounts":
                payload["accounts"] = payload["accounts"][:1]
            return payload

    adapter = SingleAccountAdapter()
    result = _repository(adapter).read()

    assert len(result.accounts) == 1
    assert len(adapter.calls) == 2


@pytest.mark.parametrize("selected", [(ACCOUNT_IDS[0],), (ACCOUNT_IDS[1],), ACCOUNT_IDS])
def test_only_explicitly_selected_accounts_are_read_and_valued(tmp_path, selected):
    adapter = FixtureAdapter()
    source = _source(tmp_path, adapter, account_ids=selected)

    read_ids = [
        args[args.index("--acc-id") + 1]
        for name, args, _timeout in adapter.calls
        if name == "get_portfolio"
    ]
    assert set(read_ids) == set(selected)
    assert source.nav_base == sum(
        (Decimal("1000") if account == ACCOUNT_IDS[0] else Decimal("250") for account in selected)
    )
    assert len(source.accounts) == len(selected)
    assert source.data_quality.coverage["accounts_returned_read"] == len(selected)
    assert all(account not in json.dumps(source.to_dict()) for account in ACCOUNT_IDS)


@pytest.mark.parametrize(
    "selected",
    [
        [],
        ["secret-value"],
        ["99999999"],
        ["90000003"],
        ["90000004"],
        [ACCOUNT_IDS[0], ACCOUNT_IDS[0]],
        ACCOUNT_IDS[0],
    ],
)
def test_invalid_selection_never_reads_portfolios_or_echoes_the_identifier(selected):
    adapter = FixtureAdapter()
    with pytest.raises(SourceDataError) as caught:
        _repository(adapter).read(account_ids=selected)

    assert all(name != "get_portfolio" for name, _args, _timeout in adapter.calls)
    assert "secret-value" not in str(caught.value)
    assert not any(
        account in str(caught.value)
        for account in (*ACCOUNT_IDS, "99999999", "90000003", "90000004")
    )


def test_unselected_broken_account_cannot_block_the_selected_account(tmp_path):
    class BrokenUnselectedAdapter(FixtureAdapter):
        def read_private_json(self, name, arguments, *, timeout=30):
            if name == "get_portfolio" and ACCOUNT_IDS[1] in arguments:
                raise AssertionError("unselected portfolio must never be read")
            return super().read_private_json(name, arguments, timeout=timeout)

    assert _source(tmp_path, BrokenUnselectedAdapter()).nav_base == Decimal("1000")


@pytest.mark.parametrize("field", ["qty", "market_val"])
@pytest.mark.parametrize("invalid", [None, "N/A", "NaN"])
def test_missing_critical_position_fact_stops_the_selected_population(tmp_path, field, invalid):
    adapter = MissingFieldsAdapter(position_fields=(field,), invalid=invalid, omit=False)
    with pytest.raises(SourceDataError, match="incomplete"):
        _source(tmp_path, adapter, account_ids=ACCOUNT_IDS)


@pytest.mark.parametrize("invalid", [None, "N/A", True, "NaN", "Infinity", ""])
@pytest.mark.parametrize("field", ["cash", "can_sell_qty", "average_cost", "nominal_price"])
def test_invalid_optional_numbers_stay_unknown_without_losing_nav_or_holdings(
    tmp_path, field, invalid
):
    adapter = MissingFieldsAdapter(
        cash=field == "cash",
        position_fields=() if field == "cash" else (field,),
        invalid=invalid,
        omit=False,
    )
    source = _source(tmp_path, adapter)
    model_field = {
        "cash": "cash",
        "can_sell_qty": "can_sell_quantity",
        "average_cost": "average_cost_local",
        "nominal_price": "last_price_local",
    }[field]

    assert source.nav_base == Decimal("1000")
    assert len(source.positions) == 2
    assert model_field in source.data_quality.missing_fields
    assert source.data_quality.status == "PARTIAL"
    if field == "cash":
        assert source.cash_base is source.cash_weight is None
        assert source.cash_balances[0].amount_base is source.cash_balances[0].weight is None
    else:
        assert getattr(source.positions[0], model_field) is None
    assert snapshot_from_document(source.to_dict()).to_dict() == source.to_dict()


def test_partial_cash_population_cannot_present_its_known_subtotal_as_total(tmp_path):
    source = _source(tmp_path, MissingFieldsAdapter(cash=True), account_ids=ACCOUNT_IDS)

    assert source.nav_base == Decimal("1250")
    assert source.cash_balances[1].amount_base == Decimal("50")
    assert source.cash_balances[1].weight == Decimal("0.04")
    assert source.cash_base is source.cash_weight is None
    assert snapshot_from_document(source.to_dict()).cash_base is None


def test_missing_optional_facts_preserve_risk_report_and_local_technical_degradation(tmp_path):
    source = _source(
        tmp_path,
        MissingFieldsAdapter(
            cash=True, position_fields=("can_sell_qty", "average_cost", "nominal_price")
        ),
    )
    risk = analyze_risk(source)
    validate_risk_document(risk.to_dict())
    assert risk.to_dict()["concentration"]["top_1"] == "0.5"

    report = build_portfolio_report(source, risk)
    validate_portfolio_report_document(report.to_dict())
    assert report.to_dict()["summary"]["cash_base"] is None
    assert "Cash: Unavailable" in report.to_markdown()

    technical = analyze_technical(source, provider=None, risk_document=risk.to_dict()).to_dict()
    validate_technical_document(technical)
    assert technical["instruments"][0]["current_price"] is None


@pytest.mark.parametrize("field", ["cash", "nominal_price"])
def test_simulation_rejects_missing_required_cash_or_price_with_a_domain_error(tmp_path, field):
    source = _source(
        tmp_path,
        MissingFieldsAdapter(
            cash=field == "cash", position_fields=() if field == "cash" else (field,)
        ),
    )
    request = RebalanceRequest(
        source.snapshot_id,
        "USD",
        changes=(RebalanceChange("US.AAPL", "TARGET_WEIGHT", Decimal("0.4")),),
    )

    with pytest.raises(RebalanceError, match="unavailable"):
        simulate_rebalance(source, request)


def test_unknown_cost_and_sellable_quantity_do_not_invent_zero_or_disable_unrelated_math(tmp_path):
    source = _source(
        tmp_path, MissingFieldsAdapter(position_fields=("can_sell_qty", "average_cost"))
    )
    request = RebalanceRequest(
        source.snapshot_id,
        "USD",
        changes=(RebalanceChange("US.AAPL", "TARGET_WEIGHT", Decimal("0.4")),),
    )
    result = simulate_rebalance(source, request)

    assert result.after.nav_base == source.nav_base
    assert source.positions[0].can_sell_quantity is None
    assert source.positions[0].average_cost_local is None


@pytest.mark.parametrize(
    "tamper", ["cash_total", "known_cash_amount", "known_cash_weight", "missing_quality"]
)
def test_reidentified_missing_cash_snapshot_cannot_manufacture_facts(tmp_path, tamper):
    source = _source(tmp_path, MissingFieldsAdapter(cash=True))
    if tamper == "cash_total":
        changed = replace(source, cash_base=Decimal("0"), cash_weight=Decimal("0"))
    elif tamper == "known_cash_amount":
        changed = replace(
            source, cash_balances=(replace(source.cash_balances[0], amount_base=Decimal("0")),)
        )
    elif tamper == "known_cash_weight":
        changed = replace(
            source, cash_balances=(replace(source.cash_balances[0], weight=Decimal("0")),)
        )
    else:
        changed = replace(
            source,
            data_quality=replace(
                source.data_quality, missing_fields=(), warnings=(), status="COMPLETE"
            ),
        )
    changed = replace(changed, snapshot_id=snapshot_identity(changed))

    with pytest.raises(InvalidSnapshot):
        snapshot_from_document(changed.to_dict())


def _cli_fixture(tmp_path, monkeypatch, adapter):
    settings = Settings(_configured_home(tmp_path))
    monkeypatch.setenv("PORTFOLIO_AGENT_HOME", str(settings.data_home))
    monkeypatch.setattr(cli, "discover_futuapi_skill", lambda: None)
    monkeypatch.setattr(
        cli,
        "generate_snapshot",
        lambda settings, **kwargs: generate_snapshot(
            settings, adapter=adapter, as_of=AS_OF, **kwargs
        ),
    )

    def invoke(arguments):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = cli.main(arguments, trusted_now=AS_OF)
        return CliResult(status, stdout.getvalue(), stderr.getvalue())

    return settings, invoke


def test_default_helper_forwards_account_choice_and_keeps_missing_facts_local(
    tmp_path, monkeypatch
):
    adapter = MissingFieldsAdapter(cash=True, position_fields=("nominal_price", "average_cost"))
    settings, invoke = _cli_fixture(tmp_path, monkeypatch, adapter)
    result = run_current_analysis(settings=settings, invoke=invoke, account_ids=(ACCOUNT_IDS[0],))

    assert len(result["snapshot"]["accounts"]) == 1
    assert result["snapshot"]["nav_base"] == "1000"
    assert result["snapshot"]["cash_base"] is None
    assert result["risk"] is not None and result["technical"] is not None
    assert result["degradations"] == []
    assert {path.name for path in settings.data_home.iterdir()} == {"config"}


def test_default_helper_stops_on_ambiguous_accounts_and_cleans_up(tmp_path, monkeypatch):
    adapter = FixtureAdapter()
    settings, invoke = _cli_fixture(tmp_path, monkeypatch, adapter)
    with pytest.raises(CurrentAnalysisError, match="ACCOUNT_SELECTION_REQUIRED"):
        run_current_analysis(settings=settings, invoke=invoke)

    assert {path.name for path in settings.data_home.iterdir()} == {"config"}
    assert all(name != "get_portfolio" for name, _args, _timeout in adapter.calls)


def test_default_helper_stops_incomplete_selected_scope_without_fallback(tmp_path, monkeypatch):
    class FailingAdapter(FixtureAdapter):
        def read_private_json(self, name, arguments, *, timeout=30):
            if name == "get_portfolio" and ACCOUNT_IDS[1] in arguments:
                raise FutuReadError("selected portfolio unavailable")
            return super().read_private_json(name, arguments, timeout=timeout)

    settings, invoke = _cli_fixture(tmp_path, monkeypatch, FailingAdapter())
    with pytest.raises(CurrentAnalysisError, match="SOURCE_UNAVAILABLE"):
        run_current_analysis(settings=settings, invoke=invoke, account_ids=ACCOUNT_IDS)

    assert {path.name for path in settings.data_home.iterdir()} == {"config"}


@pytest.mark.parametrize("account_ids", [[], "90000001", ["secret"], ["90000001", "90000001"]])
def test_helper_invalid_account_selection_cannot_fall_back_to_auto_selection(tmp_path, account_ids):
    def never_invoke(_arguments):
        pytest.fail("invalid account selection must not invoke any command")

    with pytest.raises(CurrentAnalysisError, match="ACCOUNT_SELECTION_INVALID"):
        run_current_analysis(
            settings=Settings(tmp_path / "private"), invoke=never_invoke, account_ids=account_ids
        )
    assert not (tmp_path / "private").exists()


def test_helper_rejects_partial_account_coverage_before_component_calls(tmp_path):
    source = _source(tmp_path, FixtureAdapter())
    payload = source.to_dict()
    payload["data_quality"]["coverage"]["accounts_returned"] = 2

    def invoke(arguments):
        assert arguments[0] == "snapshot"
        return CliResult(0, json.dumps(payload), "")

    with pytest.raises(CurrentAnalysisError, match="incomplete"):
        run_current_analysis(settings=Settings(tmp_path / "private"), invoke=invoke)
    assert {path.name for path in (tmp_path / "private").iterdir()} == {"config"}


def test_missing_underlying_price_keeps_option_money_delta_unknown():
    from portfolio_analyst.models import AccountNAV, DataQuality
    from portfolio_analyst.option_risk import analyze_option_risk
    from portfolio_analyst.schema import validate_option_risk_document
    from test_option_risk import Clock, Provider, _position, _snapshot
    from test_option_semantics import MetadataProvider, _metadata, _stock

    option = "US.AAPL260116C00150000"
    source = _snapshot(
        (_position(option.removeprefix("US."), quantity="-1", market_value="-200"), _stock())
    )
    source = replace(
        source,
        schema_version="1.1",
        nav_source="OPEND_TOTAL_ASSETS",
        account_navs=(AccountNAV("acct-a", "USD", source.nav_base, Decimal("1"), source.nav_base),),
        positions=(source.positions[0], replace(source.positions[1], last_price_local=None)),
        data_quality=DataQuality(
            "PARTIAL",
            warnings=("last_price_local is unavailable",),
            missing_fields=("last_price_local",),
        ),
    )
    source = replace(source, snapshot_id=snapshot_identity(source))
    snapshot_from_document(source.to_dict())
    report = analyze_option_risk(
        source,
        quote_provider=Provider(),
        metadata_provider=MetadataProvider({option: _metadata(option)}),
        clock=Clock(),
    ).to_dict()

    # Source PARTIAL propagates, but the observed leg Greek remains available.
    assert report["status"] == "PARTIAL"
    assert report["greek_aggregates"]["delta"]["coverage"]["available_legs"] == 1
    assert report["leg_exposures"][0]["notional_delta_base"] is None
    assert report["leg_exposures"][0]["moneyness"] == "UNKNOWN"
    assert report["leg_exposures"][0]["relationship"]["status"] == "UNKNOWN"
    validate_option_risk_document(report)


def test_helper_command_line_passes_repeated_account_selection(monkeypatch, capsys):
    from portfolio_analyst import current_analysis

    observed = []

    def run(*, account_ids):
        observed.extend(account_ids)
        return {"ok": True}

    monkeypatch.setattr(current_analysis, "run_current_analysis", run)
    assert current_analysis.main(["--acc-id", ACCOUNT_IDS[0], "--acc-id", ACCOUNT_IDS[1]]) == 0
    assert observed == list(ACCOUNT_IDS)
    output = capsys.readouterr().out
    assert all(account not in output for account in ACCOUNT_IDS)
