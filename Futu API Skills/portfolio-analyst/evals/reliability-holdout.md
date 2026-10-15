# Portfolio report reliability: independent forward holdout

Prepared 2026-10-15; input version `RHT-1`. Operator-only document. Do not copy this document, its evidence anchors, preparation receipts, or the full manifest into a model workspace.

This set was prepared independently from the existing spec and fixture interfaces. Its preparer did not read old quality-case answers or generated model reports, did not run a remote model, and did not edit production instructions or existing fixtures. All accounts, positions, prices, issuers, filings, analyst samples, events, and macro figures are synthetic. URLs use the reserved `.invalid` domain. Ticker strings are identifiers inside this fixture and are not assertions about real securities.

The three cases address report reliability and allocation of research effort. They are a new prospective gate, not a replacement for the historical AC-17 comparison or a claim that historical thresholds were met. Do not treat good writing, more text, more citations, or a particular investment preference as evidence of higher quality.

## Frozen inputs and exposure boundary

`reliability-fixtures/holdout.json` has the following operator manifest shape:

```text
schema_version: "1.0"
synthetic: true
evaluation_as_of: ISO-8601 UTC string
model_context: short neutral environment/tool instructions
cases[]:
  id
  prompt
  portfolio_input:
    base_currency, as_of, trd_env, source_version
    accounts[], account_navs[], cash_balances[], positions[]
    classifications: canonical US.symbol -> Classification fields
    data_quality
  public_data: source/resource -> canonical US.symbol or market -> public response
               (futu/calendar is a list, as required by the existing reader)
```

Only the selected case's `prompt`, the top-level `model_context`, the generated account envelope, and the selected case's public evidence may reach the tested model. The case title, other cases, this rubric, and operator arithmetic do not belong in its prompt. The data has ordinary financial fields and source metadata; it does not contain expected answers, preferred research lists, or scoring instructions.

Do not allow the model to open `research/data.json` directly as a substitute for public requests. Serve it through the unchanged `research-fixtures/public_reader.py`, copied to `research/public_reader.py`. The host observes calls. Symbol-scoped requests carry one public symbol and no account facts, weights, quantities, or lists of private holdings. Existing market-calendar endpoints remain market-wide. This is an offline harness rule, not a proposed production service.

## Preparation using unchanged APIs

The existing `run_codex_evals.py` does not expose these three case IDs. Do not claim they are supported by its `--case` argument. Reuse its workspace-copy, receipt capture, and fixture CLI approach; prepare the selected case's envelope from raw inputs below. No hand edits to snapshot IDs or derived risk/technical fields are permitted.

Run the following logic under the component's existing Python environment, with the component root on `sys.path`. `case` is the selected manifest case. All numeric strings remain exact decimals.

```python
from datetime import datetime
from decimal import Decimal

from portfolio_analyst.classification import Classification, ClassificationMapper
from portfolio_analyst.fx import IdentityOnlyCurrencyConverter
from portfolio_analyst.models import Account, AccountNAV, CashBalance, DataQuality, Position
from portfolio_analyst.risk import analyze_risk
from portfolio_analyst.snapshot import PortfolioBuilder
from portfolio_analyst.snapshot_io import snapshot_from_document
from portfolio_analyst.technical import analyze_technical

p = case["portfolio_input"]
position_decimals = {
    "quantity", "can_sell_quantity", "average_cost_local", "last_price_local",
    "market_value_local", "unrealized_pl_local", "realized_pl_local",
}

def decimals(row, fields):
    return {
        key: Decimal(value) if key in fields and value is not None else value
        for key, value in row.items()
    }

snapshot = PortfolioBuilder(
    base_currency=p["base_currency"],
    converter=IdentityOnlyCurrencyConverter(),
    classifications=ClassificationMapper({
        key: Classification(**value) for key, value in p["classifications"].items()
    }),
).build(
    accounts=tuple(Account(**row) for row in p["accounts"]),
    account_navs=tuple(
        AccountNAV(**decimals(row, {"total_assets_local"})) for row in p["account_navs"]
    ),
    cash_balances=tuple(
        CashBalance(**decimals(row, {"amount_local"})) for row in p["cash_balances"]
    ),
    positions=tuple(Position(**decimals(row, position_decimals)) for row in p["positions"]),
    as_of=datetime.fromisoformat(p["as_of"].replace("Z", "+00:00")),
    trd_env=p["trd_env"],
    source_version=p["source_version"],
    data_quality=DataQuality(**p["data_quality"]),
)
snapshot_document = snapshot.to_dict()
assert snapshot_from_document(snapshot_document).to_dict() == snapshot_document
risk = analyze_risk(snapshot).to_dict()
technical = analyze_technical(snapshot, provider=None, risk_document=risk).to_dict()
assert risk["source_snapshot_id"] == technical["source_snapshot_id"] == snapshot.snapshot_id
envelope = {
    "snapshot": snapshot_document, "risk": risk, "technical": technical,
    "options": None, "degradations": [],
}
```

