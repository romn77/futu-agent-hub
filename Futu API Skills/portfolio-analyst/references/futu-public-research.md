# Futu public research: capability locations and actual limits

Load this reference when a public data gap needs capability selection. These names locate capabilities
in the reviewed baseline, not a runtime command whitelist, adapter specification or promise that the
host has connected them. Obtain actual paths, parameters and applicable instructions from the current
futuapi skill. API capability, script output and authorized host execution are separate questions;
account doctor success and a generic SDK minimum version do not prove every public interface works.
Use only returned, qualified evidence under [evidence-policy.md](evidence-policy.md).

| Need | Existing capability location | Use and boundary |
|---|---|---|
| Account, holdings, costs, NAV, cash, weights | Analyst helper and audited `get_accounts/get_portfolio` | Sole account source; company cash is not user cash. Public research never rebuilds the account. |
| Risk, correlation and daily/weekly technical context | Existing analyst risk/technical and `get_kline.py` | Reuse existing results/timeframes; no duplicate yfinance recalculation or replacement indicator engine. |
| Held option risk | Existing options, option static information and `get_option_quote.py` | Keep conditional path; no strategies, combination quotes, subscriptions or trading for fundamental research. |
| Profile, principal business, website | `get_company_profile.py` | Reuse labels/links; date-check changing management/business facts against disclosures. |
| Four quarters, income/balance sheet/cash flow/key metrics | `get_financials_statements.py` | Use needed statement types and returned field IDs, periods, currencies and missing values. Four returned records do not prove four consecutive standalone quarters. |
| Product/business/geographic revenue | `get_financials_revenue_breakdown.py` | Use returned historical-date list; align segment definitions and reporting periods. |
| Research quote, market cap, shares, basic multiples | API `get_market_snapshot`; `get_snapshot.py` | API fields exceed baseline script output; use only verified returned fields, not documentation as evidence of values. |
| PE/PB/PS, historical percentiles, valuation background | `get_valuation_detail.py` | Reuse trend/industry modules; state historical window; unknown forward period is not NTM/FY1. |
| Industry/concepts and peers | `get_owner_plate.py`, `get_valuation_plate_stock_list.py` | Inspect existing valuation-detail industry data first. Read public sector/index once, filter locally. Futu classification is not GICS; same sector is not automatically comparable. |
| Ratings, target prices and changes | `get_research_analyst_consensus.py`, `get_research_rating_summary.py` | Separate current aggregate from historical rating records. Rating count is not EPS forecast coverage. |
| Earnings dates, single-event EPS/revenue/EBIT forecast and actual | `get_earnings_calendar.py` | Public market/date requests, local matching, at most seven days per segment. Event dates need confirmation status; a later query does not establish pre-release consensus. |
| Earnings price reactions/event history | `get_financials_earnings_price_move.py`, `get_financials_earnings_price_history.py` | Choose by question and reuse adequate results. Distinguish announcement session, trading-day offsets and causal claims. |
| Recent 30-day news, announcements and rating leads | `get_search_news.py` | Titles, source/time/URLs are leads; read necessary original pages through authorized capabilities. Top N does not prove complete coverage. |
| Economic events and previous/expected/actual | `get_economic_calendar.py` | Useful macro evidence; baseline pagination limits below prevent unqualified completeness claims. |
| Growth/inflation and other macro series | `get_macro_indicator_list.py`, then `get_macro_indicator_history.py` | Resolve actual indicator IDs, never hardcode them. Separate observation period, release date, query time and revisions. Verify field-specific units. |
| FOMC market pricing and member projections | `get_fed_watch_target_rate.py`, `get_fed_watch_dot_plot.py` | Probability and dot plot differ; neither is a policy commitment. FedWatch is not a Treasury yield or DCF risk-free rate. |
| Dividends, ex-dates, splits/reverse splits | `get_corporate_actions_dividends.py`, `get_corporate_actions_stock_splits.py` | Only for relevant catalysts/comparability; declaration text is not automatically precise numeric evidence. Do not readjust already adjusted prices. |
| Moat, bull/bear views, fair-value reference | `get_research_morningstar_report.py` | Optional when relevant and entitled; identify Morningstar opinion/rating and field dates. Not issuer fact or a completed analyst DCF. |
| Institutional holders, insiders, short interest | `get_shareholders_institutional.py`, `get_insider_trade_list.py`, `get_short_interest.py` | Optional topics; distinguish report/disclosure lag, proposed sale versus execution, short stock versus daily short volume. Not mandatory for each holding. |
| US buyback amount/plans | `get_corporate_actions_buybacks.py` exists but official scope is Hong Kong/A shares | Not an established US buyback source. Use issuer disclosures/cash-flow fields; actual purchases differ from authorizations. Empty results do not prove no buybacks. |
| Multi-period earnings/revenue consensus distributions and counts | Reviewed Futu interfaces do not establish the full equivalent field combination | Fill concrete gaps with existing authorized yfinance `earnings_estimate/revenue_estimate` or professional sources. Single-event forecasts are not complete consensus. |
| EPS 7/30/60/90-day trends, upward/downward revisions | Reviewed Futu interfaces do not establish equivalent coverage | Fill gaps with authorized `eps_trend/eps_revisions` or suitable sources. Ratings or price changes are not earnings revisions. |
| Guidance, earnings explanation, primary text | Futu company/news links and financial numbers locate evidence | Read IR/regulatory/earnings material for needed verification. If text cannot be read, limit affected conclusions; retain primary-evidence requirements. |
| Full DCF/comps, portfolio implications, advice | Futu inputs plus applicable research methods and analyst synthesis | Actual checkable calculations and qualified WACC/FCF/diluted-share inputs remain necessary. Vendor targets/fair value do not complete a model. |

