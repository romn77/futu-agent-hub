# Markdown chat output contract

The default deliverable is readable Markdown in chat with no default persistence. Do not attach raw
JSON or claim that a report/snapshot was saved. Keep the overview compact and the holdings complete.

## Required section order

1. **Scope and data time** — REAL/USD scope, `snapshot_id`, `as_of`, research query time.
2. **One-line conclusion** — the most decision-relevant portfolio-level finding and its limitation.
3. **Overview** — NAV, cash/cash weight, filled holding count, concentration, current costs, market
   values, unrealized P&L and contribution where supported.
4. **Three principal risks** — exactly three evidence/gap slots, each tied to affected positions and
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
10. **Data gaps and sources** — local degradations, omitted deep-research names, and source links.

Use these stable heading choices in the report's language, preserving the order above:

| English | Chinese |
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

Within Selected deep research, organize each name as **Question — Evidence — Interpretation —
Portfolio impact — Counterevidence/gaps** (or **问题 — 证据 — 解释 — 组合影响 — 反证/缺口**).
State why it was selected and the thesis baseline's source. If no reliable historical rationale is
available, label it “Current research hypothesis” / “本次研究假设”; never infer the original purchase
reason or read lifecycle automatically. Ordinary deep research examines 2–3 relevant pillars and
at least one material counterexample that could weaken the conclusion; explicitly say when
counterevidence was not obtained.

Synthesize shared demand, industry/macro variables, and overlapping exposures across names locally.
Several bullish theses do not prove diversification; qualitative common drivers are not computed
correlation coefficients. Keep research quotes separately timestamped from snapshot table facts.
Return one portfolio report, not parallel standalone skill reports. Upstream target prices, long
reports, Excel outputs, or save instructions do not enlarge the deliverable. Explicit advice loads
recommendation policy; quantitative simulation requires sufficient goals and account fields.

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

Account facts cite the same snapshot ID/as-of near the table or overview. Changing facts show source
and source date; news/announcements use clickable direct links. Put a limitation beside any affected
claim, then repeat it concisely in Data gaps and sources. Missing is “Unknown/Unavailable,” never 0.

## Classification uncertainty and metric basis

Show known/unknown classification coverage beside any sector, region or split-theme constraint.
Keep BREACHED, WITHIN_LIMIT, UNKNOWN and NOT_CONFIGURED distinct. UNKNOWN is a missing-evidence
bucket, never an actual industry or a confirmed limit breach. Do not treat a null breach boolean
as false. Use the authoritative structured risk status in the final Markdown.

Label NAV HHI separately from holdings-only HHI/effective positions. Cash affects invested capital,
not the relative diversification metric; legacy inverse NAV-HHI reports require a legacy label.
