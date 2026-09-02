# Intent routing

Route by requested outcome. Explicit user intent overrides defaults but never the read-only boundary.

| Intent | Route | Default? |
|---|---|---|
| Analyze, review, or diagnose current holdings | Fresh REAL/USD comprehensive workflow | Yes |
| Explain option exposure already held | Comprehensive workflow plus options policy | Conditional on held options |
| Recommend a quantified change | Recommendation policy; simulate only when parameters are sufficient | Explicit only |
| Test a target weight/amount/share plan | One non-executable rebalance simulation | Explicit only |
| Review pending/open orders or historical activity | Trade-review workflow | Explicit only |
| Attribute historical performance | Performance-attribution workflow | Explicit only |
| Create/inspect/validate lifecycle facts | Position-lifecycle workflow | Explicit only |
| Save snapshot/report/history or journal | Relevant persistence command | Explicit only |
| Find or compare unheld candidates | Research only user-named names or requested candidates | Explicit only |
| Compare multiple scenarios | Run only the scenarios the user asks for | Explicit only |

“What should I do?” is actionable intent and loads recommendation policy, but do not invent a target
or simulate until the request supplies a quantifiable target weight, amount, or shares. If absent,
provide evidence and ask for the missing decision parameter.

Do not interpret a general portfolio review as permission to include open orders, history,
performance, lifecycle records, new-stock ideas, persistent artifacts, or multiple simulations.
