# Selective research and opportunity policy

## Selection and budgets

Every holding receives its baseline row; deep research is selective. Build the candidate union from
holdings >=5% of NAV, fill to the top five weights if fewer qualify, include names implicated in the
three principal risks and user-named held securities. Use bound snapshot identities/weights only.

Honor explicit named scope first; an explicit "only these names" request narrows research to them,
while the baseline holdings table remains complete. Otherwise prioritize portfolio impact, material risk/opportunity,
decision disagreement, catalysts and gaps likely to change the conclusion; break equivalent
priorities by weight descending, then symbol. Explain selection and every omitted eligible name.
Default: a maximum of eight held names. Do not silently drop baseline positions or expand the budget
because a skill is available. Handle simple, verified cash-management/short-Treasury funds through
liquidity, maturity, distributions and risk checks in Overview by default; use released research
capacity for the next material company question. Explicitly requested, uncertain, complex or materially risky funds require focused deep research
and count normally; disclose any budget omission rather than treating their risk as assessed.
Never guess fund structure.

Full valuations have a separate default maximum of two models across held and unheld names;
sensitivities within one model count once. Valuation context remains available without a full model.
An explicit wider request expands only the corresponding scope; comprehensive review alone does not.
Before calculating a target price, valuation range or sensitivity, read
[valuation policy](valuation-policy.md), including simple P/E models. Disclose actual models and
budget-skipped work separately from data gaps; never invent an unperformed model's target.

When new opportunities/candidates/strategies are explicitly requested or covered by an established
session preference, research at most two unheld candidates and one special offensive strategy by
default. These are additional to held-name slots but share the two-model valuation budget. Do not
use a strategy to introduce unlimited extra names. Keep candidates out of holdings and held-research
headings. Apply [recommendation policy](recommendation-policy.md) when that advisor route is active;
ordinary diagnosis without that intent does not add candidates. User-requested wider scope wins.

## Six question routes

Choose one suitable main method from the available host catalog under the
[research capability contract](tool-contract/core/research.md); reuse qualified evidence across methods.

| Question | Trigger | Required substance |
|---|---|---|
| Financials/operations | Selected company needs operating evidence | Growth, margins, cash conversion, balance-sheet changes and periods; check material facts against issuer evidence. |
| Estimate revisions | Requested, or changing expectations drive a principal thesis/risk | Forecast period, consensus, available coverage counts, revision windows and evidence; ratings are not earnings estimates. |
| Thesis test | Ordinary deep research or requested holding logic | Baseline, relevant pillars, supporting/contrary evidence, thesis invalidation and portfolio implications. |
| Earnings preview | Confirmed release in the next 14 calendar days, or requested | Date/stage certainty, expectations, disputed business metrics and verifiable scenarios. |
| Earnings recap | Release within the last 14 calendar days, latest earnings still a core catalyst, or requested | Results versus contemporaneous pre-release expectations, guidance, business drivers and thesis implications. |
| Valuation | Requested or price is a principal dispute | Suitable method, period-matched inputs, executed calculations, assumptions, range and sensitivities. |

Prefer specialist steps over duplicate generic collection. A method's provider example may accept
equivalent qualified data, but source-specific/historical steps need their actual inputs. Using a
skill does not authorize its unrelated workbook, full initiation report or saved-thesis workflow.
Missing optional methods do not stop the original analysis.

The 14-day windows route research, not trade signals. Confirm issuer dates versus third-party
estimates; uncertain stages stay uncertain. For one release use its applicable stage by default.
Before/after comparisons require requested scope and real historical expectations, never post-event
consensus substituted into the past. ETFs do not receive company DCFs; underlying-company research
does not complete an option-contract analysis.

## Evidence and judgment per selected name

- Latest company report and at least four recent quarters, verified as consecutive standalone
  quarters; relevant security-specific context for funds/options.
- Current valuation, historical and suitable peer context; recent 30 days of news, latest earnings
  and known material events. Disclose incomplete coverage rather than quietly reducing requirements.
- Portfolio uniqueness/redundancy, industry cycle and macro sensitivity, thesis invalidation and
  technical context where actually available.

Select 1–2 decisive questions, not an inventory of every obtainable metric. Connect relevant facts
to business drivers, earnings/cash flow, expectations/valuation and this portfolio. Mark inference
and missing links. Growth does not establish a beat, a good company is not automatically attractive
at its price, and a lower quote does not establish value. A principal portfolio risk or opportunity
needs sufficient company evidence; otherwise qualify it and disclose the research gap.

Without supplied or already-authorized historical rationale, label the baseline a **current research
hypothesis** (本次研究假设). Test two or three material pillars and seek substantive contrary evidence.
Respond to that evidence: revise the view/confidence or explain why it does not overturn it. Generic
competition/volatility warnings are not a thesis test. If no substantive contrary fact was obtained,
say so and identify an evidenced observable invalidation condition; a future condition is not a
completed counterevidence check. Never invent numerical thresholds or balanced-looking objections.

Synthesize shared drivers and differentiated exposures locally. Theme-associated weights are not
pure economic exposure or measured correlation; maintain consistent inclusion/exclusion and avoid
double counting. Strong company opinions alone do not establish portfolio diversification. Separate
company-research coverage from total coverage including reserve assets. Use portfolio policy for
materiality and recommendation policy for active views, allocation trade-offs and new strategies.

## Source choice and reuse

Reuse qualified evidence first, including non-Futu evidence. For gaps use issuer/SEC originals for
disclosures, qualified forecasts for expectations, and authorized Futu outputs for other covered facts.
When using relevant Futu steps, consult [Futu public capabilities](futu-public-research.md) for actual
limitations; do not rebuild a provider/parser or rely on `collect.py` zero-filled/truncated summaries.
Necessary issuer/full-text or independent checks are not redundant dataset requests.

Reuse by symbol, provider, dataset, reporting period, retrieval time and basis. Request companies
individually. Share public calendars, sector/index peer sets and macro series by public query keys,
then match locally; peers are comparisons, not candidates. Inspect existing valuation industry/history
data before fetching another peer set. Fetch each relevant earnings-calendar window once in chunks
of no more than seven days; split 14-day windows, deduplicate, and do not repeat per holding.

Keep reuse in this run or permitted temporary state, without a default database/cross-session cache.
Explain necessary refetches due to freshness, missing fields or verification. Follow evidence policy
for numerical/privacy rules and failure policy for ordinary corrections, bounded retries and denials.

## Actual technical coverage

Intended context is daily: six months and weekly: two years, including swings, volume congestion,
moving averages and support/resistance zones; technicals are never the sole basis for an action.
Consume the bound technical result's actual selected identities, dates and available fields. Its
hard cap does not expand with research; the helper does not pass user-named symbols. Distinguish a
name absent from technical, a selected name lacking indicators and whole-component failure. Explain
each affected holding and summarize gaps; do not reacquire the account, rerun technical or substitute
another price source to manufacture coverage. Technical-only names do not expand deep research.
