# Failure and degradation

## Whole-workflow stop conditions

**STOP** when current holdings, NAV, or USD basis cannot be built reliably from one fresh bound
snapshot. Also preserve every helper fatal result, including source-binding or snapshot identity
mismatch, guarded cleanup failure, inability to select the requested REAL account, or evidence that
the snapshot population is not a reliable view of current filled holdings. Explain the specific
missing authority or failed invariant and the smallest user/environment action needed.

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

## Research recovery

Missing optional skills are a normal branch, not a data failure. Continue the original research
method and any available authorized evidence. Mention an unavailable skill only when the user
explicitly requested it or when its missing capability causes a real evidentiary limitation.

| Situation | Response |
|---|---|
| Skill lacks a dependency or required tool | Skip only that step; apply suitable methods to existing evidence. Do not install or rebuild the missing implementation. |
| Ordinary parameter/date/format error | Correct it using actual instructions and existing tools, then continue without a new confirmation. |
| Actual permission denial or unavailable subscription | Stop the denied execution path; a capability-level entitlement denial applies across symbols for the rest of this run. Do not retry it by changing company/identity/transport. Other independently authorized sources remain usable. |
| Web reader absent but public SDK/MCP available | Use permitted numeric data; disclose missing full text where material, without claiming article verification. |
| Temporary timeout/rate limit | Use a few spaced retries within budget/service limits, then another authorized source or a specific gap. |
| Script truncates fields or replaces missing values with zero | Use another existing qualified output/source. Do not infer API fields from documentation or patch upstream inside this analysis. |
| News is snippets, or a calendar is only the first page/Top N | State actual coverage; do not claim full text or complete date coverage. |
| Interface supports another market | Use a market-appropriate source; no results do not prove no event. |
| Required valuation input missing, periods mismatch, or model unsuitable | Retain reliable facts; omit the affected precise value/model or clearly label an evidenced scenario. |
| Historical thesis or pre-earnings consensus unavailable | Use a current research hypothesis/current facts; do not invent purchase reasons or backfill with post-event consensus. |
| Research quote differs from snapshot | Keep account facts unchanged; label research timing separately. |
| Budget exhausted | Disclose unperformed names/steps and the budget reason; do not call it unavailable data. |
| Upstream instructions/requested artifacts exceed this task | Apply only suitable steps; do not upload private data, trade, install or persist artifacts by default. |

Keep missing, actual zero, not applicable and not performed distinct. Preserve authoritative
account/component statuses, but express public research limitations naturally without creating a
mandatory status object for every field. Nonessential metadata gaps do not invalidate reliable
evidence. A local research failure never erases a holding or changes an account/helper fatal error
into success; failed guarded cleanup also stops successful completion.

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
