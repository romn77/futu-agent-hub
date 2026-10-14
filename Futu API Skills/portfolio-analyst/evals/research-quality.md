# Controlled research-quality comparison

Operator/evaluator instructions. **Never copy this file, its scorecard, the case
registry, another report, or a version label into the model workspace or prompt.**
These are nine independent, entirely SYNTHETIC manual samples for the revision-3
[quality specification](../../../docs/superpowers/specs/2026-10-14-portfolio-analyst-prompt-simplification.md).
They are not new case IDs accepted by `run_codex_evals.py`, not live facts about
real tickers, and not evidence that a model run passed. No model was invoked to
prepare this fixture. No real portfolio was read or copied.

## Input interface and invariants

`quality-fixtures/cases.json` has `schema_version`, `synthetic`, `research_date`,
`method_files`, and `cases`. A case contains only `id`, `account_fixture`,
`user_prompt`, `optional_method_paths`, and `public_data`. It contains no expected
answer or score. `public_data` is a standalone dataset for the unchanged
`research-fixtures/public_reader.py`: symbol resources map public symbols to
bodies; `futu/calendar` is an event list; peers/economic calendar use `market`.
Methods in `method_files` are model-visible neutral method instructions, not
expected conclusions. Only Q09 selects the additional strategy method; its user
payload includes a host catalogue entry. Existing earnings, estimates, valuation
and already-injected financial-thesis methods are reused without modification.

| Case | Existing account fixture | Research question |
|---|---|---|
| Q01 | conditional-options-degradation | Salient public drawdown versus shared demand and smaller option management risks |
| Q02 | explicit-rebalance-request | Operating improvement versus the expectations embedded in price |
| Q03 | explicit-rebalance-request | Profit growth, cash conversion and a plausible collection-timing explanation |
| Q04 | explicit-rebalance-request | Q03 plus one verified, same-period receivables counterevidence object |
| Q05 | explicit-rebalance-request | Fiscal-period conflict and incomplete valuation inputs with usable operating facts |
| Q06 | explicit-rebalance-request | Requested adjustments without supplied target weights |
| Q07 | conditional-options-degradation | Unknown spending horizon, 52% cash reserve and external quote-unit ambiguity |
| Q08 | explicit-rebalance-request | Active held-position choices with explicit cash/concentration constraints |
| Q09 | explicit-rebalance-request | Two named unheld candidates and one event strategy with missing contract inputs |

All accounts remain byte-for-byte equal to the existing producer's envelopes:
no cost, P/L, date, symbol, classification, options, risk or technical mutation.
The three-position fixture is AAPL 40%, MSFT 30%, SPY 20%, cash 10%. The options
fixture is AAPL/MSFT/SPY 15% each, three option positions 1% each, cash 52%.
NAV is USD 100,000; snapshot as-of is 2026-09-02T14:00:00Z. Research is fixed at
2026-10-14, not a refreshed snapshot. New public prices must not overwrite account
P/L, weights or NAV. Existing technical responses have no usable price provider;
inspect their actual identity sets/statuses and never manufacture levels.

Q01 intentionally uses a **public since-snapshot price decline**, not a synthetic
negative snapshot P/L. The original account P/L remains positive. Its January 15
options are shorter than the user's six-month review cycle, not falsely described
as expiring this week. This refinement preserves the intended salience/materiality
comparison without invalidating or rebuilding account evidence. Q07's 3-contract
illustration belongs exclusively to an external research receipt; the account's
actual contract quantities are unchanged. A disputed component calculation cannot
by itself make a valid authoritative snapshot fatal.

Every prompt explicitly retains at most eight held names and two full valuations.
Q09 expressly permits at most two candidates, AMZN/GOOGL, and one AMZN call spread;
the two-valuation budget applies across held names and candidates together.
Same-model sensitivities count as one valuation. No case requests simulation,
trading, history, installation, persisted analysis, or outside data. Q06/Q07/Q08
request advice only within held assets; ordinary diagnosis does not authorize new
candidates. A catalogue listing is not a demand to invoke every method.

## Freeze and prepare

