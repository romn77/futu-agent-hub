"""Comprehensive deterministic portfolio report rendering."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping

from .models import PortfolioSnapshot, _deep_freeze, _json_value
from .rendering import markdown_text
from .risk import RiskReport, aggregate_economic_positions


@dataclass(frozen=True)
class PortfolioReport:
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _deep_freeze(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self.payload)

    def to_markdown(self, *, json_artifact: str | None = None) -> str:
        data = self.to_dict()
        md = markdown_text
        matrix_digest = hashlib.sha256(
            json.dumps(
                data["correlation"]["matrix"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        pair_text = lambda pair: (  # noqa: E731
            f"{md(pair['symbols'][0])}/{md(pair['symbols'][1])}: "
            f"{pair['coefficient']} ({pair['band']}, n={pair['sample_count']})"
        )
        lines = [
            "# Portfolio analysis",
            "",
            f"- source_snapshot_id: `{md(data['source_snapshot_id'])}`",
            f"- As of: {data['as_of']}",
            f"- NAV: {data['summary']['nav_base']} {data['base_currency']}",
            f"- Cash: {data['summary']['cash_base']} {data['base_currency']}",
            f"- Position count: {data['summary']['position_count']}",
            "- All weights below use a NAV basis.",
            f"- Data range: point-in-time snapshot at {data['as_of']}; "
            f"correlation lookback {data['correlation']['lookback']} trading days.",
            f"- Data quality: {data['data_quality_status']}",
            "",
            "## Coverage",
            "",
            f"- Source holdings: {data['coverage']['source_holdings']}",
            f"- Economic positions: {data['coverage']['positions']}",
            f"- Total current positions: {data['coverage']['total_positions']}",
            f"- Omitted positions: {data['coverage']['omitted_positions']}",
            f"- Budget status: {data['coverage']['budget_status']}",
            "- Budget reason: "
            + (
                md(data["coverage"]["budget_reason"])
                if data["coverage"]["budget_reason"]
                else "None"
            ),
            f"- Classified sector: {data['coverage']['classified_sector']}",
            f"- Classified region: {data['coverage']['classified_region']}",
            f"- P&L evidence: {data['coverage']['pnl_evidence_status']}",
            f"- Price histories: {data['coverage']['price_histories']['available']}/"
            f"{data['coverage']['price_histories']['requested']}",
            "",
            "## Data quality notes",
            "",
            *([f"- {md(note)}" for note in data["data_quality_notes"]] or ["- None"]),
            "",
            "## Top holdings",
            "",
            *[
                f"- {md(holding['symbol'])}: {holding['weight']} "
                f"({holding['market_value_base']} {data['base_currency']})"
                for holding in data["summary"]["top_holdings"]
            ],
            "",
            "## Largest three risks",
            "",
        ]
        for index, risk in enumerate(data["largest_risks"], 1):
            lines.extend(
                [
                    f"### Risk {index}",
                    "",
                    f"- Type: {md(risk['type'])}",
                    f"- Severity: {md(risk['severity'])}",
                    f"- Summary: {md(risk['summary'])}",
                    f"- Evidence: {md(json.dumps(risk['evidence'], ensure_ascii=False, sort_keys=True))}",
                    "- Affected positions: "
                    + (", ".join(md(item) for item in risk["affected_positions"]) or "None"),
                    f"- Review priority: {risk['review_priority']}",
                    "- Simulation direction: "
                    + (
                        md(risk["simulation_direction"])
                        if risk["simulation_direction"]
                        else "Not available"
                    ),
                    "",
                ]
            )
        lines.extend(
            [
                "",
                "## Concentration and exposure",
                "",
                f"- Top 1: {data['concentration']['top_1']}",
                f"- Top 3: {data['concentration']['top_3']}",
                f"- Top 5: {data['concentration']['top_5']}",
                f"- HHI: {data['concentration']['hhi']}",
                f"- Effective positions: {data['concentration']['effective_positions']}",
                "- Sector: "
                + ", ".join(
                    f"{md(label)}: {weight}"
                    for label, weight in data["exposures"]["sector"]["weights"].items()
                ),
                "- Region: "
                + ", ".join(
                    f"{md(label)}: {weight}"
                    for label, weight in data["exposures"]["region"]["weights"].items()
                ),
                "Theme overlap-full and additive-split views are reported separately and must not be summed together.",
                "- Theme full overlap: "
                + ", ".join(
                    f"{md(label)}: {weight}"
                    for label, weight in data["exposures"]["theme_full"]["weights"].items()
                ),
                "- Theme additive split: "
                + ", ".join(
                    f"{md(label)}: {weight}"
                    for label, weight in data["exposures"]["theme_split"]["weights"].items()
                ),
                "",
                "## Correlation",
                "",
                f"- Correlation: {data['correlation']['status']}",
                f"- Coverage: {data['correlation']['coverage']['available']}/"
                f"{data['correlation']['coverage']['requested']} histories",
                f"- Medium threshold: {data['correlation']['medium_threshold']}",
                f"- High threshold: {data['correlation']['high_threshold']}",
                "- Market close cutoffs: "
                + md(json.dumps(data["correlation"]["date_cutoffs"], sort_keys=True)),
                "- Positive risk pairs: "
                + (", ".join(pair_text(pair) for pair in data["correlation"]["pairs"]) or "None"),
                "- Inverse diversification pairs: "
                + (
                    ", ".join(pair_text(pair) for pair in data["correlation"]["inverse_pairs"])
                    or "None"
                ),
                "- Clusters: "
                + (
                    ", ".join(
                        "/".join(md(item) for item in cluster)
                        for cluster in data["correlation"]["clusters"]
                    )
                    or "None"
                ),
                (
                    f"- Matrix: authoritative JSON at `{md(json_artifact)}`"
                    if json_artifact
                    else "- Matrix: authoritative JSON representation/output"
                ),
                f"- Matrix JSON digest: `{matrix_digest}` (SHA-256)",
                f"- Policy status: {md(data['policy_status'])}",
                "- Policy checks: "
                + md(json.dumps(data["policy_checks"], ensure_ascii=False, sort_keys=True)),
                "",
                "## Hypothetical simulation candidates",
                "",
                *[f"- {md(candidate)}" for candidate in data["hypothetical_simulation_candidates"]],
                "",
                "## Availability",
                "",
                f"- Lifecycle: {data['lifecycle']['status']}",
                f"- Performance: {data['performance']['status']}",
                f"- Options: {data['options']['status']}",
                "",
                "## Data gaps",
                "",
                *[f"- {md(note)}" for note in data["data_quality_notes"]],
            ]
        )
        return "\n".join(lines) + "\n"


def build_portfolio_report(snapshot: PortfolioSnapshot, risk: RiskReport) -> PortfolioReport:
    if risk.source_snapshot_id != snapshot.snapshot_id or risk.as_of != snapshot.as_of:
        raise ValueError("risk and portfolio report inputs must reference the same snapshot")
    economic_positions = aggregate_economic_positions(snapshot)
    top_holdings = sorted(
        economic_positions, key=lambda position: (-abs(position.weight), position.identity)
    )
    payload = {
        "schema_version": "1.0",
        "as_of": snapshot.as_of,
        "source_snapshot_id": snapshot.snapshot_id,
        "base_currency": snapshot.base_currency,
        "coverage": risk.coverage,
        "data_quality_status": risk.data_quality_status,
        "data_quality_notes": risk.data_quality_notes,
        "overall_risk": risk.overall_risk,
        "policy_status": risk.policy_status,
        "policy_checks": risk.policy_checks,
        "summary": {
            "nav_base": snapshot.nav_base,
            "cash_base": snapshot.cash_base,
            "cash_weight": snapshot.cash_weight,
            "position_count": len(economic_positions),
            "top_holdings": [
                {
                    "symbol": position.identity,
                    "weight": position.weight,
                    "market_value_base": position.market_value_base,
                }
                for position in top_holdings[:5]
            ],
        },
        "largest_risks": risk.top_risks,
        "concentration": risk.concentration,
        "exposures": risk.to_dict()["exposures"],
        "correlation": risk.correlation,
        "lifecycle": {"status": "UNAVAILABLE_UNTIL_PHASE_4"},
        "performance": {"status": "UNAVAILABLE_UNTIL_PHASE_5"},
        "options": {"status": "NOT_RUN_PHASE_6"},
        "hypothetical_simulation_candidates": [
            finding.simulation_direction
            for finding in risk.top_risks
            if finding.simulation_direction is not None
        ],
        "data_gaps": risk.data_quality_notes,
        "risk_report": risk.to_dict(),
    }
    return PortfolioReport(payload)
