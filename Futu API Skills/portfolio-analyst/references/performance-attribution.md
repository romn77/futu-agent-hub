# Performance attribution contract

## Scope and interval

`performance --from YYYY-MM-DD --to YYYY-MM-DD` selects at least two validated immutable snapshots
whose precise timezone-aware `as_of` values fall inside the inclusive UTC calendar dates. The
period is capped at 366 days and 366 snapshots. Both requested dates need a valuation. Adjacent
valuations more than 36 hours apart are serialized as gaps and prohibit an accurate mode.

All selected snapshots must retain the same environment, base currency, account identities and
alias provenance. Each snapshot is reconstructed through the strict Phase 1 schema, complete-body
content identity, and financial semantic validator. The report records every source ID plus a full
SHA-256 serialization digest, selected valuation timestamps/NAVs, precise endpoint rules, and gap
coverage.

## P&L authority

Positions first aggregate by canonical `MARKET.SYMBOL` while retaining sorted account aliases.
Sector, region and theme classifications must agree across every account leg and both endpoints.
Account-leg changes, endpoint absence, or incomplete realized/unrealized base fields make that
symbol unavailable. This avoids interpreting transfers, opens, closes or corporate actions as
investment performance.

For an available symbol:

```text
realized contribution   = ending cumulative realized P&L base - beginning value
unrealized contribution = ending cumulative unrealized P&L base - beginning value
symbol contribution     = realized contribution + unrealized contribution
```

`ACCURATE_PNL` requires complete contributions, both endpoint dates, continuous snapshots and
consistent FX currency-pair source provenance. It claims only exact endpoint-field deltas. It does
not claim transaction-date fee, FX, transfer or corporate-action reconstruction. Otherwise the mode
is `RECONSTRUCTED` with LOW confidence and explicit unavailable symbols; no missing value is filled
with a market-value delta.

When complete external-flow coverage exists, the report also reconciles available attributed P&L
against `ending NAV - beginning NAV - external flows`. A non-zero residual is serialized as
`PNL_NAV_RECONCILIATION_MISMATCH` and prevents `ACCURATE_PNL`; possible causes such as fees,
corporate actions, or cost-basis resets are not guessed. Without complete flow evidence this
cross-check is explicitly `UNAVAILABLE` and does not turn a snapshot-field delta into a return claim.

Sector and region are additive. Theme-full repeats a multi-theme symbol's full contribution in each
theme and is intentionally overlapping. Theme-split divides it equally and is additive. An empty
theme list maps to `UNKNOWN`. Rankings use contribution descending then canonical identity for
contributors, and contribution ascending then identity for detractors.

## Cash flows and returns

External-flow evidence has strict unique IDs, timezone-aware timestamps, signed deposits and
withdrawals, explicit internal transfers, currency, positive FX, base amount, source provenance and
coverage. Internal transfers never affect return. Rows are scoped to `(first valuation, final
valuation]`; a flow at the beginning is already represented by beginning NAV.

TWR is available only when flow coverage spans all selected valuations, both endpoint dates are
covered, and cadence is continuous:

```text
subperiod factor = (ending NAV - external flows in (begin, end]) / beginning NAV
TWR = product(subperiod factors) - 1
```

This is the versioned `TWR_END_OF_SUBPERIOD_FLOW_ADJUSTMENT` convention. The current CLI cannot use
the official cash-flow script because it is outside the immutable audited adapter allowlist; it
records that limitation and returns null. IRR is explicitly not calculated.

## Price evidence and budgets

Price evidence is independent of snapshot-field P&L. The only provider is the pinned audited
`get_kline` command using forward-adjusted daily closes. Each series records exact rows, requested
dates, latest completed market-local close under `REGULAR_SESSIONS_V1`, digest, source and coverage.
Rows before the requested start or after the completed-close cutoff are rejected. When comparing
multiple sequences, closes are intersected on common dates before any return interval is formed.

Budgets are 100 symbols, 1,000 rows per symbol, 20,000 rows in aggregate, two seconds per call and 30 seconds overall. Three
consecutive failures open a source-wide circuit breaker. Snapshot, position, flow, schema-node,
document-byte, validation-operation and validation-wall budgets fail closed or produce explicit
price-only partial coverage as appropriate.

## Output and security

The strict schema rejects unknown fields and caps every collection. Runtime validation reconstructs
period gaps, P&L totals and splits, aggregates, rankings, price row digests/cutoffs, flow totals,
TWR, quality coverage and the complete report ID. Human and Markdown views escape all untrusted
labels, show coverage, formulas, source evidence, limitations and unavailable modules.

`--output DIR` writes `performance_attribution.json`, `performance_attribution.md`, then
`manifest.json` as one private no-replace transaction below `PORTFOLIO_AGENT_HOME`. `--no-save`
always wins. No command writes trades or appends journal events.
