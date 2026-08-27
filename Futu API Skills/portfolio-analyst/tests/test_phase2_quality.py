from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import time

import pytest

from portfolio_analyst import cli, risk, snapshot_io
from portfolio_analyst.persistence import SnapshotAlreadyExists, SnapshotStore
from portfolio_analyst.identity import snapshot_identity
from portfolio_analyst.models import DataQuality
from portfolio_analyst.reporting import PortfolioReport, build_portfolio_report
from portfolio_analyst.risk import (
    PortfolioPolicy,
    PricePoint,
    RiskInputError,
    analyze_risk,
)
from portfolio_analyst.settings import Settings
from portfolio_analyst.snapshot_io import InvalidSnapshot, load_snapshot

from test_risk_analysis import snapshot


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        (datetime(2026, 8, 27, 6, 29, 59, tzinfo=timezone.utc), date(2026, 8, 26)),
        (datetime(2026, 8, 27, 6, 30, 0, tzinfo=timezone.utc), date(2026, 8, 27)),
        (datetime(2026, 8, 27, 6, 30, 1, tzinfo=timezone.utc), date(2026, 8, 27)),
    ],
)
def test_jp_regular_close_cutoff_uses_1530_jst_boundary(instant, expected):
    assert risk._latest_complete_market_close_date(instant, "JP") == expected


def test_removed_max_positions_budget_argument_is_rejected():
    with pytest.raises(TypeError, match="max_positions"):
        risk.RiskBudget(max_positions=3)


def test_pair_returns_are_computed_after_common_close_date_alignment():
    start = date(2026, 1, 1)
    left = {
        start + timedelta(days=offset): value
        for offset, value in enumerate(map(Decimal, (100, 200, 100, 200, 100)))
    }
    right = {
        start + timedelta(days=offset): value
        for offset, value in zip((0, 2, 3, 4), map(Decimal, (100, 200, 100, 200)))
    }

    left_returns, right_returns = risk._aligned_pair_returns(left, right)

    assert left_returns == (Decimal("0"), Decimal("1"), Decimal("-0.5"))
    assert right_returns == (Decimal("1"), Decimal("-0.5"), Decimal("1"))


def test_strong_negative_correlation_is_inverse_evidence_not_concentration_pair_or_cluster():
    class InversePrices:
        def history(self, symbol, *, market, lookback, as_of):
            del market, lookback, as_of
            points = []
            level = Decimal("100")
            for offset in range(70):
                change = Decimal("1.01") if offset % 2 else Decimal("0.99")
                level *= change if symbol == "AAA" else Decimal("2") - change
                points.append(PricePoint(date(2026, 1, 1) + timedelta(days=offset), level))
            return tuple(points) if symbol in {"AAA", "BBB"} else None

    report = analyze_risk(snapshot(), price_provider=InversePrices())

    assert report.correlation.pairs == ()
    assert report.correlation.clusters == ()
    assert len(report.correlation.inverse_pairs) == 1
    assert report.correlation.inverse_pairs[0].coefficient == Decimal("-1")


def test_zero_holdings_are_excluded_from_every_current_risk_population():
    source = snapshot()
    zero = replace(
        source.positions[0],
        quantity=Decimal("0"),
        market_value_base=Decimal("0"),
        weight=Decimal("0"),
    )
    report = analyze_risk(replace(source, positions=(zero, source.positions[1])))

    assert report.coverage["source_holdings"] == 2
    assert report.coverage["positions"] == 1
    assert report.concentration.top_1 == source.positions[1].weight
    assert all("US.AAA" not in finding.affected_positions for finding in report.top_risks)


def test_zero_only_snapshot_has_empty_unrated_risk_population():
    source = snapshot()
    zero = replace(
        source.positions[0],
        quantity=Decimal("0"),
        market_value_base=Decimal("0"),
        weight=Decimal("0"),
    )
    report = analyze_risk(replace(source, positions=(zero,)))

    assert report.coverage["positions"] == 0
    assert report.overall_risk == "UNRATED"
    assert {finding.severity for finding in report.top_risks} == {"UNRATED"}
    assert {finding.evidence["available_positions"] for finding in report.top_risks} == {0}


def test_signed_current_portfolio_fails_with_explicit_unsupported_domain_error():
    source = snapshot()
    short = replace(
        source.positions[0],
        quantity=Decimal("-1"),
        market_value_base=Decimal("-100"),
        weight=Decimal("-0.1"),
    )
    with pytest.raises(RiskInputError, match="signed"):
        analyze_risk(replace(source, positions=(short,), short_exposure_base=Decimal("100")))


