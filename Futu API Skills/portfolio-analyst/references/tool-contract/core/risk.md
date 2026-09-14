# Risk tool contract

## Current CLI

```bash
python scripts/portfolio.py risk --snapshot TEMP_RELATIVE_SNAPSHOT.json --lookback 120 --json --no-save
```

Exactly one of `--snapshot` and `--latest` is required. Use the report-bound temporary snapshot.
`--output FILE` is explicit persistence; default/`--no-save` is side-effect free.

Current output covers full-population structural concentration/exposures, selected adjusted-price
history and correlation under explicit budgets, P&L evidence coverage, three findings, policy checks,
and data quality. Typical status fields include `COMPLETE`, `PARTIAL`, `UNAVAILABLE`, `BUDGET_LIMIT`,
`CONFIGURED`, `NOT_CONFIGURED`, and `UNRATED`; preserve them.

The CLI's optional policy comes from private `config/risk_policy.json`. With no configured policy,
the deterministic report remains `UNRATED`. The prompt layer may apply the documented fallback
thresholds only as transparent analysis-priority annotations; it must not rewrite the tool's policy
status or imply an automatic trade signal.

## Version 1.1 classification and concentration authority

Preserve `classification_coverage` for sector, region, and theme_split. Its known/unknown weights
and lower/upper bounds separate uncertain evidence from actual concentration. UNKNOWN constraint
status is not a pass or a breach; `policy_checks[dimension].breached` is null in that case.
Never report UNKNOWN as a concentrated industry/region/theme or suggest reducing an UNKNOWN bucket.
A known lower-bound breach can be confirmed even when other positions remain unclassified.

`concentration.hhi` retains NAV weighting. `holdings_hhi` and `effective_positions` use relative
nonnegative holdings weights, excluding cash, and are null when the holdings population is empty.
A single stock at 10% NAV and 90% cash therefore has effective_positions=1. Legacy report 1.0 keeps
its inverse NAV-HHI definition and must be labeled legacy rather than interpreted as the new metric.
