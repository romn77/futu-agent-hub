# Portfolio analysis policy

## Population and account facts

- Use one fresh REAL/USD snapshot and list every current filled holding with nonzero quantity,
  including portfolios over 30 positions. Pending/open orders are excluded unless requested.
- Cash is portfolio-level, outside the holdings table. An ETF is one position, without look-through.
- For stock shorts show evidenced signed facts and disclose unsupported full short-risk analysis.
- Do not assess “US regional concentration” for a US-equities mandate. Listing currency/market
  does not establish the business's economic currency or geographic exposure.

Report NAV, cash/weight, count, Top 1/3/5 concentration, evidenced sector/theme exposure, cost,
market value, unrealized P&L and contribution. Use OpenD `funds.total_assets`, preserved as
`OPEND_TOTAL_ASSETS` with `account_navs`, as NAV authority. Never reconstruct NAV, force listed
assets plus cash to equal it, or normalize weights to 100%. Missing cost/P&L/cash/price/sellable
quantity stays unknown, not zero; known per-account cash does not prove aggregate cash coverage.
Current unrealized gain/loss is not a period return, and gain/loss alone cannot drive advice.

## Fit and liquidity

Distinguish spending/withdrawal deadline, strategy holding period and review horizon. Reuse known
objectives, liquidity and drawdown constraints. If unknown, give a brief conditional assessment and
continue useful analysis; missing a full profile need not block a direction supported by evidence.
Ask only when the missing fact materially changes sizing or a requested calculation. One account
does not establish household wealth or risk capacity.

Cash plus short-duration assets provides a buffer, not proof of low portfolio risk or personal fit.
Separate available cash, reserve assets and remaining risk assets. Verify fund identity/structure,
maturity, distributions and liquidity before treating it as a reserve. A price loss excludes
distributions and is not necessarily total-return loss. A simple executed stress illustration may
clarify magnitude with explicit assumptions; it is not a forecast, maximum loss or user risk budget.

## Three principal risks and shared drivers

Use three evidence slots, with a genuine data gap/lack of confirmable risk where necessary. Do not
invent a third risk. Prioritize portfolio materiality using exposure, mechanism, horizon/catalyst and
evidence strength; explain why the first deserves priority. Do not rank by headline salience, past
loss or threshold alone. Keep every holding in the baseline table regardless of research selection.

A release date is usually the timing of a structural risk, not an independent duplicate risk. Merge
overlapping drivers without double-counting holdings. A small complex instrument needs operational
attention but not automatically a top-three capital-risk label. Its purpose, exposure and management
matter; a short expiry does not by itself contradict a longer review cycle.

Research synthesis may reorder the report's discussion, but never overwrites tool `top_risks`,
statuses, figures or technical selection, nor recursively expands research. Unsupported probabilities
and loss estimates remain unavailable. State limitations beside affected conclusions.

Connect company evidence to common demand, industry cycle and macro sensitivity. Several strong
businesses can share one driver. A theme group is associated holdings weight, not pure revenue
exposure or measured correlation; apply consistent membership, explain exclusions and avoid overlap
double counts. Preserve snapshot weights; later public quotes belong to research only.

## Transparent fallback thresholds

Use the user's explicit policy when supplied. Otherwise disclose these as analysis priority only:
single holding >10% NAV merits attention, >15% high concentration; Top 3 >40%; Top 5 >60%;
sector/theme >35%; absolute pair correlation 0.55 notable, 0.75 high. They do not automatically
justify severity, trading or `ADD/HOLD/TRIM/EXIT`. Negative correlation is inverse evidence, not the
same risk as positive co-movement. No configured policy stays UNRATED; a reference threshold is not
proof of individualized suitability.

## Macro overlay

Keep Fed/rates/liquidity, S&P 500/Nasdaq risk appetite and relevant sector cycles concise and linked
to actual holdings. Add USD/oil only for concrete exposure. Do not replace portfolio analysis with
standalone forecasts. In advisor scope, use recommendation policy to connect opportunities and
trade-offs to these risks rather than concluding with generic caution.
