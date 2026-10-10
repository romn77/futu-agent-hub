# Recommendation and simulation policy

Load this policy only when the user explicitly requests actionable change or supplies a quantifiable
target weight, amount, or shares. A comprehensive review by itself is not actionable authorization.

## Advice boundary

- Explain evidence and trade-offs before any proposed change.
- Give supported qualitative advice without requiring numeric targets. Ask for weight, amount, or
  shares only when a requested simulation needs them; insufficient simulation fields do not block
  otherwise supported advice.
- Do not force every holding into `ADD/HOLD/TRIM/EXIT`.
- Gain/loss, one threshold, or technical evidence alone cannot determine a recommendation.
- Do not proactively recommend unheld new stocks. Research replacements/candidates only when the user
  names them or explicitly asks for candidates.
- Keep every proposal non-executable. Never unlock trading, request credentials, or place an order.

## Simulation boundary

Run a simulation only when the request contains a quantifiable target weight, amount, or shares (or
the user supplies it after a focused clarification). Normally produce one scenario. Produce multiple
scenarios only when explicitly requested. State assumptions, turnover, cash impact, resulting
weights/concentration, unavailable fees/taxes/slippage, and that output is hypothetical.

Threshold breaches prioritize analysis and possible scenario discussion; they do not automatically
authorize or size a trade.
