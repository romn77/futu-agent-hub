# Snapshot tool contract

## Current CLI

```bash
python scripts/portfolio.py snapshot --trd-env REAL --base-currency USD --json --no-save
```

`--trd-env` currently accepts `REAL` or `SIMULATE`; `--base-currency` accepts the currencies declared
by the parser. The comprehensive route always supplies `REAL` and `USD`. `--save` explicitly creates
a canonical immutable history snapshot. `--output` is unsupported for snapshots. Default/`--no-save`
creates no canonical snapshot or stable alias key.

The normalized output provides `snapshot_id`, `as_of`, accounts, cash, positions, valuation totals,
FX evidence, and data-quality status/notes. It depends on audited read-only account/portfolio calls.
Missing position currency, required FX, invalid/non-positive NAV, or unsafe source identity fails
closed instead of manufacturing valuation.

For one-report binding, use `scripts/current_analysis.py`. Its owned temporary-state contract is in
`references/workflow.md`; it passes an exact restricted snapshot to downstream reads and performs
allowlisted identity-checked cleanup within that document's stated threat model. This is
orchestration, not a new snapshot CLI flag or canonical persistence mode.
