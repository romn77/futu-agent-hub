# Lifecycle tool contract

## Current CLI

```bash
python scripts/portfolio.py lifecycle list --json
python scripts/portfolio.py lifecycle inspect MARKET.SYMBOL --json
python scripts/portfolio.py lifecycle validate --snapshot TEMP_RELATIVE_SNAPSHOT.json --json --no-save
```

`list`, `inspect`, and default `validate` are read-only local operations. `validate` requires exactly
one of `--snapshot`/`--latest`; `--output DIRECTORY` is explicit persistence. `lifecycle init` creates
one local user record and is allowed only on an explicit request with required role, dates, target
range, thesis, invalidation conditions, review triggers, and last-review time. No lifecycle action
unlocks trading or changes a brokerage position.
