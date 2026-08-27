# Rebalance simulation contract

The request contract is version `1.1` and result contract is `1.3`; earlier results without the
authoritative request body and immutable per-symbol price/FX evidence are rejected.

The simulator consumes one immutable, schema- and identity-validated snapshot plus one confined,
strict JSON request. Duplicate JSON keys, duplicate symbol changes, ambiguous identities, missing or
conflicting cross-account metadata, non-finite Decimal values, missing prices/FX, and path traversal
fail closed.

Operations are deterministic:

- `TARGET_WEIGHT`: final NAV times target weight minus current base-currency market value.
- `AMOUNT_DELTA`: an explicit base-currency market-value change.
- `SHARE_DELTA`: share change times evidenced local price and snapshot FX rate.
- `new_cash_base`: increases both hypothetical cash and NAV in the declared base currency.

All operations use canonical market-qualified identities. Position values, quantities, NAV, cash,
weights, Top-1/3/5, HHI, and sector/region/full-theme/split-theme exposures are recomputed without
requesting history. The original snapshot and request are frozen and never mutated. Runtime semantic
validation rechecks operation formulas, cash plus positions equals NAV, state weights, additive
exposures, concentration, cash/NAV/change deltas, risk deltas, constraint feasibility, quality
status, and a simulation identity bound to the complete result body.
It first runs the complete Phase 1 snapshot schema, content identity, alias, FX, cash/NAV, weights,
exposure, and quality semantics over the embedded source, then requires `before` cash, NAV,
quantities, and market values to match it. Decimal ratios, weights, and signed exposure maps use one
documented relative reconciliation tolerance of `1e-24`; monetary NAV identities remain exact.
Every result is self-contained: `as_of`, `base_currency`, currency-qualified monetary fields,
source/change/price/FX coverage, warnings, and data-quality notes travel with the hypothetical state.
`coverage.source_positions` is exactly the number of embedded source position rows; zero is valid and
complete for an all-cash snapshot, so no-op and new-cash-only results remain self-verifying. The
source quality status must exactly match the embedded snapshot. Canonical notes use fixed source
warning, missing-field, source-error, and simulation-warning prefixes and are rebuilt exactly during
validation; synchronized free-text rewrites plus a new simulation hash are rejected.
Risk-change status is `STRUCTURAL_ONLY` because no historical price request occurs. With explicit
`allow_short=true`, signed source or hypothetical states use `STRUCTURAL_ONLY_SIGNED` authority and
report absolute short, long, gross, and net market values/weights plus long/short/gross/net exposure
views. Long-only Top-N/HHI and concentration are `null`/`UNAVAILABLE_SIGNED`; warnings and partial
quality remain explicit whenever either before or after is signed.

Evidence is account-leg preserving. Each canonical identity carries immutable source legs, unit
price/FX, sector, region, themes, and `FULL_AND_EQUAL_SPLIT` theme allocation semantics. Same-direction
multi-account changes use deterministic proportional allocation by absolute source-leg market value,
with an exact residual assigned to the final sorted masked account leg; runtime semantics rebuild the
aggregate source state and verify allocations. Opposite-direction legs for the same identity fail
closed as `UNSUPPORTED_OFFSETTING_LEGS`, so +400/-360 can never be netted into a misleading long-only
40 position. Exposure views are rebuilt from classification evidence, making consistent classification
and report rehash tampering invalid.
The complete content-addressed source snapshot is embedded and revalidated, so changing TECH to
ENERGY and merely recomputing the simulation hash cannot redefine the source classification.
Runtime semantics rebuild every source leg directly from embedded snapshot positions, including its
masked account alias, canonical identity, quantity, base market value, currency/FX, and
sector/region/themes. Position aliases must exist in the embedded snapshot account set; rebuilt legs
must exactly equal evidence in stable account-alias order. Operation allocations are recalculated
from those rebuilt legs, never from caller-editable evidence totals. Thus redistributing 400/200 to
300/300 and synchronously editing allocation plus simulation hash is rejected.

Without `allow_margin`, negative hypothetical cash is infeasible. Without `allow_short`, negative
hypothetical quantity/value is rejected. Sell-only rejects positive deltas. New-cash-only rejects
sells and purchases above explicitly supplied new cash. Explicit permission changes feasibility only
inside the simulation; it never enables an execution capability.

Every representation is labeled `HYPOTHETICAL` and `execution_disabled=true`. The module contains no
password, unlock, order generation, or trading-write API. No-save is the default. `--output DIR`
persists `request.json`, `result.json`, and escaped `summary.md` together and never overwrites an
existing artifact.
