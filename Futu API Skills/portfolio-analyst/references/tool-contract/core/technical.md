# Technical-evidence capability contract

## Capability status

No dedicated technical CLI is currently exposed by `scripts/portfolio.py`. The package's risk path
can request adjusted price history for bounded correlation analysis, but that is not an exposed
support/resistance, moving-average, or chart-study command. Do not invent one.

When authorized read-only market-history capability is available, acquire daily six-month and weekly
two-year series only for deep-research names. Record market-data source, as-of, adjustment convention,
session coverage, and timeframe. Derive swing highs/lows, volume congestion, major moving averages,
and support/resistance zones without fake precision.

If either timeframe or volume/adjustment evidence is missing, mark the affected technical conclusion
unavailable/degraded. Never use technical evidence as the sole basis for an action.
