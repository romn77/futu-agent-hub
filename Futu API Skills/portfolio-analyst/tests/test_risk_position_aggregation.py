from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from portfolio_analyst.risk import RiskInputError, aggregate_economic_positions, analyze_risk
from portfolio_analyst.reporting import build_portfolio_report
from portfolio_analyst.identity import snapshot_identity
from portfolio_analyst.persistence import SnapshotStore
from portfolio_analyst.settings import Settings
from portfolio_analyst import cli
import json

from test_risk_analysis import snapshot


def test_duplicate_account_holdings_are_one_economic_position_for_top_n_and_hhi():
    source = snapshot()
    first = replace(source.positions[0], weight=Decimal("0.25"), market_value_base=Decimal("250"))
    duplicate = replace(
        first,
        account_ref="acct-other",
        weight=Decimal("0.15"),
        market_value_base=Decimal("150"),
    )
    report = analyze_risk(
        replace(source, positions=(first, duplicate, source.positions[1], source.positions[2]))
    )

    assert report.concentration.top_1 == Decimal("0.4")
    assert report.concentration.top_3 == Decimal("0.9")
    assert report.concentration.hhi == Decimal("0.29")
    assert report.top_risks[0].affected_positions[0] == "US.AAA"
    portfolio = build_portfolio_report(
        replace(source, positions=(first, duplicate, source.positions[1], source.positions[2])),
        report,
    ).to_dict()
    assert portfolio["summary"]["position_count"] == 3
    assert portfolio["summary"]["top_holdings"][0] == {
        "symbol": "US.AAA",
        "weight": "0.4",
        "market_value_base": "400",
    }


def test_same_bare_ticker_in_different_markets_stays_distinct_and_requests_two_series():
    source = snapshot()
    positions = (
        replace(source.positions[0], symbol="DUP", market="US"),
        replace(source.positions[1], symbol="DUP", market="HK"),
        source.positions[2],
    )

    class RecordingProvider:
        def __init__(self):
            self.calls = []

        def history(self, symbol, *, market, lookback, as_of):
            del lookback, as_of
            self.calls.append((market, symbol))
            return None

    provider = RecordingProvider()
    report = analyze_risk(replace(source, positions=positions), price_provider=provider)

    assert report.concentration.top_1 == Decimal("0.4")
    assert report.concentration.hhi == Decimal("0.29")
    assert set(provider.calls) == {("US", "DUP"), ("HK", "DUP"), ("US", "CCC")}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("currency", "HKD"),
        ("currency_provenance", "AUDITED_SOURCE"),
        ("sector", "ENERGY"),
        ("region", "EU"),
        ("themes", ("OTHER",)),
        ("asset_class", "BOND"),
        ("security_type", "ETF"),
        ("position_role", "DEFENSIVE"),
    ],
)
def test_duplicate_economic_position_conflicting_metadata_fails_closed(field, value):
    source = snapshot()
    original = replace(source.positions[0], position_role="OFFENSIVE")
    conflict = replace(original, account_ref="acct-other", **{field: value})

    with pytest.raises(RiskInputError, match="conflicting metadata"):
        analyze_risk(replace(source, positions=(original, conflict, *source.positions[1:])))


@pytest.mark.parametrize(
    "position",
    [
        replace(snapshot().positions[0], market=""),
        replace(snapshot().positions[0], symbol=""),
        replace(snapshot().positions[0], symbol="US.AAA"),
        replace(snapshot().positions[0], symbol="AAA."),
        replace(snapshot().positions[0], symbol="A..B"),
    ],
)
def test_unqualified_or_ambiguous_security_identity_fails_closed(position):
    source = snapshot()
    with pytest.raises(RiskInputError, match="market-qualified"):
        analyze_risk(replace(source, positions=(position, *source.positions[1:])))


def test_duplicate_holding_pnl_does_not_enter_current_structure_findings_before_phase_5():
    source = snapshot()
    first = replace(source.positions[0], unrealized_pl_base=Decimal("40"))
    duplicate = replace(
        first,
        account_ref="acct-other",
        weight=Decimal("0.1"),
        market_value_base=Decimal("100"),
        unrealized_pl_base=Decimal("50"),
    )
    other = replace(source.positions[1], unrealized_pl_base=Decimal("10"))
    report = analyze_risk(replace(source, positions=(first, duplicate, other)))

    assert "PNL_CONCENTRATION" not in {item.type for item in report.top_risks}


@pytest.mark.parametrize(
    "sentinel", ["", "   ", "UNKNOWN", " unknown ", "N/A", "n/a", "NONE", "null"]
)
def test_risk_aggregation_canonicalizes_classification_unknown_sentinels(sentinel):
    source = snapshot()
    position = replace(
        source.positions[0],
        sector=sentinel,
        region=sentinel,
        themes=(sentinel,),
    )

    report = analyze_risk(replace(source, positions=(position,)))

    assert report.exposures.sector == {"UNKNOWN": position.weight}
    assert report.exposures.region == {"UNKNOWN": position.weight}
    assert report.exposures.theme_full == {"UNKNOWN": position.weight}
    assert report.exposures.theme_split == {"UNKNOWN": position.weight}


def test_risk_aggregation_removes_sentinel_themes_and_deduplicates_canonical_themes():
    source = snapshot()
    position = replace(
        source.positions[0],
        themes=(" AI ", "AI", " ", "unknown", "N/A", "none", "NULL"),
    )

    economic = aggregate_economic_positions(replace(source, positions=(position,)))
    report = analyze_risk(replace(source, positions=(position,)))

    assert economic[0].themes == ("AI",)
    assert report.exposures.theme_full == {"AI": position.weight}
    assert report.exposures.theme_split == {"AI": position.weight}


def test_duplicate_metadata_compares_canonical_classifications_before_conflict_check():
    source = snapshot()
    first = replace(source.positions[0], sector=" unknown ", region="n/a", themes=(" AI ", "none"))
    duplicate = replace(
        first,
        account_ref="acct-other",
        sector="NULL",
        region="NONE",
        themes=("AI", "unknown"),
    )

    economic = aggregate_economic_positions(replace(source, positions=(first, duplicate)))

    assert economic[0].sector == "UNKNOWN"
    assert economic[0].region == "UNKNOWN"
    assert economic[0].themes == ("AI",)


def test_cli_metadata_conflict_is_structured_invalid_snapshot(monkeypatch, tmp_path, capsys):
    source = snapshot()
    conflict = replace(source.positions[0], account_ref="acct-other", currency="HKD")
    invalid = replace(
        source, snapshot_id="", positions=(source.positions[0], conflict, *source.positions[1:])
    )
    invalid = replace(invalid, snapshot_id=snapshot_identity(invalid))
    home = tmp_path / "private"
    home.mkdir()
    SnapshotStore(home).save("snapshots/conflict.json", json.dumps(invalid.to_dict()))
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))

    assert cli.main(["risk", "--snapshot", "snapshots/conflict.json", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_SNAPSHOT"
