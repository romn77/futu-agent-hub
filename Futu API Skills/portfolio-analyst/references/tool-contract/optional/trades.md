# Trades tool contract

## Current CLI

```bash
python scripts/portfolio.py trades --from YYYY-MM-DD --to YYYY-MM-DD --trd-env REAL --json --no-save
```

The inclusive period is required and bounded. `--latest` or `--snapshot` is optional NAV evidence,
not permission to replace current holdings. `--output DIRECTORY` explicitly persists a bundle;
default/`--no-save` does not. The audited readers fetch historical orders/fills read-only, mask raw
identifiers, and expose coverage/status. Incomplete context, fee, corporate-event, or FX evidence
stays partial/unavailable; a missing fact is not a zero.
