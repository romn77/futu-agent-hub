# Risk analysis contract

A-share positions retain their original `SH.` / `SZ.` identity in the optional snapshot
`source_code` field while keeping `CN` as the reporting market. Risk and performance commands
use that source identity for Futu history requests. Legacy snapshots remain readable with their
original content hashes; missing or conflicting exchange evidence makes only that identity's
history unavailable, without guessing an exchange or opening the source failure circuit.

Phase 2 answers one question: what does one immutable portfolio snapshot say about current
structure and its largest review risks? It does not produce independent buy/sell conclusions or
orders. Every JSON and Markdown report repeats the snapshot's `snapshot_id`, `as_of`, coverage,
quality state, and quality notes. Snapshot bytes use descriptor-relative `O_NOFOLLOW` traversal with
file and parent inode revalidation. Platforms without equivalent secure handles fail closed rather
than falling back to path checks. After schema and full-body
identity checks, a dedicated Phase 1 validator checks FX, valuation, weight, NAV source evidence,
exposure, alias, and quality invariants before analysis trusts the document. Snapshot `1.1` uses
OpenD account totals as NAV; only legacy `1.0` validates a cash-plus-positions NAV identity.

The snapshot ID timestamp is the UTC `as_of` floored to a whole second. A precise microsecond
`as_of` remains in the body and therefore in its full-body digest; validators compare the readable
ID timestamp at whole-second precision but reject a mismatch crossing a second boundary.

## Metrics and bases

Concentration uses nonnegative position weights divided by NAV; cash is excluded. Top 1, Top 3,
and Top 5 are descending sums. Risk/portfolio report 1.1 retains NAV HHI as
`sum(weight ** 2)`. Holdings-only HHI is `NAV_HHI / sum(weight) ** 2`, and effective positions
is `sum(weight) ** 2 / NAV_HHI`. These two relative-holdings metrics exclude cash; an empty
holdings population yields null. NAV, cash and individual/Top-N NAV weights are unchanged.
Legacy report 1.0 retains its original `1 / NAV_HHI` effective-position definition and is labeled
as legacy when rendered. New report versions cannot be downgraded by stripping fields.
Before these metrics, holdings from different accounts are grouped by normalized
`MARKET.SYMBOL`. The report, correlation matrix, clusters, and affected-position lists use that
market-qualified identity. A bare ticker in two markets remains two economic positions. Duplicate
rows with conflicting currency/provenance, security type, sector, industry, region, theme set,
asset class, or configured role fail closed; empty, already-qualified, or unsupported identities
are not guessed. Market values and weights are summed only after these gates. Exact-zero quantity
or base-value rows are excluded from every current Phase 2 population. Any nonzero short/hedge
position fails with `UNSUPPORTED_PORTFOLIO`; signed net weights are never presented as ordinary NAV
exposure or fed to HHI.

P&L concentration is structural snapshot evidence, not the Phase 5 performance series. It is
computed only when every analyzed current economic position has finite `unrealized_pl_base` and
`realized_pl_base` evidence and no current position was omitted by the work budget. The two fields
are summed per canonical position, then positive totals and absolute negative totals form separate
profit and loss pools. The report records each pool total and its largest contributor share; a
zero-size pool has a null share. Duplicate-account P&L is aggregated under the canonical position.
Missing, nonfinite, or budget-omitted evidence sets `pnl_evidence_status: PARTIAL`, adds a quality
note, and emits no `PNL_CONCENTRATION` finding. An empty current population is `UNAVAILABLE`.

Sector and region views add each position's NAV weight. Aggregation defensively applies the shared
Phase 1 sentinel normalization: blank values and case-insensitive `UNKNOWN`, `N/A`, `NONE`, and
`NULL` become the literal `UNKNOWN`. Theme tags use the same normalization, discard sentinel tags
when a real tag exists, and are deterministically deduplicated; a position with no real tag uses
only `UNKNOWN`. Empty or alternate-sentinel exposure keys are never emitted. Theme exposure always
has two separately named views:

- `OVERLAP_FULL_ALLOCATION`: every tag receives the full position weight. Totals may exceed 1.
- `ADDITIVE_SPLIT_ALLOCATION`: weight is divided equally over unique tags. A position without tags
  contributes to `UNKNOWN`; the view is additive.

These views must never be added together or substituted for each other. Role imbalance is emitted
only when every analyzed holding has an explicit role. Option delta is intentionally not calculated
in Phase 2.

## Correlation evidence

The audited official `quote/get_kline.py` is pinned by normalized SHA-256 content identity. Its
adapter allowlist permits only a historical, daily (`1d`), forward-adjusted (`forward`) invocation
with explicit start/end dates, `NONE` session, page size 1000, and JSON output. Other K types,
unadjusted prices, arbitrary pagination, malformed codes/dates, and unknown flags fail before a
subprocess starts. The official file is not modified.