def test_signed_cli_error_is_structured_as_unsupported_not_invalid_snapshot(
    monkeypatch, tmp_path, capsys
):
    source = snapshot()
    short = replace(
        source.positions[0],
        quantity=Decimal("-1"),
        market_value_base=Decimal("-100"),
        weight=Decimal("-0.1"),
    )
    signed = replace(source, positions=(short,), short_exposure_base=Decimal("100"))
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))
    monkeypatch.setattr(cli, "load_snapshot", lambda *_args: signed)

    assert cli.main(["risk", "--snapshot", "snapshots/signed.json", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "UNSUPPORTED_PORTFOLIO"


def test_unconfigured_policy_produces_unrated_findings_and_overall_risk():
    report = analyze_risk(snapshot())

    assert report.overall_risk == "UNRATED"
    assert {finding.severity for finding in report.top_risks} == {"UNRATED"}


def test_policy_rated_findings_sort_before_unrated_observations():
    report = analyze_risk(
        snapshot(),
        policy=PortfolioPolicy(single_position=Decimal("0.3"), sector=Decimal("0.6")),
    )

    assert [finding.severity for finding in report.top_risks[:2]] == ["HIGH", "HIGH"]
    assert [finding.review_priority for finding in report.top_risks[:2]] == ["P1", "P2"]
    assert report.overall_risk == "HIGH"


def test_configured_region_threshold_produces_a_rated_region_finding():
    report = analyze_risk(snapshot(), policy=PortfolioPolicy(region=Decimal("0.6")))

    finding = next(item for item in report.top_risks if item.type == "REGION_CONCENTRATION")
    assert finding.severity == "HIGH"
    assert finding.evidence["policy_threshold_applied"] is True
    assert report.overall_risk == "HIGH"


def test_future_price_row_is_rejected_as_unavailable_not_used_as_lookahead():
    class FuturePrices:
        def history(self, symbol, *, market, lookback, as_of):
            del symbol, market, lookback
            return (
                PricePoint(as_of - timedelta(days=1), Decimal("100")),
                PricePoint(as_of + timedelta(days=1), Decimal("101")),
            )

    report = analyze_risk(snapshot(), price_provider=FuturePrices())
    assert report.correlation.status == "UNAVAILABLE"
    assert report.correlation.coverage["available"] == 0


def test_history_rows_are_capped_to_lookback_price_capacity():
    class LongPrices:
        def history(self, symbol, *, market, lookback, as_of):
            del symbol, market, lookback
            level = Decimal("100")
            points = []
            for offset in range(130):
                level *= Decimal("1.01") if offset % 2 else Decimal("0.99")
                points.append(PricePoint(as_of - timedelta(days=129 - offset), level))
            return tuple(points)

    report = analyze_risk(snapshot(), price_provider=LongPrices())

    assert report.correlation.pairs[0].sample_count == 119


def test_optional_policy_private_config_error_is_structured_config_error(
    monkeypatch, tmp_path, capsys
):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    SnapshotStore(home).save("snapshots/source.json", json.dumps(snapshot().to_dict()))
    config = home / "config"
    config.mkdir()
    target = tmp_path / "policy.json"
    target.write_text("{}", encoding="utf-8")
    (config / "risk_policy.json").symlink_to(target)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(data_home=home)))

    assert cli.main(["risk", "--snapshot", "snapshots/source.json", "--json"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["error"]["code"] == "CONFIG_ERROR"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda source: replace(source, cash_base=Decimal("101")),
        lambda source: replace(source, nav_base=Decimal("1001")),
        lambda source: replace(
            source,
            positions=(replace(source.positions[0], weight=Decimal("0.41")), *source.positions[1:]),
        ),
        lambda source: replace(
            source,
            positions=(
                replace(source.positions[0], market_value_base=Decimal("401")),
                *source.positions[1:],
            ),
        ),
        lambda source: replace(
            source, exposures={**source.exposures, "sector": {"FAKE": Decimal("0.9")}}
        ),
        lambda source: replace(
            source,
            positions=(
                replace(source.positions[0], account_ref="acct-missing"),
                *source.positions[1:],
            ),
        ),
        lambda source: replace(source, data_quality=DataQuality("COMPLETE")),
        lambda source: replace(
            source,
            fx_evidence=(replace(source.fx_evidence[0], source="UNKNOWN"),),
        ),
    ],
)
def test_reidentified_financially_false_snapshot_is_rejected(mutate, tmp_path):
    false = mutate(snapshot())
    false = replace(false, snapshot_id=snapshot_identity(false))
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    SnapshotStore(home).save("snapshots/false.json", json.dumps(false.to_dict()))

    with pytest.raises(InvalidSnapshot, match="financial semantics"):
        load_snapshot(home, "snapshots/false.json")


