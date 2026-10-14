# Futu public-research capability reference

Load this reference only for relevant Futu research steps. It records checked capabilities and
limitations, not a runtime command whitelist, invocation configuration or promise that every
interface works locally. Resolve current paths, arguments, versions and entitlement from the actual
futuapi skill and its relevant references (`fundamentals.md`, `rankings-calendar.md`,
`shareholders-institutions.md`, `quote-commands.md` or `analysis-frameworks.md`). The public script
names below were located under its `scripts/quote/` directory.

Follow [research policy](research-policy.md) for reuse before Futu preference, budgets and routing,
[research capability contract](tool-contract/core/research.md) for authorization, and
[evidence policy](evidence-policy.md) for qualification and calculations. Use existing qualified
outputs; do not create an analyst provider adapter, copy SDK requests, patch an upstream script,
add workers, or expand the audited account path. New relevant authorized public capabilities need
not appear in this table to be usable. Doctor success and futuapi's general minimum version do not
prove compatibility with each public interface.

## Capability map

| Need | Existing capability | Use and boundary |
|---|---|---|
| Account, holdings, cost, NAV, cash and weights | Existing analyst helper and audited `get_accounts` / `get_portfolio` | Sole account source. Company cash is not user cash; public data cannot reconstruct or refresh the bound account. |
| Risk, correlations and daily/weekly technicals | Existing analyst risk/technical and `get_kline.py` | Reuse results and periods; do not duplicate equivalent prices through yfinance or switch indicator engines. |
| Held-option risk | Existing options route, static metadata and `get_option_quote.py` | Keep the conditional existing route; no strategy, combo quote, subscription or trading steps for fundamental research. |
| Company profile, business and official links | `get_company_profile.py` | Use supported labels/links; verify dates and disclosures for changing management/business claims. |
| Four quarters, income statement, balance sheet, cash flow and key metrics | `get_financials_statements.py` | Request needed types/periods; preserve field IDs, periods, currencies and missing values. Verify four consecutive standalone quarters; `num=4` alone is insufficient. |
| Product, business and geographic revenue | `get_financials_revenue_breakdown.py` | Resolve historical dates from returned choices and check segment definitions/periods. |
| Research price, market cap, shares and basic multiples | API `get_market_snapshot`; script `get_snapshot.py` | The script does not expose every API field and may fill missing values with zero. Use only verified outputs; see limitations below. |
| PE/PB/PS, history and valuation context | `get_valuation_detail.py` | Reuse raw trends/industry information; disclose historical window. Unknown forward periods cannot be relabeled NTM/FY1. |
| Industry/concept membership and peers | `get_owner_plate.py`, `get_valuation_plate_stock_list.py` | First reuse valuation detail's industry module. Fetch public sector/index groups once and select peers locally. Futu sectors are not automatically GICS or economically comparable peers. |
| Ratings, target prices and changes | `get_research_analyst_consensus.py`, `get_research_rating_summary.py` | Separate current aggregates from rating history; rating counts are not EPS-estimate coverage counts. |
| Earnings dates and single-event EPS/revenue/EBIT forecast/actual | `get_earnings_calendar.py` | Query public market/date windows in chunks of at most seven days and match locally. Event dates need confirmation status; post-event retrieval does not prove pre-event consensus. |
| Earnings price reaction and event history | `get_financials_earnings_price_move.py`, `get_financials_earnings_price_history.py` | Choose the relevant dataset, not both by default. Distinguish announcement session, trading-day offsets, price reaction and fundamental causality. |
| Recent 30-day news, announcements and rating leads | `get_search_news.py` | Headlines, source, time and URL locate evidence; fetch necessary original text separately. A finite search is not complete 30-day coverage. |
| Economic events, previous/expected/actual values | `get_economic_calendar.py` | Useful macro context with bounded query scope; default-page and pagination limits below prevent assuming completeness. |
| Growth, inflation and other macro series | `get_macro_indicator_list.py` then `get_macro_indicator_history.py` | Resolve actual indicator IDs before relevant history; separate observation period, publication date, retrieval time and revisions. |
| FOMC market pricing and member projections | `get_fed_watch_target_rate.py`, `get_fed_watch_dot_plot.py` | Keep market-implied probabilities separate from the dot plot; neither promises future policy. |
| Dividends and splits | `get_corporate_actions_dividends.py`, `get_corporate_actions_stock_splits.py` | Only when relevant to catalysts/comparability. Do not treat parsed declaration text as assured numeric data or adjust an already-adjusted price twice. |
| Moat, bull/bear views and fair-value reference | `get_research_morningstar_report.py` | Optional when relevant and entitled. Attribute Morningstar opinion, rating type and field dates; fair value is not an analyst-built DCF. |
| Institutional ownership, insiders and short interest | `get_shareholders_institutional.py`, `get_insider_trade_list.py`, `get_short_interest.py` | Optional questions only. Check reporting lag, proposed versus executed insider sales, and short-interest stock versus daily short-sale volume. |
| US buybacks and authorizations | Issuer disclosures and qualified cash-flow fields | `get_corporate_actions_buybacks.py` covers HK/A shares in the checked version; do not claim US coverage. Separate actual repurchases from authorization amounts. |
| Multi-period EPS/revenue consensus distribution and coverage | No confirmed complete equivalent Futu field set in the checked interfaces | Fill the actual gap with authorized `earnings_estimate` / `revenue_estimate` or a professional source. Single-event forecasts are not complete consensus. |
| EPS 7/30/60/90-day trends and up/down revision counts | No confirmed equivalent Futu capability in the checked interfaces | Authorized `eps_trend` / `eps_revisions` may fill the gap. Rating changes or stock-price changes cannot substitute for earnings revisions. |
| Management guidance, earnings explanation and primary text | Futu news/profile links and financial facts help locate sources | Still verify IR, regulatory filings or earnings materials. If full text is missing, limit affected claims; numerical feeds do not replace issuer evidence. |
| Complete DCF/comps and portfolio implications | Qualified data inputs plus a suitable research method and analyst synthesis | Require actual checkable calculations and appropriate FCF/WACC/share inputs. Targets or Morningstar fair value are attributed opinions, not completed models. |

