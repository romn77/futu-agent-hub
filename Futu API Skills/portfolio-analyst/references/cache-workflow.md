# Local cache protocol

Read for cache request creation, evidence/summary registration and final report publication. This is
local storage and orchestration, not a new research provider. Source permissions and research budgets
still apply. Use a canonical private temporary directory outside Git; control files must be 0600.

## Prepare current analysis

Create a JSON request using the actual user question and preferences available in this session:

```json
{"schema_version":1,"question":"分析当前组合","preferences":null,"language":"zh-CN","cache_mode":"prefer","analysis_mode":"current"}
```

Use `account_ids` only for explicit selection, as a list of private string IDs. Optional `scope`
records actual intent, explicit symbols and budgets so a different request cannot reuse the report.
Do not invent preferences. `freshness_requirement:"latest"` refreshes current account/discovery;
`refresh_all` bypasses all requested business cache reuse. Known trades use `refresh_account` or
`events:[{"type":"account_changed"}]`; known SEC filings use `events:[{"type":"sec_filing","cik":"..."}]`.

```bash
python scripts/cached_analysis.py --request /absolute/private/request.json --json
```

- `report_hit`: return the provided Markdown with original data/report time and reuse note. Stop.
- `components_ready`: retain envelope and context, then research only gaps; honor `research_cache_mode`.
- `research_pending`: another draft owns this request; retry within the request budget, do not duplicate.
- `historical_snapshot`: explicitly historical, never label current. Request a known `snapshot_id` with
  `analysis_mode:"historical"`; no snapshot is reconstructed if the record is missing.
- `error`: preserve the reason. Missing/ambiguous account identity is not permission to select another.

`off` bypasses local cache; the old helper still uses controlled temporary files. It is not a promise
of zero filesystem writes by every library. Default account TTL is five minutes from original as_of.
Expiry is checked on requests; no timer or background job is created. Private retention is at most
24 hours and subject to capacity eviction; use explicit history for durable daily comparisons.

## Public evidence before another source request

Query fields are `provider`, `interface`, `version`, one public `subject`, `dataset`, and `parameters`.
Use the observed source/normalization contract version. SEC uses CIK/accession, and market data uses
the full security, interval, adjustment and session. Parameters are bounded public scalars/lists,
never portfolio objects. Supported datasets: sec.index/content/facts/holdings, market.ohlcv, news,
estimates, valuation, disclosure and summary. Inspect the installed cache request schema if needed.

```bash
python scripts/cache.py lookup --request /absolute/private/public-query.json --json
python scripts/cache.py reserve --request /absolute/private/public-query.json --json
python scripts/cache.py publish --manifest /absolute/private/evidence-manifest.json --payload /absolute/private/evidence.json --lease /absolute/private/lease.json --json
python scripts/cache.py release --lease /absolute/private/lease.json --json
```

The lookup request wraps `{"query":{...}}`; optional cutoff must be timezone-qualified. `force:true`
bypasses lookup for a requested refresh. Reserve returns OWNER with a lease or BUSY. Store the lease
privately; release it after publication/failure. Do not pass tokens to source tools. Publication
manifest contains `query`, `kind:"public_evidence"`, source_url, source_as_of, available_at, complete,
and applicable dependencies/guards. Source result bytes are JSON, not executable text.

SEC and Futu adapters register their supported raw receipts. Other authorized host tools must project
verified public facts to `{"observations":[{"text":"...","value":null,"unit":null,"period":"..."}]}`;
the dataset must be disclosure/news/estimates/valuation. Do not put arbitrary tool objects or private
report prose there. Fields are checked, but field shape or a claimed URL does not prove public origin.
Never ingest material you cannot separate from private context. Public source timing and gaps remain
visible. Errors/rejections are not successful evidence; partial/truncated coverage stays partial.

