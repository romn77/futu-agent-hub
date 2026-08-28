---
name: portfolio-analyst
description: Read-only portfolio snapshots, risk, raw option Greeks, trade review, lifecycle, performance attribution, and non-executable rebalance simulation using the official futuapi Skill.
allowed-tools: Bash Read
metadata:
  version: 0.6.0
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

`snapshot` is read-only and defaults to no persistence. `--save` writes to the canonical
`snapshots/YYYY/MM/<snapshot_id>.json` content-addressed repository. Exact duplicates are idempotent;
conflicting or invalid bodies fail closed and existing files are never replaced. Custom snapshot
`--output` paths are rejected so CLI-created history cannot become invisible to repository listing.

Snapshot `as_of` preserves the acquisition timestamp, including microseconds when present. The
readable timestamp segment in `snapshot_id` encodes the UTC whole second (floor, never round), while
its digest still binds the complete body including precise `as_of`. Risk, lifecycle, journal,
repository, and bundle checks compare ID time at that precision and reject cross-second mismatch.

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
no-follow read through `SnapshotRepository`. `--latest` enumerates only bounded canonical
`snapshots/YYYY/MM` history and fails closed on corruption. An explicit non-canonical import path
must still be beneath the private home with every directory `0700` and a bounded, single-link
regular file at `0600`; it is never silently added to history. `risk --output FILE` saves immutable
JSON; `analyze --output DIRECTORY`
transactionally saves `portfolio_report.json` and `portfolio_report.md` together.
`--no-save` always wins and the default remains side-effect free. Reports preserve one
`source_snapshot_id` and `as_of`, contain exactly three review risks, and never emit buy/sell or
order conclusions. See [references/risk-analysis.md](references/risk-analysis.md) for metric,
correlation, optional user-policy, unavailable-data, and integrated option-risk contracts.
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

Inventory recognized option legs from the same immutable snapshot:

```bash
python scripts/portfolio.py options --snapshot snapshots/FILE.json --json --no-save
python scripts/portfolio.py options --latest --output reports/option-risk
```

Only nonzero positions whose canonical `security_type` is exactly `OPTION` are included. A nonzero
`UNKNOWN` security type prevents `NOT_APPLICABLE`; labels and option codes are never parsed to infer
type, direction, multiplier, or underlying. US and HK contracts may use the separately
SHA-256-pinned official `quote/get_option_quote.py` capability. That optional pin is isolated from
the core `futuapi` pins: a missing or changed option script makes live option quotes structurally
unavailable without disabling accounts, portfolio, K-line, trade history, or doctor behavior.

Each unique supported contract is requested sequentially once as the canonical single leg
`BUY`/`1.0` read shape solely to obtain a unit quote. Snapshot quantity remains the authoritative
signed position direction. Reports label every Delta, Gamma, Vega, Theta, and Rho value
`RAW_CONTRACT_UNITS_NO_MULTIPLIER`; leg exposure is signed snapshot quantity times the raw unit
Greek. Aggregate net and `gross_abs` totals are emitted only with complete full-population coverage.
Option net/gross market value and gross/NAV always cover the entire recognized source population,
including unsupported or quote-budget-omitted contracts. Expiry buckets keep calendar DTE by
market-local snapshot date and mark expired contracts explicitly. Implied volatility is retained
only as an independently covered raw vendor value: it is not aggregated and has no inferred
high/low interpretation.

The live quote budget is 30 unique supported contracts, selected by gross absolute base market
value descending then identity ascending, with 5 seconds per call, a 30-second total wall bound,
and a consecutive-failure circuit breaker. A daemon worker enforces those bounds even when a
provider ignores its timeout; runtime replay rejects request windows over 5 seconds or an operation
window over 30 seconds. Request start/finish windows are recorded, but quote
freshness remains `UNVERIFIED` because the official response supplies no trusted quote timestamp.
Missing fields remain unavailable and never become zero. SDK/OpenD/timeouts/quote-shape failures
degrade the option subreport while `analyze` continues to preserve Phase 2's exactly-three findings
and overall-risk policy; Phase 2's signed-portfolio fail-closed rule is unchanged. `options` and
`analyze` freeze one trusted UTC instant and reject a later snapshot as input error
`FUTURE_SNAPSHOT`; a future-dated report cannot self-authorize through replay.