Before any production prompt editing, copy this document and `cases.json` into an
operator-only freeze directory; hash them alongside the original complete B0
component, common prompt, reader, existing methods, runtime and schemas. Freeze
host/CLI and exact model versions, reasoning setting, Python environment,
execution deadline/output limits, tool catalogue, and the randomized paired run
schedule. Unknown server model revision means comparison conditions are
insufficient; never substitute an alias silently. Record the baseline git SHA and
uncommitted-file manifest. Use the same frozen code, account fixtures, common
prompt, methods and selected public data for both versions. B1 may replace only
`SKILL.md` and the allowed `references/` instructions. If those inputs change,
start a new explicitly recorded comparison; do not overwrite an earlier freeze.

The commands below prepare one attempt without invoking a model. `QUALITY_CODE`
is the frozen B0 component. `QUALITY_PROMPTS` is B0 for a baseline run or the B1
component for the revised run. `QUALITY_CASES` must point to the frozen registry,
not an editable working copy. Run from the current component so its existing
`.venv/bin/python` is available. Use a fresh output directory per version/repeat;
keep the version/repeat labels outside the model-visible workspace.

```bash
export QUALITY_CODE=/private/tmp/portfolio-advisor-quality-ctbrfxpd/baseline
export QUALITY_PROMPTS="$QUALITY_CODE"
export QUALITY_CASES=/private/tmp/quality-freeze/cases.json
export QUALITY_CASE=Q03
export QUALITY_OUT=/private/tmp/quality-Q03-repeat1-baseline
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python - <<'PY'
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

code = Path(os.environ['QUALITY_CODE']).resolve()
prompts = Path(os.environ['QUALITY_PROMPTS']).resolve()
registry = Path(os.environ['QUALITY_CASES']).resolve()
case_id = os.environ['QUALITY_CASE']
out = Path(os.environ['QUALITY_OUT'])
payload = json.loads(registry.read_text())
assert payload['synthetic'] is True and payload['research_date'] == '2026-10-14'
assert [row['id'] for row in payload['cases']] == [f'Q{i:02}' for i in range(1, 10)]
case = next(row for row in payload['cases'] if row['id'] == case_id)
q3, q4 = payload['cases'][2:4]
assert q3['user_prompt'] == q4['user_prompt']
assert q3['account_fixture'] == q4['account_fixture']
control = json.loads(json.dumps(q4['public_data']))
assert control['filing/segments']['US.AAPL'].pop('counterevidence')
assert control == q3['public_data']
out.mkdir(mode=0o700, parents=False, exist_ok=False)
spec = importlib.util.spec_from_file_location('quality_fixture_producer', code / 'evals/run_codex_evals.py')
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)
workspace = out / 'workspace'
envelope = producer.prepare_workspace(workspace, case['account_fixture'])
from portfolio_analyst.snapshot_io import snapshot_from_document
from portfolio_analyst.schema import validate_option_risk_document, validate_technical_document
snapshot_from_document(envelope['snapshot'])
validate_technical_document(envelope['technical'])
if envelope['options'] is not None:
    validate_option_risk_document(envelope['options'])
assert envelope == producer._fixture(case['account_fixture'])
if prompts != code:
    shutil.copy2(prompts / 'SKILL.md', workspace / 'SKILL.md')
    shutil.rmtree(workspace / 'references')
    shutil.copytree(prompts / 'references', workspace / 'references')
source = code / 'evals/research-fixtures'
research = workspace / 'research'
research.mkdir()
shutil.copy2(source / 'public_reader.py', research / 'public_reader.py')
shutil.copytree(source / 'skills', research / 'skills')
(research / 'data.json').write_text(json.dumps(case['public_data'], ensure_ascii=False, indent=2) + '\n')
for relative in case['optional_method_paths']:
    target = Path(relative)
    assert target.parent.as_posix() == 'research/skills' and target.name in payload['method_files']
    (workspace / target).write_text(payload['method_files'][target.name])
assert not (workspace / 'evals').exists()
assert not (workspace / 'research-quality.md').exists()
assert not (workspace / 'cases.json').exists()
(out / 'prompt.txt').write_text((source / 'common-prompt.md').read_text() + '\n\n' + case['user_prompt'] + '\n')
# Smoke-test every supplied public resource before exposing it; keep these
# receipts operator-side. Do not seed them into model memory.
receipts = []
for key, resource in case['public_data'].items():
    src, name = key.split('/')
    queries = ([['--start', '2026-10-15', '--end', '2026-10-21'],
                ['--start', '2026-10-22', '--end', '2026-10-28']]
               if key == 'futu/calendar' else
               [[] if symbol == 'market' else ['--symbol', symbol] for symbol in resource])
    for args in queries:
        command = [sys.executable, 'research/public_reader.py', src, name, *args]
        result = subprocess.run(command, cwd=workspace, capture_output=True, text=True, check=True)
        body = json.loads(result.stdout)
        assert body['synthetic'] is True and 'data' in body
        expected = ([row for row in resource if args[1] <= row['date'] <= args[3]]
                    if key == 'futu/calendar' else resource[args[1] if args else 'market'])
        assert body['data'] == expected
        receipts.append({'command':command[1:], 'exit_code':result.returncode,
                         'stdout':result.stdout, 'stderr':result.stderr})
(out / 'preflight-receipts.json').write_text(json.dumps(receipts, ensure_ascii=False, indent=2))
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
manifest = {str(path.relative_to(workspace)):digest(path)
            for path in sorted(workspace.rglob('*')) if path.is_file()}
(out / 'inputs.json').write_text(json.dumps({'case':case_id,
    'account_fixture':case['account_fixture'], 'registry_sha256':digest(registry),
    'prompt_sha256':digest(out / 'prompt.txt'), 'files':manifest}, indent=2))
print(out)
PY
```

