# Comprehensive current-holdings workflow

This is the default orchestration for a new comprehensive analysis. Its unit of evidence is one
fresh portfolio snapshot, not a mixture of saved history and live reads.

## 1. Confirm scope without needless questions

Default to the user's current REAL securities account, USD base currency, and US-listed equity/ETF
holdings. Ask only when an ambiguity prevents reliable account selection, current holdings, NAV, or
USD valuation. A request such as “analyze/review/diagnose my portfolio” selects this route.

## 2. Diagnose and acquire once

Run `doctor` when environment readiness is unknown. Do not substitute `--latest` or an old saved
snapshot for a new comprehensive request. Freeze the acquired `snapshot_id` and `as_of`; reject
accidental later-snapshot mixing.

## Owned one-shot helper

From the Skill directory run:

```bash
python scripts/current_analysis.py
```

Capture its stdout as internal evidence for report composition; never paste the raw JSON into chat or
send it to web tools. The helper is lifecycle-free: it runs a fresh REAL/USD `snapshot`, `risk`, and
`options` only when an option position is present. It never calls `analyze`, lifecycle, saved history,
or a persistence flag.

Expected startup failures return exit status 2 with a stable JSON envelope: `CONFIG_ERROR` for
invalid settings, `PRIVATE_HOME_ERROR` for an unavailable/unsafe data home, and `IO_ERROR` for
pre-acquisition I/O failure. These messages omit exception detail, tracebacks, and source paths.
Unexpected programming errors and process-control exceptions are not caught.

The helper resolves `Settings.from_env()` and calls
`ensure_private_home(settings.data_home, create=True)`. It creates one direct mode-0700 temporary
child and mode-0600 regular files. It records the device and inode of the private home, temporary
directory, and every file at creation.

Cleanup validates those identities through anchored directory descriptors before making any change.
It requires the directory entries to equal its explicit whitelist and rechecks the directory and
file identity immediately before each unlink. It verifies the directory identity and remaining entry
set after each deletion, then rechecks before and after removing the empty directory.
It never recursively deletes. If a checked identity changed, a link or unknown entry appeared, or a
check/removal failed, cleanup fails closed, preserves the remaining state, and returns an error
containing the absolute retained path. No cleanup exception or error may omit that path.

Threat model: these checks defend against detected path replacement, symlinks, and accidental or
non-cooperative changes. Portable POSIX APIs provide no portable atomic unlink-if-inode operation;
therefore same-UID malicious concurrent mutation is outside this portable guarantee. The adjacent
pre-unlink and post-deletion checks narrow that time-of-check/time-of-use window and detect many
races, but do not claim an atomic owned-only deletion guarantee against such an attacker.

Snapshot acquisition/validation failure stops because current holdings authority is unavailable.
In contrast, risk and options failures are local degradation: the helper returns `risk` or `options`
as `null`, records the component's structured error in `degradations`, continues with the reliable
snapshot and other component, and still performs safe cleanup. In particular,
`UNSUPPORTED_PORTFOLIO` for stock shorts does not abort the remaining report.

## 3. Build the portfolio layer

Validate current holdings, NAV, cash, USD basis, and snapshot identity. Use the helper's deterministic
`risk` result when available, then compose the comprehensive Markdown from snapshot/risk evidence and
the bounded research layers. Reconcile position count and ensure every nonzero current filled holding
will appear in the final table. Keep cash outside the position table. Do not run `analyze`; it reads
local lifecycle records and snapshot history even without output persistence.

If risk is degraded, retain available snapshot facts, place the limitation beside affected
conclusions, and continue every reliable section. Never portray an unavailable risk/short analysis as
complete.

## 4. Select and gather deep evidence

Apply the deterministic selection in `research-policy.md`, capped at eight. Gather company,
valuation, news/event, industry/macro sensitivity, and technical evidence only for those names.
Research failure for one name does not erase its baseline holding row.

## 5. Add conditional layers

Use the helper's `options` result only when option positions exist. Load recommendation/simulation,
trades, performance, lifecycle, candidates, pending orders, or persistence only upon their explicit
triggers in `intent-routing.md`.

## 6. Compose

Return the output-contract sections as Markdown in chat. Cite all account facts with the same
`snapshot_id` and `as_of`; cite changing external facts with traceable links and timestamps. Show
local gaps next to impacted conclusions and summarize them in the final gaps section. Before a
successful envelope returns, the helper has completed guarded cleanup under the threat model above.