The option report does not contain bid/ask, portfolio pricing, max profit/loss, breakeven,
probability, margin, scenario P/L, money/beta/underlying-share delta, inferred multiplier or
underlying, or trading advice. `options --output DIRECTORY` transactionally writes
`option_risk.json`, `option_risk.md`, the source snapshot, and a digest manifest; default and
`--no-save` runs write nothing. See [references/option-risk.md](references/option-risk.md).

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
budget-omitted masked contexts even when successful reads are empty. Source `HKCC` contexts use the
canonical `HK` evidence key. If no context succeeds (including when no eligible account yields zero
requested contexts), the report is `UNAVAILABLE` and every observed activity metric is `null`; only
a successfully queried empty context establishes a true zero. Raw order/deal aliases are stable
context-scoped HMACs; duplicate identities, cross-market bodies, DST-ambiguous times, or row-budget
overflow fail the context atomically. Monetary values remain in explicit currency buckets unless an immutable snapshot supplies
complete base-currency, NAV, security-currency, and FX evidence; missing evidence makes base
aggregates `null`, never mixed-currency totals. Orders and fills are reconciled, unsupported
corporate-action/transfer/cost-adjustment fields are disclosed, and structured verified behavior
findings remain separate from `unverified_interpretations`. With no cooldown/open-window policy,
those policy counts/rates are `null`. The optional
private `config/trade-policy.yaml` follows `config/trade-policy.example.yaml` and requires a real
0700 config directory plus a regular 0600 file. See
[references/trade-review.md](references/trade-review.md).
The report embeds that normalized policy body so semantic validation can rebuild policy-window
observations and verified behavior findings from exchange-local OrderGroups.
Base-currency fills have exact 1:1 FIFO realized P&L and base win/loss statistics. Cross-currency
trips without transaction-date FX stay null/partial while per-currency local P&L pools remain exact.
Realized P&L is gross pre-fee; closed trips require the structured `FEES_UNAVAILABLE` quality code
when fee evidence is absent. Other stable quality codes are reconstructed from coverage,
reconciliation, FX, corporate-event, and current-session evidence. Analysis is indexed and bounded by
operation and wall-clock budgets. Snapshot/date checks use requested exchange-local markets.
Quality issue codes, ordering, details, and data-quality notes are canonical and runtime-rebuilt. Direct analysis and
semantic validation have separate hard row caps plus cumulative operation/wall budgets; FIFO uses a
deque and indexed validation remains linear for the documented 20,000-row boundary. Before JSON
Schema or report-ID canonicalization, semantic validation iteratively preflights the entire untrusted
body for bounded depth, nodes, array items, map entries, strings, and normalized document bytes. The
published schema also caps every array/map, including contexts, policy/quality/behavior evidence,
currency buckets, symbol turnover, and provenance maps. Shape relationships follow the builder:
orders plus fills are at most 20,000; groups/trips/open lots cannot exceed fills; findings cannot
exceed `floor(groups / 2) + 7`; and per-finding/cumulative affected evidence is bounded by groups and
the eight finding types. This admits the documented 10,000-symbol SELL-to-BUY boundary without
permitting Cartesian findings-by-evidence expansion.
Externally supplied source notes are preserved in bounded `external_source_evidence` entries with
`EXTERNAL_SOURCE_NOTE` and `UNVERIFIED_EXTERNAL`; they are escaped in Markdown/terminal output and
are ambiguity evidence only, not independently verified facts or inferred motives. Before becoming
authoritative JSON, its raw Python string is type/length checked at 512 characters before any
normalization or scan. Two bounded NFKC detection views respectively delete C*/Zl/Zp separators or
map them to one space, covering both internal splitting and separator substitution. Complete
password/secret/token/API-key/credential/credentials labels followed by `:`, `=`, whitespace, or a mapped control
separator and any nonempty value are always sensitive. Bearer plus any token is sensitive except the
explicit financial phrases bearer shares/securities/bonds/instruments/certificates when no later
credential punctuation or header exists; `sk-` still requires a sufficiently long suffix. Matches replace the
whole note with `[REDACTED_EXTERNAL_CREDENTIAL]`. A separate copy folds whitespace only between
digits, and any rejoined 8+-digit identifier replaces the whole note with
`[REDACTED_EXTERNAL_IDENTIFIER]`. Otherwise the display copy safely becomes one line and passes the shared account
scrub. Oversize input fails with `EXTERNAL_SOURCE_EVIDENCE_TOO_LONG`; runtime
validation reapplies the same sanitizer. Internal row-cap, turnover, failure, and budget evidence is
derived only from coverage/status codes and never enters `external_source_evidence`.

