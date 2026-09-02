# Options tool contract

## Current CLI

```bash
python scripts/portfolio.py options --snapshot TEMP_RELATIVE_SNAPSHOT.json --json --no-save
```

Exactly one of `--snapshot` and `--latest` is required. Use the report-bound temporary snapshot.
`--output DIRECTORY` explicitly persists an option bundle; default/`--no-save` does not persist.

The current command inventories nonzero positions whose canonical `security_type` is `OPTION` and can
query pinned US/HK option quotes. It reports signed snapshot quantity, option market value, expiry
buckets, quote selection/coverage, and raw Delta/Gamma/Vega/Theta/Rho when available. Those Greeks
are labeled `RAW_CONTRACT_UNITS_NO_MULTIPLIER`; aggregates require complete population coverage.
Status may be `COMPLETE`, `PARTIAL`, `UNAVAILABLE`, or `NOT_APPLICABLE`; quote freshness is
`UNVERIFIED`.

A package-owned bounded quote-only worker may additionally provide Futu static option metadata:
multiplier (`lot_size`, provenance `FUTU_STATIC_INFO_LOT_SIZE`), exact underlying, Call/Put right,
expiry, and strike. From those fields and only the same embedded snapshot, the command derives
multiplier-adjusted Delta units, local/base notional Delta, ITM/ATM/OTM state, and same-account
covered-call or protective-put inventory coverage. The ATM band is
`max(abs(strike) * 0.001, 0.01)` local currency units. Conflicting static and quote fields are
suppressed with `OPTION_METADATA_CONFLICT`; the raw-Greek status is preserved.

The command never fetches a separate underlying price. Missing, cross-account, or disagreeing
underlying rows keep money Delta and moneyness unknown. It excludes max profit/loss, breakeven,
probability, margin, scenario P&L, beta adjustment, cash-secured puts, spreads, collars, and strategy
inference. Never infer fields from the contract label or code.