Run this preparation separately for all cases and both repeats/versions. A fresh
workspace is a fresh model session; never use resume or share reports. Q03/Q04
are independent sessions, not consecutive messages. Before each paired execution,
compare non-instruction file hashes and prompt hashes: they must match exactly.
The source-code tree must match the frozen runtime; differences are allowed only
in the listed product instruction files. Record actual R/T sets and field status
from the envelope, not from a presumed eight-name technical limit.

## Execute and retain evidence

Execution requires the same host authorization as the existing manual research
samples. A host rejection is not permission to switch model, endpoint, agent,
credentials or sandbox. Preparation and a rejected execution are not model passes.
Set `QUALITY_MODEL` to the frozen explicit model identifier and `QUALITY_EFFORT`
to the frozen supported reasoning setting; do not use a moving default. Retain
CLI/model-version evidence separately. From the prepared workspace:

```bash
cd "$QUALITY_OUT/workspace"
codex -a never exec --ignore-user-config --ephemeral --sandbox read-only \
  --skip-git-repo-check --model "$QUALITY_MODEL" \
  -c "model_reasoning_effort=\"$QUALITY_EFFORT\"" --json --color never - \
  < "$QUALITY_OUT/prompt.txt" \
  > "$QUALITY_OUT/events.jsonl" 2> "$QUALITY_OUT/stderr.txt"
```

Wrap this exact argv with the existing `_bounded_subprocess_run` (imported from
`portfolio_analyst.option_metadata`, as in `run_codex_evals.run_case`) using
the same 300-second deadline and 8 MiB output bound in every attempt, or use a
host-equivalent bound frozen for both versions. The host must preserve raw events,
stderr, return code and timeout/output-limit state. Never delete an unsuccessful
attempt. Extract the final completed `agent_message` and actual command receipts;
verify post-run workspace hashes against the manifest. Every model should invoke
`scripts/current_analysis.py` once successfully. After acquisition it may inspect
the retained envelope, but it must not acquire again, read other backing account
files, reader source, or `research/data.json` directly. Public queries use the
public interfaces. Existing method/reference acquisition and arithmetic must be
host-observed; a report's claim that it read/calculated something is insufficient.

For 9 cases × 2 independent repeats × 2 versions retain 36 reports/attempts. Record
paired randomized execution order before running. Assign random A/B report labels
and remove version/order metadata from the evaluator packet. The independent
reviewer gets public evidence, account evidence and this scorecard, but not the
version. Finish scores and paragraph/receipt citations before unblinding. Model
self-scores or an automatic preference are not the sole acceptance evidence.
Legacy regression cases remain separate; no assertion or fixture here replaces
or relaxes them. No exact wording or new B1 file name is a B0 quality requirement.

## Shared scoring and hard errors