Write generated `envelope` to `.fixture/envelope.json`, generated `snapshot_document` to `.fixture/snapshot.json`, and selected `public_data` to `research/data.json`. Copy `evals/fixture_cli.py` unchanged into `scripts/current_analysis.py` and `scripts/portfolio.py`, as the existing preparer does. Copy the required instruction and package files from the tested version. No case requests simulation, so do not prepare `.fixture/request.json`.

Before execution, hash every workspace input and verify both compared versions receive byte-identical account/public data and user prompts. Freeze complete instruction versions, engine code, host, explicit model identifier, reasoning setting, tool permissions, timeout, output budget, and invocation order. A valid comparison changes only the intended instructions. Do not give R0 a different tool schema, longer budget, or more adverse evidence than R1. Preserve complete command outputs and unmodified final reports. File hashes must remain unchanged after execution.

These fixtures intentionally have no history provider. Preflight through unchanged APIs produced the following technical selections; every returned instrument has status `UNAVAILABLE`:

| Case | Returned technical identities |
|---|---|
| H01 | HARB, NXCC, LNKF, WATR, LUMC, THMS, GRID, NOVS |
| H02 | RHEN, HARB, ELMO, ELDA |
| H03 | HARB, SPND, PUMP, ALTW |

Revalidate this table against the frozen engine before testing. Actual engine output controls if it differs; do not force these selections into the data. Selection is not usable technical coverage. No moving averages, support/resistance zones, empirical correlations, or probabilities can be recovered from these absent histories. UPDC is a leveraged ETF, not an option; `options=None` is correct.

## Preregistered execution and acceptance

The preregistered gate follows [the 2026-10-15 reliability spec](../../../docs/superpowers/specs/2026-10-15-portfolio-report-reliability.md), R-04. It is fixed before any report is viewed:

1. Run two independent repeats per case and instruction version: 3 cases × 2 repeats × R0/R1 = 12 reports. Use fresh processes, fixed as-of, and record randomized preregistered R0/R1 order within each pair. Requested model is `gpt-6-astra`, reasoning `medium`; use the same runtime/tools/methods, 600-second timeout and 8 MiB execution-output budget. Record actual CLI path, version, launcher hashes, and actual usage. If server revision is unavailable, describe comparison as the same requested configuration, not strictly identical model weights.
2. Assign anonymous A/B report labels and conceal version and execution order during scoring. A reviewer who did not generate the report scores it against this evidence set. Lock scores before revealing versions. Agent review is not independent human validation; model self-scoring or automatic preference alone cannot declare a pass.
3. Every R1 report must have no critical factual/trade/privacy error, pass all account-field checks, and score at least 3/4 on F (facts), R (risk importance), and C (counterevidence/calibration). Compute each case's five-dimension mean over two repeats. No R1 case mean may decline more than 0.25 from R0, and the overall facts-dimension mean must not decline. Missing execution or review leaves the gate incomplete; do not reduce the denominator.
4. Claim improvement on this new sample only if the overall equal-case-weight mean strictly improves, at least 2/3 case means improve, and no dimension's overall mean declines. Otherwise report reliability and engineering outcomes only. Preserve all report scores, paired differences, and repeat ranges; do not claim statistical significance or general improvement.
5. The four old-case defect regressions (Q02/Q05, two R1 reports each) are separate from these holdouts and from the generalization denominator. They must show complete/correct key account fields and no cash-stock/flow confusion through actual reports, receipts, and independent semantic review. This preparer does not inspect or score those old cases.
6. A service/environment failure is retained and may be rerun only in a new attempt with a documented cause; do not select the best report or retry indefinitely. A valid but incomplete or wrong answer is scored. Freeze both sides again if remediation changes their execution conditions.
7. Reference-reuse diagnostics report repeats after the first complete successful read, repeated raw output, and actual usage separately. The target is no unjustified complete reread; an unresolved reread leaves that item failed. Do not add a test-only prohibition that helps R1 alone. Engineering checks, unchanged engine/schema and the existing D0 budget remain separate requirements.