@pytest.mark.skipif(os.name != "posix", reason="descriptor anchor contract is POSIX-specific")
def test_snapshot_read_rejects_leaf_anchor_swap(monkeypatch, tmp_path):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    SnapshotStore(home).save("snapshots/source.json", json.dumps(snapshot().to_dict()))
    monkeypatch.setattr(snapshot_io, "_same_file_anchor", lambda *args: False, raising=False)

    with pytest.raises(InvalidSnapshot, match="changed during anchored read"):
        load_snapshot(home, "snapshots/source.json")


@pytest.mark.skipif(os.name != "posix", reason="descriptor anchor contract is POSIX-specific")
def test_snapshot_read_rejects_parent_anchor_swap(monkeypatch, tmp_path):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    SnapshotStore(home).save("snapshots/source.json", json.dumps(snapshot().to_dict()))
    monkeypatch.setattr(snapshot_io, "_same_parent_anchor", lambda *args: False, raising=False)

    with pytest.raises(InvalidSnapshot, match="changed during anchored read"):
        load_snapshot(home, "snapshots/source.json")


def test_analyze_bundle_preflight_collision_leaves_no_partial_sibling(tmp_path):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    store = SnapshotStore(home)
    store.save("reports/run/portfolio_report.md", "existing")

    with pytest.raises(FileExistsError):
        store.save_bundle(
            "reports/run",
            {"portfolio_report.json": "json", "portfolio_report.md": "markdown"},
        )

    assert not (home / "reports/run/portfolio_report.json").exists()
    assert (home / "reports/run/portfolio_report.md").read_text() == "existing"


@pytest.mark.skipif(os.name != "posix", reason="hard-link injection contract is POSIX-specific")
def test_analyze_bundle_rolls_back_first_link_when_second_commit_fails(monkeypatch, tmp_path):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    store = SnapshotStore(home)
    real_link = os.link
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second-link failure")
        return real_link(*args, **kwargs)

    monkeypatch.setattr(os, "link", fail_second)
    with pytest.raises(OSError, match="injected"):
        store.save_bundle(
            "reports/run",
            {"portfolio_report.json": "json", "portfolio_report.md": "markdown"},
        )

    assert not (home / "reports/run/portfolio_report.json").exists()
    assert not (home / "reports/run/portfolio_report.md").exists()


@pytest.mark.skipif(os.name != "posix", reason="hard-link injection contract is POSIX-specific")
def test_analyze_bundle_normalizes_commit_race_collision_and_rolls_back(monkeypatch, tmp_path):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    store = SnapshotStore(home)
    real_link = os.link
    calls = 0

    def collide_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise FileExistsError("injected collision")
        return real_link(*args, **kwargs)

    monkeypatch.setattr(os, "link", collide_second)
    with pytest.raises(SnapshotAlreadyExists):
        store.save_bundle(
            "reports/run",
            {"portfolio_report.json": "json", "portfolio_report.md": "markdown"},
        )

    assert not (home / "reports/run/portfolio_report.json").exists()
    assert not (home / "reports/run/portfolio_report.md").exists()


def test_markdown_escapes_untrusted_labels_notes_and_finding_text():
    source = snapshot()
    risk_report = analyze_risk(source)
    payload = build_portfolio_report(source, risk_report).to_dict()
    payload["data_quality_notes"] = ["\x1b[31m<script>bad</script>\n[next](evil)"]
    payload["largest_risks"][0]["summary"] = "<b>risk</b> [click](evil)\x00"
    payload["exposures"]["sector"]["weights"] = {"TECH|x": "0.9"}

    markdown = PortfolioReport(payload).to_markdown()

    assert "\x1b" not in markdown and "\x00" not in markdown
    assert "<script>" not in markdown and "<b>" not in markdown
    assert "[next](evil)" not in markdown and "[click](evil)" not in markdown
    for label in (
        "Medium threshold",
        "High threshold",
        "Positive risk pairs",
        "Inverse diversification pairs",
        "Clusters",
        "Policy status",
        "Policy checks",
        "Matrix JSON digest",
    ):
        assert label in markdown


def test_human_risk_output_strips_terminal_controls_and_carries_full_evidence():
    payload = analyze_risk(snapshot()).to_dict()
    payload["data_quality_notes"] = ["bad\x1b[2J\nforged"]
    payload["top_risks"][0]["summary"] = "risk\rforged\x00"

    output = cli._human_risk_report(payload)

    assert "\x1b" not in output and "\x00" not in output
    assert "bad forged" in output and "risk forged" in output
    assert "Inverse diversification pairs:" in output
    assert "Policy status:" in output