The table covers all research dependencies without requiring every interface. Morningstar and holder/
insider/short-interest topics are optional enhancements for an actual question. Interface existence
does not authorize ARK tracking, market-wide rankings, stock screening or broader research scope.

## Known script limits

| Reviewed output | Actual limitation | Response |
|---|---|---|
| `get_snapshot.py --json` | `_parse_snapshot_row` keeps symbol/name and few price/volume fields; omits API update_time, cap, shares, PE/PB; missing numerics may become zero. | Do not infer fields from API docs or trust unverified zeros. Choose another existing qualified output/source, or state the gap; no analyst parser patch/worker. |
| `collect.py` financial data | Requests only income statement; emits latest period's first 12 named fields; `main_index` initialized empty without retrieval. | Not complete three statements/four quarters. Use existing single-purpose scripts for required types/periods. |
| `collect.py` quality/scope | Defaults missing values to zero, removes quote time without verbose, always fetches fund flows; verbose does not remove 12-field truncation. | Prefer explicit single-purpose evidence. Do not describe script defects as absence of underlying Futu data. |
| `get_economic_calendar.py` | Default first page; JSON omits next_page/has_more; `--all-pages` has no explicit page cap. | Small windows and bounded counts; state unverified coverage. Continue only if existing steps bound work and verify coverage; otherwise use other authorized evidence. No unbounded paging or analyst paginator. |
| `get_search_news.py` | Limited titles/links, no guarantee of body retrieval. | Search leads only; read relevant original text when possible. No claim of full 30-day coverage. |
| futuapi analysis templates | Earnings/holdings templates contain judgment thresholds and actions. | Use applicable checks, no second account retrieval or automatic action thresholds. Different year-over-year directions alone do not prove erroneous earnings. |

A JSON stdout or exit code zero does not establish evidence quality. Truncated or zero-filled original
information cannot be recovered by the model; changing all zeros to null is not a repair. Exact
numbers require qualified evidence retaining missing semantics, units and timing. Maintain any future
upstream repair in its owning data skill as a separate task. Preserve the original account
`portfolio_source_worker.py` responsibilities without deletion or expansion.
