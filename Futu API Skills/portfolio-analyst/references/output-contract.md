# Markdown chat output contract

The default deliverable is readable Markdown in chat with no default persistence. Do not attach raw
JSON or claim that a report/snapshot was saved. Keep the overview compact and the holdings complete.

## Required section order

1. **Scope and data time** — REAL/USD scope, `snapshot_id`, `as_of`, research query time.
2. **One-line conclusion** — the most decision-relevant portfolio-level finding and its limitation.
3. **Overview** — NAV, cash/cash weight, filled holding count, concentration, current costs, market
   values, unrealized P&L and contribution where supported.
4. **Three principal risks** — exactly three, each tied to affected positions and evidence.
5. **Complete holdings table** — every nonzero current filled holding.
6. **Selected deep research** — why each name was selected, evidence, thesis/invalidation,
   redundancy, cycle/macro sensitivity, and technical context. Use the bound `technical` report's
   observed regions; do not restate them as targets or predictions.
7. **Option risk** — only if applicable; otherwise omit the section.
8. **Macro impact** — concise and connected to the current portfolio.
9. **Observations** — evidence-backed portfolio implications; recommendations only under the
   conditional recommendation policy.
10. **Data gaps and sources** — local degradations, omitted deep-research names, and source links.

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
