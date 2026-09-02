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

The command does not currently expose reliable option right, underlying, multiplier, moneyness,
covered/protected relationships, or notional/share/beta Delta. It also excludes max profit/loss,
breakeven, probability, margin, and scenario P&L. These are unavailable/degraded until a future tool
contract exposes them; never infer them from the contract label or code.
