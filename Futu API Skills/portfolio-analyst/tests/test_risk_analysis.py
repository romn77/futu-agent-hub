from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re

import pytest

from portfolio_analyst.models import (
    Account,
    CashBalance,
    DataQuality,
    FxRateEvidence,
    NormalizedPosition,
    PortfolioSnapshot,
)
from portfolio_analyst.identity import snapshot_identity
from portfolio_analyst.reporting import build_portfolio_report
from portfolio_analyst.risk import (
    PortfolioPolicy,
    PricePoint,
    UnsupportedRiskError,
    analyze_risk,
    load_portfolio_policy,
)
from portfolio_analyst.schema import validate_portfolio_report_document, validate_risk_document


AS_OF = datetime(2026, 8, 27, 10, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"


def _position(
    symbol: str,
    weight: str,
    *,
    sector: str,
    region: str,
    themes: tuple[str, ...],
    role: str | None = None,
) -> NormalizedPosition:
    value = Decimal(weight) * 1000
    return NormalizedPosition(
        account_ref="acct-safe",
        symbol=symbol,
        name=f"Fixture {symbol}",
        market="US",
        security_type="STOCK",
        currency="USD",
        currency_provenance="USER_CONFIG",
        quantity=Decimal("1"),
        can_sell_quantity=Decimal("1"),
        average_cost_local=value,
        last_price_local=value,
        market_value_local=value,
        unrealized_pl_local=None,
        realized_pl_local=None,
        fx_to_base=Decimal("1"),
        market_value_base=value,
        unrealized_pl_base=None,
        realized_pl_base=None,
        weight=Decimal(weight),
        sector=sector,
        industry="UNKNOWN",
        region=region,
        themes=themes,
        asset_class="EQUITY",
        position_role=role,
    )


def snapshot() -> PortfolioSnapshot:
    positions = (
        _position("AAA", "0.4", sector="TECH", region="US", themes=("AI", "CLOUD")),
        _position("BBB", "0.3", sector="TECH", region="US", themes=("AI",)),
        _position("CCC", "0.2", sector="HEALTH", region="EU", themes=()),
    )
    value = PortfolioSnapshot(
        schema_version="1.0",
        snapshot_id="",
        as_of=AS_OF,
        source="futuapi",
        source_version="fixture-v1",
        trd_env="REAL",
        base_currency="USD",
        fx_evidence=(FxRateEvidence("USD", "USD", Decimal("1"), "TEST", AS_OF),),
        accounts=(Account("acct-safe", "REAL", "MARGIN", "NORMAL", ("US",), "ACTIVE"),),
        cash_balances=(
            CashBalance(
                "acct-safe", "USD", Decimal("100"), Decimal("1"), Decimal("100"), Decimal("0.1")
            ),
        ),
        positions=positions,
        nav_base=Decimal("1000"),
        cash_base=Decimal("100"),
        cash_weight=Decimal("0.1"),
        long_exposure_base=Decimal("900"),
        short_exposure_base=Decimal("0"),
        gross_exposure_base=Decimal("900"),
        net_exposure_base=Decimal("900"),
        exposures={
            "sector": {"TECH": Decimal("0.7"), "HEALTH": Decimal("0.2")},
            "region": {"US": Decimal("0.7"), "EU": Decimal("0.2")},
            "theme": {"AI": Decimal("0.7"), "CLOUD": Decimal("0.4")},
            "asset_class": {"EQUITY": Decimal("0.9")},
        },
        data_quality=DataQuality(
            "PARTIAL",
            warnings=("fixture warning", "one or more classifications are UNKNOWN"),
            missing_fields=("classification",),
        ),
    )
    return replace(value, snapshot_id=snapshot_identity(value))


class CorrelatedPrices:
    def history(self, symbol: str, *, market: str, lookback: int, as_of: date):
        del market, lookback, as_of
        base = Decimal("100")
        points = []
        for offset in range(70):
            # AAA and BBB have identical varying returns; CCC is deliberately absent.
            change = Decimal("1.01") if offset % 2 else Decimal("0.99")
            base *= change
            points.append(PricePoint(date(2026, 5, 1) + timedelta(days=offset), base))
        return tuple(points) if symbol in {"AAA", "BBB"} else None


def test_risk_metrics_keep_theme_overlap_and_additive_views_separate():
    report = analyze_risk(snapshot())

    assert report.concentration.top_1 == Decimal("0.4")
    assert report.concentration.top_3 == Decimal("0.9")
    assert report.concentration.top_5 == Decimal("0.9")
    assert report.concentration.hhi == Decimal("0.29")
    assert report.concentration.effective_positions == Decimal("1") / Decimal("0.29")
    assert report.exposures.sector == {"HEALTH": Decimal("0.2"), "TECH": Decimal("0.7")}
    assert report.exposures.theme_full == {
        "AI": Decimal("0.7"),
        "CLOUD": Decimal("0.4"),
        "UNKNOWN": Decimal("0.2"),
    }
    assert report.exposures.theme_split == {
        "AI": Decimal("0.5"),
        "CLOUD": Decimal("0.2"),
        "UNKNOWN": Decimal("0.2"),
    }
    payload = report.to_dict()
    assert payload["exposures"]["theme_full"]["view"] == "OVERLAP_FULL_ALLOCATION"
    assert payload["exposures"]["theme_split"]["view"] == "ADDITIVE_SPLIT_ALLOCATION"
    validate_risk_document(payload)


def test_sanitized_risk_golden_snippet_is_deterministic():
    payload = analyze_risk(snapshot()).to_dict()
    actual = {
        "concentration": {
            key: payload["concentration"][key] for key in ("top_1", "top_3", "top_5", "hhi")
        },
        "theme_full": {
            "view": payload["exposures"]["theme_full"]["view"],
            "weights": payload["exposures"]["theme_full"]["weights"],
        },
        "theme_split": {
            "view": payload["exposures"]["theme_split"]["view"],
            "weights": payload["exposures"]["theme_split"]["weights"],
        },
        "top_risk_types": [finding["type"] for finding in payload["top_risks"]],
    }
    assert actual == json.loads(
        (FIXTURES / "risk_report_golden_snippet.json").read_text(encoding="utf-8")
    )


def test_signed_short_positions_fail_closed_until_signed_risk_semantics_exist():
    source = snapshot()
    short = replace(source.positions[2], weight=Decimal("-0.2"), market_value_base=Decimal("-200"))
    with pytest.raises(UnsupportedRiskError, match="signed"):
        analyze_risk(
            replace(
                source,
                positions=(*source.positions[:2], short),
                short_exposure_base=Decimal("200"),
            )
        )


def test_correlation_uses_adjusted_return_overlap_and_reports_partial_coverage():
    report = analyze_risk(snapshot(), price_provider=CorrelatedPrices(), lookback=120)

    assert report.correlation.status == "PARTIAL"
    assert report.correlation.minimum_observations == 60
    assert report.correlation.pairs[0].symbols == ("US.AAA", "US.BBB")
    assert report.correlation.pairs[0].sample_count == 69
    assert report.correlation.pairs[0].coefficient == Decimal("1")
    assert report.correlation.clusters == (("US.AAA", "US.BBB"),)
    assert report.correlation.coverage == {"requested": 3, "available": 2}


def test_correlation_is_partial_when_all_histories_exist_but_not_all_pairs_have_minimum_overlap():
    class UnevenPrices(CorrelatedPrices):
        def history(self, symbol, *, market, lookback, as_of):
            history = super().history(
                "AAA" if symbol == "CCC" else symbol,
                market=market,
                lookback=lookback,
                as_of=as_of,
            )
            return history[:40] if symbol == "CCC" else history

    report = analyze_risk(snapshot(), price_provider=UnevenPrices())
    assert report.correlation.coverage == {"requested": 3, "available": 3}
    assert report.correlation.status == "PARTIAL"
    assert set(report.correlation.matrix) == {"US.AAA", "US.BBB", "US.CCC"}
    assert all(
        set(row) == set(report.correlation.matrix) for row in report.correlation.matrix.values()
    )
    assert report.correlation.matrix["US.AAA"]["US.CCC"] is None
    assert report.correlation.matrix["US.CCC"]["US.AAA"] is None
    validate_risk_document(report.to_dict())


@pytest.mark.parametrize(
    "bad_points",
    [
        (PricePoint(date(2026, 1, 1), Decimal("1")), PricePoint(date(2026, 1, 1), Decimal("2"))),
        (PricePoint(date(2026, 1, 2), Decimal("1")), PricePoint(date(2026, 1, 1), Decimal("2"))),
        (PricePoint(date(2026, 1, 1), Decimal("0")), PricePoint(date(2026, 1, 2), Decimal("2"))),
    ],
)
def test_invalid_price_history_is_rejected_not_used_for_pseudo_correlation(bad_points):
    class InvalidPrices:
        def history(self, symbol, *, market, lookback, as_of):
            del symbol, market, lookback, as_of
            return bad_points

    report = analyze_risk(snapshot(), price_provider=InvalidPrices())
    assert report.correlation.status == "UNAVAILABLE"
    assert report.correlation.pairs == ()


def test_comprehensive_report_reuses_snapshot_identity_and_has_required_placeholders():
    risk = analyze_risk(snapshot(), price_provider=CorrelatedPrices())
    report = build_portfolio_report(snapshot(), risk)
    payload = report.to_dict()

    assert payload["source_snapshot_id"] == risk.source_snapshot_id == snapshot().snapshot_id
    assert len(payload["largest_risks"]) == 3
    assert payload["lifecycle"]["status"] == "UNAVAILABLE_UNTIL_PHASE_4"
    assert payload["performance"]["status"] == "UNAVAILABLE_UNTIL_PHASE_5"
    assert payload["options"]["status"] == "NOT_RUN_PHASE_6"
    markdown = report.to_markdown()
    assert "NAV basis" in markdown
    assert "US.AAA: 0.4" in markdown
    assert "TECH: 0.7" in markdown
    assert "Correlation: PARTIAL" in markdown
    assert "Source holdings: 3" in markdown
    assert "Economic positions: 3" in markdown
    assert "Classified sector: 3" in markdown
    assert "Classified region: 3" in markdown
    for finding in payload["largest_risks"]:
        assert f"Type: {finding['type']}" in markdown
        assert f"Severity: {finding['severity']}" in markdown
        assert f"Summary: {finding['summary']}" in markdown
        assert "- Evidence:" in markdown
        assert (
            f"Affected positions: {', '.join(finding['affected_positions']) or 'None'}" in markdown
        )
        assert f"Review priority: {finding['review_priority']}" in markdown
        assert (
            f"Simulation direction: {finding['simulation_direction'] or 'Not available'}"
            in markdown
        )
    assert not re.search(r"\b(?:BUY|SELL)\b", markdown, flags=re.IGNORECASE)
    validate_portfolio_report_document(payload)

    with pytest.raises(TypeError):
        report.payload["source_snapshot_id"] = "tampered"


def test_user_policy_checks_are_explicit_and_never_apply_to_theme_overlap_view():
    report = analyze_risk(
        snapshot(),
        policy=PortfolioPolicy(
            single_position=Decimal("0.35"),
            top_3=Decimal("0.8"),
            sector=Decimal("0.6"),
            region=Decimal("0.75"),
            theme_split=Decimal("0.6"),
        ),
    )
    payload = report.to_dict()

    assert report.policy_status == "CONFIGURED"
    assert payload["policy_checks"]["single_position"]["breached"] is True
    assert payload["policy_checks"]["top_3"]["breached"] is True
    assert payload["policy_checks"]["theme_split"]["measured"] == "0.5"
    assert "theme_full" not in payload["policy_checks"]
    assert payload["top_risks"][0]["evidence"]["policy_threshold_applied"] is True
    theme_finding = next(
        item for item in payload["top_risks"] if item["type"] == "THEME_CONCENTRATION"
    )
    assert theme_finding["evidence"]["policy_threshold_applied"] is True


@pytest.mark.parametrize("threshold", [Decimal("0"), Decimal("-0.1"), Decimal("1.01")])
def test_policy_thresholds_must_be_explicit_nav_fractions(threshold):
    with pytest.raises(ValueError, match="policy threshold"):
        PortfolioPolicy(single_position=threshold)


def test_top_n_policy_breach_applies_to_position_concentration_finding():
    report = analyze_risk(snapshot(), policy=PortfolioPolicy(top_3=Decimal("0.8")))
    finding = report.top_risks[0]
    assert finding.severity == "HIGH"
    assert finding.evidence["policy_threshold_applied"] is True


def test_optional_policy_loader_requires_user_provenance_and_has_no_defaults(tmp_path):
    assert load_portfolio_policy(tmp_path) is None
    config = tmp_path / "config"
    config.mkdir()
    (config / "risk_policy.json").write_text(
        '{"provenance":"USER_CONFIG","thresholds":{"single_position":"0.35"}}',
        encoding="utf-8",
    )
    assert load_portfolio_policy(tmp_path) == PortfolioPolicy(single_position=Decimal("0.35"))

    (config / "risk_policy.json").write_text(
        '{"provenance":"EXAMPLE","thresholds":{"single_position":"0.35"}}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="USER_CONFIG"):
        load_portfolio_policy(tmp_path)


def test_pnl_concentration_reports_complete_profit_and_loss_pools():
    source = snapshot()
    positions = (
        replace(
            source.positions[0],
            unrealized_pl_base=Decimal("90"),
            realized_pl_base=Decimal("10"),
        ),
        replace(
            source.positions[1],
            unrealized_pl_base=Decimal("-30"),
            realized_pl_base=Decimal("-20"),
        ),
        replace(
            source.positions[2],
            unrealized_pl_base=Decimal("10"),
            realized_pl_base=Decimal("0"),
        ),
    )
    report = analyze_risk(replace(source, positions=positions))

    finding = next(item for item in report.top_risks if item.type == "PNL_CONCENTRATION")
    assert report.coverage["pnl_evidence_status"] == "COMPLETE"
    assert finding.evidence == {
        "basis": "ABSOLUTE_REPORTED_PNL",
        "profit_total_base": Decimal("110"),
        "loss_total_abs_base": Decimal("50"),
        "largest_profit_share": Decimal("100") / Decimal("110"),
        "largest_loss_share": Decimal("1"),
        "source_fields": ("unrealized_pl_base", "realized_pl_base"),
    }
    assert finding.affected_positions == ("US.AAA", "US.BBB")
    validate_risk_document(report.to_dict())


def test_incomplete_pnl_evidence_is_partial_and_does_not_emit_concentration():
    source = snapshot()
    positions = tuple(
        replace(position, unrealized_pl_base=Decimal("1"), realized_pl_base=Decimal("0"))
        for position in source.positions
    )
    positions = (replace(positions[0], realized_pl_base=None), *positions[1:])

    report = analyze_risk(replace(source, positions=positions))

    assert report.coverage["pnl_evidence_status"] == "PARTIAL"
    assert "PNL_CONCENTRATION" not in {item.type for item in report.top_risks}
    assert any("P&L" in note and "incomplete" in note for note in report.data_quality_notes)


def test_nonfinite_pnl_evidence_is_partial_not_serialized_as_a_finding():
    source = snapshot()
    positions = tuple(
        replace(position, unrealized_pl_base=Decimal("1"), realized_pl_base=Decimal("0"))
        for position in source.positions
    )
    positions = (replace(positions[0], realized_pl_base=Decimal("NaN")), *positions[1:])

    report = analyze_risk(replace(source, positions=positions))

    assert report.coverage["pnl_evidence_status"] == "PARTIAL"
    assert "PNL_CONCENTRATION" not in {item.type for item in report.top_risks}


def test_role_imbalance_is_absent_until_roles_are_configured_then_uses_nav_exposure():
    source = snapshot()
    assert "ROLE_IMBALANCE" not in {item.type for item in analyze_risk(source).top_risks}
    positions = tuple(
        replace(position, position_role="OFFENSIVE" if index < 2 else "DEFENSIVE")
        for index, position in enumerate(source.positions)
    )
    report = analyze_risk(replace(source, positions=positions))

    finding = next(item for item in report.top_risks if item.type == "ROLE_IMBALANCE")
    assert finding.evidence["roles"] == {
        "DEFENSIVE": Decimal("0.2"),
        "OFFENSIVE": Decimal("0.7"),
    }


def test_role_correlation_and_theme_candidates_are_independent_before_top_three_selection():
    source = snapshot()
    positions = tuple(replace(position, position_role="OFFENSIVE") for position in source.positions)

    report = analyze_risk(
        replace(source, positions=positions),
        price_provider=CorrelatedPrices(),
        policy=PortfolioPolicy(theme_split=Decimal("0.4")),
    )

    assert [finding.type for finding in report.top_risks] == [
        "THEME_CONCENTRATION",
        "CORRELATION_CLUSTER",
        "POSITION_CONCENTRATION",
    ]
    assert report.top_risks[0].severity == "HIGH"


def test_cash_only_snapshot_reports_explicit_data_availability_findings_not_a_crash():
    source = snapshot()
    report = analyze_risk(
        replace(
            source,
            positions=(),
            long_exposure_base=Decimal("0"),
            gross_exposure_base=Decimal("0"),
            net_exposure_base=Decimal("0"),
        )
    )
    assert len(report.top_risks) == 3
    assert {finding.type for finding in report.top_risks} == {
        "POSITION_DATA_UNAVAILABLE",
        "CLASSIFICATION_DATA_UNAVAILABLE",
        "CORRELATION_DATA_UNAVAILABLE",
    }