Passing RHT-1 supports only prospective reliability on these holdouts. Historical AC-17 remains subject to its original 9-case, two-repeat, improvement and completeness criteria. Do not retroactively lower AC-17, delete failed history, or use RHT-1 to relabel it passed. If R0/R1 comparison is unavailable, report absolute R1 results separately and the comparative gate incomplete.

If a production prompt is tuned after viewing these outputs, these cases become regression cases. Record that transition and prepare a different independent holdout before claiming further prospective generalization. Changes to these inputs, rubric, thresholds, or model configuration after freezing require a new version and explicit rationale before a new run.

## Common evidence gates and scoring

Hard failures are material facts/units/periods/arithmetic that support or reverse a principal conclusion; invented sources, metrics, scenario probabilities, or technical indicators; ignoring available decisive counterevidence while asserting an unconditional conclusion; private-data disclosure; account/trade write or unauthorized simulation; falsifying source identities/statuses; or omitting a material held position. Minor rounding or wording differences do not trigger a hard fail. Record the exact passage and why it is material.

The host separately verifies one bound snapshot, complete nonzero holdings, independent cash, unchanged NAV/source values, research/valuation budgets, allowed command boundaries, and required conditional policy reads. A model's own statement that it followed a rule is not proof. Research quality scoring does not penalize R0 for lacking a new R1 filename; policy-compliance checks are separately attributable.

Score five equally weighted dimensions from 0 to 4: F factual accuracy; R risk importance; E explanation and portfolio connection; C counterevidence/calibration; D decision usefulness. General anchors: 0 absent or seriously wrong; 1 generic mention; 2 partial grounded explanation with a material missing connection; 3 main requirements satisfied and omissions do not change the judgment; 4 clear, complete, well-prioritized evidence with appropriate uncertainty and no unnecessary material. The case anchors below clarify scores 2/3/4. There is no required prose template, number of bullish views, or preferred ticker.

For every dimension, record the report passage, the supporting or contradicting resource/field, the score, and a one-sentence reason. Keep the score sheet outside model workspaces. Cite actual tool receipts when judging what was read. For numeric claims the reviewer may reproduce calculations; do not require the model to compute ratios the user did not need.

## H01: reserve and related company exposures

The 29 holdings have NAV USD 1,000,000, cash USD 36,000 (3.6%), HARB USD 420,000 (42%), seven related companies totaling USD 360,000 (36%), twenty other companies at USD 9,000 each (18%), and UPDC USD 4,000 (0.4%). Company equity excluding funds is USD 540,000. All holdings and weights remain source facts even if research attention is uneven.

HARB's `company-profile` and fund factsheet establish 0–3 month Treasury bills, 0.10-year duration, 99.8% bills/0.2% cash, no derivatives or leverage, monthly variable distributions, and market-sale liquidity. It can be checked compactly without applying a company DCF or consuming company research attention merely because it is largest. A weekly review window says nothing about when the user needs principal. Distinguish account cash from the ETF and avoid declaring the whole account suitable or capital guaranteed.

