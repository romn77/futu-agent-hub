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

By default research a maximum of eight names. When the union exceeds the budget, prioritize larger
weight and direct risk implication, retain user-named current holdings where possible, use symbol
as deterministic tie-breaker, and disclose every omitted name and reason. Selection weights come
only from the bound snapshot, never a later research quote. Never reduce baseline holding coverage.

Full valuation models have a separate default budget of at most two automatically selected names.
Valuation context (relevant multiples, history and peers) remains part of deep research without
requiring a full DCF for each name. An explicit wider request may expand the corresponding research
or valuation budget; "comprehensive review" alone does not. Disclose actual coverage and distinguish
budget-skipped work from unavailable data. A name without a completed model receives no invented
model target price.

## Six question routes

Select one suitable main method by the question, evidence needed and requested depth, not by skill
brand or installation order. Use relevant available skills under the
[research capability contract](tool-contract/core/research.md); missing optional skills leave the
original analysis available. The examples below are navigation hints, not required installations.

| Question | Trigger and main method | Required result |
|---|---|---|
| Financials and operations | A selected ordinary-share company needs financial evidence. Use suitable futuapi financial/earnings steps, `yfinance-data`, or another available financial method. | Growth, margins, cash flow, balance-sheet changes and reporting periods; verify material facts against issuer disclosures where possible. |
| Estimate revisions | Explicit request, or changing expectations are a principal thesis/risk. Use `estimate-analysis` or another estimate method; do not survey every field merely because a skill is installed. | Forecast periods, consensus levels, coverage counts, revision windows and supporting evidence. |
| Thesis test | Ordinary deep research needs to test a business assumption, or the user asks about holding logic. Use suitable `thesis-tracker` steps or another thesis method. | Thesis baseline and pillars, supporting and contrary evidence, changes, invalidation conditions and portfolio implications. |
| Earnings preview | Release confirmed for the next 14 calendar days, or explicit request. Choose one suitable `earnings-preview` method for the business scenarios and depth; supplement only missing inputs. | Earnings stage/date status, expectations, business metrics, disputed points and verifiable scenarios. |
| Earnings recap | Released in the last 14 calendar days, the latest earnings remain a core catalyst, or explicit request. Choose suitable futuapi earnings-commentary, `earnings-analysis` or `earnings-recap` steps. | Results versus verifiable expectations from before that release, guidance changes, business drivers and thesis implications. |
| Valuation | Explicit valuation request or valuation is a principal dispute. Check security, business model, evidence and deliverable constraints before selecting suitable DCF/comps or `company-valuation` steps. | Appropriate method, period-matched inputs, actual calculations, assumptions, range and material sensitivities under the evidence policy. |

Specialist steps take priority over duplicate generic collection: when estimate analysis already
has the needed consensus, do not re-fetch it to demonstrate use of `yfinance-data`. An upstream
formal-coverage, workbook or historical-thesis workflow is not automatically applicable because the
company is held. Use only suitable modules and describe the scope actually completed.

Without a supplied or already-authorized historical thesis, label the baseline **current research
hypothesis**, not the user's original purchase rationale. Thesis research does not trigger saved
history or lifecycle reads.

The 14-day windows are routing defaults, not backtested investment thresholds. Distinguish an
issuer-confirmed date from a third-party estimate. If the stage is uncertain, state it; do not
portray a preview/recap as confirmed. For a given earnings release, use only its currently applicable
stage by default. Comparing before and after requires an explicit request and contemporaneous
pre-release expectations; never backfill them with post-release consensus.

Do not apply a company DCF directly to an ETF. Keep options on the existing conditional options
route; underlying-company research is not a complete assessment of an option contract.

## Choose and reuse evidence

Choose the method separately from the source:

1. Reuse qualified evidence already obtained this run, including non-Futu evidence. A different
   method can use the same facts; Futu preference never requires reverse re-fetching.
2. For remaining fields, prefer suitable existing authorized Futu public reads guided by the
   current futuapi skill. The [Futu capability reference](futu-public-research.md) maps the full
   research needs and known output limitations; load it when those public steps are relevant.
3. When an actual coverage, missing-field, entitlement, timeliness or basis gap remains, check for
   another existing qualified Futu output, then another already-authorized source. Respect host
   denials. Do not fill gaps with `collect.py` zeros or rebuild an upstream parser.
4. Retain required issuer/full-text checks and necessary cross-checks. They answer distinct evidence
   questions, not duplicate dataset requests. Different providers may share an upstream source.
5. A method's Yahoo example may accept equivalent Futu inputs. Truly source-specific or historical
   consensus steps must still satisfy their actual input requirements.

Reuse company data by symbol, provider, dataset, reporting period, retrieval as-of and basis. Share
public earnings/economic calendars, sector/index peers and macro series by their public query keys,
then associate them with holdings locally. Inspect valuation detail's existing industry/history
data before fetching another peer dataset. Peer samples are comparisons, not unheld recommendations
or additions to the current holdings table.

Company requests are individual; public aggregate queries contain only public market, date, sector,
index or indicator identifiers, never account/watchlist identifiers or a portfolio list. Fetch each
market earnings-calendar window once in chunks of **no more than seven days**, split a 14-day window
accordingly, deduplicate events and match companies locally. Do not repeat the calendar per holding.
Follow the [evidence policy](evidence-policy.md) for privacy, source qualification and all numerical
rules; preserve missing values, units and dates rather than trusting a successful JSON envelope.

Reuse stays in this analysis context or compliant temporary state, with no default database or
cross-session cache. If freshness, a missing field or cross-verification requires a repeated fetch,
record the reason and timing difference. Reading a news article after receiving its headline/link
is not a duplicate dataset request.

Correct ordinary parameter/date/format errors using documented existing capabilities. Temporary
timeouts/rate limits allow a few spaced retries within the task budget and service requirements.
Do not blindly retry explicit denials, unsupported interfaces or unrecoverable gaps; persistent
failure restricts only affected conclusions. No fixed one-retry rule or new retry framework is needed.

## Evidence package per selected name

- latest company report and at least four recent quarters, with business/earnings direction;
  verify four consecutive standalone quarters rather than treating `num=4` as proof;
- current valuation plus relevant historical and peer comparison;
- news from the recent 30 days, latest earnings, and known material upcoming or completed events;
- current thesis, thesis invalidation conditions, and evidence that would disconfirm it;
- portfolio uniqueness or redundancy relative to other current holdings;
- industry cycle position and macro sensitivity;
- technical context under the rule below.

Do not fill unavailable data with estimates unless clearly labeled as an estimate with methodology.
Research an unheld replacement/candidate only when the user names it or explicitly asks for ideas.
Do not silently drop four-quarter, historical/peer or event coverage to fit a budget; disclose any
specific gap. Apply company-report requirements to companies, with suitable security-specific
context for ETF/option holdings. Use the evidence policy for source and valuation quality.

## Technical evidence

For deep-research names only, use daily: six months and weekly: two years. Identify swing highs/lows,
volume congestion, and major moving averages. Express support/resistance zones rather than fake point
precision. State market-data as-of and timeframe. Technical analysis is context, never the sole basis
for an action. Reuse the bound risk/technical results and existing periods; do not change the indicator
engine or fetch equivalent prices from another source merely to run a different skill.

## Macro integration

Use a short overlay tied to specific positions: Fed/rates/liquidity, S&P 500/Nasdaq trend and risk
appetite, and the relevant industry cycle. Include USD or oil only if material to a selected holding.
