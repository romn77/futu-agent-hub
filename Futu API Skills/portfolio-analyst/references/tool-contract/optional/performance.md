# Performance tool contract

## Current CLI

```bash
python scripts/portfolio.py performance --from YYYY-MM-DD --to YYYY-MM-DD --json --no-save
```

The command reads bounded canonical saved snapshot history for an inclusive UTC date range. It does
not accept the report's temporary snapshot as a replacement for history. `--output DIRECTORY` is
explicit persistence; default/`--no-save` does not persist. Preserve attribution coverage and
external-flow limitations; unavailable history cannot be reported as zero performance.
