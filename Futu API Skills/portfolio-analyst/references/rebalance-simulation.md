# Rebalance simulation contract

Request and result contracts are version `1.1`; the earlier unqualified `new_cash` shape is rejected.

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
validation rechecks state weights, additive exposures, concentration, cash/NAV/change deltas, risk
deltas, constraint feasibility, and quality status.
Every result is self-contained: `as_of`, `base_currency`, currency-qualified monetary fields,
source/change/price/FX coverage, warnings, and data-quality notes travel with the hypothetical state.
Risk-change status is `STRUCTURAL_ONLY` because no historical price request occurs. If explicit
shorting creates a signed hypothetical portfolio, it becomes `STRUCTURAL_ONLY_SIGNED`, carries a
partial-quality warning, and does not claim Phase 2 long-only risk authority.

Without `allow_margin`, negative hypothetical cash is infeasible. Without `allow_short`, negative
hypothetical quantity/value is infeasible. Sell-only rejects positive deltas. New-cash-only rejects
sells and purchases above explicitly supplied new cash. Explicit permission changes feasibility only
inside the simulation; it never enables an execution capability.

Every representation is labeled `HYPOTHETICAL` and `execution_disabled=true`. The module contains no
password, unlock, order generation, or trading-write API. No-save is the default. `--output DIR`
persists `request.json`, `result.json`, and escaped `summary.md` together and never overwrites an
existing artifact.