Score five equally weighted dimensions, each 0–4: **F** factual/evidence accuracy,
**R** risk materiality, **E** explanation and portfolio connection, **C** response
to counterevidence/calibration, **D** decision usefulness. General anchors: 0 is
missing/seriously false; 1 generic mention; 2 partially supported with a key link
missing; 3 substantial requirements met with only non-material omissions; 4 clear,
complete and focused. The scenario tables below specify 2/3/4 anchors for each
dimension. A reasonable alternative conclusion can score 4. Extra length,
optimism, caution, recommendations, citations, or trades are not extra credit.

Locate each score in report paragraphs and receipts and identify the supporting
or missing evidence. Do not infer a computed portfolio loss from a qualitative
risk description. Hard errors include invented decisive facts/sources/metrics,
material period/unit/arithmetic mistakes, deterministic conclusions ignoring
provided core counterevidence, and account/trade/privacy boundary violations.
A hard error in B1 fails the quality gate regardless of average. General gaps
must not be silently converted to zero or to a negative fundamental judgement.

For each report average F/R/E/C/D; average the two repeats within each case, then
weight all nine cases equally. B1 must reach ≥3.0/4 and improve ≥0.4 over B0;
no dimension's across-case mean may fall; at least 6/9 case means must strictly
improve; no case mean may fall more than 0.25. Report each repeat difference and
spread without significance or return claims. Missing/uncomparable pairs leave
acceptance incomplete; never shrink the denominator. AC-01–16/18–23 and existing
behavior/account gates also apply. Frozen cases used for prompt tuning become a
regression set; reserve new equivalent untuned cases for a fresh generalization
claim. If B0 has a ceiling effect, do not reduce the improvement gate.

## Q01 — common driver and salience

The account has 30% NAV in AAPL+MSFT directly, each 15%; SPY 15% is equity exposure
but has no look-through data. Three options are 1% each and have incomplete
monetary sensitivities. Cash is 52%, not evidence that the owner can tolerate
arbitrary loss. AAPL's public quote moved 100→60 since the account date: a 40%
public price decline, **not** current account P/L. The survey's spending growth
plan slowed +20%→+5%; it did not report spending falling 15% or a quantified EPS
cut. Shared demand affects hardware promptly and subscriptions over 6–12 months.
October 21/23 earnings are observation windows for that risk, not automatically
two new structural risks. The options expire January 15, 2027.

Acceptable: place shared demand first; or place the reliability/staleness constraint
first if it materially limits prioritization and shared demand receives appropriate
weight. A transparent case for near-term option monitoring is acceptable without
claiming it has the greatest economic loss potential. No forced third invented risk.

| Dimension | 2 | 3 | 4 |
|---|---|---|---|
| F | Quotes figures but mixes some times/bases | Distinguishes public price change, account date and survey growth | Also preserves exact technical/options states and unknown monetary sensitivities |
| R | Names shared demand but repeats it as earnings risk | Weighs 30% direct common exposure against 1% options; explains first priority | Integrates timing, evidence strength, reserves and uncertainty without double counting |
| E | Company facts with limited transmission | Links capex intentions to orders/renewals and the two holdings | Explains different transmission lags and why SPY cannot simply be added to that theme |
| C | Generic warning that demand may recover | Keeps survey intentions and signed contracts distinct | States observable order/renewal changes that would weaken or strengthen concern |
| D | A list of risks with little ordering | Clear diagnostic conclusion and useful monitoring order | Makes the main portfolio tradeoff clear without unsolicited buy/sell instructions |

## Q02 — good business and price

AAPL quarterly revenue rises 100→145, NI 15→29, CFO 17→35, consistent GAAP and
USD millions. Public price 140 and FY2027 EPS 5 imply 28x; matched hardware peers
are 20/22/24x. Optional P/E sensitivities are 100/110/120: the median value is
about 21.43% below 140, not upside. July Q3 guidance is unchanged and consensus
is within it; improvement alone is not an earnings beat. Utilization/mix supports
margins; a possible second supplier is a counterpoint, not confirmed lost orders.
Account exposure is AAPL 40%, MSFT 30%, with overlapping customers.

Acceptable: operating quality is strong but price requires continued execution;
or defend a premium with an explicit growth/durability assumption and acknowledge
that the supplied peer comparison does not independently prove it. A full valuation
is optional; a sourced multiple comparison can support the diagnostic conclusion.

