# Optional performance attribution

Load only when the user explicitly requests historical performance or attribution. Read
[the existing detailed contract](../performance-attribution.md) and the performance tool contract.

Attribution uses canonical immutable snapshot history over an explicit bounded date range. It cannot
be reconstructed from the one temporary current snapshot. Distinguish market P&L, external flows,
and unavailable transaction-period evidence; do not turn current unrealized gain/loss into historical
performance. No history is created merely to answer the request.