For each pair, correlation first intersects adjusted-close dates and only then computes both return
series over identical consecutive common-date intervals. It never compares a multi-session return
with a one-session return under one end date. Defaults are 120 trading prices, at least 60
overlapping return intervals, positive high correlation 0.75, and positive medium 0.55. Lookback is an
integer from 2 through 5000; minimum overlap is a positive integer no greater than `lookback - 1`.
Thresholds are finite Decimals satisfying `0 <= medium <= high <= 1` and are serialized in the
correlation evidence. Pearson means, covariance, variance, square root, and division all remain
Decimal at a deterministic precision; there is no float conversion. Dates must be
strictly increasing and unique; closes must be finite and positive. Constant series, malformed
rows, duplicate/non-monotonic dates, missing histories, or too little overlap produce
`PARTIAL`/`UNAVAILABLE` coverage and notes. No value is imputed and no pseudo-correlation is made.
Positive `pairs` and concentration clusters use only `coefficient >= threshold`. Strong negative
coefficients are serialized separately as `inverse_pairs` diversification evidence and never form
a shared-shock cluster. High-correlation clusters are deterministic connected components of
positive high-correlation pairs.
The matrix is square over every available canonical identity. A symmetric `null` cell explicitly
marks a pair without sufficient evidence; it is not an imputed coefficient. Runtime validation in
both report entry points checks coverage/status consistency, threshold ordering, square symmetric
matrix identity and diagonal rules, pair uniqueness/count/band agreement, and exact connected
components for high-correlation clusters. Structurally valid but semantically inconsistent reports
are rejected with a domain validation error.
The versioned `REGULAR_SESSIONS_V1` approximation computes the upper date bound separately for
every market-qualified identity from the snapshot instant, its IANA market timezone, and regular
close time (including Japan at 15:30 JST). Before that local close, the preceding weekday is used;
weekends roll back to Friday. This prevents a US close later on the snapshot's UTC calendar date
from entering evidence. This is deliberately not a complete exchange calendar: holidays,
unscheduled halts, and exceptional or early closes are not inferred. Missing or inconsistent bars
remain fail-closed and produce `PARTIAL`/`UNAVAILABLE` evidence rather than a fabricated session.
The requested interval starts
`max(2 * lookback, 180) days` before that cutoff, and every cutoff is serialized in `date_cutoffs`.
Rows outside the interval are rejected as lookahead/out-of-request evidence. Exchange holidays
produce no row and therefore cannot be fabricated as a return observation.

All O(N) structure metrics, exposures, policies, and findings always use the complete canonical
current-position population. The misleading `max_positions` setting has been removed. Only
adjusted-history requests and O(N²) pair work are bounded.
Defaults cap history symbols and requests at 100, pair operations at 4,950, wall time at 30 seconds,
each provider call at 5 seconds, and lookback at 5,000. Top NAV-weight history symbols are selected
deterministically; history omissions set `BUDGET_LIMIT`, a reason, and partial quality without
changing structural metrics or producing a false policy non-breach. Requests are sequential,
optionally rate-spaced, and a timeout or source-wide exception opens an early circuit breaker.

The live adapter requires the Futu SDK and OpenD. When either is unavailable, the portfolio report
still succeeds and states correlation is unavailable. Tests inject deterministic adjusted-price
providers and do not require live access.

## Policy thresholds

There are no personal-investment defaults. Optional thresholds come only from
`config/risk_policy.json` below `PORTFOLIO_AGENT_HOME`:

```json
{
  "provenance": "USER_CONFIG",
  "thresholds": {
    "single_position": "0.35",
    "top_3": "0.75",
    "top_5": "0.90",
    "sector": "0.60",
    "region": "0.75",
    "theme_split": "0.60"
  }
}
```

