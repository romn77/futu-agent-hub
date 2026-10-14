# Technical evidence tool contract

## Current CLI

```bash
python scripts/portfolio.py technical --snapshot <relative-snapshot.json> --json --no-save
```

Bind an available validated same-snapshot risk report with
`--risk-report <relative-risk-report.json>`. CLI-only `--symbol AAPL` is repeatable for at most eight
explicit nonzero held US STOCK/ETF symbols; unheld securities/options fail closed. ETFs remain
single positions. The default one-shot helper passes no user-named symbols: it runs technical after
risk, using successful risk evidence or weight-only selection. Consume that result; do not rerun
technical, the helper, or another price source to fill coverage gaps.

## Actual selection and coverage

Eligible account rows group by canonical `US.<SYMBOL>` with absolute snapshot weight. The hard cap
is eight, even when research expands. Selection includes ≥5% NAV identities; fills to five by weight
if needed; then includes explicit held identities; then identities affected by the first three
`top_risks` in risk order. Within a rule, ties use weight descending then canonical identity ascending;
truncate at eight.

Use actual selected identities and available fields, not the cap or research count, to describe
coverage. `explicit_requested` records bounded sorted requests even when omitted; selection reasons,
tie-breaker, truncation, and omissions explain coverage, not scores or recommendations. A researched
name absent from technical has no technical coverage; state a selection/budget reason only if the
receipt supports it. Selected names can still lack a timeframe/indicator. Technical-only names do
not expand research or recommendations.

## Periods and interpretation

Each selected identity permits at most two audited read-only `get_kline` calls, five seconds each:
forward-adjusted regular-session daily `1d` (200 calendar days, ≤160 bars) and weekly `1w`
(735 calendar days, ≤110 bars). Cutoff is the latest modeled completed US regular-session date at
or before snapshot `as_of`, never the execution clock. Preserve cutoff, source, adjustment/session,
and each series/zone evidence date.

Daily moving averages use 20/50 closes; weekly averages use 20/40. Prices, averages, ATR14, and zone
bounds are price amounts, not returns/probabilities. Zones describe observed support/resistance,
not forecasts or trade signals. Fewer than 20 daily bars yields no zones; insufficient observations,
sparse histories, and zero volume leave affected values unknown. Never use technicals alone for advice.

Timeouts or invalid/empty/future/missing series are unavailable/degraded only for the affected symbol/timeframe. Preserve
statuses/nulls and unsupported conclusions as unknown. Whole technical failure is local to the
report, leaving the reliable snapshot usable; source-binding mismatch remains fatal under
[failure policy](../../failure-and-degradation.md).

[Technical implementation](../../implementation/technical.md) is maintenance-only: precision,
pivots, volume/zone derivation, hashes, size limits, and validation replay. Routine analysis need not
read it.
