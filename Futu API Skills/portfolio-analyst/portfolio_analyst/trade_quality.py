"""Canonical structured trade-quality issue templates."""

from __future__ import annotations

from collections.abc import Iterable


QUALITY_DETAILS = {
    "CONTEXT_FAILED": "one or more requested contexts failed atomically",
    "CONTEXT_OMITTED_BUDGET": "one or more requested contexts were omitted by a bounded work budget",
    "CURRENT_SESSION_INCOMPLETE": "the requested range includes an exchange-local session that may still be in progress",
    "CORPORATE_ACTION_EVIDENCE_UNSUPPORTED": "corporate-action, transfer, and cost-adjustment evidence is unsupported",
    "NONTRADE_EVENTS_EXCLUDED": "recognized corporate-action, transfer, or cost-adjustment rows were excluded from trade metrics",
    "ORPHAN_FILL": "one or more fills have no matching normalized order",
    "ORDER_MISSING_FILLS": "one or more dealt orders lack normalized fills",
    "ORDER_FILL_QTY_MISMATCH": "order dealt quantity and fill quantity do not reconcile",
    "ORDER_STATUS_CONFLICT": "order status conflicts with dealt quantity or fills",
    "UNMATCHED_SELL": "sell evidence lacks opening inventory in the requested range",
    "BASE_MONETARY_FX_INCOMPLETE": "base monetary aggregation lacks complete verified FX evidence",
    "BASE_REALIZED_PNL_FX_INCOMPLETE": "historical base realized P&L lacks transaction-date FX evidence",
    "FEES_UNAVAILABLE": "realized P&L is gross pre-fee because fee evidence is unavailable",
    "SOURCE_UNAVAILABLE": "no requested context completed successfully",
    "ANALYSIS_BUDGET_EXCEEDED": "bounded trade analysis budget was exceeded",
    "SOURCE_HISTORY_ROW_CAP": "historical order evidence is bounded by the audited 200-row endpoint option",
    "REENTRY_POLICY_UNCONFIGURED": "reentry metric is unavailable because no cooldown policy is configured",
    "OPEN_WINDOW_POLICY_UNCONFIGURED": "market-open metric is unavailable because no open-window policy is configured",
    "TURNOVER_SNAPSHOT_NAV": "turnover uses one immutable snapshot NAV rather than average period NAV",
}


def canonical_quality_issues(codes: Iterable[str]) -> list[dict[str, str]]:
    unique = sorted(set(codes))
    if any(code not in QUALITY_DETAILS for code in unique):
        raise ValueError("unknown structured trade-quality code")
    return [{"code": code, "detail": QUALITY_DETAILS[code]} for code in unique]
