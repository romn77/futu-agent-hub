# Snapshot implementation

Maintenance-only reference for debugging, auditing, or changing snapshot acquisition. Routine
portfolio analysis need not read this file. The [snapshot contract](../tool-contract/core/snapshot.md)
defines the current CLI, account selection, NAV, null, currency, and coverage semantics.

## Source currency and bounded static-type lookup

Position currency is preserved directly from OpenD `position_list_query.currency`, before the
forked reader drops it. Exact-code `security_metadata.json` is a fallback for absent currency;
conflicting source/config currencies fail closed rather than silently overriding OpenD. An owned
quote-only `get_stock_basicinfo(code_list=...)` worker supplies missing security types in one batch
of at most 200 exact held codes, with a 5-second deadline and 256 KiB combined output cap. Unmatched,
invalid, timed-out, or capped-out types remain UNKNOWN. No market/name-based type or currency guesses.
This query never accesses accounts, creates subscriptions, trades, or saves a metadata cache.
Source APIs: [position currency](https://openapi.futunn.com/futu-api-doc/en/trade/get-position-list.html)
and [exact-code static type](https://openapi.futunn.com/futu-api-doc/en/quote/get-static-info.html).

Missing or damaged classification configuration degrades only classification. Valid rows remain
usable when other rows are malformed; warnings disclose the local gap without exposing raw errors.

## Owned source worker

The owned `portfolio_source_worker.py` invokes the unchanged pinned official portfolio function
inside the adapter's existing isolated subprocess. For consumed numeric fields it preserves source
missing/invalid values before the official helpers can default them to zero, including critical NAV,
quantity and market-value fields. It adds no SDK calls, retries, or valuation reconstruction, and
retains the official query/refresh/close behavior plus the adapter's argument, timeout, environment,
redaction, and output-budget controls. Forked source files and their pinned hashes are unchanged.
The same worker preserves each source row's currency with exact row/code alignment; this adds no
account query. The optional static-type lookup described above is a separate bounded quote read.

Implementation: [source worker](../../portfolio_analyst/portfolio_source_worker.py),
[adapter](../../portfolio_analyst/futu_adapter.py), and
[security type source](../../portfolio_analyst/security_type_source.py).

## Schema compatibility

Snapshot schema `1.1` uses OpenD `funds.total_assets` as the authoritative account NAV. The output
sets `nav_source: OPEND_TOTAL_ASSETS` and retains each masked account's source amount/currency and
conversion in `account_navs`. A single base-currency account uses its returned total directly;
multiple accounts sum those reported totals, converting only where currency differs. Never derive,
replace, or fill missing NAV using cash plus positions. Missing/invalid total-assets evidence fails.

Holding and cash weights use this OpenD NAV as their denominator. Listed holdings plus cash need not
sum to NAV or 100% weight; do not alter cash, invent positions, or normalize weights to force equality.
Legacy `1.0` snapshots remain readable with their original stored values and validation rules; they
are not relabeled as OpenD NAV evidence. New snapshot acquisition always emits `1.1`.

In `1.1`, missing/invalid cash, average cost, current price, and sellable quantity are JSON `null`,
with explicit partial quality. Genuine numeric zero remains zero. Unknown account cash has null
local/base amounts and weight; any incomplete cash coverage makes aggregate `cash_base` and
`cash_weight` null. Known holdings and OpenD NAV remain available. Legacy `1.0` retains its original
non-null contracts and content identity; saved historical zeros are not retroactively reinterpreted.

## One-report orchestration

For one-report binding, use [current_analysis.py](../../scripts/current_analysis.py). It passes an
exact restricted snapshot to downstream reads and performs allowlisted identity-checked cleanup
under the [owned-state threat model](owned-state.md). This is orchestration, not a new snapshot CLI
flag or canonical persistence mode. The [runtime workflow](../workflow.md) defines invocation.