| Dimension | 2 | 3 | 4 |
|---|---|---|---|
| F | Repeats growth and price but leaves basis vague | Matches FY2027 GAAP EPS, quote date and peer period | Correctly distinguishes guidance, consensus and optional executed sensitivity arithmetic |
| R | Calls AAPL expensive without account context | Relates premium and 40% concentration to portfolio vulnerability | Connects shared drivers and valuation sensitivity without invented portfolio losses |
| E | Treats growth as the whole thesis | Explains utilization/mix→profits/cash and price assumptions | Separates operational progress, what consensus embeds and the cost of a premium |
| C | Generic competition warning | Weighs real margin/cash strength against possible second supplier | States evidence that would justify/reject sustaining the premium without claiming an order loss |
| D | Says good company/uncertain price | Clear price-conditioned portfolio conclusion | Prioritizes the valuation issue and observable conditions without automatic addition or trade instruction |

## Q03 / Q04 — cash conversion and controlled counterevidence

Requests and account envelopes are identical. All base public data are identical.
Q04 adds only `filing/segments.US.AAPL.counterevidence`. Compare these objects
structurally before running; do not mention the pairing to the model.

In Q03 AAPL revenue increases 100→130 and NI 15→24, while CFO declines from 22
in Q4 to 19 in Q1 and 12 in Q2; Q2 CFO/NI is 50%, versus Q1 about 90.48%.
Q2 receivables rise 35→48, or about 37.14%, versus revenue 120→130, about 8.33%.
Free cash flow, if defined CFO minus positive capex outflow, is 13 in Q1 and 5 in
Q2. Receivables growth can fund valid accepted deliveries; management attributes
11 million to late-quarter milestones and expects Q3 collection. Independent
collection confirmation is absent. Do not silently treat this explanation as
verification or label all growth fraudulent. Four quarters are single-quarter,
not YTD, amounts. AAPL is 40% of snapshot NAV; MSFT's cash trends are healthier,
but shared customers remain a portfolio driver.

Q04's October 12 supplement concerns the **same June 30 receivable population**:
15 million >90 days overdue versus 3 prior quarter; 12 million disputed billings
are inside that 15, with missing acceptance documents. Management cannot date
collection; no restatement or quantified loss allowance has been announced. The
agreed-upon-procedures evidence is narrower than a full financial audit opinion.
This directly challenges the simple accepted-delivery/timing explanation. Do not
add 12+15 or subtract either as an already-confirmed earnings loss.

Acceptable Q03: cash conversion is the leading unresolved question; or a cautiously
constructive thesis conditional on collections, distinguishing reported growth
from quality. Acceptable Q04: lower confidence/more urgent verification, higher
importance, or explain why an already-conservative Q03 judgement remains appropriate
while explicitly addressing the added evidence. A change of action label is not
required. Fraud, default or a particular share-price loss is not established.

| Dimension | 2 | 3 | 4 |
|---|---|---|---|
| F | Mentions CFO divergence with limited basis detail | Uses consistent quarterly profit/CFO/AR facts and source periods | Any ratios are actually calculated; Q04 avoids overlap/restatement/audit-scope errors |
| R | Treats this as an isolated accounting point | Links AAPL 40% weight and shared customers to priority | Distinguishes cash-quality concern from event timing; contrasts MSFT without asserting independence |
| E | Profit versus CFO statement without mechanism | Explains working-capital absorption and collection timing | Shows why growth quality can affect earnings durability/valuation and which bridge remains unknown |
| C | Mentions timing or overdue amounts generically | Q03 preserves timing alternative; Q04 addresses disputed acceptance | Weighs evidence strength, calibrates confidence/priority and identifies observable collection/acceptance tests |
| D | Generic monitoring | A clear supported diagnostic view and focused follow-up | Integrates evidence into a useful portfolio conclusion; Q04's added evidence changes or defensibly sustains the judgement |

Score Q03 and Q04 independently. In the pair note, cite how each version responds
to the controlled addition; a boilerplate identical conclusion that ignores it is
not calibration. Core counterevidence ignored in a deterministic Q04 thesis is a
hard error, not merely a small wording deduction.

## Q05 — incomplete/conflicting evidence

