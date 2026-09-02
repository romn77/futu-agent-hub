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

The current `options` tool inventories recognized option legs and may provide raw contract-unit
Greeks, expiry buckets, market value, and independently covered raw implied volatility. A separate
bounded static-information read may provide contract multiplier, exact underlying, option right,
expiry, strike, multiplier-adjusted Delta, same-snapshot notional Delta, moneyness, and conservative
covered-call/protective-put inventory relationships. These fields remain unknown when their evidence
contract is incomplete or conflicting; raw-Greek report status is not downgraded solely because
static metadata is unavailable.

Never parse a contract code or label to manufacture a field. Do not infer cash-secured puts, spreads,
collars, margin, payoff, probability, or advice. Missing evidence is **unavailable/degraded**.

Option failure is local: keep the complete stock/ETF holdings table and other supported portfolio
analysis, while placing the limitation beside the option conclusion and in data gaps.
