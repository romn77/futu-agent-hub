# Snapshot tool contract

## Current CLI

```bash
python scripts/portfolio.py snapshot --trd-env REAL --base-currency USD --json --no-save
```

The comprehensive route uses the [cache-aware workflow](../../workflow.md), with its owned one-shot
helper on a miss, not a second snapshot command. The CLI accepts `REAL`/`SIMULATE` and parser-declared currencies; this route always
uses REAL/USD. `--save` explicitly creates immutable history; `--output` is unsupported.
Default/`--no-save` creates no canonical snapshot or stable alias key.
The separate cache-aware entry may create its stable scope key and private short-term cache; it
does not change that old CLI promise. Cached snapshots retain original IDs and data times.

`--acc-id ACCOUNT_ID` selects a private account; repeat only for explicit aggregation. Without it,
only one eligible account may be auto-selected. Multiple candidates return
`ACCOUNT_SELECTION_REQUIRED` before portfolio reads; malformed, duplicate, or unavailable IDs return
`ACCOUNT_SELECTION_INVALID`. Unselected portfolios are not read. Any failed selected portfolio,
missing quantity, or missing market value stops acquisition. Coverage counts describe selected
accounts; discovery can independently remain `UNVERIFIED`.

Output includes `snapshot_id`, `as_of`, accounts, cash, positions, valuation totals, FX evidence,
and quality status/notes from audited read-only calls. Missing position currency, required FX,
invalid/non-positive NAV, or unsafe source identity fails closed. Source position currency is
preserved; exact-code metadata can supply absent currency, but conflicts fail. Never guess currency
or security type from market/name. Failed type lookup leaves UNKNOWN; damaged classification
configuration degrades classification only, preserving valid rows and disclosing gaps.

Current schema `1.1` uses OpenD `funds.total_assets` as authoritative NAV (`nav_source:
OPEND_TOTAL_ASSETS`). `account_navs` retains masked-account source amounts/currencies and conversion.
Use a single base-currency total directly; aggregation sums reported totals with required currency
conversion. Missing/invalid total-assets evidence fails. Never derive or fill NAV from cash plus
positions. Holding/cash weights use this NAV; totals need not reconcile to NAV/100%. Do not normalize
weights, alter cash, or invent positions.

Missing/invalid cash, average cost, current price, and sellable quantity are JSON `null` with partial
quality; actual zero remains zero. Unknown account cash has null local/base amounts and weight;
any incomplete coverage makes aggregate `cash_base`/`cash_weight` null. Known holdings and NAV remain
usable. Legacy `1.0` keeps stored values/contracts/content identity; do not reinterpret historical
zeros or relabel it OpenD NAV evidence. New acquisition emits `1.1`.

[Snapshot implementation](../../implementation/snapshot.md) is maintenance-only, covering source
workers, bounded metadata reads, and legacy compatibility; routine analysis need not read it.