Run a deterministic, non-executable adjustment simulation from one immutable snapshot:

```bash
python scripts/portfolio.py simulate \
  --snapshot snapshots/FILE.json \
  --request requests/rebalance.json \
  --json --no-save
```

The strict request supports `TARGET_WEIGHT`, `AMOUNT_DELTA`, and `SHARE_DELTA`, plus explicit new
cash (`new_cash_base`), sell-only, new-cash-only, margin, and short constraints. Results carry
the authoritative request plus immutable per-symbol unit-price/FX evidence, and the complete body is
bound by `simulation_id`. Signed states require explicit `allow_short=true`, expose long, absolute
short, gross, and net monetary/weight/exposure views, and suppress long-only Top-N/HHI as unavailable.
Results carry `as_of`, `base_currency`, coverage and quality notes. Every result says `HYPOTHETICAL` and
`execution_disabled=true`; there is no order generation or trading-write path. `--output DIR`
transactionally saves `request.json`, `result.json`, and `summary.md` without replacement. See
[references/rebalance-simulation.md](references/rebalance-simulation.md).
Per-identity evidence includes account-level source legs and sector/region/theme classifications.
Same-direction multi-account changes are allocated deterministically and proportionally; opposite
cross-account legs fail closed as `UNSUPPORTED_OFFSETTING_LEGS` rather than being netted away.
The complete content-addressed source snapshot is embedded and revalidated to prevent classification
evidence from being rewritten by recomputing only the simulation hash.
Runtime semantics rebuild stable account-level legs from the embedded positions and account aliases,
require exact one-to-one evidence equality, and recompute allocation weights from those authoritative
legs; equal-total leg redistribution plus a new simulation hash remains invalid.
Rebalance validation invokes the complete Phase 1 snapshot semantics and binds `before` cash/NAV and
positions to that source. Decimal weight/exposure reconciliation uses the documented `1e-24`
relative tolerance while cash-plus-positions and NAV remain exact.
Coverage binds `source_positions` exactly to the embedded snapshot position-row count, including a
valid explicit zero for an all-cash portfolio. Source quality status and canonical prefixed notes are
rebuilt exactly from embedded warnings, missing fields, errors, and simulation warnings.

Review user-authored position lifecycle facts without inferring a thesis:

```bash
python scripts/portfolio.py lifecycle list --json
python scripts/portfolio.py lifecycle inspect US.MU --json
python scripts/portfolio.py lifecycle validate --latest --json --no-save
```

Lifecycle records live only in the private `positions/` directory and follow the strict example at
`config/position-lifecycle.example.yaml`. The home and positions directory must be `0700`; records
must be `0600` non-linked files. Missing records produce `UNCONFIGURED`, and missing history makes
persistent role-drift evidence unavailable rather than guessed. Weight boundaries are inclusive.
Holding horizons support bounded days, weeks, months, or years and use the documented deterministic
upper bound. Thesis status changes only from an explicit user review; price and performance never
create a thesis fact. History evidence is semantically validated, deduplicated and limited to each
record's opened/current window; reports serialize snapshot IDs, timestamps, canonical identities,
weights and the exact role-drift policy so runtime validation can rebuild persistence. Human and
Markdown output prints the sanitized user-authored thesis, invalidation conditions, review triggers,
and notes with explicit `USER_CONFIG` / `system inferred: false` provenance. The JSON report embeds
the authoritative sanitized thesis plus a bound digest and character count. Saved reports remain
inside the private `PORTFOLIO_AGENT_HOME`; `--no-save` sends this private material directly to the
requesting user's terminal/stdout, so redirect it only to a private destination. Top-level lifecycle
quality status and canonical structured reasons are rebuilt from history coverage, exclusions,
insufficient evidence, and every position quality note. See
[references/position-lifecycle.md](references/position-lifecycle.md).

