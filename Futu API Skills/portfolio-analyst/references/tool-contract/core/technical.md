# Technical-evidence capability contract

## Capability status

No dedicated technical CLI is currently exposed by `scripts/portfolio.py`. The package's risk path
can request adjusted price history for bounded correlation analysis, but that is not an exposed
support/resistance, moving-average, or chart-study command. Do not invent one.

When the host provides the authorized read-only host tools `WebSearch` and `WebFetch`, use
`WebSearch` to locate an authoritative current market-data source and `WebFetch` to retrieve its
documented daily six-month and weekly two-year series for deep-research names. Record source, direct
link, query time, market-data as-of, adjustment convention, session coverage, and timeframe. Derive
swing highs/lows, volume congestion, major moving averages, and support/resistance zones without fake
precision. Do not treat a search snippet as price-history evidence.

If either timeframe or volume/adjustment evidence is missing, mark the affected technical conclusion
unavailable/degraded. Never use technical evidence as the sole basis for an action.
