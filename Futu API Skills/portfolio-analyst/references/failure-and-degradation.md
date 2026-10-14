# Failure and degradation

## Fatal authority failures

**STOP** if one fresh bound snapshot cannot establish reliable current filled holdings, NAV, or USD
basis. Preserve every helper fatal result, including snapshot/source-binding mismatch, requested
REAL account selection failure, and guarded cleanup failure. Explain the failure and
smallest corrective action. Account ambiguity needs `--acc-id`; never drop unreadable selected
accounts, substitute another, or query unselected portfolios. Retain requested scope.

Never switch to `--save`, `--latest`, stale artifacts, SIMULATE, or another base currency to continue.

## Local gaps

Sector/theme classification, correlation, options, research, technical, and unsupported stock-short analysis
failures are local. Continue reliable snapshot facts, complete holdings, available pairs and other
evidence; label affected conclusions. Do not claim complete exposure/option/short coverage, current
fundamental/news/technical conclusions, or absence of co-movement risk without evidence.

Unknown cash/cost/price/sellable quantity remains unknown, not zero; retain evidenced NAV, holdings,
market values, and concentration under [snapshot semantics](tool-contract/core/snapshot.md).
Preserve `PARTIAL`, `UNAVAILABLE`, `STALE`, and other authoritative statuses. Keep missing, actual
zero, not applicable, and budget-skipped work distinct. Noncritical metadata gaps do not erase
reliable evidence. Public gaps need no mandatory per-field status object. Local failure never erases
a holding or converts fatal failure to success; see [workflow](workflow.md) for component handling.

## Research recovery

- Missing optional skills: continue the original method and authorized evidence. Mention only if
  requested or materially limiting. Missing dependency/tool: skip that step, use existing evidence;
  do not install or rebuild it.
- Ordinary parameter/date/format errors: correct from actual instructions without new confirmation.
- Permission/subscription denial: stop the denied path. Capability-level entitlement denial applies
  across symbols for this run, unlike object-specific no-data. Never retry via changed company,
  identity, credentials, endpoint, or transport. Independently authorized alternatives remain usable.
- Temporary timeout/rate limit: a few spaced retries within budget/service limits, then an authorized
  alternative or specific gap. Unsupported interfaces/unrecoverable gaps are not retry targets.
- Truncated fields/lossy zeros: use another qualified existing output; never infer returned fields
  from documentation or patch upstream.
- Missing web reader: permitted SDK/MCP numbers remain usable; disclose material missing full text.
  Snippets/first-page/Top-N calendars support only actual coverage, not article/full-calendar verification.
- Wrong-market interface: use an appropriate source; no results do not prove no event.
- Exhausted budget: name unperformed work and budget reason, not unavailable data.

Follow [evidence](evidence-policy.md) and [research](research-policy.md) for quote timing, valuation,
and thesis/consensus gaps: preserve account facts, omit unsupported precise values, and never invent
purchase reasons or pre-release expectations. Upstream instructions cannot expand authorized scope.

## Cleanup

On success or component failure, guarded exact-name/identity/allowlist cleanup removes owned state,
never deleting recursively or touching canonical history. Failure retains remaining state and reports its absolute
path. Same-UID malicious concurrent mutation is outside the portable guarantee. Detailed checks and
threat model are [maintenance-only](implementation/owned-state.md), not required for routine analysis.
