# Comprehensive current-holdings workflow

This is the default orchestration for a new comprehensive analysis. Its unit of evidence is one
fresh portfolio snapshot, not a mixture of saved history and live reads.

## 1. Confirm scope without needless questions

Default to the user's current REAL securities account, USD base currency, and US-listed equity/ETF
holdings. Ask only when an ambiguity prevents reliable account selection, current holdings, NAV, or
USD valuation. A request such as “analyze/review/diagnose my portfolio” selects this route.

## 2. Diagnose and acquire once

Run `doctor` when environment readiness is unknown, then acquire a fresh snapshot with the current
snapshot contract. Do not substitute `--latest` or an old saved snapshot for a new comprehensive
request. Freeze `snapshot_id` and `as_of`; reject accidental later-snapshot mixing.

The default `snapshot --no-save` response is transient. If downstream deterministic commands need a
path, materialize exactly that response as a regular `0600` file inside a newly created private
`0700` temporary directory below `PORTFOLIO_AGENT_HOME`. Use an explicit relative `--snapshot` path
for every downstream command. Remove that temporary directory after the response, including after a
failure. Do not place it in canonical `snapshots/YYYY/MM` history.

## 3. Build the portfolio layer

Validate current holdings, NAV, cash, USD basis, and snapshot identity. Run deterministic `risk`
against the bound snapshot, then compose the comprehensive Markdown from snapshot/risk evidence and
the bounded research layers. Reconcile position count and ensure every nonzero current filled holding
will appear in the final table. Keep cash outside the position table. Do not run `analyze`; it reads
local lifecycle records and snapshot history even without output persistence.

If stock shorts make the current deterministic risk calculation unsupported, retain available
snapshot facts, label full short-risk analysis unavailable, and continue the other reliable sections.
Do not change the Python calculation or portray the unsupported result as complete.

## 4. Select and gather deep evidence

Apply the deterministic selection in `research-policy.md`, capped at eight. Gather company,
valuation, news/event, industry/macro sensitivity, and technical evidence only for those names.
Research failure for one name does not erase its baseline holding row.

## 5. Add conditional layers

Run `options` only when option positions exist. Load recommendation/simulation,
trades, performance, lifecycle, candidates, pending orders, or persistence only upon their explicit
triggers in `intent-routing.md`.

## 6. Compose and clean up

Return the output-contract sections as Markdown in chat. Cite all account facts with the same
`snapshot_id` and `as_of`; cite changing external facts with traceable links and timestamps. Show
local gaps next to impacted conclusions and summarize them in the final gaps section.

Delete the restricted temporary snapshot after composing the report. Do not save a canonical
snapshot, report, journal entry, research cache, or history artifact unless the user explicitly asks.