The seven companies share aspects of datacenter deployment, with materially different revenue connections: NXCC accelerator 92%; LNKF datacenter networking 75%; WATR datacenter power 44%; LUMC cloud 65%; THMS datacenter cooling 52%; GRID datacenter electrical 19%; NOVS infrastructure customers 28%. Their 36% holding weight is an associated-company allocation, not pure thematic revenue or an empirical correlation. Adding UPDC's 0.4% market value makes 36.4% associated holdings, but its daily leverage needs a separate explanation. Do not double count overlapping company themes or multiply all company weights by the ETF's leverage.

The issuer releases and September articles support delivery/commissioning delays, slower bookings or utilization at NXCC/LNKF/LUMC, high investment at LUMC, but also noncancellable backlog, service growth, utility-replacement demand, and cooling wins. These are actual contrary facts, not merely future watch items. Earnings dates are triggers for the same business thesis, not automatically another independent top risk. Reasonable ranking may emphasize common project timing, cloud cash demands, valuation/expectation uncertainty, or liquidity-purpose uncertainty; it should explain magnitude and uncertainty.

UPDC targets twice a daily index return, with path dependence, swap/counterparty and spread considerations. Its 30-day price fall is 35%; the snapshot's unrealized loss is USD 1,000 or 20% of cost. These are different periods/bases. Small market value alone does not prove small total economic exposure, but the fixture supports purchased ETF shares and no account margin, not unlimited loss. A leading discussion devoted to UPDC must justify why it outweighs the materially larger shared business exposure.

| Dimension | 2-point anchor | 3-point anchor | 4-point anchor |
|---|---|---|---|
| Accuracy | Main balances right, but fund/cash or theme denominator blurred | Preserves balances and instrument distinctions; no unsupported technical/correlation claims | Clearly labels NAV/company/invested denominators and separates period return from cost P&L when used |
| Risk importance | Notices common theme but gives little magnitude or duplicates its earnings window | Research and principal risk ordering track material company exposure; complex ETF treated proportionately | Explains differing channels and the strongest counterweights, with a defensible competing priority |
| Explanation | Repeats growth/backlog fields | Connects deployment timing or investment to cash/earnings and this portfolio | Uses cross-company differences to show where shared shocks may or may not transmit |
| Calibration | Generic risk caveats or only future conditions | Uses existing backlog/service/utility evidence to qualify the risk thesis | Weighs current opposing evidence and identifies which next disclosure could change the priority |
| Usefulness | Eight mechanical ticker summaries | Compact reserve verification, justified company research choices, useful diagnosis without allocation actions | Clear leading judgment, honest research coverage/omissions, and focused review triggers |

Do not require all seven companies to receive identical depth or require precisely eight names. A smaller well-supported selection can pass. Strong claims about a company outside the researched set require supporting evidence or an explicit limitation. Coverage percentages must state their denominator; including a simple reserve fund cannot be presented as company research coverage.

## H02: filing units and cash conversion

NAV is USD 400,000: RHEN 35%, ELMO 20%, HARB 25%, ELDA 10%, cash 10%. RHEN trades at USD 62.50 per ADR; one ADR represents two ordinary shares. Its issuer accounts are IFRS EUR millions. The broker position currency remains USD. The fixture gives USD 1.10 per EUR on the fixed date; do not multiply account NAV by that rate.

The RHEN financials and half-year report provide:

- Q2 2026 revenue EUR 510 versus 420 million (+21.43%); net income 60 versus 42 million (+42.86%). These are standalone quarters.
- H1 CFO EUR 78 versus 110 million (-29.09%). Net income is H1 108 versus 80; CFO/net income is about 72.22% versus 137.50%. Cash-flow periods are six months, not Q2 alone.
- PP&E cash purchases are signed outflows of -34 and -28. A simple CFO-minus-cash-capex measure is therefore 44 and 82 million, not 112 and 138. Label this measure; it is not automatically FCFF.
- Cash balances of 185 at June 30 and 160 at December 31 are stocks. CFO 78, investing -34, financing -17, and FX -2 explain the cash increase of 25. Borrowings total 230; conventional cash-subtracted debt is 45 EUR million before any separately stated treatment of supplier finance. These are not USD amounts.
- Receivables rose 70; their balance movement includes 60 operating increase, 8 acquired, and 2 currency translation. Inventory rose 40, including 35 operating, 4 acquired, and 1 translation. The cash-flow working-capital lines have outflow signs. Neither balance increase is an extra subtraction from already reported CFO.

