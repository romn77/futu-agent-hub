---
name: portfolio-analyst
description: Read-only diagnostics and, in later phases, portfolio analysis using the official futuapi Skill.
allowed-tools: Bash Read
metadata:
  version: 0.1.0
---

# Portfolio Analyst

This Skill is strictly read-only. It never unlocks trading, accepts a trading password, or exposes an
order-entry command.

## Commands

Run diagnostics from this directory:

```bash
python scripts/portfolio.py doctor --json --no-save
```

Local state defaults to `~/.moomoo-portfolio-agent`. Set `PORTFOLIO_AGENT_HOME` to override it.
Set `FUTUAPI_SKILL_DIR` only when the official `futuapi` Skill is outside the repository or supported
global skill directories.

The Futu SDK must be installed in the selected Python interpreter or its active virtual environment.
Adapter and diagnostic child processes disable Python user-site packages and ignore inherited Python
import-path hooks so portfolio reads cannot execute untrusted per-user modules.

Build a Phase 1 snapshot without saving it:

```bash
python scripts/portfolio.py snapshot --trd-env REAL --base-currency USD --json --no-save
```

`snapshot` is read-only and defaults to no persistence. `--save` writes once to
`snapshots/<snapshot_id>.json`; `--output` implies saving to that relative path; and `--no-save`
always wins over `--output`. Every path remains below `PORTFOLIO_AGENT_HOME`, and an existing file is
never replaced.

Currency rates are manual facts loaded from `config/fx_rates.json` under the data home:

```json
{"rates": {"HKD/USD": "0.125"}}
```

Missing cross-currency rates stop the snapshot. Optional exact-code classifications are loaded from
`config/classifications.json`; absent facts remain the literal `UNKNOWN` and are reported as partial
data quality. The snapshot uses signed position market values: long exposure is positive, short
exposure is the absolute value of negative positions, NAV is `cash + long - short`, gross exposure
is `long + short`, and net exposure is `long - short`. In ordinary long-only portfolios, position
weights plus cash weight must reconcile to 1 within `0.00000001`.
