# Comprehensive current-holdings workflow

Conditional work follows [intent routing](intent-routing.md).

## 1. Select the account

Auto-select only one eligible account. Multiple candidates return `ACCOUNT_SELECTION_REQUIRED`;
invalid choices return `ACCOUNT_SELECTION_INVALID`. Never substitute accounts. Explicit aggregation
reads exactly the requested accounts and stops if any is unreadable. Keep private IDs local.

## 2. Resolve cached analysis

If readiness is unknown: `python scripts/portfolio.py doctor --json --no-save`.
When preparing requests or publishing, follow the [cache protocol](cache-workflow.md).
Create a private mode-0600 JSON request with actual question/preferences and optional `account_ids`:

```bash
python scripts/cached_analysis.py --request /absolute/private/request.json --json
```

Identity is verified before lookup. On `report_hit`, return the original Markdown/times and stop
research. On `components_ready`, retain the bound envelope and private context; continue below.
On `research_pending`, retry within budget without duplicate analysis. Inspect/filter retained stdout;
never rerun to redisplay it, send it to public tools, or substitute `--latest`/history.

A miss runs REAL/USD `snapshot`, `risk`, `technical`, and `options` only when held. Freeze `snapshot_id`/`as_of`;
technical uses bound risk or snapshot weights. Do not run `analyze` or read lifecycle history.
Use `refresh_account` for known account changes, `refresh_all` for all requested evidence, `off`
for no caching. Reads never renew TTL. UNVERIFIED option freshness prevents complete report replay.

Explicit uncached route: `python scripts/current_analysis.py`. Invoke the helper exactly once;
`--acc-id ACCOUNT_ID` selects; repeat only for aggregation. Legacy `--no-save` creates no canonical
history or stable alias key.

## 3. Consume validated results

Follow [failure and degradation](failure-and-degradation.md). Snapshot/validation,
source-binding mismatch, and cleanup failure are fatal. Risk/technical/options failures, including
`UNSUPPORTED_PORTFOLIO` and `INVALID_COMPONENT_OUTPUT`, remain local structured degradations.

Reconcile all filled holdings under [portfolio policy](portfolio-analysis-policy.md) and
[snapshot semantics](tool-contract/core/snapshot.md). Unknown cash/cost/price/sellable quantity stays
null. Partial cash supports only its known scope; missing cash/required prices blocks simulation.

Guarded cleanup failure retains state and its absolute retained path. Never substitute custom deletion.
Protection excludes same-UID malicious concurrent mutation; [owned-state implementation](implementation/owned-state.md)
is maintenance-only.

## 4. Research and compose

Apply the loaded six question routes and budgets from [research policy](research-policy.md),
[research capability contract](tool-contract/core/research.md), [evidence policy](evidence-policy.md),
and actual [technical coverage](tool-contract/core/technical.md). Keep public queries private-data-free.

Compose [output contract](output-contract.md) sections, citing bound account time and sourced external
facts. Connect supporting/contrary findings to portfolio implications and disclose local gaps.
Finalize with context, coverage and actual research refs. `components_ready` with refresh_attempt=1
requires one account rebind; retain valid research. `historical_only` cannot be presented as current.
Without context or with caching off, return validated output with the actual limitation.