Every eligible history snapshot contributes an explicit observation with `present` and nullable
weight. An absence interrupts persistence and cannot support `ROLE_DRIFT`. Canonical timestamped
snapshot IDs are bound to `as_of`, weights are finite long-only values, and snapshot, record,
observation, operation, and wall-time budgets fail closed. Accepted history is indexed once.

Attribute evidenced P&L over bounded immutable snapshot history:

```bash
python scripts/portfolio.py performance --from 2026-08-01 --to 2026-08-27 --json --no-save
```

Dates are inclusive UTC calendar dates. One snapshot is sufficient for an explicitly limited
reconstruction; `ACCURATE_PNL` requires at least two, both requested endpoint dates, and no gap
longer than 36 hours. The range reader filters canonical timestamp paths before applying its
consumer row/byte budgets, then descriptor-reads matching bodies and filters exact microsecond
`as_of` values. Every selected snapshot is
loaded through `SnapshotRepository`, so schema, full-body identity, and Phase 1 financial semantics
are revalidated before attribution. The CLI freezes one trusted current instant, rejects later
same-day snapshots as `FUTURE_SNAPSHOT`, and binds that instant into price cutoffs and the report.
Every source snapshot's quality status, warnings, missing fields, and errors are embedded and bound.
Bounded per-position quality notes are also embedded by account and canonical identity; any such
note prevents an exact/high claim. Phase 1 rejects duplicate economic position legs with the same
account and canonical identity before attribution begins. Library callers that do not inject
`analysis_now` use the real UTC clock, never the ending snapshot timestamp, so future snapshots
also fail closed outside the CLI.
Accounts, base currency, classifications, account-level legs, and FX source provenance must remain
consistent. Attribution traverses every adjacent snapshot and retains the union of all trajectory
identities; a middle-only, missing/opened/closed/transferred/reset leg is `UNAVAILABLE`, never
silently netted at the canonical `MARKET.SYMBOL` level.

Symbol P&L uses only complete cumulative realized/unrealized base-field deltas summed across every
adjacent trajectory pair. Positive and negative deltas are equally authoritative; falling values
produce detractors and do not by themselves imply a reset. Cross-currency legs bind each changing
rate to its snapshot FX evidence, effective timestamp, base target, position currency provenance,
and stable source/non-lookahead authority contract. Therefore an audited 1.1 to 1.2 rate movement
is continuous and its effect remains inside base-P&L delta, while missing, look-ahead, partial/stale,
currency-changing, or source-changing evidence fails closed. Sector,
region, theme-full, and additive theme-split totals are Decimal-only and reconcile to the available
symbol population. With only a current/end snapshot, common aligned adjusted closes can estimate
base-currency current holdings as `ending quantity * adjusted-close change`. When the existing
stable alias key and pinned audited Skill are available, `performance` also reuses the bounded,
context-atomic Phase 3 history service. Account-and-symbol FIFO matches can add gross pre-fee
realized P&L and fully closed symbols. Authoritative open lots compute current unrealized P&L from
each lot cost to the aligned ending close, and their quantities must exactly match ending account
legs. Each lot's ending close must be on or after its first eligible completed market session
(the next session when the fill occurs after close); older price rows cannot create pre-entry P&L.
The period-price-change estimate is only a separately named no-trade LOW-confidence fallback.
Base-currency fills are exact 1:1. Cross-currency FIFO remains
in local buckets and base P&L stays null without transaction-date FX. Opening inventory, nontrade
events, context gaps, orphan fills, quantity mismatches, and status conflicts fail closed. For
multi-snapshot attribution, full-period authoritative trade coverage is required even when every
snapshot trajectory appears continuous because a fully opened-and-closed symbol may never appear
in a snapshot. Such closed-only FIFO round trips are added as gross pre-fee contributions. For an
identity present in snapshots, FIFO realized P&L is not added: it must reconcile exactly to the
snapshot cumulative realized delta or that identity becomes unavailable. Without usable trade
history the LOW-confidence result explicitly excludes realized P&L and exited positions and can
never be `ACCURATE_PNL`/HIGH.
The current Phase 3 adapter reports corporate-action evidence as unsupported, so live CLI history is
retained as structured unavailable evidence rather than authorizing FIFO contribution claims.
Timeout, budget, source-read, normalized-contract, and stable-alias acquisition failures are
sanitized to fixed trade-source reason codes; independent price/snapshot attribution continues.
Theme splits sort labels and assign the exact Decimal residual to the last label. Top contributors
and detractors are deterministically ranked. `ACCURATE_PNL` additionally requires complete
authoritative full-period trade coverage and
at least two common aligned price
points for every authoritative trajectory symbol. It describes P&L-field/FIFO coverage authority only.

