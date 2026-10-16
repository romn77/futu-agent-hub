---
name: institutional-style
description: Analyze a user-configured list of institutional managers' 13F holdings across quarters to identify allocation, industry and style research leads, with explicit sample coverage and disclosure limits.
metadata:
  version: 0.1.0
---

# Institutional Allocation Research

Investigate disclosed allocation changes in the user's institution list. Use only the managers they specify; do not auto-populate a famous-fund or largest-manager universe.

Read [workflow](references/workflow.md), [13F contract](../sec-research-core/references/institutional.md) and [shared evidence](../sec-research-core/references/evidence.md), reusing loaded content. Read [commands](../sec-research-core/references/tools.md) when executing the collector or calculations.

1. Resolve the supplied list and CIKs. No list means ask for the missing list/path; do not scan the user's directories. Use the empty [JSON template](config/managers.example.json) or [YAML template](config/managers.example.yaml).
2. Fix calendar quarters and a knowledge cutoff. Default four snapshots yield at most three adjacent changes. Explicit in-progress quarters are preliminary.
3. Acquire complete original and amended filings through the existing read-only helper. Preserve report-period, source, unit and completeness evidence.
4. Use deterministic normalization and calculations; supply only verified corporate-action, price, classification and manager-relationship enrichment.
5. Interpret sample breadth, magnitude, persistence and opposing evidence. Explain limitations and produce research questions.

MCP holdings/portfolio_diff summaries are navigation aids, not proof of full positions or amendment coverage. Missing records are not zeros. Independent votes require resolved manager overlap. Reported holdings do not reveal complete assets, short exposure, execution prices or motives.

Deliver a Markdown research report; no forced style call or buy/sell signal. Small lists and missing enrichment still support bounded observations. Company deep dives and portfolio review are follow-ups only within the user's requested scope; never read the brokerage account merely to run this skill.

Configuration edits, saved history, schedules and outbound notifications are separate user intents. This skill neither trades nor automatically changes the user's institution list.
