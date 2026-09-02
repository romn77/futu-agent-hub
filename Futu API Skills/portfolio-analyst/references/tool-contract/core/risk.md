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
