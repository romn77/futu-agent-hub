# Conditional options policy

Load this policy only when the bound snapshot contains option positions or the user explicitly asks
about held options.

## Intended evidence contract

For each evidenced leg, anticipate long/short Calls/Puts, signed quantity, contract multiplier,
notional Delta, expiry, ITM/OTM state, strike/underlying where reliable, and the link to an underlying
holding. Identify covered/protected relationships only when the underlying, direction, quantity,
multiplier, and snapshot identity make the relationship reliable. Unknown relationships stay
explicitly unknown.

Summarize option market value, expiry concentration, directional/sensitivity evidence, and concrete
portfolio implications. Max profit/loss, margin, and probability are not required. Do not infer them.

## Current capability boundary

The current `options` tool does not currently expose all fields in the intended contract. It
inventories recognized option legs and may provide raw contract-unit Greeks, expiry buckets, market
value, and independently covered raw implied volatility. It does not infer multiplier, underlying,
option right, moneyness, covered/protected relationships, beta/share/notional Delta, probability,
margin, or payoff.

Until the tool contract exposes a required field with reliable provenance, label that field
**unavailable/degraded**. Never parse a contract code or label to manufacture it. Do not claim that a
documented future field is already callable.

Option failure is local: keep the complete stock/ETF holdings table and other supported portfolio
analysis, while placing the limitation beside the option conclusion and in data gaps.
