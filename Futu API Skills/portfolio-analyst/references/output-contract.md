# Markdown chat output contract

The default deliverable is readable Markdown in chat with no default persistence. Do not attach raw
JSON or claim that a report/snapshot was saved. Keep the overview compact and the holdings complete.

## Required section order

1. **Scope and data time** — REAL/USD scope, `snapshot_id`, `as_of`, research query time.
2. **One-line conclusion** — the most decision-relevant portfolio-level finding and its limitation.
3. **Overview** — NAV, cash/cash weight, filled holding count, concentration, current costs, market
   values, unrealized P&L and contribution where supported.
4. **Three principal risks** — exactly three evidence items, each tied to affected positions and
   evidence. Where a dimension cannot be assessed, explicitly show its data gap or lack of a
   confirmable risk; never invent a concentration conclusion to fill three slots.
5. **Complete holdings table** — every nonzero current filled holding.
6. **Selected deep research** — why each name was selected, evidence, thesis/invalidation,
   redundancy, cycle/macro sensitivity, and technical context. Use the bound `technical` report's
   observed regions; do not restate them as targets or predictions.
7. **Option risk** — only if applicable; otherwise omit the section.
8. **Macro impact** — concise and connected to the current portfolio.
9. **Observations** — evidence-backed portfolio implications; recommendations only under the
   conditional recommendation policy.
10. **Data gaps and sources** — local degradations, omitted deep-research names/reasons, and sources.

Use level-two Markdown headings with exactly one of the following titles in the report language.
Keep this order; the body may use natural language. Do not rename or combine sections. Put each
selected name in a level-three heading using its snapshot symbol (for example `### US.AAPL`), or
in a research table's first column named `Symbol`. When research is unavailable, retain each selected
name with its limitation. List names omitted by the research budget and the reason in Data gaps
and sources.

Within a required section, use level-three/four subheadings, ordinary bullets or bold paragraph
labels ending with a colon (for example `**Data gaps:**` or `**分析能力缺口：**`). A standalone
bold title without a trailing colon is a section heading, not an internal paragraph label.
Keep omitted names and their budget reason inside Data gaps and sources, including when that
section also contains evidence/source notes.

In the conditional Option risk section, explicitly distinguish `Tool status: <status>`
(`工具状态：<状态>`) for the authoritative options result from `Analysis capability gaps:`
(`分析能力缺口：`) for unavailable economic-risk fields. List actual gaps such as contract
multiplier, underlying and moneyness (ITM/ATM/OTM); a COMPLETE raw-Greek result does not fill them.
These labels preserve the scope of completeness; never change a source status to match prose.

For unheld peer comparisons, use a level-four `#### Peer comparisons` or `#### 同行比较` heading
under the relevant selected holding's level-three snapshot-symbol heading. This comparison scope
ends at the next level-four or higher heading. Keep peer prose and tables within that scope;
comparison names are not selected holdings or candidate recommendations. Do not place advice to add
an unheld name in that scope. A separate unheld research heading or a later proposal in Observations
remains an unheld-candidate mention. The declared label does not prove that the prose is only a
comparison: validation still requires semantic review of the content.

| Canonical section | Chinese heading |
|---|---|
| Scope and data time | 范围与数据时间 |
| One-line conclusion | 一句话结论 |
| Overview | 组合概览 |
| Three principal risks | 三个主要风险 |
| Complete holdings table | 完整持仓表 |
| Selected deep research | 重点研究 |
| Option risk | 期权风险 |
| Macro impact | 宏观影响 |
| Observations | 观察 |
| Data gaps and sources | 数据缺口与来源 |

## Research synthesis

For each selected holding organize the substance as question, evidence, explanation, portfolio
implication, and contrary evidence/gaps. Cover the relevant financial, expectations, earnings and
valuation routes at the agreed depth. Distinguish valuation context from an actually executed full
model; a provider target price is neither a self-built valuation nor an automatic action.

State the thesis baseline's source. With no user-provided or explicitly authorized historical
reason, label it a "current research hypothesis" (本次研究假设), not the user's original rationale.
Normally inspect two or three relevant pillars and at least one substantive potential disconfirming
fact. If none was obtained, say so rather than inventing a balanced-looking counterargument.

Explain shared demand, industry cycles, macro drivers and overlapping exposures across holdings.
Several positive company views do not establish diversification; qualitative overlap is not a
computed correlation coefficient. Associate public evidence with the bound portfolio locally.
Preserve technical observed regions, authoritative risk/option status and classification gaps.
Return one portfolio report, without standalone upstream research reports, workbooks or thesis logs.

## Baseline holding table

Use these conceptual columns, preserving explicit currency/unknown states:

| Symbol | Current weight | Market value | Unrealized P&L | Sector/theme | Short comment |
|---|---:|---:|---:|---|---|

The Short comment is 1–3 sentences grounded in portfolio context and available evidence. Do not add
Role, Inferred role, Score, Ranking, or categorical Attention status. Do not force
`ADD/HOLD/TRIM/EXIT`.
Sort deterministically by current weight descending, then symbol; identify shorts without treating a
negative quantity as a complete short-risk assessment.

## Source presentation

Account facts cite the same snapshot ID/as-of near the table or overview. Apply evidence-policy.md
to key external facts: necessary source, period and units, direct links to read news/announcements,
or actual tool-result locators when no data page exists. Shared provenance can be stated once.
Show a separate timestamp for a research quote; it never changes account weights or selection.
Put a material limitation beside the affected claim and group repeated gaps in Data gaps and sources.
Missing is “Unknown/Unavailable,” never 0. Briefly distinguish method source from numerical source;
do not expose account IDs, private paths, raw JSON, full logs or per-step status bookkeeping.

## Classification uncertainty and metric basis

Show known/unknown classification coverage beside any sector, region or split-theme constraint.
Keep BREACHED, WITHIN_LIMIT, UNKNOWN and NOT_CONFIGURED distinct. UNKNOWN is a missing-evidence
bucket, never an actual industry or a confirmed limit breach. Do not treat a null breach boolean
as false. Use the authoritative structured risk status in the final Markdown.

Label NAV HHI separately from holdings-only HHI/effective positions. Cash affects invested capital,
not the relative diversification metric; legacy inverse NAV-HHI reports require a legacy label.
