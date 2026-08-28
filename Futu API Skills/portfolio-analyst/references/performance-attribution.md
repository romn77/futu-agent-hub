# Performance attribution contract

Point-in-time `analyze` reports `NOT_INCLUDED_USE_PERFORMANCE_COMMAND` and does not invoke this
historical workflow. Attribution is available only through the explicit `performance` command
described below.

## Scope and interval

`performance --from YYYY-MM-DD --to YYYY-MM-DD` selects at least one validated immutable snapshot
whose precise timezone-aware `as_of` values fall inside the inclusive UTC calendar dates. The
period is capped at 366 days and 366 snapshots. Both requested dates need a valuation. Adjacent
valuations more than 36 hours apart are serialized as gaps and prohibit an accurate mode.

All selected snapshots must retain the same environment, base currency, account identities and
alias provenance. Each snapshot is reconstructed through the strict Phase 1 schema, complete-body
content identity, and financial semantic validator. The report records every source ID plus a full
SHA-256 serialization digest, the source `data_quality` status/warnings/missing fields/source
errors, plus bounded per-position quality notes keyed by account and canonical identity, selected
valuation timestamps/NAVs, precise endpoint rules, and gap coverage. Any position note is treated
as incomplete source quality. Phase 1 rejects duplicate economic legs with the same account and
canonical identity before a report can be built. A partial,
stale, reconstructed, unavailable, or error source cannot authorize `ACCURATE_PNL` or HIGH
confidence. The CLI freezes one trusted `analysis_now`; future snapshots, including later instants
on the same UTC date, fail with `FUTURE_SNAPSHOT`. Price cutoffs cannot exceed that instant.
Direct library calls use the real UTC clock when `analysis_now` and an injected clock are absent;
they never trust the ending snapshot as the current time.

## P&L authority

Positions first aggregate by canonical `MARKET.SYMBOL` while retaining sorted account aliases.
The identity population is the union across every selected snapshot, not just the endpoints. Each
report embeds an adjacent-snapshot trajectory with presence, account-leg quantity, cumulative
realized/unrealized base fields, classification, FX and quality evidence. Sector, region and theme
classifications must agree across every account leg and the full trajectory. A middle-only
identity, account-leg/quantity lifecycle reset or transfer, FX authority-contract discontinuity, or
incomplete field is explicit and unavailable unless complete authoritative trade evidence
reconciles it; it is never silently dropped or presented as HIGH-confidence snapshot attribution.
Realized and unrealized P&L values are not assumed monotonic: any positive or negative adjacent
delta is valid evidence and can rank as a contributor or detractor. A numeric decline alone is not
a reset signal.

For an available symbol:

```text
realized contribution   = sum(delta(cumulative realized P&L base) for every adjacent snapshot pair)
unrealized contribution = sum(delta(cumulative unrealized P&L base) for every adjacent snapshot pair)
symbol contribution     = realized contribution + unrealized contribution
```

For cross-currency positions, every trajectory leg binds the position currency and provenance to
the snapshot FX evidence rate, target base currency, source authority, effective timestamp, and
`SNAPSHOT_AS_OF_NON_LOOKAHEAD_BASE_CONVERSION_V1` contract. The rate value may legitimately change
between snapshot timestamps. When the per-snapshot evidence remains semantically valid and the
source/authority contract is continuous, cumulative base-P&L deltas include the resulting FX
movement. Missing, look-ahead, stale/partial-quality, currency-changing, or source-changing evidence
downgrades the identity; a normal rate move such as 1.1 to 1.2 does not.

`ACCURATE_PNL` requires at least two snapshots, complete contributions, both endpoint dates,
continuous snapshots, consistent FX currency-pair source provenance, authoritative full-period
trade coverage, and at least two adjusted closes on one common aligned interval for every
authoritative trajectory symbol. Full-period trade coverage is required even when snapshots are
stable because a symbol can be opened and fully closed between valuations without ever appearing
in a trajectory. It claims only exact cumulative-field deltas plus fully evidenced closed-only
FIFO round trips. It does
not claim transaction-date fee, FX, transfer or corporate-action reconstruction. Otherwise the mode
is `RECONSTRUCTED` with LOW confidence and explicit unavailable symbols. With one current/end
snapshot and sufficient common adjusted-close history, current base-currency holdings may carry a
gross unrealized estimate:

```text
estimated gross unrealized base = ending quantity * (aligned ending adjusted close - aligned beginning adjusted close) * 1
```

The optional authoritative trade path reuses Phase 3 `FutuTradeHistoryService`, its pinned read-only
orders/fills commands, stable account aliases, context-atomic acquisition, and existing row,
context, failure-circuit, and wall budgets. The CLI attempts this path only when the stable alias
key and audited Skill already exist; it never creates alias state during performance analysis.
The present Phase 3 adapter marks corporate-action evidence as unsupported, so its collected review
is retained as structured evidence but cannot authorize FIFO contributions until that coverage is
made authoritative.

