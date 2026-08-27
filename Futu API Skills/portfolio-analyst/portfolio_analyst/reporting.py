"""Comprehensive deterministic portfolio report rendering."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

from .models import PortfolioSnapshot, _deep_freeze, _json_value
from .risk import RiskReport, aggregate_economic_positions


@dataclass(frozen=True)
class PortfolioReport:
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _deep_freeze(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self.payload)

    def to_markdown(self) -> str:
        data = self.to_dict()
        lines = [
            "# Portfolio analysis",
            "",
            f"- source_snapshot_id: `{data['source_snapshot_id']}`",
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
            f"- Classified sector: {data['coverage']['classified_sector']}",
            f"- Classified region: {data['coverage']['classified_region']}",
            f"- Price histories: {data['coverage']['price_histories']['available']}/"
            f"{data['coverage']['price_histories']['requested']}",
            "",
            "## Data quality notes",
            "",
            *([f"- {note}" for note in data["data_quality_notes"]] or ["- None"]),
            "",
            "## Top holdings",
            "",
            *[
                f"- {holding['symbol']}: {holding['weight']} "
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
                    f"- Type: {risk['type']}",
                    f"- Severity: {risk['severity']}",
                    f"- Summary: {risk['summary']}",
                    f"- Evidence: {json.dumps(risk['evidence'], ensure_ascii=False, sort_keys=True)}",
                    "- Affected positions: " + (", ".join(risk["affected_positions"]) or "None"),
                    f"- Review priority: {risk['review_priority']}",
                    "- Simulation direction: " + (risk["simulation_direction"] or "Not available"),
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
                    f"{label}: {weight}"
                    for label, weight in data["exposures"]["sector"]["weights"].items()
                ),
                "- Region: "
                + ", ".join(
                    f"{label}: {weight}"
                    for label, weight in data["exposures"]["region"]["weights"].items()
                ),
                "Theme overlap-full and additive-split views are reported separately and must not be summed together.",
                "- Theme full overlap: "
                + ", ".join(
                    f"{label}: {weight}"
                    for label, weight in data["exposures"]["theme_full"]["weights"].items()
                ),
                "- Theme additive split: "
                + ", ".join(
                    f"{label}: {weight}"
                    for label, weight in data["exposures"]["theme_split"]["weights"].items()
                ),
                "",
                "## Correlation",
                "",
                f"- Correlation: {data['correlation']['status']}",
                f"- Coverage: {data['correlation']['coverage']['available']}/"
                f"{data['correlation']['coverage']['requested']} histories",
                "",
                "## Hypothetical simulation candidates",
                "",
                *[f"- {candidate}" for candidate in data["hypothetical_simulation_candidates"]],
                "",
                "## Availability",
                "",
                f"- Lifecycle: {data['lifecycle']['status']}",
                f"- Performance: {data['performance']['status']}",
                f"- Options: {data['options']['status']}",
                "",
                "## Data gaps",
                "",
                *[f"- {note}" for note in data["data_quality_notes"]],
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
    }
    return PortfolioReport(payload)
