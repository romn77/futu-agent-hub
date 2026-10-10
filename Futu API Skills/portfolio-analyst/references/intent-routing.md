# Intent routing

Route by requested outcome. Explicit user intent overrides defaults but never the read-only boundary.

| Intent | Route | Default? |
|---|---|---|
| Analyze, review, or diagnose current holdings | Fresh REAL/USD comprehensive workflow | Yes |
| Explain option exposure already held | Comprehensive workflow plus options policy | Conditional on held options |
| Recommend a quantified change | Recommendation policy; simulate only when parameters are sufficient | Explicit only |
| Test a target weight/amount/share plan | One non-executable rebalance simulation | Explicit only |
| Review pending/open orders or historical activity | Trade-review workflow | Explicit only |
| Attribute historical performance | Performance-attribution workflow | Explicit only |
| Create/inspect/validate lifecycle facts | Position-lifecycle workflow | Explicit only |
| Save snapshot/report/history or journal | Relevant persistence command | Explicit only |
| Find or compare unheld candidates | Research only user-named names or requested candidates | Explicit only |
| Compare multiple scenarios | Run only the scenarios the user asks for | Explicit only |

“What should I do?” is actionable intent and loads recommendation policy, but do not invent a target
or simulate until the request supplies a quantifiable target weight, amount, or shares. If absent,
give supported recommendations directly. Ask for a missing decision parameter only when an
explicitly requested simulation actually needs it; evidence-based advice does not require a
quantified target.

Do not interpret a general portfolio review as permission to include open orders, history,
performance, lifecycle records, new-stock ideas, persistent artifacts, or multiple simulations.

## Six public-research routes

Select methods by the current question, independently of data-source selection in
[research-policy.md](research-policy.md). Candidate skills below are examples from an open host
catalog, not a whitelist or required installation set. Use a relevant available skill's applicable
steps; reuse instructions already injected or loaded in this round. Do not merely name a skill as
used. If none is available, continue the original analysis with existing evidence channels.

| Question | Trigger and main method selection | Required result |
|---|---|---|
| Operating and financial change | Selected common equity needs financial evidence. Use applicable futuapi financial/earnings-review steps, `yfinance-data`, or another relevant financial skill; reuse evidence before fetching gaps. | Growth, margins, cash flow, balance sheet and reporting periods; verify key facts against issuer material when needed. |
| Earnings expectations and revisions | Explicit request, or changing expectations are a principal thesis/risk issue. Use `estimate-analysis` or another relevant expectations method. | Forecast periods, consensus levels, coverage counts, revision windows and supporting evidence. |
| Holding thesis | Ordinary major-holding research needs business assumptions tested, or user asks about holding logic. Use applicable `thesis-tracker` steps or another thesis method. | Baseline, pillars, supporting/disconfirming evidence, changes and portfolio implications. If original rationale is absent, label a current research hypothesis; do not invent a buying rationale or read lifecycle/history automatically. |
| Before earnings | Confirmed release within the next 14 calendar days, or explicit request. Select one applicable `earnings-preview` main workflow by business-scenario/data needs, without brand priority. | Phase, expectations, business KPIs, disputed issues and verifiable scenarios. |
| After earnings | Release within the last 14 calendar days, latest earnings remain the core catalyst, or explicit request. Select one main workflow by depth: futuapi earnings-review framework, `earnings-analysis`, `earnings-recap`, or another relevant method. | Results versus verifiable expectations at the time, guidance changes, drivers and thesis implications. |
| Valuation | Explicit valuation request or valuation is the principal dispute. Check security, business, evidence and delivery constraints, then choose applicable `dcf-model`, `comps-analysis`, `company-valuation`, or another valuation method. | Appropriate method, period-matched inputs, actual calculations, assumptions, range and principal sensitivities; apply [evidence-policy.md](evidence-policy.md). |

Distinguish company-confirmed earnings dates from third-party estimates. An unknown phase is not a
confirmed preview or recap. The 14-day window is a routing default, not a validated investment
threshold. For the same release use its currently applicable phase by default. A requested before/
after comparison needs contemporaneous expectations; never backfill them with post-release consensus.

Prefer a specialized applicable workflow over redundant general retrieval: successful consensus
retrieval through `estimate-analysis` does not require repeating it through `yfinance-data`.
Applicable methods may analyze already acquired Futu evidence. Formal coverage reports, Excel,
stored thesis histories and upstream action labels are not implied by holding a stock; report the
modules actually used within the current portfolio task. ETF research does not use company DCF;
held options retain the existing options path. Research on the underlying company is not a complete
assessment of the option contract.
