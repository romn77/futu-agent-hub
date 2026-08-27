---
name: portfolio-analyst
description: Read-only portfolio snapshots, risk and trade review, plus non-executable rebalance simulation using the official futuapi Skill.
allowed-tools: Bash Read
metadata:
  version: 0.3.0
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
{
  "source": "YOUR_VERIFIABLE_RATE_SOURCE",
  "effective_at": "2026-08-27T00:00:00+00:00",
  "max_age_hours": 96,
  "rates": {"HKD/USD": "0.125"}
}
```

The source and timezone-aware effective timestamp are mandatory. The maximum age is explicit and
capped at 96 hours so a Friday close can cover an ordinary weekend without allowing older evidence.
Future, stale, missing, non-positive, or provenance-free rates stop valuation. The snapshot records
only the rate evidence actually used, including deterministic base-currency identity evidence.
If `fx_rates.json` is absent, base-currency-only portfolios can still be valued with that identity
evidence; the first required cross-currency conversion fails closed. A present malformed or unsafe
FX file remains a configuration error and is never treated as absent.

Every returned securities account also requires an explicit valuation currency, keyed by the private
account ID in `config/account_currencies.json`. This is query configuration, not firm or market
inference, and supports Canadian accounts configured as either USD or CAD:

```json
{
  "provenance": "USER_CONFIG",
  "accounts": {"YOUR_PRIVATE_ACCOUNT_ID": "CAD"}
}
```

Because this file contains raw account IDs, `config` must be a real private directory with mode
`0700` and `account_currencies.json` must be a regular, non-symlink file with mode `0600`. Reads use
anchored no-follow descriptors and reject concurrent path replacement.

Raw account IDs remain private. Public snapshots use installation-private HMAC aliases backed by a
0600 key created atomically below the data home when the snapshot is saved. Default and explicit
`--no-save` runs create no alias key or other filesystem state; they use invocation-only aliases and
mark `alias_provenance` as `EPHEMERAL` with a quality warning. `--save` or `--output` uses stable
installation aliases marked `STABLE`. Because the audited account-list command suppresses individual
context discovery failures, normal production snapshots mark discovery `UNVERIFIED` and cannot claim
`COMPLETE` merely because every returned account was read.

The audited portfolio response does not contain per-position currency. Provide exact-code facts in
`config/security_metadata.json`; a market prefix is never treated as currency evidence:

```json
{
  "provenance": "USER_CONFIG",
  "securities": {
    "US.AAPL": {"currency": "USD", "security_type": "STOCK"}
  }
}
```

Missing position currency or cross-currency rates stop unsafe aggregation. Optional exact-code classifications are loaded from
`config/classifications.json`; absent facts remain the literal `UNKNOWN` and are reported as partial
data quality. The snapshot uses signed position market values: long exposure is positive, short
exposure is the absolute value of negative positions, NAV is `cash + long - short`, gross exposure
is `long + short`, and net exposure is `long - short`. In ordinary long-only portfolios, position
weights plus cash weight must reconcile to 1 within `0.00000001`.
Non-positive NAV stops snapshot generation because portfolio weights would be misleading.

Analyze an existing immutable snapshot without persistence:

```bash
python scripts/portfolio.py risk --snapshot snapshots/FILE.json --lookback 120 --json --no-save
python scripts/portfolio.py analyze --latest --json --no-save
```

Exactly one of `--latest` and `--snapshot` is required. The selected document is schema-validated
and its full-body identity plus Phase 1 financial semantics are recomputed after an anchored
no-follow read. `risk --output FILE` saves immutable JSON; `analyze --output DIRECTORY`
transactionally saves `portfolio_report.json` and `portfolio_report.md` together.
`--no-save` always wins and the default remains side-effect free. Reports preserve one
`source_snapshot_id` and `as_of`, contain exactly three review risks, and never emit buy/sell or
order conclusions. See [references/risk-analysis.md](references/risk-analysis.md) for metric,
correlation, optional user-policy, unavailable-data, and Phase 4-6 placeholder contracts.
Duplicate account holdings are aggregated only after exact `MARKET.SYMBOL` identity and metadata
agreement; ambiguous identities or currency/classification/role conflicts stop analysis rather
than distorting Top-N, HHI, P&L, role, or correlation evidence. Risk aggregation reapplies the
shared unknown-sentinel canonicalizer to sector, region, and theme tags before that agreement gate.
Zero holdings are excluded, signed portfolios fail closed as unsupported, no-policy findings are
UNRATED, negative correlations are separate inverse evidence, and work budgets are explicit.
Risk candidates are generated independently before the deterministic Top-3 selection. Snapshot
P&L concentration uses separate profit and absolute-loss pools only when realized and unrealized
base P&L are finite and complete for the full analyzed current population; otherwise coverage is
PARTIAL and no P&L finding is emitted.
O(N) structure, exposure, policy, and finding calculations always use every canonical current
position; only history requests and correlation pairs are budgeted. Correlation uses serialized
market-local completed-close cutoffs under the versioned `REGULAR_SESSIONS_V1` approximation, so a
later close on the same UTC date is excluded. It is not a full exchange calendar; exceptional
sessions remain fail-closed/partial when bars are absent or inconsistent.

Review observed trading activity over an explicit, bounded period:

```bash
python scripts/portfolio.py trades --from 2026-08-01 --to 2026-08-27 --json --no-save
```

The command calls only the digest-pinned historical order and fill readers. It masks account and
source order/deal identifiers, separates accounts and market-qualified securities, merges partial
fills by order, and reconstructs only evidenced long-stock FIFO matches. Sells without opening
inventory in the selected window and the upstream 200-order cap are explicit `PARTIAL` quality
notes, never fabricated round trips. Market-open checks use exchange-local IANA timezones (including
DST). All behavior and frequency metrics use the merged OrderGroup (first fill is its effective
time); exact fills remain the FIFO/P&L unit. The report captures acquisition `as_of` from an
injectable UTC clock, keeps snapshot as-of separate, and preserves requested/successful/failed/
budget-omitted masked contexts even when successful reads are empty. Monetary values remain in explicit currency buckets unless an immutable snapshot supplies
complete base-currency, NAV, security-currency, and FX evidence; missing evidence makes base
aggregates `null`, never mixed-currency totals. Orders and fills are reconciled, unsupported
corporate-action/transfer/cost-adjustment fields are disclosed, and structured verified behavior
findings remain separate from `unverified_interpretations`. With no cooldown/open-window policy,
those policy counts/rates are `null`. The optional
private `config/trade-policy.yaml` follows `config/trade-policy.example.yaml` and requires a real
0700 config directory plus a regular 0600 file. See
[references/trade-review.md](references/trade-review.md).

Run a deterministic, non-executable adjustment simulation from one immutable snapshot:

```bash
python scripts/portfolio.py simulate \
  --snapshot snapshots/FILE.json \
  --request requests/rebalance.json \
  --json --no-save
```

The strict request supports `TARGET_WEIGHT`, `AMOUNT_DELTA`, and `SHARE_DELTA`, plus explicit new
cash (`new_cash_base`), sell-only, new-cash-only, margin, and short constraints. Results carry
`as_of`, `base_currency`, coverage and quality notes. Every result says `HYPOTHETICAL` and
`execution_disabled=true`; there is no order generation or trading-write path. `--output DIR`
transactionally saves `request.json`, `result.json`, and `summary.md` without replacement. See
[references/rebalance-simulation.md](references/rebalance-simulation.md).
