# Risk analysis contract

Phase 2 answers one question: what does one immutable portfolio snapshot say about current
structure and its largest review risks? It does not produce independent buy/sell conclusions or
orders. Every JSON and Markdown report repeats the snapshot's `snapshot_id`, `as_of`, coverage,
quality state, and quality notes. Snapshot bytes use descriptor-relative `O_NOFOLLOW` traversal with
file and parent inode revalidation (Windows uses lstat/reparse checks). After schema and full-body
identity checks, a dedicated Phase 1 validator recomputes FX, valuation, weight, NAV, exposure,
alias, and quality invariants before analysis trusts the document.

## Metrics and bases

Concentration uses nonnegative position weights divided by NAV; cash is excluded. Top 1, Top 3,
and Top 5 are descending sums. HHI is `sum(weight ** 2)`, and effective positions is `1 / HHI`.
Before these metrics, holdings from different accounts are grouped by normalized
`MARKET.SYMBOL`. The report, correlation matrix, clusters, and affected-position lists use that
market-qualified identity. A bare ticker in two markets remains two economic positions. Duplicate
rows with conflicting currency/provenance, security type, sector, industry, region, theme set,
asset class, or configured role fail closed; empty, already-qualified, or unsupported identities
are not guessed. Market values and weights are summed only after these gates. Exact-zero quantity
or base-value rows are excluded from every current Phase 2 population. P&L evidence is deferred to
Phase 5. Any nonzero short/hedge position fails with `UNSUPPORTED_PORTFOLIO`; signed net weights are
never presented as ordinary NAV exposure or fed to HHI.

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
The requested calendar interval is `as_of - max(2 * lookback, 180) days` through `as_of`, inclusive;
rows outside it are rejected as lookahead/out-of-request evidence. Defaults cap current positions at 200, history symbols and
requests at 100, pair operations at 4,950, wall time at 30 seconds, each provider call at 5 seconds,
and lookback at 5,000. Top NAV-weight positions are selected deterministically; omissions set
`BUDGET_LIMIT`, a reason, and partial quality. Requests are sequential, optionally rate-spaced, and
a timeout or source-wide exception opens an early circuit breaker.

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
Without a policy, every finding and overall risk are `UNRATED`; evidence is not silently converted
to LOW/MEDIUM advice. Policy-backed findings are rated strictly from serialized thresholds and
breach booleans, then sorted by severity, review priority, stable type, and identity.

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
directions, gaps, and explicit unavailable placeholders for lifecycle (Phase 4), performance
(Phase 5), and options (Phase 6). A simulation direction is hypothetical evidence work, never an
order or an independent buy/sell conclusion.

Both human risk output and comprehensive Markdown repeat `source_snapshot_id`, `as_of`, complete
coverage, data-quality status and notes. Every one of the three findings includes type, severity,
summary, structured evidence, affected market-qualified positions, review priority, and its
explicitly hypothetical simulation direction or an unavailable marker. JSON Schemas recursively
close structured objects with `additionalProperties: false`; dynamic exposure/matrix keys retain
typed value schemas.
Markdown escapes every untrusted label, note, finding, link/HTML metacharacter, and control;
terminal output strips ANSI/control/newline injection. Matrix details remain authoritative in
`portfolio_report.json`, and Markdown records the matrix SHA-256 digest. Portfolio JSON embeds the
complete risk report, and runtime validation requires every duplicated risk field to match exactly.
