# Institutional research workflow

## Establish the sample

Read the explicit user-provided list/path, or the user-configured `config/managers.local.json` fallback relative to the skill. Validate with validate-universe. Resolve ambiguous institution names using public evidence and ask only if identity remains ambiguous. Store no inferred persistent preferences. User notes never go to data providers.

When the user asks to save a list, use the supported schema: map `name` to `display_name`, preserve user `group` and `priority` as `tags`, and retain `id`, `sec_filer_name`, `focus`, `watch_forms`, `analysis_warning` and `historical_cik_notes` in local `notes`. Read the raw configuration for these notes; the helper's public summary omits them. User groups are not verified `strategy_group` values, and priority tags do not exclude watch-tier managers. Keep incomplete source fields explicitly marked; never invent missing managers. Save personal configuration in the git-ignored local file; keep the distributed templates empty.

For the local fallback, if `config/managers.source.local.json` exists, read it as the user-supplied source, including its list version and snapshot metadata. `managers.local.json` is its schema-compatible projection for the helper; reconcile CIKs, names, enabled membership and notes before use, and regenerate it from the source when the user updates that source. Preserve `enabled` when supplied (default true); keep `universe_id` stable across routine membership updates because the helper computes a separate membership version. The source's `as_of`, `last_complete_quarter` and `next_quarter_deadline` are user-supplied snapshot metadata, not a permanently pinned research period or independently verified filing calendar.

Historical CIK notes require public verification of the entity/period relationships during research. The current collector reads configured CIKs and does not automatically stitch predecessor filings. Do not count historical CIKs as additional independent managers, sum overlapping filers blindly, or interpret a filer transition as a portfolio entry/exit. If continuity remains unresolved, report the historical gap and limit comparisons accordingly.

`watch_forms` retained in notes is descriptive only: the current collector handles 13F holdings/amendments and checks 13F notices. It does not collect 13D/13G filings or schedule monitoring. Preserve that distinction when describing what configuration enables.

Disclose the enabled set and list version. Check parent/subsidiary, included-manager and delegated relationships before independent aggregation. Strategy labels need evidence; unknown groups remain unknown. Small or correlated user pools are valid research scopes with narrower conclusions.

## Acquire and compare

Use collect with the list, target quarter, number of snapshots and frozen cutoff. When the optional analyst cache bridge is available, pass `--cache-mode prefer`. Collect rechecks the required manager directories and reuses normalized filings by manager/accession/content version; adding a manager or quarter fetches missing documents without refetching unaffected filings. Keep the selected-universe bundle in same-run temporary state, or export only if requested; the public cache stores individual public filings, not the user's selected list or notes. Review per-manager discovery errors and returned periods.

Use raw full holdings for all relevant originals/amendments. Validate summary totals, units and row completeness; reconstruct effective quarters before comparing. An updated filing of an older quarter is not a new current-quarter trade. Preserve partial managers instead of silently deleting them.

Collect public action/classification/relationship evidence only to fill actual gaps. Then run analyze on the bundle, with the same universe version/cutoff and optional enrichment. The public helper has no account inputs. Use its actual metrics and status; do not recompute by mental arithmetic or override missing coverage.

Analyze runs on the current full bundle and enrichment; do not reuse old calculations solely because `universe_version` is unchanged. Classification, relationships, action evidence, configuration revision and calculation method all affect the result. Preserve historical cutoff restrictions for amendments. `refresh_all` rechecks fixed documents too; `off` keeps standalone behavior and writes no cache. Bridge failures are explicit diagnostics, never permission to install a backend or change tools.

If overlap, corporate actions, price coverage or classification are unresolved, explain the individual disclosed changes and what data would resolve the question. Do not promote missing metadata into a marketwide trend.

## Interpret

Distinguish raw value/weight changes, share changes, common-price allocation changes and the possible explanations. Common-price revaluation removes price drift but not subscriptions, index effects or mandate changes.

Industry is a mutually exclusive classification; business themes overlap. Growth/value/size classifications need stated methods and dated external inputs. A sector increase alone does not prove a factor rotation.

For each important observation explain:

- Which managers and securities support it, and which oppose it.
- The exact quarters, breadth, size and continuity on a fixed common sample.
- Coverage, overlaps, timing limitations and alternative explanations.
- A concrete company/industry question that could confirm or weaken the hypothesis.

Default at most five expanded research leads. Include meaningful divergence or no-signal findings. Selection thresholds are research triage criteria, not alpha estimates.

## Output and follow-up

The helper's Markdown is a factual scaffold; add sourced interpretation and contrary evidence. Report scope/time, universe/coverage, main changes, underlying positions, research priorities and local gaps/sources. Keep exact filing links.

Send public company questions to company-research when requested. Portfolio matching occurs locally inside portfolio-analyst and must retain its one-snapshot, full-holdings, budget and advice-intent contracts. No forced end-to-end chain when the user requested only institution research.
