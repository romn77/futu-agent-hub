# Markdown chat output contract

Return one Markdown report in chat; no default saved artifacts or raw JSON. Use the required sections below.

## Required section order

Use these literal Chinese headings or `## ` followed by the exact English title, in order.
Omit Option risk only when inapplicable; do not rename or combine sections.

| Canonical section | Chinese heading | Content |
|---|---|---|
| Scope and data time | `## 范围与数据时间` | REAL/USD, bound snapshot_id/as_of including timezone and precision, separate research time. |
| One-line conclusion | `## 一句话结论` | Most consequential supported portfolio view and material qualification. In active advice, state the preferred opportunity/choice. |
| Overview | `## 组合概览` | NAV, cash/weight, reserve context, count, concentration, costs/values, unrealized P&L/contribution where supported. |
| Three principal risks | `## 三个主要风险` | Three supported risks or explicit gaps, ordered by materiality with affected holdings, mechanism and priority reason. |
| Complete holdings table | `## 完整持仓表` | Every nonzero filled holding, exactly once. |
| Selected deep research | `## 重点研究` | Selection reason, decisive question/evidence, thesis/counterevidence, valuation, shared drivers and actual technical coverage. |
| Option risk | `## 期权风险` | Conditional: preserve tool status and distinguish economic-risk gaps. |
| Macro impact | `## 宏观影响` | Short transmission to current exposures. |
| Observations | `## 观察` | Portfolio implications and concrete review triggers; conditional advice/candidates/strategies as requested. |
| Data gaps and sources | `## 数据缺口与来源` | Local gaps, omitted research names/reasons, actual coverage and source notes. |

Within sections use level-three/four subheadings, bullets, or bold paragraph labels with a trailing
colon, such as `**Data gaps:**`. Bold labels are paragraphs, not replacements for the required headings.
Keep omissions and their budget reasons inside Data gaps and sources, not an unrecognized appendix.

## Holdings and research presentation

| Symbol | Current weight | Market value | Unrealized P&L | Sector/theme | Short comment |
|---|---:|---:|---:|---|---|

Preserve currency and Unknown/Unavailable states. Sort by snapshot weight descending, then symbol.
Short comment: 1–3 evidence-based sentences. No Role, Inferred role, Score, Ranking, Attention status
or forced `ADD/HOLD/TRIM/EXIT` columns. Identify shorts without implying complete short-risk analysis.
Cash remains in Overview; researched candidates never enter this table.

Before sending, check headings once each in order against this loaded contract. Reconcile final rows
to the retained snapshot: weight, market value and unrealized
P&L must remain in their own columns, including unknowns. A correct total or complete symbol list
does not excuse a missing cell. Repair only the affected presentation; do not reacquire account data.

For each selected held name use a level-three snapshot symbol heading (e.g. `### US.AAPL`) or a
research table's first column named `Symbol`. Retain selected names even when evidence is unavailable.
Explain 1–2 decisive questions through evidence, interpretation, portfolio implication and contrary
facts/gaps. State thesis baseline source; absent historical rationale, write “current research
hypothesis” / “本次研究假设”. Do not invent purchase motives or balanced-looking objections.

Distinguish valuation background from executed models, and attributed provider targets from own
calculations. Separate technical nonselection, selected-but-missing fields and whole-tool failure.
Use observed regions with timeframe/as-of, never convert them into targets. Summarize affected
identities in gaps without reacquiring prices. Report company-research coverage separately from
total coverage including reserve assets.

For unheld peers use `#### Peer comparisons` or `#### 同行比较` inside the relevant held symbol's
section. Its scope ends at the next level-four or higher heading. Keep comparison prose/tables
there; it is not a candidate recommendation. Advice outside that scope remains a candidate mention;
labels alone never establish that the content is merely a comparison.

## Advisor conclusions

With actionable intent or an established advisor preference, apply recommendation policy inside
Observations. State prioritized opportunity/adjustment, comparator, rationale, catalyst/period,
trade-off and invalidation. Use compact prose or a useful comparison table, not mandatory scoring.
Sufficient evidence must produce a view; generic “continue observing” is not enough. Distinguish
directional advice, conditional allocation assumptions and a separately requested simulation.

Authorized new candidates and strategies belong in an explicit subsection here, separate from
held research. Show incremental value versus an existing holding/cash and strategy evidence limits;
do not imply those objects are held or validated by the held-options/technical tools. No standalone
upstream research report, workbook or saved thesis is part of the default deliverable.

## Sources, statuses and limitations

Account facts cite the same snapshot ID/as-of near the overview/table. External key facts follow
[evidence policy](evidence-policy.md): necessary period/units, direct supporting links or actual
tool-result locators, with shared provenance once. Do not expose account IDs, private paths, full
logs or per-step bookkeeping. State material gaps beside the claim and group repeated gaps at end.

In Option risk use separate explicit labels `Option tool status` / `期权工具状态` and
`Analysis capability gaps` / `分析能力缺口`. Copy authoritative options status with raw-Greek units,
coverage and freshness. Name actual missing multiplier, underlying mapping, moneyness (ITM/ATM/OTM)
or other fields; do not invent missing fields or downgrade COMPLETE solely for broader gaps.

Show classification known/unknown coverage alongside exposure claims. Preserve BREACHED,
WITHIN_LIMIT, UNKNOWN and NOT_CONFIGURED; UNKNOWN is neither an industry nor a pass/breach, and
null breach is not false. Label NAV HHI separately from holdings-only HHI/effective positions;
cash does not change relative holdings diversification. Mark legacy inverse NAV-HHI as legacy.
