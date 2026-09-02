# Optional rebalance simulation

Load only when the user explicitly requests an actionable change and supplies a quantifiable target
weight, amount, or shares. Read [the existing detailed contract](../rebalance-simulation.md),
[recommendation policy](../recommendation-policy.md), and the simulate tool contract.

Use the same bound snapshot where the simulation belongs to the current report. Normally run one
scenario; run multiple only when requested. Present pre/post weights, concentration, turnover, cash,
assumptions, constraints, and data gaps. Output is hypothetical and non-executable. Fees, taxes,
slippage, liquidity, and fills remain unavailable unless explicitly evidenced.
