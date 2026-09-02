# Evidence and traceability policy

## Account facts

All account facts in one report must cite the same `snapshot_id` and `as_of`. This includes NAV,
cash, position quantity, market value, cost, unrealized P&L, option inventory, weights, and holdings
used for research selection. Do not combine a later quote or saved snapshot into those account facts.
Summarize evidence; never dump raw snapshot JSON into the chat response.

## Changing external facts

For each changing fundamental, valuation, news, announcement, macro, or technical fact, record:

- source/publisher;
- publication, filing, report, event, or market-data date;
- query time, including timezone;
- a clickable direct link for news and announcements;
- the covered period and units where applicable.

Prefer issuer filings/investor relations, regulators, exchanges, index providers, and primary market
data. When using secondary news, distinguish the publisher's report from the underlying event.
Separate sourced facts from inference. Never cite a search-results page as the evidence link.

## Coverage and conflicts

Missing facts remain unavailable/unknown; never convert them to zero. Preserve `COMPLETE`, `PARTIAL`,
`UNAVAILABLE`, `STALE`, or equivalent source status. If sources conflict, state the disagreement and
avoid a precise conclusion until it can be reconciled. A nearby citation must support the exact
claim, security, period, and value.

Account evidence is point-in-time; external research may have its own as-of. State both so the reader
can see any timing mismatch. Research selection remains based on the bound snapshot weights, not a
later external price.
