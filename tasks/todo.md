# v0.8.0 implementation tasks

Scope and dependencies: [plan.md](plan.md). Status is updated only after the stated evidence exists.

## 1. Baseline (T1)
- [x] Record branch, starting commit, existing user files and same-session test/doctor results.
- [x] Run available Ruff and diff checks; map account invariants to existing tests.
- Verify: component pytest baseline; cached Ruff check/format; `git diff --check`.
- Files: execution records and evaluation README.

## 2. First research collaboration path (T2)
- [x] Update entry point, workflow and research contract consistently.
- [x] Available, relevant skills use existing authorized capabilities; already loaded content and
  evidence are reused; missing skills preserve the original analysis.
- [x] Account reads retain their exact CLI boundary; public SDK/MCP permission is independent of
  web availability, and actual denials cannot be bypassed.
- Verify: focused prompt-contract tests and manual consistency review.
- Files: `SKILL.md`, `references/workflow.md`, `references/tool-contract/core/research.md`,
  `tests/test_prompt_architecture.py`.
- Depends on: 1.

## Checkpoint: collaboration
- [x] Focused tests pass; owned helper and account implementation unchanged by this work.

## 3. Financial/thesis, earnings and valuation routes (T3)
- [x] Six routes have concrete triggers, evidence and expected results.
- [x] Eight-name/two-model defaults, explicit expansion, source reuse and public calendar windows
  are consistent; known Futu output limitations remain visible.
- Verify: routing-contract checks and representative behavior cases in task 6.
- Files: `references/intent-routing.md`, `references/research-policy.md`, optional focused
  `references/futu-public-research.md`.
- Depends on: 2.

## 4. Evidence and valuation quality (T4)
- [x] Privacy covers all public request channels; shared public market queries are allowed.
- [x] Necessary source/period/unit information supports key claims without mandatory metadata noise.
- [x] Valuation requires actual calculation, matching periods and valid assumptions; incomplete
  inputs restrict only affected conclusions.
- Verify: representative valid and invalid valuation, estimate and privacy inputs in task 6.
- Files: `references/evidence-policy.md`; optional focused valuation reference only if needed.
- Depends on: 2; may run alongside 3.

## 5. Portfolio synthesis and local degradation (T5)
- [x] One report contract uses stable headings, full holdings, thesis evidence and portfolio implications.
- [x] Missing history, optional skills, data, invalid models and budget omissions are distinct;
  preserve authoritative component status and fatal account/cleanup failures.
- [x] Advice and quantifiable simulation retain separate triggers.
- Verify: prompt tests; offline report behavior cases.
- Files: output, failure, portfolio and recommendation policies as needed.
- Depends on: 3, 4.

## Checkpoint: synthesis
- [x] Integrated contract review and focused tests pass; actual model regressions are tracked below.
- [x] No account core, Futu upstream or default dependency changes from this implementation.

## 6. Representative behavior verification (T6–T7)
- [x] Register concrete synthetic research inputs and observations for V01–V08 without weakening
  the three existing account/report cases or exposing live accounts to the runner.
- [x] Execute the three baseline model cases and representative research cases; inspect receipts,
  calculations and reports, recording exact pass/failure/unavailable status.
- [x] Exercise one authorized public research chain, recording actual coverage and source.
- Verify: real model/host tool receipts plus focused and full pytest, Ruff and diff checks.
- Files: `evals/evals.json`, `evals/README.md`, research samples; evaluation code/tests only for an
  observed gap.
- Depends on: 5; sample preparation may begin during 3–5.

## 7. Version, distribution and review (T8)
- [x] Synchronize entry point, package and runtime to 0.8.0 and update version assertions.
- [x] Verify complete skill directory/relative references and actual loading in fresh isolated
  Codex sessions. Global installation is unchanged; wheel building lacks setuptools.
- [x] Record tested scope, limitations, rollback and final review; do not claim untested sources.
- Verify: final pytest, Ruff, diff and package/entry-point checks; code quality review.
- Files: version declarations/test, evaluation README and release notes.
- Depends on: 6.

## Execution notes

- Development started from the user's approved PRD and plan. No real account data is needed for
  implementation or synthetic acceptance.
- T2–T5 production instructions implemented; routing/evidence tasks authored independently and
  integrated by the primary developer. The user subsequently authorized local commits of this
  completed v0.8.0 work; unrelated concurrent trading/account changes remain outside those commits.
- Fixed-base verification in `/private/tmp/portfolio-v080-validation-01`: 1245 tests passed,
  one Git-ignore test needed actual Git metadata in the copied fixture; after `git init` and copying
  the baseline `.gitignore`, that test passed separately. Ruff check/format and skill-link validation
  passed. A deliberate peer-scope mutation was caught by the new regression, then restored.
- Another session changed Futu trading/account files during shared-workspace tests, causing audit
  digest failures. Those changes were preserved and excluded from the fixed-base research overlay.
- Public Futu financials failed twice with `Network interruption.`; authorized issuer/SEC web
  fallback, calculations and synthetic-portfolio association succeeded. No live account was read.
- User explicitly approved sending the prepared analyst code/schema and eight synthetic cases to
  Codex/OpenAI, including needed failure-fix reruns, after an initial automatic approval rejection.
- First three real model runs produced reports but failed formal assertions: 17/18 comprehensive,
  16/20 options, 14/16 simulation. Actual receipts exposed Python `-B` handling and report-format
  observation gaps. Original artifacts are retained; corrected attempts passed as recorded below.
- Five separate research host-log samples completed and passed independent review; a launched
  sample alone is not a PASS. First-attempt failures remain unchanged alongside corrected reruns.
- Wheel build was attempted in the existing interpreter but `setuptools` is absent; no dependency
  was installed. Version/schema tests and full-directory reference checks are available separately.

## Final checkpoint

- [x] Shared repository full regression: **1286 passed in 17.13s**; all owned Python files pass
  Ruff check/format and all skill references resolve. Git diff whitespace checks pass.
- [x] Three real baseline model executions passed all **54/54** assertions and independent semantic
  review: comprehensive attempt 03, options attempt 03, rebalance attempt 02.
- [x] Five research cases passed observed host-receipt review: collaboration attempt 03,
  estimates/valuation attempt 01, degradation/boundaries attempt 01, expanded/default scope attempt 02.
- [x] Known limits and rollback documented in
  `Futu API Skills/portfolio-analyst/docs/2026-10-14-v0.8.0-implementation.md`.
- No real-account end-to-end run, global installation, push or public release was performed.
  The source implementation and approved development acceptance are complete. Futu public
  financials still returned network interruption; a public issuer-web fallback was exercised.
- Follow-up diagnosis: SDK/OpenD versions both 10.10.7008 and the local handshake succeeded, but
  `qot_logined=false` and `trd_logined=false`; ordinary quotes also returned Network interruption.
  The user will handle login/connectivity later and authorized committing the tested code now.
