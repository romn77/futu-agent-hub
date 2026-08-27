# Risk analysis contract

Phase 2 answers one question: what does one immutable portfolio snapshot say about current
structure and its largest review risks? It does not produce independent buy/sell conclusions or
orders. Every JSON and Markdown report repeats the snapshot's `snapshot_id`, `as_of`, coverage,
quality state, and quality notes. A saved snapshot is schema-validated and its full-body content ID
is recomputed before analysis.

## Metrics and bases

Concentration uses nonnegative position weights divided by NAV; cash is excluded. Top 1, Top 3,
and Top 5 are descending sums. HHI is `sum(weight ** 2)`, and effective positions is `1 / HHI`.
Before these metrics, holdings from different accounts are grouped by normalized
`MARKET.SYMBOL`. The report, correlation matrix, clusters, and affected-position lists use that
market-qualified identity. A bare ticker in two markets remains two economic positions. Duplicate
rows with conflicting currency/provenance, security type, sector, industry, region, theme set,
asset class, or configured role fail closed; empty, already-qualified, or unsupported identities
are not guessed. Market values, weights, and supported P&L fields are summed only after these gates.
When a snapshot has any signed short/hedge weight or positive short exposure, all long-only
concentration metrics are `UNAVAILABLE`: signed weights are never silently squared into an HHI.
The report retains gross/net evidence for review.

Sector and region views add each position's NAV weight. Missing classifications remain the literal
`UNKNOWN`. Theme exposure always has two separately named views:

- `OVERLAP_FULL_ALLOCATION`: every tag receives the full position weight. Totals may exceed 1.
- `ADDITIVE_SPLIT_ALLOCATION`: weight is divided equally over unique tags. A position without tags
  contributes to `UNKNOWN`; the view is additive.

These views must never be added together or substituted for each other. P&L concentration is
emitted only when reported realized or unrealized base-currency P&L exists. Role imbalance is
emitted only when every analyzed holding has an explicit role. Option delta is intentionally not
calculated in Phase 2.

## Correlation evidence

The audited official `quote/get_kline.py` is pinned by normalized SHA-256 content identity. Its
adapter allowlist permits only a historical, daily (`1d`), forward-adjusted (`forward`) invocation
with explicit start/end dates, `NONE` session, page size 1000, and JSON output. Other K types,
unadjusted prices, arbitrary pagination, malformed codes/dates, and unknown flags fail before a
subprocess starts. The official file is not modified.

Correlation uses aligned adjusted-close daily returns. Defaults are 120 trading prices, at least 60
overlapping return observations, high absolute correlation 0.75, and medium 0.55. Lookback is an
integer from 2 through 5000; minimum overlap is a positive integer no greater than `lookback - 1`.
Thresholds are finite Decimals satisfying `0 <= medium <= high <= 1` and are serialized in the
correlation evidence. Pearson means, covariance, variance, square root, and division all remain
Decimal at a deterministic precision; there is no float conversion. Dates must be
strictly increasing and unique; closes must be finite and positive. Constant series, malformed
rows, duplicate/non-monotonic dates, missing histories, or too little overlap produce
`PARTIAL`/`UNAVAILABLE` coverage and notes. No value is imputed and no pseudo-correlation is made.
High-correlation clusters are deterministic connected components of high-correlation pairs.

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
Without a policy, LOW/MEDIUM severity is a neutral deterministic review-order label based on the
relative structure present in the same snapshot; it is not a threshold breach or personal advice.
Only a serialized `policy_threshold_applied: true` can elevate a supported finding for a configured
threshold breach.

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
saves immutable `portfolio_report.json` and `portfolio_report.md`. Non-persisting Phase 1 snapshots
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
