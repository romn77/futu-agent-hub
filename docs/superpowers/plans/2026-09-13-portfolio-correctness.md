# Portfolio Correctness Implementation Plan

> **For agentic workers:** Use subagent-driven-development for isolated runtime work and independent review. Track execution with the checkboxes below.

**Goal:** Implement the approved correctness/runtime PRD without changing account NAV or read-only scope.

**Architecture:** Add small deterministic classification and diversification functions, version risk output as 1.1, and preserve validation of 1.0 reports. Keep diagnostic stages separate and bounded. Existing normalized snapshot and audited account boundaries remain authoritative.

**Tech Stack:** Python, Decimal, JSON Schema, pytest; reuse original checkout virtual environments.

All component paths below are under `Futu API Skills/portfolio-analyst` in this worktree. Test command from that directory is `/Users/yaoma/project/futu-agent-hub/Futu API Skills/portfolio-analyst/.venv/bin/python -m pytest -o addopts='' -q`. Runtime command uses `/Users/yaoma/project/futu-agent-hub/.venv/bin/python scripts/portfolio.py`.

### Task 1: Capture independent regression examples

- [x] Create `tests/test_risk_correctness_v11.py` using valid synthetic snapshots from existing test builders. Assert all-unknown 90% with a 60% cap is UNKNOWN; known 70% breaches; known 40% plus unknown 30% is UNKNOWN; known 30%/20% plus unknown 10% is within 60%; no threshold is NOT_CONFIGURED.
- [x] Assert one 10% stock plus 90% cash has effective positions 1, two 25% stocks have 2, proportional cash changes preserve diversification, empty stock population returns null, and old saved reports remain accepted.
- [x] Run this file and record expected failing assertions before implementation.

### Task 2: Implement risk metric and versioned classification semantics

- [x] Create `portfolio_analyst/risk_metrics.py` with Decimal-only functions. Compute known weight excluding UNKNOWN, unknown weight, lower bound=max known exposure, upper bound=lower+unknown. Status is NOT_CONFIGURED for no threshold, BREACHED for lower>threshold, WITHIN_LIMIT for upper<=threshold, otherwise UNKNOWN.
- [x] Compute holdings HHI as NAV-HHI divided by sum(weights)^2 and effective positions as sum(weights)^2 divided by NAV-HHI; use null for an empty population. Preserve existing NAV-HHI and all NAV weights.
- [x] Modify `portfolio_analyst/risk.py` to emit version 1.1, coverage per classification dimension, normalized HHI, and tri-state configured breach values. Generate classification-gap findings instead of UNKNOWN concentration claims; known exposures remain reviewable. Keep exactly three evidence-backed findings/gaps and deterministic sorting.
- [x] Update `schemas/report_defs.schema.json`, `schemas/risk_report.schema.json`, and `schemas/portfolio_report.schema.json` with strictly versioned 1.0/1.1 objects. Keep old shapes closed and old semantics readable.
- [x] Update `portfolio_analyst/schema.py` to independently validate bounds, metric ratios, policy states and finding authority. Bind new mirrored portfolio fields to the embedded versioned risk report.
- [x] Run new regressions and existing risk/schema tests. Update existing expectations only where they explicitly encode the corrected financial definitions.

### Task 3: Report and consumer integration

- [x] Modify `portfolio_analyst/reporting.py`, human risk rendering in `portfolio_analyst/cli.py`, and risk/analysis/output policies to display both NAV-HHI and holdings HHI with coverage and UNKNOWN constraint states.
- [x] Check technical selection, rebalance, performance, and model fixture/eval consumers. Rebalance only uses Top 1/3/5 and NAV-HHI, so preserve those semantics; verify that assumption in tests.
- [x] Add version downgrade/tampering tests and a legacy fixture; update golden output only for intentional behavior changes.

### Task 4: Separate runtime diagnostic stages

- [x] Add failing tests in `tests/test_doctor.py` for successful SDK import followed by OpenD timeout, missing package, import PermissionError and connection refusal.
- [x] Modify `portfolio_analyst/doctor.py` and `portfolio_analyst/opend_probe_worker.py` to retain installation/version/import evidence and use bounded independent SDK and quote-handshake stages. Include interpreter identity; no global environment fallback.
- [x] Test existing diagnostic behavior plus new cases. Document root runtime versus component test interpreter and accurate read-only diagnostic commands in `docs/runtime.md`.

### Task 5: Platform failure investigation

- [x] Reproduce `/tmp` error-code failure, define invalid symlink/config precedence, and make its expectation platform-aware while asserting `/private/tmp` unsafe-home rejection explicitly.
- [x] Diagnose process termination with bounded state observation; check running versus zombie state on supported macOS/Linux without weakening kill-tree or output-budget guarantees. Retain regression proof using delayed side effects when appropriate.
- [x] Reproduce journal concurrency on both available interpreters; fix any confirmed race, otherwise record the evidence and limitation without claiming a fix.

### Task 6: Acceptance and review

- [x] Run full suite, lint, formatter and diff checks: 1228 tests passed after evaluation-parser regressions.
- [x] Real-model producer: user authorized export on 2026-09-13; three comprehensive-many-holdings calls completed with immutable evidence. Latest execution gates pass, assertions 15/18; formal behavior PASS is not claimed. See component docs/2026-09-13-model-evaluation.md for remaining heading-contract limitation.
- [x] Run root-venv doctor. If OpenD remains absent, record integration unavailable; do not query accounts or claim live success.
- [x] Check Linux runtime availability without modifying external servers; report untested if no suitable local runtime exists.
- [x] Independent spec review passed; findings fixed and covered by regression tests.
- [x] Final code-quality review passed; no actionable findings. Reviewer also validated 200 randomized synthetic portfolios.
- [x] Update the approved PRD state and write `docs/2026-09-13-correctness-acceptance.md` with actual results and any external prerequisites.
