# Simulate tool contract

## Current CLI

```bash
python scripts/portfolio.py simulate --snapshot TEMP_RELATIVE_SNAPSHOT.json --request REQUEST.json --json --no-save
```

Exactly one of `--snapshot` and `--latest` plus `--request` is required. The request path is below the
private data home. `--output DIRECTORY` is explicit persistence; default/`--no-save` is not.
The command performs deterministic non-executable rebalance arithmetic. It never places an order and
does not model fills, taxes, fees, slippage, or liquidity unless its documented schema says otherwise.
