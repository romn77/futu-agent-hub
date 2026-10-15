# Portfolio Analyst external audit-envelope validator

The advisor-quality/refactoring work adds a separate [paired quality protocol](research-quality.md)
and a sixth [manual research sample](research-cases.md), `named-technical-gap`. These do not change
the three core case IDs or weaken their gates. Quality inputs are synthetic; the operator rubric,
registry, version mapping and expected results are never supplied to the model under test.
Current implementation, local test results and actual model-execution status are recorded in
[the implementation record](../docs/2026-10-14-prompt-simplification.md). A prepared payload or a
passing loading/budget test is not a behavior PASS or proof of improved research quality.

`evals.json` defines behavior specifications. `run_evals.py` validates assertions against an
audit-envelope sidecar supplied by an external harness; it does not execute the Skill, a model, or
any portfolio command. Its success means only that a structurally valid external envelope satisfies
the configured assertions. Without a trustworthy producer and retained execution evidence, this
validator cannot establish or claim a real Skill behavior evaluation PASS.

The actual user deliverable remains `response_markdown`; the trace is external evaluation evidence
and is not appended to the chat response.

## Report field integrity (producer 1.0.8)

`holdings.field_integrity` checks the visible holdings table against the bound snapshot's nonzero
canonical securities, including weight, base-currency market value and unrealized P&L. The execution
gate `holdings_fields_match_snapshot` requires verified values as well as the existing unique-symbol
gate. Empty/shifted cells, sign errors, unknown-as-zero and wrong values cannot pass just because
the symbols are present. Existing historical receipts are not rewritten.

The evaluation-only parser accepts English/Chinese column headers, reordered columns, explicit
percentages, currency labels, parentheses for losses and declared numeric unit scales. Display
rounding is checked at the reported precision. Unknown layouts/units remain `UNVERIFIED`; this is
not proof that unsupported prose is financially wrong. The check does not assess company cash-flow
versus cash-balance language or investment judgment; those still need independent semantic review.

The follow-up scope and prospective gates are in the
[reliability spec](../../../docs/superpowers/specs/2026-10-15-portfolio-report-reliability.md).

`reference_reads` records successful complete reads, repeated full-source characters, observed
output size and partial/failed attempts from host receipts. Duplicate logs of the same item ID
are counted once; conflicting duplicates are disclosed. This cannot infer whether a reread was
necessary after context loss, or certify that fragments form a complete read. `token_usage`
comes only from `turn.completed.usage`; missing/invalid fields remain null, never zero. Document
characters, actual provider token usage and semantic quality are different measurements.

Producer 1.0.8 additionally recognizes the exact observed zsh `-lc` startup diagnostic
``(eval):<line>: parse error near `end'`` before a single complete JSON response. It still requires
the original successful command receipt and exact fixture equality. Unknown chatter, other shells,
non-login commands, multiple JSON documents, trailing text and truncated JSON remain rejected.
Rechecking an earlier receipt creates a separately versioned replay; never rewrite its original
audit or label an observer correction as a regenerated model report.

## Prospective reliability cases

`prepare_reliability.py` reconstructs the raw synthetic portfolios in
`reliability-fixtures/holdout.json` through the unchanged snapshot/risk/technical libraries, then
reuses the existing isolated workspace and offline reader. It does not call a model or broker.
Only the chosen case's request/data are copied; the registry and
[operator rubric](reliability-holdout.md) stay outside the tested workspace.

```bash
python evals/prepare_reliability.py --registry evals/reliability-fixtures/holdout.json \
  --case H01-reserve-and-company-exposures --output /private/new-reliability-attempt \
  --instructions /private/frozen-instructions
