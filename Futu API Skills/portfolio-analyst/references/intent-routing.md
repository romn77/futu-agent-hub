# Intent routing

Route by requested outcome. Explicit user intent overrides defaults but never the read-only boundary.

| Intent | Route | Default? |
|---|---|---|
| Analyze, review, or diagnose current holdings | Fresh REAL/USD comprehensive workflow | Yes |
| Investigate financials, estimate revisions, thesis, earnings, or valuation | Relevant question route in [research policy](research-policy.md), within the requested scope | Conditional on the question |
| Explain option exposure already held | Comprehensive workflow plus options policy | Conditional on held options |
| Recommend an actionable change | Recommendation policy; simulate only when parameters are sufficient | Explicit only |
| Test a target weight/amount/share plan | One non-executable rebalance simulation | Explicit only |
| Review pending/open orders or historical activity | Trade-review workflow | Explicit only |
| Attribute historical performance | Performance-attribution workflow | Explicit only |
| Create/inspect/validate lifecycle facts | Position-lifecycle workflow | Explicit only |
| Save snapshot/report/history or journal | Relevant persistence command | Explicit only |
| Find or compare unheld candidates | Research only user-named names or requested candidates | Explicit only |
| Compare multiple scenarios | Run only the scenarios the user asks for | Explicit only |

"What should I do?" is actionable intent and loads recommendation policy. Give evidence-backed
advice when the available facts support it; a missing numerical target does not block qualitative
advice. Do not invent a target or simulate from a generic advice request. Ask for a missing target
weight, amount, or shares only when a requested simulation actually requires it.

The six research routes select methods, not new account workflows. Thesis research does not by
itself authorize saved lifecycle/history reads. A valuation request does not authorize a trade or
simulation. Broader research requires an explicit wider request; a general comprehensive review
keeps the default eight deep-research names and two automatic full valuations.

Do not interpret a general portfolio review as permission to include open orders, history,
performance, lifecycle records, new-stock ideas, persistent artifacts, or multiple simulations.