The table covers dependencies, not a checklist to call every interface. Optional thematic sources
do not expand the selected holdings or research budget. ARK tracking, marketwide rankings and
screening do not become requirements merely because an endpoint exists.

## Known output limitations

| Checked output | Actual limitation | Required handling |
|---|---|---|
| `get_snapshot.py --json` | `_parse_snapshot_row` keeps only code/name and a few price/volume fields; it drops API update time, market cap, shares and PE/PB fields, and can replace missing numbers with zero. | Do not claim API-documented fields were returned. Obtain another existing qualified output/source or mark the gap; unverified zeros are not evidence. |
| `collect.py` financial output | It requests only the income statement, keeps the latest period's first 12 named fields, and initializes `main_index` without fetching it. | This is not three statements or four quarters. Use suitable existing individual statement scripts; do not make `collect.py` the default research evidence base. |
| `collect.py` quality and scope | Multiple zero defaults, quote-time removal outside verbose mode, unconditional capital-flow reads; verbose does not remove the financial truncation. | Prefer specific qualified reads even when an upstream template suggests collect. These are script limitations, not proof that Futu has no underlying data. |
| `get_economic_calendar.py` | Default first page; JSON lacks `next_page` / `has_more`. `--all-pages` can paginate without an explicit page-count bound. | Use small windows and finite counts; disclose unverified coverage. Continue only when existing steps can bound work and verify coverage. Otherwise use permitted evidence without a new paginator or unbounded paging. |
| `get_search_news.py` | Finite headline/link results do not guarantee original text or a complete period. | Read material originals using authorized page access and qualify coverage; metadata alone cannot support an unread article's claims. |
| futuapi analysis templates | Include earnings/holding diagnosis and template-specific thresholds/actions. | Reuse suitable checks only; do not reread accounts, call differing year-on-year directions an automatic error, or automatically prescribe reductions. |

Lost/truncated information cannot be reconstructed by a model, and converting every zero to null
does not repair an output. Precise claims require evidence that preserves actual missingness, units
and time. A zero exit status or JSON wrapper alone proves neither availability nor completeness.
Upstream repairs belong to separate maintenance of the data skill; do not describe an unimplemented
fix as an available capability.