Values are Decimal NAV fractions in `(0, 1]`. The report says `NOT_CONFIGURED` when the file is
absent and reports evidence without threshold-based claims. When configured, every check serializes
the measured value, threshold, and breach boolean. Policy never checks the overlap-full theme view.
Version 1.1 exposes `classification_coverage` independently for sector, region and theme_split:
known/unknown NAV weights, largest-known lower bound, lower-plus-unknown upper bound, optional
threshold, and status. UNKNOWN remains visible in exposure tables but is never a real sector,
region or theme concentration candidate. Lower > threshold confirms BREACHED; upper <= threshold
confirms WITHIN_LIMIT; an intervening threshold is UNKNOWN; an absent threshold is NOT_CONFIGURED.
Configured classification checks retain measured=lower, threshold and status; breached is null
for UNKNOWN, true for BREACHED, false for WITHIN_LIMIT. Never interpret null as a passed check.
All-unknown classifications generate explicit UNRATED classification gaps with no simulation
suggestion. A known category with unknown constraint status remains UNRATED. Complete known
breaches remain HIGH even if other classification data is missing.
Without a policy, every finding and overall risk are `UNRATED`; evidence is not silently converted
to LOW/MEDIUM advice. Policy-backed findings are rated strictly from serialized thresholds and
breach booleans, then sorted by severity, review priority, stable type, and identity.
Position, sector, region, theme, P&L, role, and correlation candidates are generated independently
before this single Top-3 sort; configuring a role cannot suppress a correlation or theme candidate.
Every nonempty portfolio generates a known-region candidate or an explicit region-classification
gap. Known-region candidates are UNRATED without policy or when constraint status is UNKNOWN;
WITHIN_LIMIT/BREACHED support LOW/HIGH. The evidence flag means a threshold was configured,
not that missing classification is resolved.
Runtime semantic validation rejects a breached policy candidate omitted in favor of a lower-ranked
finding. When more than three breached dimensions exist, the same deterministic rank selects the
three highest-priority candidates while every breach remains explicit in `policy_checks`.

## Commands and persistence

```bash
python scripts/portfolio.py risk --snapshot snapshots/FILE.json --lookback 120 --json --no-save
python scripts/portfolio.py risk --latest --json --output reports/risk.json
python scripts/portfolio.py analyze --snapshot snapshots/FILE.json --json --no-save
python scripts/portfolio.py analyze --latest --output reports/RUN
```

Exactly one of `--latest` and `--snapshot` is required. Inputs and outputs are relative and confined
below `PORTFOLIO_AGENT_HOME`; symlinked snapshot paths are rejected. Default and `--no-save` runs
create no state. `risk --output FILE` saves one immutable JSON file. `analyze --output DIRECTORY`
saves immutable `portfolio_report.json` and `portfolio_report.md` as one staged, preflighted,
rollback-capable transaction; collision or commit failure leaves neither new sibling. Non-persisting Phase 1 snapshots
use ephemeral account aliases; reports from separate ephemeral invocations cannot safely join those
aliases and explicitly inherit that quality warning.

The comprehensive report contains data range/quality, NAV/cash/count/top holdings, exactly three
largest review risks, concentration/exposure/correlation evidence, hypothetical simulation
directions, gaps, and the complete Phase 6 option-risk subreport. The option subreport can degrade
to `PARTIAL`/`UNAVAILABLE` without changing Phase 2 findings or overall-risk policy. Phase 2 still
fails closed for signed portfolios; standalone `options` analysis does not bypass that rule in
`analyze`. Point-in-time `analyze` does not silently run historical attribution: its performance
status is `NOT_INCLUDED_USE_PERFORMANCE_COMMAND`, directing callers to the separately bounded
`performance` command. A simulation direction is hypothetical evidence work, never an order or an independent
buy/sell conclusion. See [option-risk.md](option-risk.md) for the separate raw-Greek and static
metadata contract.

Both human risk output and comprehensive Markdown repeat `source_snapshot_id`, `as_of`, complete
coverage, data-quality status and notes. Every one of the three findings includes type, severity,
summary, structured evidence, affected market-qualified positions, review priority, and its
explicitly hypothetical simulation direction or an unavailable marker. JSON Schemas recursively
close structured objects with `additionalProperties: false`; dynamic exposure/matrix keys retain
typed value schemas.
Markdown escapes every untrusted label, note, finding, link/HTML metacharacter, and control;
terminal output strips ANSI/control/newline injection. Matrix details remain authoritative in
the JSON representation, and Markdown records the matrix SHA-256 digest. Saved bundles name
`portfolio_report.json`; no-save Markdown does not claim that an artifact exists. Portfolio JSON
has a recomputed content-addressed `portfolio_report_id`, and the bundle manifest uses that ID
rather than the lifecycle subreport identity. It embeds both the complete risk report and complete
option-risk report. Runtime validation requires
duplicated risk fields to match exactly and independently reconstructs the option source selection,
quote evidence, legs, per-Greek coverage/aggregates, expiry buckets, quality reasons, status, and
content identity from the embedded immutable snapshot. Schema 1.1 additionally replays static
metadata, same-snapshot money Delta/moneyness, and aggregated underlying relationships; legacy 1.0
reports retain their original raw-Greek-only replay. Only after that option evidence validates,
the portfolio validator binds NAV and cash to the embedded source snapshot and checks weight,
position count, and Top-5 from its positions. It never substitutes cash plus holdings for an
OpenD-sourced NAV, and binds gaps and simulation candidates to the embedded risk report.