def test_large_portfolio_caps_only_history_and_keeps_full_structural_population():
    source = snapshot()
    positions = tuple(
        replace(
            source.positions[0],
            symbol=f"P{index:03d}",
            weight=Decimal(100 - index) / Decimal("1000"),
            market_value_base=Decimal(100 - index),
            market_value_local=Decimal(100 - index),
        )
        for index in range(10)
    )

    class RecordingPrices:
        def __init__(self):
            self.calls = []

        def history(self, symbol, *, market, lookback, as_of):
            del market, lookback, as_of
            self.calls.append(symbol)
            return None

    provider = RecordingPrices()
    report = analyze_risk(
        replace(source, positions=positions),
        price_provider=provider,
        budget=risk.RiskBudget(max_history_symbols=2, max_pair_operations=1),
    )

    assert report.coverage["total_positions"] == 10
    assert report.coverage["positions"] == 10
    assert report.coverage["omitted_positions"] == 0
    assert report.coverage["budget_status"] == "BUDGET_LIMIT"
    assert report.concentration.top_1 == Decimal("0.1")
    assert report.concentration.top_5 == Decimal("0.49")
    assert report.exposures.sector == {"TECH": Decimal("0.955")}
    assert provider.calls == ["P000", "P001"]
    assert report.correlation.coverage["requested"] == 2
    assert len(report.correlation.matrix) <= 2


def test_nonbreaching_region_policy_still_emits_low_candidate():
    report = analyze_risk(snapshot(), policy=PortfolioPolicy(region=Decimal("0.8")))

    finding = next(item for item in report.top_risks if item.type == "REGION_CONCENTRATION")
    assert finding.severity == "LOW"
    assert finding.evidence["policy_threshold_applied"] is True


@pytest.mark.skipif(os.name != "posix", reason="descriptor temp cleanup is POSIX-specific")
def test_bundle_cleans_registered_posix_temp_when_file_fsync_fails(monkeypatch, tmp_path):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    real_fsync = os.fsync
    calls = 0

    def fail_first(descriptor):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected file fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_first)
    with pytest.raises(OSError, match="injected file fsync"):
        SnapshotStore(home).save_bundle(
            "reports/run",
            {"portfolio_report.json": "json", "portfolio_report.md": "markdown"},
        )

    assert not tuple((home / "reports/run").glob(".*.tmp"))


def test_bundle_fallback_cleans_registered_temp_when_file_fsync_fails(monkeypatch, tmp_path):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="UNSUPPORTED_PLATFORM_SECURITY"):
        SnapshotStore(home)._save_bundle_fallback(
            ("reports", "run"),
            (("portfolio_report.json", "json"), ("portfolio_report.md", "markdown")),
        )

    assert not tuple((home / "reports/run").glob(".*.tmp"))


def test_us_same_utc_date_close_after_snapshot_is_rejected_by_market_cutoff():
    source = snapshot()
    requested = []

    class LookaheadPrices:
        def history(self, symbol, *, market, lookback, as_of):
            requested.append((symbol, market, as_of))
            return (
                PricePoint(source.as_of.date() - timedelta(days=1), Decimal("100")),
                PricePoint(source.as_of.date(), Decimal("101")),
            )

    report = analyze_risk(
        replace(source, positions=(source.positions[0],)),
        price_provider=LookaheadPrices(),
        lookback=2,
        minimum_observations=1,
    )

    assert requested == [("AAA", "US", date(2026, 8, 26))]
    assert report.correlation.date_cutoffs == {"US.AAA": "2026-08-26"}
    assert report.correlation.coverage["available"] == 0


def test_price_provider_timeout_trips_circuit_breaker_without_waiting_for_all_symbols():
    source = snapshot()
    calls = []

    class SlowPrices:
        def history(self, symbol, *, market, lookback, as_of):
            del market, lookback, as_of
            calls.append(symbol)
            time.sleep(0.1)
            return None

    started = time.monotonic()
    report = analyze_risk(
        source,
        price_provider=SlowPrices(),
        budget=risk.RiskBudget(
            max_history_symbols=10,
            max_pair_operations=45,
            max_wall_seconds=Decimal("0.05"),
            provider_timeout_seconds=Decimal("0.01"),
        ),
    )

    assert time.monotonic() - started < 0.08
    assert len(calls) == 1
    assert report.correlation.status == "UNAVAILABLE"
    assert any("circuit breaker" in note.lower() for note in report.correlation.notes)


def test_fake_clock_wall_budget_stops_before_any_history_request():
    calls = []

    class Prices:
        def history(self, symbol, *, market, lookback, as_of):
            calls.append((symbol, market, lookback, as_of))
            return None

    ticks = iter((Decimal("0"), Decimal("31")))
    report = analyze_risk(snapshot(), price_provider=Prices(), clock=lambda: next(ticks))

    assert calls == []
    assert report.correlation.status == "UNAVAILABLE"
    assert any("wall-time budget" in note for note in report.correlation.notes)