```

Preparation validates the generated identities/semantics, preserves nullable numbers, records
input hashes and refuses an existing attempt directory. These cases contain stocks/ETFs; use
the existing option fixture for contract cases. R0/R1 paired input hashes must match outside
SKILL.md/references, and prompts must be identical. External execution needs actual authorization
for its concrete code/data payload; a prepared workspace is not a successful model evaluation.
The sidecar must not be supplied or self-reported by the model under test.

## Audit envelope 1.0

One JSON file represents one case:

```json
{
  "schema_version": "1.0",
  "case_id": "comprehensive-many-holdings",
  "provenance": {
    "producer_kind": "external_harness",
    "runner_id": "trusted-agent-harness",
    "runner_version": "1.0.0",
    "generated_at": "2026-09-02T00:00:00+00:00",
    "case_id": "comprehensive-many-holdings",
    "case_input_sha256": "<64 lowercase hex characters>"
  },
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

Required fields are `schema_version`, `case_id`, `provenance`, `response_markdown`, `trace.snapshot`,
`trace.loaded_references`, `trace.executed_commands`, and `trace.observations`. The first two trace
arrays contain strings; snapshot and observations are objects. Observations are structured facts
computed by the harness from host-observed tool receipts and independently inspected response
sections, never copied from a model-authored audit or test verdict. Keep the sidecar local and do not place
private account IDs, user identity, or raw snapshot bodies in it.

`provenance.producer_kind` must be `external_harness`. `runner_id` and `runner_version` identify the
independently controlled producer. `generated_at` is a timezone-aware ISO-8601 timestamp. The nested
`case_id` must match the envelope. `case_input_sha256` binds the audit to the immutable case input:
canonical UTF-8 JSON of `{"fixture": case.fixture-or-null, "id": case.id, "prompt": case.prompt}`
with sorted keys and compact separators, hashed with SHA-256. The validator recomputes this hash.

These fields are a producer adapter contract, not authentication. The validator rejects missing
provenance, a declared model self-report, malformed timestamps, and mismatched case hashes, but it
cannot prove that a claimed external harness is honest.

## Trust requirements for execution evidence

A human- or agent-controlled external harness is responsible for freezing the case input, invoking
the real Skill/model boundary, and preserving the response plus host-observed trace. Tool/reference
events must come from runner instrumentation or durable execution logs, not statements generated by
the model under test. The harness owns runner identity/version, timestamping, immutable input hash,
artifact custody, and any independent review needed before calling the evidence trustworthy.

This repository provides `run_codex_evals.py`, a real Codex CLI producer over isolated synthetic
fixtures, in addition to the case specifications, producer contract and validator.
Running `run_evals.py` alone still performs
only `EXTERNAL_AUDIT_ENVELOPE_ONLY` validation and must never be reported as a Skill behavior PASS.

## Execute a synthetic Codex evaluation

```bash
python evals/run_codex_evals.py --case comprehensive-many-holdings --output /private/new-eval-run
python evals/run_codex_evals.py --case conditional-options-degradation --output /private/new-options-run
python evals/run_codex_evals.py --case explicit-rebalance-request --output /private/new-rebalance-run
```

The output directory must not exist. Each run copies only the Skill, references, package and schemas
into an isolated workspace, replacing the CLI data boundary with a synthetic-only fixture. No
forked account reader is copied, and no live brokerage or web access is requested. The synthetic
NAV, holdings, risk/options/technical evidence and optional simulation use the real deterministic
library. The model receives the case prompt and Skill, not assertions, expected answers or verdicts.

The runner invokes `codex exec --json --ephemeral --sandbox read-only --ignore-user-config` with
approvals disabled, a 300-second default deadline (maximum 600), and an 8 MiB combined output budget.
Codex must already be installed/authenticated and able to initialize its own runtime. The runner
does not change credentials, user configuration, install dependencies or loosen the model sandbox.
CLI interface: [Codex non-interactive documentation](https://developers.openai.com/codex/noninteractive).

Retained artifacts include input hashes, prompt, raw host JSONL events, stderr, final Markdown,
audit and assertion results when execution completes. No recursive cleanup removes this evidence.
Initialization/timeout/malformed event failures produce `EXECUTION_UNAVAILABLE`, `behavior_pass:
false`, and nonzero exit status; an external envelope validator PASS never replaces a failed run.

The producer recognizes completed `command_execution` receipts and `agent_message` output. A
snapshot counts only after the actual fixture helper command returns the exact fixture body once.
Reference loads require the full source text in a successful command's observed output. Holdings
are counted from symbol cells in the holdings table; selected research names from level-three
subheadings or research-table symbol cells; omission disclosure from the gaps section. English and
Chinese heading aliases are recognized as Markdown headings or standalone bold titles. Unknown peer or higher-level headings terminate the preceding section; research/appendix tables never count as holdings merely because a heading was unrecognized. Simulation results must pass semantic and
snapshot-binding validation. These conservative parsers may reject alternate layouts: inspect the
retained trace/report, do not manufacture missing observations or edit evidence into a PASS.
The execution gate also requires full default-reference reads, a single bound helper receipt,
the source ID/time in the response, and complete unique holdings coverage. Time matching requires the exact snapshot instant in an explicit-zone ISO timestamp or an equivalent `YYYY-MM-DD HH:MM[:SS] UTC` representation; dates alone, absent zones and lost nonzero precision do not match. Execution gates are serialized individually under `execution.checks`. Duplicate rows cannot
substitute for missing holdings. Changed fixture files or reported file-change events fail the run.

`ASSERTIONS_PASSED` concerns only this synthetic routing/report boundary. Human review is still
required for semantic quality, and this offline suite does not validate current citations, live
web privacy/injection defenses, REAL accounts, SDK compatibility, or OpenD connectivity.

`response_markdown` must be a non-empty, non-whitespace string. Bundled cases assert their required
sections through harness-observed `report.sections`; the options case also checks that authoritative tool status and
broader analysis capability gaps remain visibly distinct in the user-facing Markdown.

The bundled cases use these observation fields:

- `report.sections`, `holdings.symbols`, `deep_research.max_names`, and
  `deep_research.eligible_omissions_disclosed`;
- `options.tool_status`, `options.reported_tool_status`, `options.analysis_gaps_disclosed`,
  `options.unit_label`, `options.capability_gaps`, and `workflow.stop`;
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

Every assertion result includes index, type, target, expected value, actual evidence, and whether the
assertion was satisfied. Malformed eval/audit input exits 2. A well-formed assertion mismatch exits
nonzero with status 1.

## Run

```bash
python evals/run_evals.py --audit /private/path/case-audit.json
python evals/run_evals.py \
  --audit /private/path/case-1.json \
  --audit /private/path/case-2.json \
  --audit /private/path/case-3.json \
  --require-all
```

Output is one JSON result labeled `validator_scope: EXTERNAL_AUDIT_ENVELOPE_ONLY`, with
`audit_validation_succeeded`, per-case assertion evidence, and satisfied/unsatisfied summary counts.
It also states that the validator itself did not execute the Skill. Audit case IDs must be unique and
match `evals.json`; `--require-all` additionally rejects missing cases.

## Producer 1.0.2 parsing corrections

Version 1.0.2 separates unknown peer sections, supports additional semantically equivalent Chinese
section titles (including bounded colon/parenthetical descriptions), and compares explicit-zone timestamps by exact instant. These changes address
observed false negatives without weakening snapshot receipts, reference-read evidence, unique
holdings coverage, mutation rejection, or assertion requirements. Original execution artifacts
remain immutable; a new run uses a new directory. Never rewrite an earlier failed audit into a PASS.

## v0.8.0 research collaboration acceptance

The product now routes financials, estimate revisions, thesis, earnings preview/recap and valuation
through suitable available skills in the same host. Qualified evidence is reused; Futu public data
is preferred for remaining matching-basis gaps. Default coverage is eight deep-research names and
two automatically selected full valuations, with explicit user scope expansion. The existing
account, risk, technical and option authority remains separate from public research.

The three `evals.json` cases remain the executable **offline account/report baseline**. They cannot
prove public SDK access or optional research methods. Their network and installed-skill isolation
is unchanged. [Research cases](research-cases.md) specify separate controlled host-log scenarios;
do not add them to `evals.json` unless the producer actually implements their fixture contract.
An unsupported case must fail instead of silently using another case. Research inputs, observed
requests/results/calculations and final reports are required; a checklist or model claim is not a
successful run. `--require-all` covers all cases in the selected specification, not arbitrary files.

Producer 1.0.3 addresses an observed mismatch between permitted peer comparisons and the old
candidate counter. Only an exact level-four `Peer comparisons` / `同行比较` block under a
level-three held-symbol heading is excluded from selected-name/candidate observations, until the
next heading of level four or higher. Unheld research headings and all Observations remain counted
conservatively, including a peer subsequently proposed there. A declared comparison label is not
semantic proof: independently inspect that it contains comparison evidence rather than advice.
The output contract now requires a first `Symbol` column for research tables and supplies fixed
Chinese/English section titles, rather than continuously expanding fuzzy aliases.

### Account invariant regression map

| Invariant | Existing verification |
|---|---|
| INV-01 account selection/scope | `test_account_scope_and_missing_values.py`, `test_snapshot_service_cli.py` |
| INV-02 one bound snapshot | `test_current_analysis_helper.py`, `test_helper_output_authority.py`, `test_codex_eval_producer.py` |
| INV-03 OpenD NAV | `test_opend_nav.py`, `test_nav_basis_compatibility.py` |
| INV-04 full holdings | snapshot and producer tests, `comprehensive-many-holdings` |
| INV-05 unknown source facts | `test_source_field_fidelity.py`, account-scope tests |
| INV-06 fatal/degradation/cleanup | helper/output-authority tests, `test_safety.py` |
| INV-07 explicit optional workflows | prompt architecture and CLI mutation-gate tests |
| INV-08 read-only account boundary | `test_adapter.py`, `test_skill_trust.py`, `test_manifest_integrity.py` |
| INV-09 no default persistence | current helper, safety and CLI mutation-gate tests |
| INV-10 no automatic trade action | prompt tests and `explicit-rebalance-request`; semantic report review |

### Execution record, 2026-10-14

Development branch `codex/portfolio-v080-research`, base `a50e3e1`. This record distinguishes
implementation, local tests and actual external execution; product version 0.8.0 is not a claim
that every source or host has been certified.

- Same-session pre-change baseline: 1227 tests passed in the filesystem sandbox; the one
  loopback-listener test passed separately with socket permission. Existing cached Ruff 0.15.12
  checked and formatted the component successfully without installing anything.
- Shared-workspace integration initially returned 70 failures / 1160 passes while another
  concurrent change modified the audited `futuapi/scripts/common.py` and account code. Preserve
  those changes; this run is not attributed to research collaboration. Verify this change against
  its fixed audited base in `/private/tmp/portfolio-v080-validation-01` and record that scope.
- New collaboration and stable-heading tests were observed failing before their corresponding
  changes. The peer-scope regression cases likewise failed before the parser correction.
- Automatic approval initially rejected sending analyst code and synthetic inputs to Codex. The
  user then explicitly approved that destination/payload and necessary failure-fix reruns; the
  actual evaluations below were run after approval, without bypassing the initial rejection.
- Public Futu smoke: `get_financials_statements.py US.AAPL --statement-type 1 --financial-type 9
  --num 4 --json` returned `Network interruption.` on the initial attempt and one spaced retry.
  Receipts remain in `/private/tmp/portfolio-v080-public-smoke-01` and `-02`. Doctor connectivity
  does not imply successful Futu financials access.
- Follow-up read-only diagnosis found matching SDK/OpenD 10.10.7008 and a successful local
  handshake, but `qot_logined=false` / `trd_logined=false`; ordinary public quotes also returned
  Network interruption. Upstream login/connectivity remains unresolved and is separate from the
  code acceptance. The user chose to handle it later and authorized local commits.
- The existing authorized web path read an Apple issuer release and its SEC exhibit, applied the
  suitable futuapi earnings-review method, executed growth/margin calculations, and associated
  findings locally with a synthetic 40% AAPL holding. Inputs, calculations and a focused note are
  in `/private/tmp/portfolio-v080-public-smoke-web-01`. This is a developer-operated public-chain
  smoke check, not proof of autonomous model behavior or a complete real-account report.

Final shared-workspace regression: **1286 passed in 17.13s** (Python 3.12.4, pytest 9.1.1,
jsonschema 4.26.0). The earlier fixed-base full regression was **1268 passed in 17.27s**; later
observer regression additions are included in the final shared result. All owned Python changes
pass Ruff 0.15.12 check/format; local skill links and version/schema declaration tests pass.

Producer **1.0.5** additionally recognizes an exact safe Python `-B` script invocation, rejects
shell chaining/substitution in account/simulation receipts, preserves bold paragraph labels ending
in a colon within their report section, and recognizes ASCII/fullwidth slashes in ITM/ATM/OTM.
Each change had a failing regression from observed output. Unknown real headings retain section
boundaries and all original source/body/identity checks remain. Declared peer scopes still require
semantic review. No failed artifact was reclassified in place.

| Actual baseline execution | Final status | Assertions |
|---|---|---|
| `/private/tmp/portfolio-v080-comprehensive-03` | ASSERTIONS_PASSED; execution evidence passed | 18/18 |
| `/private/tmp/portfolio-v080-options-03` | ASSERTIONS_PASSED; execution evidence passed | 20/20 |
| `/private/tmp/portfolio-v080-rebalance-02` | ASSERTIONS_PASSED; execution evidence passed | 16/16 |

The three actual model runs also passed independent report review in
`/private/tmp/portfolio-v080-baseline-final-review.md`. The aggregate validator result is retained
at `/private/tmp/portfolio-v080-final-audit.json` (54/54); its scope remains envelope validation,
not a replacement for those real executions.

Five separate research runs are independently reviewed using their actual receipts, arithmetic,
reports and unchanged manifests. Detailed verdicts and retained failure attempts are recorded in
[the implementation/acceptance record](../docs/2026-10-14-v0.8.0-implementation.md). Research sample
reviews are not passed off as automatic `evals.json` assertions. Large-portfolio sample failures
led to an explicit single-helper rule and read-only inspection of the retained same-invocation
account result; hidden public backing-data access remains prohibited. Capability-level permission
denial now explicitly stops further symbols on that denied capability for the run.

Fresh isolated Codex sessions loaded the complete 0.8.0 entry and required references. No global
skill installation was changed. Optional wheel building was unavailable because setuptools is
absent from the existing interpreters; no dependency was installed. Missing optional providers
require no installation, and live accounts and untested source/host combinations remain unverified.

## Reviewed branch integration follow-up

The subsequent maintenance merge incorporates `4fa748f` and `65f6927` while retaining main's
newer research and evaluator checks. Producer 1.0.6 accepts the older regression's explicit
Chinese `工具状态为 COMPLETE` label; strict command parsing, paragraph/section boundaries and peer
scopes remain. The restored older tests passed alongside the current suite.

The complete integration passed 1348 tests and 91 subtests, including offline trading-boundary
tests. A new real read-only helper run also verified successful option quote/metadata acquisition,
COMPLETE option status, exact component binding and cleanup. This supersedes the earlier statement
that real account integration had not been attempted, but does not claim complete classification,
OTC data coverage or compatibility with untested accounts and providers. See the
[pending-fixes review](../docs/2026-10-14-pending-fixes-review.md) for the precise scope; no real
account payload was added to fixtures, repository artifacts or extra model evaluations.