AAPL's annual EPS 7 is explicitly FY2028, with unknown contributors and prior
estimate, whereas the peer multiple set is FY2027. The field labeled `fy1_eps`
is null. It cannot support a matching FY2027 target or a confident consensus
revision. MSFT's FCFF worksheet has WACC=g=4%, making the Gordon denominator
zero; debt is unknown. No annual EPS fallback is supplied for MSFT. Quarterly
operating statements, company profiles, dates, and AAPL quarterly consensus remain
usable. Missing annual consensus does not erase those facts or become zero EPS.

Acceptable: decline exact targets for both and still analyze operating progress,
concentration and the data needed to complete valuation. It is also acceptable to
explain a conditional model algebraically without inventing an input. Do not require
two numeric models merely because the budget is two.

| Dimension | 2 | 3 | 4 |
|---|---|---|---|
| F | Notices some missing inputs but misses period mismatch | Identifies FY2028/FY2027 mismatch, WACC=g and unknown debt | Separates contributor/revision gaps and preserves reliable quarterly evidence with dates |
| R | Declares generic uncertainty | Links valuation uncertainty to concentrated positions | Prioritizes what could change the portfolio judgement without treating absence as deterioration |
| E | Stops after invalid model labels | Explains why each missing/mismatched field blocks the calculation | Completes the business→portfolio explanation where inputs are usable and locates its unresolved valuation bridge |
| C | Disclaims certainty broadly | Separates conflict from missing data and source strength | Gives specific resolving evidence while avoiding invented repair assumptions |
| D | Entire analysis stalls | Useful conclusion with limited precise estimates | Prioritized operating/portfolio implications and a concise completion path; restraint is not penalized |

## Q06 — actionable advice, no target supplied

AAPL price 140, EPS 5 and peers 20/22/24x support a demanding price; MSFT price
100, FY2027 EPS 6 and peers 18/20/22x support 108/120/132 if calculated. MSFT's
80% renewal completion at 115% retention versus plan 110% supports the update,
but 20% and two large customers remain unresolved. AAPL is 40%, MSFT 30%, SPY
20%, cash 10%; adding MSFT alone does not eliminate shared demand exposure.
The user explicitly requests adjustments, not simulation, and supplies no risk
budget or spending horizon.

Acceptable: reduce AAPL priority for new funds, prefer MSFT conditionally, preserve
cash or broader exposure; or a cautious allocation change justified against the
supplied evidence. Exact target weights are optional and must be conditional if
used. Do not demand numeric targets before giving direction; do not assume risk
capacity or automatically use the prepared simulation request.

| Dimension | 2 | 3 | 4 |
|---|---|---|---|
| F | Gives directional claims with weak price/period support | Uses matching valuation/renewal facts and snapshot weights | Any valuation/sizing numbers have executed arithmetic and stated assumptions |
| R | Treats moving AAPL→MSFT as full diversification | Explains concentration and residual common exposure | Weighs valuation, renewal uncertainty and cash flexibility in order |
| E | Company ranking only | Explains why a relative opportunity affects funding choices | Connects operating evidence, price and opportunity cost of the present allocation |
| C | Caveats all suggestions generally | Names unfinished renewals and price/collection conditions | States what would reverse the priority and limits personalization without paralyzing advice |
| D | Observe-only or asks for targets before helping | Gives clear directions, priorities, funding/use and tradeoffs | Concrete conditional proposal with meaningful alternatives to the status quo, no unsolicited simulation/candidates |

## Q07 — suitability, reserves and economic units

The options fixture has 52% cash; a six-month review cycle is not a six-month
spending or investment horizon. Cash can be a reserve and option for future use,
not automatically idle money. AAPL+MSFT direct share weights total 30%; that is
**theme-associated position weight**, not 30% pure datacenter revenue exposure.
SPY remains a separate equity ETF; no look-through inference is supported. The
simple cash balance does not warrant corporate earnings/DCF research.

The external option component says COMPLETE, quote 0.02, multiplier 100, a
3-contract illustration and reported value 0.06 USD. Its dictionary admits that
quote basis is omitted and the legacy value equals quantity×display premium.
If per underlying share, the illustrative amount would be 6 USD; if per contract,
0.06 USD. These are conditional interpretations, not an account correction. The
dictionary confirms the multiplier, not the display unit. The held-option
account tool does not establish this external receipt's units. Keep snapshot
NAV, actual positions and existing options tool status intact. No extra account
read or guessed ×100 repair is justified.

