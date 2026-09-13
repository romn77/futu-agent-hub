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
| research source unavailable | baseline holding row and other sourced facts | current fundamental/news conclusion |
| technical history unavailable | fundamental/portfolio evidence | current support/resistance or trend |
| stock short unsupported by analyzer | signed snapshot facts and other positions | complete short-risk coverage |
| cash/cost/price/sellable quantity unknown | OpenD NAV, known holdings/market values, structural concentration | zero cash/cost, complete cash weight, or unsupported price/availability conclusions |

Missing facts are unknown, not zero. Preserve source status such as `PARTIAL`, `UNAVAILABLE`, or
`STALE`; do not soften it in prose. A degraded deep-research name still appears in the complete
holdings table.

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
