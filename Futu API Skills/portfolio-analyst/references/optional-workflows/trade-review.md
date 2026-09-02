# Optional trade review

Load only when the user asks for pending/open orders, historical trades, fills, or trading behavior.
Read [the existing detailed contract](../trade-review.md) and the trades tool contract.

The current command reviews bounded historical orders/fills; it is not part of the default current-
holdings report. Pending/open-order inclusion must be explicitly requested and limited to evidence
actually returned by the current history readers. Preserve `COMPLETE`/`PARTIAL`/`UNAVAILABLE`, masked
identities, period, market/timezone, and gross-pre-fee limitations. Do not infer motives.
