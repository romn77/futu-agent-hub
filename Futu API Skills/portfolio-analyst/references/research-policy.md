# Selective deep-research policy

Baseline coverage and deep research are different: every current holding receives a table row, while
only a bounded subset receives extended evidence.

## Deterministic selection

Build the candidate union from:

1. every holding **>=5% of NAV**;
2. if that yields fewer than five holdings, fill in descending weight order to the **top five weights**
   (or all holdings if fewer than five exist);
3. every holding implicated in any of the **three principal risks**;
4. every **user-named** current holding.

Research a default maximum of eight names. An explicit request for broader research expands the
corresponding budget; disclose actual completed coverage. A general complete portfolio review does
not itself expand it. When the union exceeds eight, prioritize larger weight and direct
risk implication, retain user-named current holdings where possible, use symbol as deterministic
tie-breaker, and disclose every omitted name and reason. Never reduce baseline holding coverage. Selection weights stay bound to the account snapshot;
external quotes do not rerank holdings. Distinguish a name skipped for budget from unavailable data.
Peer valuation samples are not candidate recommendations and do not enter the holdings table.

## Evidence package per selected name

- latest company report and at least four recent quarters, with business/earnings direction;
- current valuation plus relevant historical and peer comparison;
- news from the recent 30 days, latest earnings, and known material upcoming or completed events;
- current thesis, thesis invalidation conditions, and evidence that would disconfirm it;
- portfolio uniqueness or redundancy relative to other current holdings;
- industry cycle position and macro sensitivity;
- technical context under the rule below.

Apply [evidence-policy.md](evidence-policy.md) to sources, missing values, reporting periods,
assumptions and numeric calculations. Do not fill unavailable data with estimates unless clearly
labeled as an estimate with methodology.
Research an unheld replacement/candidate only when the user names it or explicitly asks for ideas.

## Technical evidence

For deep-research names only, use daily: six months and weekly: two years. Identify swing highs/lows,
volume congestion, and major moving averages. Express support/resistance zones rather than fake point
precision. State market-data as-of and timeframe. Technical analysis is context, never the sole basis
for an action.

## Macro integration

Use a short overlay tied to specific positions: Fed/rates/liquidity, S&P 500/Nasdaq trend and risk
appetite, and the relevant industry cycle. Include USD or oil only if material to a selected holding.

## Method and data selection

Use the six routes in [intent-routing.md](intent-routing.md). Choose an applicable method for the
question, not a brand ranking. Relevant available skills must actually contribute applicable steps;
already loaded instructions are reused. The exposed catalog is open: unlisted relevant skills use
the same rules. Missing optional skills do not imply missing data or stop the original analysis.

Separate valuation background from a full model. Every selected name retains valuation context,
historical/peer comparison and operating expectations where available. Automatically selected full
valuations default to at most two names; explicit broader valuation requests expand that budget.
Disclose actual scope and budget omissions. Without a performed model, do not invent its target price.
ETF and sector-specific model applicability and numeric validation follow evidence policy; options
keep their existing conditional path.

For data, first reuse qualified evidence acquired this round, including external evidence; do not
refetch it merely to prefer Futu. For remaining fields, use applicable futuapi instructions and
existing authorized public capabilities when same-basis data is available. Check another existing
qualified Futu output for a concrete gap before using another already authorized source. Gaps can
be missing fields, periods, coverage, timeliness, permissions or incompatible basis. A method's Yahoo
example can use equivalent Futu inputs; a genuinely necessary specific source/history condition must
still be met. Primary disclosure checks and necessary conflict checks are purposeful verification.
See [futu-public-research.md](futu-public-research.md) for capability locations and actual output limits.

## Reuse, public aggregation and bounded recovery

Reuse company evidence by symbol, provider, dataset, reporting period, retrieval time and basis in
this analysis context. One earnings release can support recap, thesis and valuation without being
retrieved again when methods change. Before each query, identify the concrete unanswered metric
and period; skip it when an existing qualified same-basis result already answers that need. Do not
loop through every exposed dataset kind: estimates, financial detail, fundamentals, and news are
not an automatic fetch sequence, and overlapping fundamentals already satisfied need no repeat
request. Shared upstream providers are not independent corroboration.
Explain a material refresh, missing-field retrieval or cross-check and its time difference.

Share calendars, peer-sector data and macro evidence by public query keys: market/date window,
public sector/index, or indicator ID. First inspect valuation-detail industry/history modules before
requesting additional peer data. Request earnings calendars in segments no longer than seven days;
The future 14-day preview window and previous 14-day recap window are separate phase windows,
not one 14-day span centered on today. Split each needed window into inclusive segments of at most
seven days, reuse matching market/date responses, and deduplicate locally. Query only windows
relevant to the request; expand dates only when that request requires it. Public sector/index peer lists can be read once and
filtered locally. Aggregates must never contain a private watchlist group or portfolio list.

External requests carry only necessary public identifiers and periods. Query companies separately;
keep account IDs, snapshot IDs, positions, weights, costs, P&L, NAV, cash, identity and private paths
out of public requests, bodies and attachments. Associate evidence with the portfolio locally. Keep
account reads, risk, technical and options calculations on the original audited analyst paths; public
research does not reconstruct account facts or take a second account snapshot.

Reuse is within the current context or compliant temporary state, without default disk databases or
cross-session caches. Correct ordinary parameter/date/format errors using existing instructions.
Temporary timeout/rate-limit failures allow a few spaced retries within task budgets and service
requirements. Do not blindly retry explicit permission denials, unsupported interfaces or irrecoverable
missing data, or switch paths to evade a denial. Continue unaffected research and explain real gaps.
Do not install, upgrade, purchase, subscribe, rebuild missing skills or add analyst adapters, workers,
CLI branches or pagination frameworks to compensate for public-data limitations.
