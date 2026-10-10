# Failure and degradation

## Whole-workflow stop conditions

**STOP** only when current holdings, NAV, or USD basis cannot be built reliably from one fresh bound
snapshot. Also stop on snapshot identity mismatch, inability to select the requested REAL account, or
evidence that the snapshot population is not a reliable view of current filled holdings. Explain the
specific missing authority and the smallest user/environment action needed.

Account ambiguity requires selection with `--acc-id`; an unreadable selected account is not a
local classification gap. Stop and retain the requested scope instead of excluding that account
or falling back to another. Failure of an unselected account must not be queried or affect the run.

## Local degradation

Failures in sector/theme classification, correlation, options, research, or technical evidence are
local. Continue all unaffected sections and place a limitation next to the affected conclusion:

| Failure | Continue with | Do not claim |
|---|---|---|
| sector/theme unavailable | complete holdings and known concentration | complete exposure classification |
| correlation partial/unavailable | structural concentration and available pairs | absence of co-movement risk |
| options fields/quotes unavailable | option inventory facts and stock analysis | complete option exposure/payoff |
| research source unavailable | baseline holding row and other sourced facts | unsupported current fundamental/news conclusion |
| additional skill absent or excluded | original analysis and existing authorized tools | that the absent skill ran, or that absence alone is an evidence gap |
| skill step dependency missing | independent applicable steps and existing qualified evidence | completion of the blocked step; do not install or recreate it |
| host/service permission denied | unaffected authorized steps | access via a workaround; stop the denied path |
| ordinary parameter/date/format error | correct from actual instructions; bounded retries within budget | that a correctable error is automatically unavailable |
| transient service failure/rate limit | bounded retry, then qualified evidence or a local gap | unlimited retries or bypassed limits |
| field missing, stale data, summary/Top N/page-only output | supported fields with period, coverage, and source limitations | zero-filled facts, freshness, full text, or complete coverage |
| valuation model inapplicable or key inputs invalid/missing | applicable methods and qualitative evidence | unsupported precise valuation or a successful model |
| research omitted for budget | full holding row and explicit omitted-name/reason disclosure | data unavailability or completed research |
| technical history unavailable | fundamental/portfolio evidence | current support/resistance or trend |
| stock short unsupported by analyzer | signed snapshot facts and other positions | complete short-risk coverage |
| cash/cost/price/sellable quantity unknown | OpenD NAV, known holdings/market values, structural concentration | zero cash/cost, complete cash weight, or unsupported price/availability conclusions |

Missing facts are unknown, not zero. Preserve source status such as `PARTIAL`, `UNAVAILABLE`, or
`STALE`; do not soften it in prose. A degraded deep-research name still appears in the complete
holdings table. Missing page-reading capability limits only source-text-dependent conclusions;
existing authorized public SDK/MCP/script data steps remain usable. User-specified unavailable
skills receive a brief disclosure, while ordinary reports need no missing-skill inventory.

Keep missing, true zero, not applicable, stale, and budget-skipped meanings distinct. Noncritical
metadata gaps do not invalidate otherwise adequate evidence. A failed research path neither erases
positions nor blocks supported advice; load recommendation policy for explicit advice and request
numeric targets only when needed for a requested simulation.

## Cleanup on failure

On success or component failure, the helper applies guarded identity and allowlist checks around
exact-name deletion, then removes the empty temporary directory. It never recursively deletes. If
identity or entry-set validation fails, cleanup must fail closed: retain the remaining state and
report its absolute path.

This protects against detected path replacement, symlink, or non-cooperative accidental change.
It is not an atomic guarantee against same-UID malicious concurrent mutation racing the portable
stat/unlink boundary. Cleanup does not delete canonical user history. Never switch to `--save`,
`--latest`, a stale artifact, SIMULATE, or a different base currency merely to make the workflow
continue.
