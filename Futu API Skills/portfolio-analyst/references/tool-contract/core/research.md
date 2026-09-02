# Research capability contract

## Capability status

No dedicated research CLI is currently exposed by `scripts/portfolio.py`. Do not invent a `research`
command or imply that company reports, valuations, news, earnings, or events came from Futu account
tools.

When the host provides the authorized read-only host tools `WebSearch` and `WebFetch`, use
`WebSearch` to locate current primary filings, issuer reports, announcements, recent news, macro
releases, and valuation sources. Use `WebFetch` to read the supporting page itself rather than relying
on a search snippet. Follow `research-policy.md` and `evidence-policy.md`: record publisher, source or
report date, query time, covered period, and direct clickable links for news/announcements. Keep
externally sourced facts separate from the bound account snapshot and label inference.

Without such access, mark each affected deep-research element unavailable/degraded and continue the
baseline holdings/portfolio analysis. Do not replace missing current facts with model memory.
