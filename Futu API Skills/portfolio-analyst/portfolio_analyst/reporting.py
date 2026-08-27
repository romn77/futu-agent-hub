"""Comprehensive deterministic portfolio report rendering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .models import PortfolioSnapshot, _deep_freeze, _json_value
from .risk import RiskReport


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
            f"- Source snapshot: `{data['source_snapshot_id']}`",
            f"- As of: {data['as_of']}",
            f"- NAV: {data['summary']['nav_base']} {data['base_currency']}",
            f"- Cash: {data['summary']['cash_base']} {data['base_currency']}",
            f"- Position count: {data['summary']['position_count']}",
            "- All weights below use a NAV basis.",
            f"- Data range: point-in-time snapshot at {data['as_of']}; "
            f"correlation lookback {data['correlation']['lookback']} trading days.",
            f"- Data quality: {data['data_quality_status']}",
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
            lines.append(
                f"{index}. **{risk['type']} / {risk['severity']} / {risk['review_priority']}** — "
                f"{risk['summary']}"
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
    top_holdings = sorted(
        snapshot.positions, key=lambda position: (-abs(position.weight), position.symbol)
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
            "position_count": len(snapshot.positions),
            "top_holdings": [
                {
                    "symbol": position.symbol,
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
