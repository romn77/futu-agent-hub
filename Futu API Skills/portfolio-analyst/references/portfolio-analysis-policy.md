# Portfolio analysis policy

## Scope and population

- Use one fresh REAL/USD snapshot of the mainly USD, US-equities account.
- List **every current filled holding** with nonzero quantity, even when the account has more than 30.
- Pending/open orders are excluded unless the user explicitly requests the trade-review workflow.
- Cash is portfolio-level and belongs in the overview, not as a holding row.
- An ETF is one position; do not perform constituent look-through.
- For a stock short, list evidenced facts. State that full short-risk analysis is unsupported when the
  current analyzer cannot produce it; never pretend that exposure is fully supported.
- Do not assess “US regional concentration.” A US-equities mandate makes that label uninformative.

## Required portfolio facts

Report NAV, cash and cash weight, filled holding count, Top 1/3/5 concentration, sector/theme
concentration where evidenced, current cost, market value, unrealized P&L, and P&L contribution.
Preserve the source's currency and availability semantics. Missing cost or P&L is unknown, not zero.

Use the snapshot's OpenD-sourced NAV (`nav_source: OPEND_TOTAL_ASSETS`) directly. Do not recompute NAV
from listed holdings and cash, or force their weights to total 100%. Account totals in `account_navs`
are the valuation authority; the positions/cash fields describe the separately returned inventory.

The rule is explicit: gain/loss alone cannot drive advice. Treat P&L as portfolio evidence alongside concentration,
fundamentals, valuation, thesis, redundancy, cycle, macro sensitivity, and technical context.

## Three principal risks

Select exactly three principal portfolio risks from supported evidence. Describe the measured value,
affected holdings, why it matters to this portfolio, and any local data limitation. Risk selection
must not cause any holding to disappear from the baseline table.

## Transparent fallback thresholds

Use the user's explicit policy when supplied. Only if none exists, apply these transparent review
priorities:

- single holding >10% of NAV: attention; >15%: high concentration;
- Top 3 >40%;
- Top 5 >60%;
- sector/theme >35%;
- absolute pair correlation 0.55: notable; 0.75: high.

These thresholds are **analysis priority only**. They never auto-trigger a trade, recommendation,
severity claim unsupported by the evidence, or `ADD/HOLD/TRIM/EXIT` label. Negative correlation is
inverse evidence and must not be described as the same risk as positive co-movement.

## Macro overlay

Keep macro concise and portfolio-specific: Fed/rates/liquidity, S&P 500/Nasdaq trend and risk
appetite, and relevant sector cycles. Discuss USD or oil only where a held position has a concrete
exposure. Explain transmission to the actual holdings; do not write a standalone macro essay or
offer broad market predictions.
