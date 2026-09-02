# Portfolio Analyst behavior evaluator

`evals.json` defines behavior cases. `run_evals.py` compares those assertions with a deterministic
audit-envelope sidecar produced by a test harness. The actual user deliverable remains
`response_markdown`; the trace is evaluation evidence and is not appended to the chat response.

## Audit envelope 1.0

One JSON file represents one case:

```json
{
  "schema_version": "1.0",
  "case_id": "comprehensive-many-holdings",
  "response_markdown": "# Portfolio review\n...",
  "trace": {
    "snapshot": {"trd_env": "REAL", "base_currency": "USD"},
    "loaded_references": ["references/workflow.md"],
    "executed_commands": ["portfolio.py risk --snapshot RUN/snapshot.json --no-save"],
    "observations": {
      "holdings": {"symbols": ["US.AAPL"]},
      "workflow": {"stop": false}
    }
  }
}
```

Required fields are `schema_version`, `case_id`, `response_markdown`, `trace.snapshot`,
`trace.loaded_references`, `trace.executed_commands`, and `trace.observations`. The first two trace
arrays contain strings; snapshot and observations are objects. Observations are structured facts
emitted by the harness, not prose scraped from the Markdown. Keep the sidecar local and do not place
private account IDs, user identity, or raw snapshot bodies in it.

`response_markdown` must be a non-empty, non-whitespace string. Bundled cases assert their required
report headings directly against it; the options case also checks that authoritative tool status and
broader analysis capability gaps remain visibly distinct in the user-facing Markdown.

The bundled cases use these observation fields:

- `holdings.symbols`, `deep_research.max_names`, and
  `deep_research.eligible_omissions_disclosed`;
- `options.tool_status`, `options.unit_label`, `options.capability_gaps`, and `workflow.stop`;
- `simulation.scenarios`, `simulation.target_weight`, and `unheld_candidates.count`.

`simulation.scenarios` and `holdings.symbols` are arrays so `count_equals` measures real items.
`options.tool_status` copies the authoritative current tool status; broader analysis capability gaps
remain a separate array and never rewrite that status.

## Assertion semantics

Dotted-path resolution walks object keys from the envelope root. Paths cannot index arrays and must
start with `response_markdown` or `trace`.

- `equals`: actual and expected JSON values must have the same type and value.
- `contains`: substring for strings; key for objects; exact item for arrays. For an array of strings,
  a substring match within any item also counts, allowing command-flag checks.
- `not_contains`: logical inverse of `contains`.
- `count_equals`: `len(actual)` equals a non-negative integer; use arrays when asserting row or
  scenario counts.

Every assertion result includes index, type, target, expected value, actual evidence, and pass/fail.
Malformed eval/audit input exits 2. A well-formed assertion failure exits nonzero with status 1.

## Run

```bash
python evals/run_evals.py --audit /private/path/case-audit.json
python evals/run_evals.py \
  --audit /private/path/case-1.json \
  --audit /private/path/case-2.json \
  --audit /private/path/case-3.json \
  --require-all
```

Output is one JSON result with `passed`, per-case assertion evidence, and summary counts. Audit case
IDs must be unique and match `evals.json`; `--require-all` additionally rejects missing cases.
