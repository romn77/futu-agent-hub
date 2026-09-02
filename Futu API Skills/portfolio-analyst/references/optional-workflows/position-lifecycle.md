# Optional position lifecycle

Load only when the user explicitly asks to create, inspect, list, or validate thesis/lifecycle facts.
Read [the existing detailed contract](../position-lifecycle.md) and the lifecycle tool contract.

Lifecycle records are user-authored local state, not inferred portfolio roles. Do not add an inferred
role to the default holding table. Read-only list/inspect/validate operations preserve user facts;
`init` is explicit local persistence and requires the user's requested values. It never mutates the
brokerage account or executes a trade.
