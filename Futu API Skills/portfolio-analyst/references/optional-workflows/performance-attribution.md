# Optional performance attribution

Load only when the user explicitly requests historical performance or attribution. Read
[the existing detailed contract](../performance-attribution.md) and the performance tool contract.

Attribution uses canonical immutable snapshot history over an explicit bounded date range. It cannot
be reconstructed from the one temporary current snapshot. Distinguish market P&L, external flows,
and unavailable transaction-period evidence; do not turn current unrealized gain/loss into historical
performance. No history is created merely to answer the request.

Do not mix legacy schema 1.0 reconstructed NAV with schema 1.1 OpenD total-assets NAV inside the
selected period, even when totals happen to coincide. `NAV_BASIS_MISMATCH` requires selecting a
same-basis period; never rewrite, relabel, or recompute old snapshots to bypass it. History outside
the selected interval does not affect this compatibility check.
