# Snapshot tool contract

## Current CLI

```bash
python scripts/portfolio.py snapshot --trd-env REAL --base-currency USD --json --no-save
```

`--trd-env` currently accepts `REAL` or `SIMULATE`; `--base-currency` accepts the currencies declared
by the parser. The comprehensive route always supplies `REAL` and `USD`. `--save` explicitly creates
a canonical immutable history snapshot. `--output` is unsupported for snapshots. Default/`--no-save`
creates no canonical snapshot or stable alias key.

`--acc-id ACCOUNT_ID` selects one private account; repeating the flag explicitly selects an
aggregate population. Without the flag, exactly one eligible account may be auto-selected. Multiple
candidates return `ACCOUNT_SELECTION_REQUIRED` before portfolio reads; malformed, duplicate, or
unavailable IDs return `ACCOUNT_SELECTION_INVALID`. Unselected portfolios are not read. A failed
selected portfolio, missing quantity, or missing market value stops acquisition rather than silently
returning a smaller population. Coverage counts describe the selected accounts; discovery may still
be `UNVERIFIED`, independently of successful reads of all selected accounts.

The normalized output provides `snapshot_id`, `as_of`, accounts, cash, positions, valuation totals,
FX evidence, and data-quality status/notes. It depends on audited read-only account/portfolio calls.
Missing position currency, required FX, invalid/non-positive NAV, or unsafe source identity fails
closed instead of manufacturing valuation.

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

The owned `portfolio_source_worker.py` invokes the unchanged pinned official portfolio function
inside the adapter's existing isolated subprocess. For consumed numeric fields it preserves source
missing/invalid values before the official helpers can default them to zero, including critical NAV,
quantity and market-value fields. It adds no SDK calls, retries, or valuation reconstruction, and
retains the official query/refresh/close behavior plus the adapter's argument, timeout, environment,
redaction, and output-budget controls. Forked source files and their pinned hashes are unchanged.
The same worker preserves each source row's currency with exact row/code alignment; this adds no
account query. The optional static-type lookup described above is a separate bounded quote read.

For one-report binding, use `scripts/current_analysis.py`. Its owned temporary-state contract is in
`references/workflow.md`; it passes an exact restricted snapshot to downstream reads and performs
allowlisted identity-checked cleanup within that document's stated threat model. This is
orchestration, not a new snapshot CLI flag or canonical persistence mode.