Acceptable: give useful conditional advice with unknown spending horizon and
reserve needs, limit precise risk sizing, investigate dictionary and retain unit
ambiguity. A short targeted suitability question can accompany analysis; a broad
questionnaire replacing analysis is weak. No forced deployment of cash or claim
that a short-lived option must be unsuitable simply because review is six months.

| Dimension | 2 | 3 | 4 |
|---|---|---|---|
| F | Flags unit oddity but omits dictionary or scope | Checks dictionary and distinguishes external illustration from account | Explains both conditional units, preserves authoritative values and exact tool states |
| R | Focuses on options solely due to complexity | Weighs stocks, reserves and option-management limits by amount/evidence | Separates review/spending/strategy horizons and avoids repeated/overlapping theme totals |
| E | Describes cash and stock facts separately | Relates reserve uncertainty and common drivers to the account | Distinguishes research effort/coverage of operating holdings from simple cash and theme-associated versus pure exposure |
| C | Generic missing-information caveat | Clearly says what unit/horizon is unresolved and what remains usable | Gives targeted evidence needed for scale/suitability and useful condition-dependent conclusions |
| D | Stops for questionnaire or declares cash drag | Continues useful held-position advice while limiting exact sizing | Prioritizes research and reserve decisions with concise next steps, no invented tolerance or account correction |

## Q08 — sufficient evidence for active held-position choices

Snapshot AAPL/MSFT/SPY/cash is 40/30/20/10%. The user now wants single-stock targets
≤35%, cash ≥10%, no leverage/shorts, no withdrawals for 12 months and monthly
review; no maximum drawdown was supplied. It is valid to propose AAPL 35%, MSFT
35%, SPY 20%, cash 10% as one conditional allocation, but **this is not the only
acceptable allocation** and it is not an executed/rebalanced account. The stale
account prevents precise trade-share and executable-funding claims.

At public price 100, AAPL FY2027 downside/base/upside EPS×P/E is 4×18=72,
5×24=120, 6×28=168: −28%, +20%, +68%. MSFT is 5×16=80, 6×20=120,
6.5×24=156: −20%, +20%, +56%. The AAPL base premium versus 22x hardware peer
median needs execution; MSFT base is at its 20x peer median. These are conditional
12-month scenarios, not forecast returns, probabilities, maximum losses or an
account-level simulation. AAPL has a November 20 acceptance milestone and weak
cash conversion; MSFT has a November 5 remaining-renewal update and competitor
discounts. Neither catalyst guarantees upside. The shared customer risk remains.

Acceptable: prioritize MSFT's better supplied downside/base tradeoff and stronger
cash evidence, fund from overweight AAPL; or favor AAPL's larger upside only with
an explicit execution/valuation case and conditions consistent with the cap.
Retaining more cash or SPY can be justified, but evidence-rich requested advice
cannot end with only generic warnings/observation. Such an answer caps D at 2 and
fails AC-21. Both coherent concentration reduction and selective risk-taking can
score well; neither arbitrary risk-aversion nor optimism earns points by itself.

| Dimension | 2 | 3 | 4 |
|---|---|---|---|
| F | Gives some plausible numbers without consistent assumptions | Correctly executes matching EPS×P/E scenarios and respects constraints | Distinguishes scenario horizon, snapshot/public prices, peer premium and missing loss probabilities |
| R | Ranks upside only or ignores shared exposure | Compares down/base/up and concentration/cash tradeoffs | Weighs evidence reliability and catalyst timing; no false portfolio loss or maximum-loss claim |
| E | Two separate bullish theses | Explains earnings/cash→valuation→funding priority | Makes opportunity cost versus keeping 40/30/20/10 explicit and connects different catalysts |
| C | General execution caution | Responds to cash collection and remaining-renewal/discount counterpoints | Names observable failure/reassessment conditions and explains how they alter priorities/scale |
| D | Observation only, or arbitrary weights | Clear priority, conditional directions/weights and funds source/use | Feasible constraint-consistent proposal with strengths and costs versus status quo, no simulation or invented tolerance |

## Q09 — two candidates and one event strategy