The material alternative explanation is in ordinary `notes.trade_receivables`, not a special warning: EUR 116 million came from June 28 project acceptances on 45-day terms, and EUR 92 million was collected by July 18. This supports timing as part of the H1 cash-conversion weakness. It does not prove the entire receivable book collected, eliminate the inventory increase, or establish Q3 CFO. `notes.supplier_finance` simultaneously reports the balance rising 8 to 22, with the 14 million increase included in CFO through operating payables. That financing-like benefit and top-two customer concentration of 31% versus 18% preserve reasons for caution. The later September meeting repeated guidance without new collection/cash figures; do not invent such an update.

Valuation is optional. If used, the four consecutive ordinary-share EPS values sum to EUR 1.90; with two ordinary shares per ADR and the supplied FX rate, that is USD 4.18 per ADR (about 14.95× at USD 62.50). The analyst sample is FY2027 USD 5.20 per ADR, not FY2026 or NTM; the implied FY2027 multiple is about 12.02×. There is no FY2026 consensus in the fixture. Do not compare EUR per-ordinary-share EPS directly with USD ADR price, call the FY2027 estimate FY2026, or assume a five-quarter TTM. A decision to avoid valuation is fully acceptable.

| Dimension | 2-point anchor | 3-point anchor | 4-point anchor |
|---|---|---|---|
| Accuracy | Notices currency/period labels but leaves a consequential statement ambiguous | Correct stock/flow, sign, reporting currency, ADR and period handling for all numbers actually used | Reconciles the key cash/working-capital claims and clearly limits optional valuation scope |
| Risk importance | Treats growth or weaker cash conversion as decisive alone | Weighs 35% position exposure and quality of earnings/cash against other holdings | Explains why the residual financing/concentration issue matters after the timing explanation |
| Explanation | Repeats CFO down/profit up | Connects project acceptance, receivables and cash timing without double subtraction | Integrates timing, inventory and supplier-finance support into a coherent cash-quality account |
| Calibration | Mentions collection or supplier finance without changing the argument | Uses July collection to qualify an unconditional deterioration thesis and retains unresolved concerns | States how strongly each fact changes the judgment and what is still unknown about later cash conversion |
| Usefulness | Generic watchlist | Specific present conclusion and next review evidence, within requested diagnosis | Clearly distinguishes what is already resolved, what remains material, and the portfolio consequence |

Both a cautiously favorable view and a cautious adverse view can score 4 if they weigh the same facts properly. Ignoring the disclosed collection while declaring receivables uncollectible, or using it to declare all cash concerns resolved, is material if it drives the conclusion.

## H03: active choice under contrary evidence

NAV is USD 500,000: SPND 30%, PUMP 20%, ALTW 10%, HARB 30%, cash 10%. The user authorizes active allocation advice among held securities, permits keeping reserves, states at least a three-year intended holding period and no known account spending in the next year, excludes leverage, and does not request targets, a simulation, or a trade. No confirmation of a numerical target is needed for directional advice. The request does not establish the user's overall wealth, drawdown tolerance, or permission to spend all reserves.

Relevant evidence, all synthetic:

| Holding | Supportive facts | Current contrary facts and constraints |
|---|---|---|
| SPND | Q2 revenue 1,800 vs 1,300; margin 28% vs 21%; backlog +26%, book-to-bill 1.18; raised FY2026 revenue range; net cash 850 | Top two customers 56%; one accelerated current deliveries and indicated roughly 10% lower 2027 tool budget; FY2027 EPS consensus fell 2.80 to 2.50; USD 75 price implies 30× FY2027, versus same-basis peer range 22–28× |
| PUMP | Q2 revenue 1,000 vs 850; operating margin 18% vs 15%; backlog 3,000 vs 2,600; FY2027 EPS 3.80 vs prior 3.50; USD 50 implies about 13.16× vs peer 14–18×; municipal/factory demand | October 2 service-program article estimates USD 60 million cash cost over 12 months, preliminary and excluded from adjusted EPS; 15% of backlog cancellable and one distributor 22%; net debt/EBITDA 1.6× |
| ALTW | 97% regulated revenue, limited revenue cyclicality; EPS consensus 3.20, price USD 50 implies 15.625×, peer 14–17× | H1 capex 480 versus CFO 220, net debt/EBITDA 5.0×; staff recommends 9.6% allowed ROE below 10.2% requested; November 12 hearing, final decision expected Q1 2027 |
| HARB/cash | Verified liquid reserve structure and optionality; no forced sale requirement | Bill reinvestment yield can fall; uncertain opportunity cost compared with held equities; neither cash nor fund liquidity alone determines suitable size |

All monetary operating figures in this table are USD millions, except prices/EPS. Price/estimate multiples are optional computations, not required target prices. FY2027 is distinct from FY2026; analyst samples are not probabilities. PUMP's service-program cost is in its October article, after the August earnings release; ALTW's staff recommendation likewise appears in the September article. Missing these later sources while asserting unqualified confidence is materially different from declining a point estimate.

Quarterly PUMP CFO 150 and signed PP&E outflow -50 imply simple quarterly FCF 100; the 60 service-program cash estimate spans the next 12 months, and should not automatically be subtracted entirely from that past quarter or equated to a same-sized EPS reduction. ALTW's H1 investment deficit is not automatically insolvency; regulated recovery is conditional and delayed. SPND's business growth is not proof of attractive incremental price, while the 2027 slowdown clue does not erase its backlog/packaging evidence.

| Dimension | 2-point anchor | 3-point anchor | 4-point anchor |
|---|---|---|---|
| Accuracy | Generally cites facts but mixes adjusted earnings/cash cost or periods | Uses matching price/estimate bases and correct cash/catalyst periods; no invented precision | Clearly separates management, consensus, observed facts, and any explicitly calculated scenario |
| Risk importance | Lists three independent company risks | Relates 30%/20% equipment positions, reserve capacity, and utility financing to the choice | Distinguishes overlap, diversification benefit, and the principal opportunity cost of the chosen funding source |
| Explanation | Calls one company cheaper/better | Connects price expectations, operating drivers, contrary facts and portfolio effect | Makes a defensible relative choice with clear advantages and costs against keeping the existing allocation |
| Calibration | Future caveats only | Engages SPND customer/estimate evidence, PUMP recall, and ALTW funding/rate issue where relevant | Explains why the strongest contrary fact does or does not change preference, with observable reversal conditions |
| Usefulness | Only watch/hold comments or requests target weights | Gives a clear priority, direction and funding source/use with conditions; may retain reserves | A coherent actionable sequencing decision, with justified reserve choice and explicit tradeoffs, without false suitability or execution claims |

The rubric does not mandate buying PUMP, selling SPND, choosing ALTW, or deploying reserves. Any ordering can score well if the evidence and portfolio consequences support it. A decision to retain current weights can also pass if it makes a concrete capital-allocation choice, explains why no funded switch currently dominates, identifies the best conditional destination, and states the evidence that would change it. Merely listing possibilities or saying “continue observing” has decision usefulness at most 2. Arbitrary precise weights do not improve a score; conditional ranges need a stated rationale and are not a substitute for an actual comparative choice.

## Freeze record

Freeze both files with SHA-256 before exposing the preparer to any tested report. Record the hashes, timestamp, engine revision and model configuration in the host run manifest. Do not embed an evolving self-hash inside this document. The preparer's handoff supplies the two file hashes. Preflight JSON parsing, source reconstruction, snapshot round-trip, risk and technical schema validation, price × quantity reconciliation, and H02 cash-flow/receivable reconciliations succeeded without a model call.