For SEC helper reads use `--cache-mode prefer`, or `refresh_all` when requested. SDK metadata and
body scope remain validated; a missing optional bridge reports its limitation. Retain `cache.refs`
from actual receipts. Index refs carry generation and epoch: retain them as dynamic dependencies.
Collect rechecks directories for its frozen cutoff but reuses unchanged fixed filing bodies.

For current findings retain discovery refs covering each relevant public subject: SEC index for
filings, news/disclosure discovery for other fixed facts. Refresh an expired discovery; unchanged
content preserves its generation, so a cited summary can remain reusable without another synthesis.
No discovered change does not prove all possible news channels were searched.

## Cited summaries

Look up summary by subject, question and method first. Publish via `kind:"public_summary"`; the
payload requires question, subject, period, facts, inferences, scenarios, supporting_refs,
contrary_refs, limitations, follow_up, method_version, model and calculations. Array fields remain
arrays. Supply actual source dependencies and dynamic discovery refs in the manifest. If no contrary
fact was found, state that limitation; do not invent one. No holdings or portfolio implications.

Use valid summaries before loading whole originals again. Expand decisive citations as needed;
new evidence changes only dependent findings. Report/summary cache hits are not new independent facts.

## Finish or abandon a draft

Save the returned context unchanged in a 0600 JSON file. Save Markdown as a **JSON string**, not plain
text, in the private report file. Coverage is a JSON object with the sorted complete holding identities,
`research_complete` boolean and `open_required_items` list. Public refs are a JSON list from real
cache receipts, including discovery refs. Neither file goes to external research tools.

```bash
python scripts/cached_analysis.py --request /absolute/private/request.json --context /absolute/private/context.json --report /absolute/private/report.json --coverage /absolute/private/coverage.json --research-refs /absolute/private/research-refs.json --json
```

The publisher checks the bound snapshot, headings, complete holdings table and numeric columns,
request, account, dependency validity and current discovery. It does not certify unsupported prose.
`report_published` returns the final text. `report_uncached` retains text with a reason; fix material
research gaps before claiming completeness. `components_ready` after finalization is the one allowed
account refresh: update all account-bound sections using its replacement context while keeping valid
public research. If that also expires, `historical_only` must be labelled historical, not current.

Current option receipts have UNVERIFIED quote freshness, so held-option reports do not auto-replay
as current; the snapshot and qualifying risk/technical data still reuse normally. Explain this limit.

If abandoning a draft, release its ownership:

```bash
python scripts/cached_analysis.py --request /absolute/private/request.json --context /absolute/private/context.json --abort --json
```

Cache maintenance is explicit: `python scripts/cache.py status --scope public --json` or `prune`;
use `--scope private` for private cache. Cleanup never means deleting canonical account history.

## Optional explicit policy overrides

Defaults remain account/report 300 seconds, SEC index 3600, news 900,
estimates/valuation/OHLCV 86400; an unfinished weekly bar is capped at 300.
A user/operator may explicitly set `RESEARCH_CACHE_` plus one of:

- `ACCOUNT_TTL_SECONDS`, `REPORT_TTL_SECONDS`, `SEC_INDEX_TTL_SECONDS`,
  `NEWS_TTL_SECONDS`, `ESTIMATES_TTL_SECONDS`, `VALUATION_TTL_SECONDS`, `OHLCV_TTL_SECONDS`:
  positive integer seconds, maximum 86400. Source-specific stricter limits still apply.
- `PRIVATE_RETENTION_SECONDS`: positive integer, at most 86400; default 86400.
- `PUBLIC_MAX_MIB` / `PRIVATE_MAX_MIB`: positive integers up to 4096; defaults 512 / 128.

Do not change these as a side effect of ordinary analysis. Changes to validity produce a new
scope-specific `policy_version`; old current records miss, even when the new TTL is longer.
Explicit historical reads retain their original provenance/retention. Capacity changes do not
extend data freshness. A report expires at the earlier of snapshot expiry and creation plus report TTL.
Invalid settings return `INVALID_POLICY`; `off` ignores unused cache settings and creates no cache/key.
