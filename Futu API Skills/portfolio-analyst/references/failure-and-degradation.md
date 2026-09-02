# Failure and degradation

## Whole-workflow stop conditions

**STOP** only when current holdings, NAV, or USD basis cannot be built reliably from one fresh bound
snapshot. Also stop on snapshot identity mismatch, inability to select the requested REAL account, or
evidence that the snapshot population is not a reliable view of current filled holdings. Explain the
specific missing authority and the smallest user/environment action needed.

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

Missing facts are unknown, not zero. Preserve source status such as `PARTIAL`, `UNAVAILABLE`, or
`STALE`; do not soften it in prose. A degraded deep-research name still appears in the complete
holdings table.

## Cleanup on failure

Remove the private temporary snapshot and intermediate files on success or failure. Cleanup does not
delete canonical user history. Never switch to `--save`, `--latest`, a stale artifact, SIMULATE, or a
different base currency merely to make the workflow continue.
