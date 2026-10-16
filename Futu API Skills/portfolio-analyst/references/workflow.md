# Comprehensive current-holdings workflow

Use one fresh snapshot for each new comprehensive review. Follow [intent routing](intent-routing.md)
for conditional work; ordinary review defaults to current REAL/USD US-listed holdings.

## 1. Select the account

Auto-select only one eligible account. Multiple candidates require the user's choice
(`ACCOUNT_SELECTION_REQUIRED`); invalid/unavailable choices return `ACCOUNT_SELECTION_INVALID`.
Never substitute an account. Explicit aggregation reads exactly the requested accounts and stops
if any is unreadable. Private account IDs belong only in local account commands, never public
queries or the report. Ask only when ambiguity prevents reliable selection, holdings, NAV, or USD
valuation.

## 2. Acquire once

From the Skill directory, diagnose unknown readiness with
`python scripts/portfolio.py doctor --json --no-save`, then run:

```bash
python scripts/current_analysis.py
```

For a chosen account, use `python scripts/current_analysis.py --acc-id ACCOUNT_ID`; repeat
`--acc-id` only for requested aggregation. This forwards selection without saving a preference.

Invoke the helper exactly once. Retain stdout as internal evidence; inspect/filter that same result
using available context or read-only access. Never rerun to redisplay, trim, reformat, or recheck it.
If it cannot be inspected reliably, disclose the limitation. Do not paste raw JSON into chat or
send it to public tools, substitute `--latest`/saved history, or acquire another snapshot.

The helper runs fresh REAL/USD `snapshot`, then `risk`, then `technical`, and `options` only for
held options. Technical uses the successful bound risk report, otherwise snapshot weights. Freeze
`snapshot_id` and `as_of` for all account/component evidence. No default persistence, lifecycle, or
history reads occur. Do not run `analyze`: it reads lifecycle and snapshot history even without saving.

## 3. Consume validated results

Follow [failure and degradation](failure-and-degradation.md). Snapshot acquisition/validation,
source-binding mismatch, and cleanup failure are fatal. Component risk/technical/options failures,
including stock-short `UNSUPPORTED_PORTFOLIO`, are local: the component is `null` with a structured
`degradations` entry; retain the reliable snapshot and other components. Malformed component evidence
is local `INVALID_COMPONENT_OUTPUT`; do not present degraded coverage as complete.

Reconcile every nonzero filled holding against the final table; cash stays portfolio-level. Apply
[portfolio policy](portfolio-analysis-policy.md) and [snapshot semantics](tool-contract/core/snapshot.md).
Keep unknown cash/cost/price/sellable quantity `null`; never repair gaps arithmetically. Known cash
from only some accounts supports only that narrower scope. Missing price limits affected technical/
option calculations; missing cash or required price prevents rebalance simulation only.

The helper performs guarded cleanup before successful completion. Failure preserves remaining
state and reports its absolute retained path; never replace this with custom deletion. The portable
checks do not guarantee protection against same-UID malicious concurrent mutation. See
[owned-state implementation](implementation/owned-state.md), maintenance-only; routine analysis need
not read it.

## 4. Research and compose

Apply the already-loaded six question routes, selection/budgets, and quality requirements in
[research policy](research-policy.md), using the [research capability contract](tool-contract/core/research.md).
Reuse qualified evidence and loaded instructions; validate facts/calculations under
[evidence policy](evidence-policy.md). Keep public requests free of private portfolio context.
Use actual [technical coverage](tool-contract/core/technical.md); research selection cannot enlarge it.

Add conditional layers only through intent routing. Compose the retained [output contract](output-contract.md)
Markdown sections in chat, connecting supported and contrary findings to portfolio implications.
Cite account facts with the bound `snapshot_id`/`as_of`; external facts need sources, periods, units,
and direct links or actual result locators. Put local gaps beside affected conclusions and summarize
them in the final gaps section.