The official cash-flow reader is not in the current digest-pinned adapter allowlist. Therefore the
CLI truthfully records external-flow evidence as unavailable and leaves `portfolio_return` null.
Library callers may supply strictly verified deposits, withdrawals, and internal transfers with
currency, FX, timestamp, coverage, and provenance. Only complete flow coverage plus continuous
valuations and `ACCURATE_PNL` enables the documented end-of-subperiod-adjusted TWR. Reconstructed
reports always leave return/TWR null. IRR is always explicitly
`NOT_CALCULATED`; it is never inferred. Price traceability uses only the audited forward-adjusted
daily `get_kline` reader, completed market-local close cutoffs, strict date intersection, per-call,
row, request, failure-circuit, and wall-time budgets. Missing or unequal price coverage downgrades
the accuracy claim and remains explicit. Snapshot range reads recheck opened descriptor identity
and actual bytes; semantic validation consumes operation/wall budgets inside source-position and
trajectory-leg loops. See
[references/performance-attribution.md](references/performance-attribution.md).

`performance --output DIRECTORY` writes JSON and escaped Markdown as one private manifest-last
transaction. The default and `--no-save` are side-effect free. Performance never appends a journal
event implicitly.

Journal writes are explicit, append-only operations:

```bash
python scripts/portfolio.py journal record-decision --source-snapshot-id ps-... \
  --decision ACCEPTED --summary "User-authored decision" --json
python scripts/portfolio.py journal list decisions --json
```

Decision and analysis streams are bounded, credential-scrubbed, hash chained, locked, fsynced JSONL.
Truncation, duplicate identities, chain corruption, unsafe links, and oversized records fail closed.
No analysis command writes a journal event as a side effect. Content-addressed snapshot history uses
`snapshots/YYYY/MM/<snapshot_id>.json`; exact duplicates are idempotent and conflicting bodies fail.
Enumeration is bounded and rejects misplaced, duplicate, or non-canonical entries while ignoring
only stale private `.tmp` leaves. Journal JSON is canonical and duplicate-key-safe, with total size
checked inside the interprocess lock. POSIX persistence revalidates `0700` directories and `0600`,
single-link regular files on opened descriptors. Platforms without equivalent handle-level
no-follow/reparse and interprocess locking fail closed with `UNSUPPORTED_PLATFORM_SECURITY`.
New private state directories are durably published by fsyncing the child and its parent; the
account-alias key additionally requires an anchored `0700` home/config, a single-link 32-byte
`0600` regular file, bounded descriptor reads, and leaf/config/home inode revalidation.
Every CLI multi-file bundle records its canonical source snapshot time/ID, report ID, and each
artifact's byte size and SHA-256. Artifacts are linked and directory-fsynced before
`manifest.json` is linked and directory-fsynced; missing manifests, unsafe descriptor metadata,
size/digest failures, or anchor changes are `INCOMPLETE`. Snapshot/lifecycle enumeration accepts
consumer count/byte limits, and lifecycle report shape/operation/wall budgets run before schema
validation. No repository operation is enabled on a platform lacking the required secure handles.