AMZN and GOOGL are not account holdings. Existing AAPL is priced at 130/FY2027
EPS 5 (26x); MSFT at 100/EPS 6 (about 16.67x). AMZN is 80/EPS 4 (20x), mainly
regulated-utility metering/services: 75% utility customers, 60% recurring maintenance,
no disclosed datacenter revenue, different demand drivers but regulatory exposure.
Its qualified defect rate 0.4% versus 1% ceiling is witnessed evidence, not an order.
October 27 customer acceptance remains pending; the October 13 eight-contributor
consensus still assumes 50% rollout. Full rollout EPS 4.8 is a conditional issuer
sensitivity, not confirmed consensus. A plausible information difference exists,
but the data do not prove that prices ignore qualification. Optional AMZN scenarios
are 3.5×16=56 (−30%), 4×20=80 (0%), 4.8×22=105.6 (+32%).

GOOGL is 90/EPS 4.5 (20x), optical equipment with 80% datacenter revenue and 55%
top-two-customer concentration. Seven of eight contributors already include
rollout; a known launch date alone is not an unpriced catalyst. Pilot power use
exceeds specification 15%; firmware correction and acceptance are unconfirmed.
Adding it can intensify existing AAPL customer exposure despite a cheaper multiple.
Different fundamental demand drivers do not establish a numeric correlation.
There are sufficient matched public facts to compare candidates with AAPL/MSFT
without valuing all four. Respect the global two-valuation cap and distinguish a
budget omission from a data gap. Peer descriptors do not add extra candidates.

The only strategy is an AMZN call debit spread around the October 27 update.
The receipt confirms American style and multiplier 100 but provides no verified
expiries, contract IDs, strikes, bid/ask, IV, open interest, ex-dividend date or
broker exercise/margin rules. Explain capped upside, premium-at-risk under an
intact managed spread, volatility/time effects, liquidity/spread cost and early
assignment/funding requirements. Algebraic expiry payoff is supported; a specific
contract, debit, monetary maximum loss, breakeven or position size is not. A chosen
expiry must actually exist and leave time for event resolution. Held-options
analysis cannot validate this new strategy. Lack of a loss budget also limits
personalized size; it does not erase the candidate comparison.

Acceptable: prefer AMZN's differentiated driver but wait for a better price or
regulatory clarity; favor adding MSFT instead because supplied valuation is more
attractive; or favor GOOGL only by directly addressing overlap, already-embedded
rollout and power risk. The candidate preference is not prescribed. It is valid
to decline a concrete spread until missing inputs arrive while completing its
mechanism and candidate comparison. Rejecting the entire research request solely
because the option chain is missing is incomplete.

| Dimension | 2 | 3 | 4 |
|---|---|---|---|
| F | Basic candidate facts but vague timing/basis | Keeps account/candidate identities distinct, dates and FY2027 comparisons correct | Quantifies only supported inputs with actual calculations, preserves all strategy gaps and tool limitations |
| R | Calls either new name diversification without support | Contrasts utility/regulatory risk with shared datacenter exposure | Explains incremental portfolio risks and strategy management/liquidity versus useful event exposure |
| E | Standalone company descriptions | Compares each to holding/adding AAPL/MSFT and identifies event expectations | Explains where information may differ from estimates and where launch is already embedded, with price conditions |
| C | Generic event could fail | Addresses regulatory request, power-spec issue and unavailable chain | Calibrates preferred candidate/strategy against strongest current counterpoints and observable acceptance tests |
| D | Declines everything or names tickers without comparison | Useful prioritized candidate comparison plus supported spread mechanics | Clear funding/opportunity-cost judgement and conditions; limits only unsupported contracts/sizing, within two/one budget |

## Operator record

For every attempt keep case/repeat, hidden version, input hashes, fixed model/host
configuration, randomized order, return/limit state, raw events/stderr, final report,
command receipts, post-run hashes and the independent scorecard. Record each F/R/E/C/D
score with paragraph and evidence citations, hard errors separately, accepted
alternative rationale, and actual research/valuation/candidate/strategy counts.
Report preparation, execution unavailable, observed failure and observed pass as
different states. The final comparison must include all 18 pairs, both repeat
differences, per-dimension means and engineering/behavior/quality gates separately.
