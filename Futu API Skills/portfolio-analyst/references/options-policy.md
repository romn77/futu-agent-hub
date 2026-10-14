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

## Report status and analysis gaps

In the option-risk section, use separate explicit labels: `期权工具状态` / `Option tool status`
and `分析能力缺口` / `Analysis capability gaps`. Under tool status, copy the authoritative
`options.status` exactly and state its raw-Greek scope, coverage, units, and freshness. Do not replace
that status with an inferred overall option-risk grade or a static-metadata status.

Under analysis capability gaps, list only fields actually missing, conflicting, or unavailable in
the bound result and explain the resulting interpretation limits. Name contract multiplier,
underlying mapping, and moneyness (ITM/ATM/OTM) when their evidence is missing; do not invent gaps
when these fields are supported. If no capability gaps remain, say so. Keep supported raw-Greek
completeness and unavailable economic exposure distinct.

## Current capability boundary

The current `options` tool inventories recognized option legs and may provide raw contract-unit
Greeks, expiry buckets, market value, and independently covered raw implied volatility. A separate
bounded static-information read may provide contract multiplier, exact underlying, option right,
expiry, strike, multiplier-adjusted Delta, same-snapshot notional Delta, moneyness, and conservative
covered-call/protective-put inventory relationships. These fields remain unknown when their evidence
contract is incomplete or conflicting; raw-Greek report status is not downgraded solely because
static metadata is unavailable.

Raw-Greek completeness is scoped to the selected account inventory. Verified full selected-account
coverage plus complete quote fields can support COMPLETE raw Greeks even when classification,
cash, cost, price, or sellable quantity makes the source snapshot PARTIAL. Preserve the source's
PARTIAL status in its own evidence. Unknown security types, missing account/position population,
source errors or stale authority still prevent complete aggregate claims. Missing underlying price
continues to degrade notional Delta/moneyness locally; raw Greeks do not imply those are available.

Never parse a contract code or label to manufacture a field. Do not infer cash-secured puts, spreads,
collars, margin, payoff, probability, or advice from this inventory tool. Missing evidence is **unavailable/degraded**.

Cross-check suspicious quantity/quote-unit/multiplier/value/Delta relationships using the
[evidence policy](evidence-policy.md). COMPLETE raw Greeks do not establish correct economic units.
Check whether a source field already includes the multiplier; never blindly multiply by 100 or
overwrite account market value/NAV. Unresolved ambiguity limits the affected claim; confirmed
unreliable account authority follows the fatal rule. Do not reacquire the account to test a guess.

A small short-dated position is not automatically a principal portfolio risk or a mistaken holding:
consider evidenced exposure, purpose and management needs. In requested advisor scope, apply the
recommendation policy separately; this held-options result does not validate any proposed new strategy.

Option failure is local: keep the complete stock/ETF holdings table and other supported portfolio
analysis, while placing the limitation beside the option conclusion and in data gaps.
