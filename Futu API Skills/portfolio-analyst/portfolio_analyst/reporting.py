"""Comprehensive deterministic portfolio report rendering."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping

from .models import PortfolioSnapshot, _deep_freeze, _json_value
from .rendering import markdown_text
from .risk import RiskReport, aggregate_economic_positions
from .lifecycle import LifecycleReport


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
        if data["lifecycle"]["status"] != "UNAVAILABLE_UNTIL_PHASE_4":
            lifecycle = data["lifecycle"]
            lines.extend(
                [
                    "",
                    "## Lifecycle findings and audit",
                    "",
                    f"- source_snapshot_id: `{md(lifecycle['source_snapshot_id'])}`",
                    f"- Status: {lifecycle['status']}",
                    "- Coverage: "
                    + md(json.dumps(lifecycle["coverage"], ensure_ascii=False, sort_keys=True)),
                    "- Quality notes: "
                    + ("; ".join(md(note) for note in lifecycle["data_quality_notes"]) or "None"),
                ]
            )
            for item in lifecycle["positions"]:
                thesis = item["original_thesis_evidence"]
                thesis_display = (
                    "not configured"
                    if thesis is None
                    else f"configured (sha256={thesis['sha256']}, characters={thesis['characters']})"
                )
                lines.extend(
                    [
                        "",
                        f"### Lifecycle {md(item['symbol'])}",
                        "",
                        f"- {md(item['symbol'])}: {item['lifecycle_status']}",
                        f"- Role / record status / lifecycle: {md(item['role'])} / {md(item['record_status'] or 'UNCONFIGURED')} / {item['lifecycle_status']}",
                        f"- Current weight: {item['current_weight'] if item['current_weight'] is not None else 'N/A'}",
                        "- Target bounds: "
                        + (
                            f"{item['target_weight']['min']} to {item['target_weight']['max']}"
                            if item["target_weight"] is not None
                            else "N/A"
                        ),
                        f"- Opened / horizon / last review: {item['opened_at'] or 'N/A'} / {md(item['holding_horizon'] or 'N/A')} / {item['last_reviewed_at'] or 'N/A'}",
                        f"- Thesis status: {item['thesis_status']}; Original thesis: {thesis_display}",
                        f"- Review priority: {item['review_priority']}",
                        f"- History status: {item['history_evidence_status']}; excluded pre-open={item['history_excluded_pre_open']}; role policy: {md(json.dumps(item['role_drift_policy'], sort_keys=True))}",
                        "- Position quality: "
                        + ("; ".join(md(note) for note in item["data_quality_notes"]) or "None"),
                        "- History provenance: "
                        + (
                            "; ".join(
                                f"{md(entry['snapshot_id'])} | {entry['as_of']} | {md(entry['identity'])} | weight={entry['weight']}"
                                for entry in item["history_evidence"]
                            )
                            or "None"
                        ),
                        "- Findings: "
                        + (
                            "; ".join(
                                f"type={finding['type']}, severity={finding['severity']}, priority={finding['review_priority']}, "
                                f"summary={md(finding['summary'])}, evidence={md(json.dumps(finding['evidence'], ensure_ascii=False, sort_keys=True))}"
                                for finding in item["findings"]
                            )
                            or "None"
                        ),
                    ]
                )
        return "\n".join(lines) + "\n"


def build_portfolio_report(
    snapshot: PortfolioSnapshot,
    risk: RiskReport,
    lifecycle: LifecycleReport | None = None,
) -> PortfolioReport:
    if risk.source_snapshot_id != snapshot.snapshot_id or risk.as_of != snapshot.as_of:
        raise ValueError("risk and portfolio report inputs must reference the same snapshot")
    if lifecycle is not None and (
        lifecycle.source_snapshot_id != snapshot.snapshot_id or lifecycle.as_of != snapshot.as_of
    ):
        raise ValueError("lifecycle and portfolio report inputs must reference the same snapshot")
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
        "lifecycle": (
            {"status": "UNAVAILABLE_UNTIL_PHASE_4"} if lifecycle is None else lifecycle.to_dict()
        ),
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