Bounded acquisition failures never expose source exception text. The CLI records one canonical
reason in `trade_evidence.reason`: `TRADE_SOURCE_TIMEOUT`, `TRADE_SOURCE_BUDGET_EXCEEDED`,
`TRADE_SOURCE_READ_FAILED`, `TRADE_SOURCE_CONTRACT_INVALID`, or
`TRADE_ALIAS_AUTHORITY_UNAVAILABLE`. These failures leave independent price/snapshot attribution
available, add a structured `TRADE_HISTORY_UNAVAILABLE` quality reason, and do not change return
authority rules. Unexpected programming errors are not swallowed by this degradation boundary.

Validated normalized fills are FIFO matched by account and canonical identity. Fully in-period
closed lots contribute gross pre-fee realized P&L, including symbols absent from the ending
snapshot. When authoritative open lots are available, current-symbol unrealized P&L is instead:

```text
open-lot unrealized gross base = sum((aligned ending close - lot cost) * lot quantity)
```

Open-lot quantities must exactly equal every ending snapshot account/identity leg. Fill prices and
adjustment/corporate-action semantics must be consistent; otherwise the base contribution is null
and partial. The aligned ending close must be on or after the lot's first eligible completed market
session; a fill after market close advances that boundary to the next completed session. Thus a
lot bought at 109 and ending at 110 contributes 1, regardless of an earlier
period close of 100. The current-holding period-price-change formula is retained only as a
separately named LOW-confidence fallback when authoritative trade evidence is unavailable.
Base-currency fills convert exactly 1:1. Non-base FIFO P&L remains visible in local-currency buckets
but its base contribution is null without transaction-date FX. Unmatched sells/opening inventory,
transfers, corporate actions, orphan fills, order/fill quantity mismatches, status conflicts, failed
contexts, and budget omissions fail closed. A fully closed FIFO round trip whose identity is absent
from every snapshot trajectory contributes gross pre-fee realized P&L and participates in rankings.
For an identity present in a trajectory, FIFO realized P&L is never added to snapshot P&L; it must
exactly reconcile to the cumulative realized delta. A mismatch makes that identity unavailable,
preventing duplicate counting. Unavailable, timed-out, partial, or corporate-action-unsupported
trade evidence cannot authorize `ACCURATE_PNL`/HIGH; current-holding-only reconstruction remains
LOW confidence and explicitly excludes realized P&L and exited positions. A reconstructed report
never claims portfolio return or TWR.

When complete external-flow coverage exists, the report also reconciles available attributed P&L
against `ending NAV - beginning NAV - external flows`. A non-zero residual is serialized as
`PNL_NAV_RECONCILIATION_MISMATCH` and prevents `ACCURATE_PNL`; possible causes such as fees,
corporate actions, or cost-basis resets are not guessed. Without complete flow evidence this
cross-check is explicitly `UNAVAILABLE` and does not turn a snapshot-field delta into a return claim.

Sector and region are additive. Theme-full repeats a multi-theme symbol's full contribution in each
theme and is intentionally overlapping. Theme-split sorts labels, divides the first `n-1` shares,
then assigns the exact Decimal residual to the last label, so each identity and aggregate reconcile
exactly. An empty theme list maps to `UNKNOWN`. Rankings use contribution descending then canonical identity for
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

Price evidence gates `ACCURATE_PNL` and supplies bounded reconstruction inputs. The only provider is the pinned audited
`get_kline` command using forward-adjusted daily closes. Each series records exact rows, requested
dates, latest completed market-local close under `REGULAR_SESSIONS_V1`, digest, source and coverage.
Rows before the requested start or after the completed-close cutoff are rejected. When comparing
multiple sequences, closes are intersected on common dates before any return interval is formed.

Budgets are 100 symbols, 1,000 rows per symbol, 20,000 rows in aggregate, two seconds per call and 30 seconds overall. Three
consecutive failures open a source-wide circuit breaker. Snapshot, position, flow, schema-node,
document-byte, validation-operation and validation-wall budgets fail closed or produce explicit
price-only partial coverage as appropriate.

## Output and security

The strict schema rejects unknown fields and caps every collection. Shape preflight runs before
schema validation, and operation/wall budgets are consumed while reading every source position and
reconstructing every trajectory snapshot, leg and adjacent pair. Runtime validation reconstructs
period gaps, source quality, P&L totals and splits, aggregates, rankings, price row digests/cutoffs,
embedded Phase 3 trade-review semantics/digest/FIFO, open-lot references/local-currency buckets,
first-eligible-session dates, overlap reconciliation, closed-only identities, flow totals, TWR,
quality coverage and the complete report ID. Human and Markdown views escape all untrusted
labels, show raw and aligned price coverage, formulas, source evidence, limitations and unavailable modules.

The CLI range reader first narrows canonical IDs to the requested UTC year/month and whole-second
envelope, then applies row/byte budgets and descriptor-reads bodies for exact microsecond `as_of`
filtering. Preflight sizes are not trusted: the opened descriptor's device/inode is matched and the
actual bytes read are accumulated again, rejecting replacement or whitespace expansion beyond the
consumer budget. Thus unrelated history and out-of-range corrupt bodies do not exhaust or block a
small range; matching misplaced, duplicate, or corrupt entries fail closed.

`--output DIR` writes `performance_attribution.json`, `performance_attribution.md`, then
`manifest.json` as one private no-replace transaction below `PORTFOLIO_AGENT_HOME`. `--no-save`
always wins. No command writes trades or appends journal events.
