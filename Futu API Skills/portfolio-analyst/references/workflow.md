# Comprehensive current-holdings workflow

This is the default orchestration for a new comprehensive analysis. Its unit of evidence is one
fresh portfolio snapshot, not a mixture of saved history and live reads.

## 1. Confirm scope without needless questions

Default to the user's current REAL securities account, USD base currency, and US-listed equity/ETF
holdings. Ask only when an ambiguity prevents reliable account selection, current holdings, NAV, or
USD valuation. A request such as “analyze/review/diagnose my portfolio” selects this route.

Only one eligible account may be auto-selected. Multiple candidates require the user's account
choice; `ACCOUNT_SELECTION_REQUIRED` is a clarification boundary, not permission to combine all
accounts. Use the selected private ID only in local account commands, never in web queries or the
final report. Invalid/unavailable choices are `ACCOUNT_SELECTION_INVALID`; do not substitute an
account. Explicit aggregation reads exactly the requested accounts and fails if any is unreadable.

## 2. Diagnose and acquire once

Run `doctor` when environment readiness is unknown. Do not substitute `--latest` or an old saved
snapshot for a new comprehensive request. Freeze the acquired `snapshot_id` and `as_of`; reject
accidental later-snapshot mixing.

## Owned one-shot helper

From the Skill directory run:

```bash
python scripts/current_analysis.py
```

When an account has been selected, use `python scripts/current_analysis.py --acc-id ACCOUNT_ID`,
replacing `ACCOUNT_ID` with the chosen private ID. Repeat `--acc-id` only when the user requests
aggregation. The helper forwards the selection to its single snapshot acquisition; it does not
create a saved account preference. An unspecified account remains valid only with one candidate.

Capture its stdout as internal evidence for report composition; never paste the raw JSON into chat or
send it to web tools. Invoke the helper exactly once for this analysis. If a response is long or a
different view is needed, inspect/filter the retained result from that same invocation using the
host's available context or read-only evidence access. Never rerun the helper to redisplay, trim,
reformat or recheck its output; that would acquire another account snapshot. If the original result
cannot be inspected reliably, disclose that limitation instead of silently reacquiring it.
The helper is lifecycle-free: it runs a fresh REAL/USD `snapshot`, `risk`,
`technical`, and `options` only when an option position is present. Technical runs after risk with
the bound risk report when available, or from snapshot weights when risk is unavailable. It never
calls `analyze`, lifecycle, saved history, or a persistence flag.

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
The helper validates the full snapshot schema, content identity and financial semantics before
downstream commands. It validates each successful component's schema/semantics, snapshot ID/as-of,
embedded snapshot when present, and the technical report's exact risk binding. A declared source
mismatch is fatal; malformed component evidence is a local `INVALID_COMPONENT_OUTPUT` degradation.
In contrast, risk, technical, and options failures are local degradation: the helper returns the
failed component as `null`, records its structured error in `degradations`, continues with the
reliable snapshot and other components, and still performs safe cleanup. In particular,
`UNSUPPORTED_PORTFOLIO` for stock shorts does not abort the remaining report.

## 3. Build the portfolio layer

Validate current holdings, NAV, cash, USD basis, and snapshot identity. Use the helper's deterministic
`risk` and `technical` results when available, then compose comprehensive Markdown from the bound
snapshot/risk/technical evidence and bounded research layers. Reconcile position count and ensure every nonzero current filled holding
will appear in the final table. Keep cash outside the position table. Do not run `analyze`; it reads
local lifecycle records and snapshot history even without output persistence.

If risk is degraded, retain available snapshot facts, place the limitation beside affected
conclusions, and continue every reliable section. Never portray an unavailable risk/short analysis as
complete.

Missing cash leaves portfolio cash and cash weight `null`, even when some selected accounts have
known cash. Known per-account amounts remain usable only with their narrower scope. Missing cost,
current price, or sellable quantity stays `null`; keep NAV, market values, holdings, and concentration
where evidenced. Do not repair these gaps with arithmetic. Missing price degrades the affected
technical/option calculations; missing cash or required price prevents rebalance simulation only.

## 4. Select and gather deep evidence

Apply the deterministic selection and six research routes in `research-policy.md`: financials,
estimate revisions, thesis, earnings preview, earnings recap and valuation. The default is at most
eight names and two automatic full valuations; expand the corresponding budget only for an explicit
wider request and disclose actual coverage. Every holding retains its baseline row.

For each relevant question:

1. Identify the public company/market, question, target period, depth and missing evidence.
2. Choose one suitable main method from the host-provided skill catalog or a user-specified location.
   Honor explicit source choices/exclusions; identify same-name skills by source/package and path.
   Load only missing instructions/references. Already loaded methods can analyze existing evidence.
3. Reuse qualified evidence first. For remaining gaps, use existing authorized public capabilities
   guided by the selected skill, preferring same-basis Futu data when available. Shared public
   calendars, peer groups and macro series are fetched once per relevant query/period, then matched
   locally. Follow `tool-contract/core/research.md`; do not change the account adapter.
4. Check actual results, important sources/periods/units and required calculations using
   `evidence-policy.md`. Correct ordinary parameter/date/format errors; respect real permission
   denials and limit retries for temporary failures. Missing optional skills do not stop the original
   research path; a real evidence gap restricts only the affected claim.
5. Connect supported and contrary evidence to the selected holding and shared portfolio drivers in
   this host context. Keep public data requests free of private portfolio context. Produce no
   independent upstream report, workbook, saved thesis, new account read or simulation by default.

Gather company, valuation, news/event, industry/macro sensitivity and existing technical context at
the selected depth. Follow Futu capability references only when using those public steps; the list
is a navigation aid, not an interface implementation backlog or permission whitelist.

## 5. Add conditional layers

Use the helper's `options` result only when option positions exist. Load recommendation/simulation,
trades, performance, lifecycle, candidates, pending orders, or persistence only upon their explicit
triggers in `intent-routing.md`.

## 6. Compose

Return the output-contract sections as Markdown in chat. Cite all account facts with the same
`snapshot_id` and `as_of`; support external facts with the evidence policy's sources, periods and
units, including direct links or actual tool-result locators. Use stable output-contract headings. Show
local gaps next to impacted conclusions and summarize them in the final gaps section. Before a
successful envelope returns, the helper has completed guarded cleanup under the threat model above.
