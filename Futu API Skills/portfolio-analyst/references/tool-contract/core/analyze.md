# Analyze tool contract

## Current CLI

```bash
python scripts/portfolio.py analyze --snapshot TEMP_RELATIVE_SNAPSHOT.json --json --no-save
```

Exactly one of `--snapshot RELATIVE_PATH` and `--latest` is required. `--lookback` defaults to 120.
`--output DIRECTORY` explicitly persists `portfolio_report.json` and `portfolio_report.md`; default
and `--no-save` do not write those reports.

This is a documented existing bundled artifact, but it is **not used by the default comprehensive route**.
The current command unconditionally opens the lifecycle repository/history path: it creates
the local `LifecycleRepository`, enumerates bounded canonical snapshot history, builds a lifecycle
report, and also builds an option report before composing the portfolio report. `--no-save` prevents
new report files; it does not prevent those lifecycle/history reads. Because lifecycle and history
are explicit-only workflows, the default route uses `risk` and conditional `options` instead.

Current deterministic output binds `source_snapshot_id` and `as_of`, overview/risk evidence, exactly
three review findings, all supported current positions, data gaps, lifecycle evidence, and an option
subreport.
Its point-in-time performance status is `NOT_INCLUDED_USE_PERFORMANCE_COMMAND`. Its Markdown is a
calculation artifact, not automatically the final chat output; apply the current output contract and
selective external research without changing account facts.

Signed stock portfolios may be rejected by the current analyzer. Treat that as local unsupported
full-risk analysis when holdings/NAV/USD remain reliable in the snapshot; do not claim analyzer
completion or change Python behavior in the prompt layer.
