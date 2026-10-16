# Institutional input and interpretation

Read for full 13F collection, normalization or cross-manager comparison. [Evidence](evidence.md) owns general provenance/privacy; [tools](tools.md) owns actual commands.

## User universe

Configuration schema_version=1 contains universe_id and managers. Each enabled manager needs a CIK; optional fields are display_name, enabled, tags, notes, family_id, relationship_source, strategy_group and strategy_source. Normalize CIKs and reject duplicates. Empty lists never trigger a default fund pool.

User labels/notes are not verified strategy metadata. Resolve relationships from public evidence when needed; add verified metadata in same-run working inputs without silently rewriting the user's persisted configuration. family_id plus relationship_source establishes the declared sampling unit. Multiple configured CIKs in the same unit are excluded from independent voting until overlap is resolved. Unknown relationships remain observations.

Never add institutions without the user's request. Membership version excludes cosmetic notes/name changes. A different enabled membership/relationship set needs a common-sample comparison or a clearly labelled historical restatement.

## Complete holdings

Use a fixed calendar quarter and cutoff. All original and amended filings in the scope must be discovered before declaring coverage complete. RESTATEMENT replaces; NEW HOLDINGS supplements. Notice/combination reports, confidentiality, unresolved amendments and missing totals restrict comparison. A late disclosure describes its historical quarter, not a current trade.

Preserve original information-table rows and composite security identities: CUSIP, class, SH/PRN, put/call and instrument type. Amounts normalize once using original-unit diagnostics; full row count/value must reconcile. The reader checks required original XML numbers against SDK values to detect zero-filling or lost precision.

Main calculations cover identifiable common shares/ADRs. ETFs, options and principal amounts stay separate. Unknown instruments reduce eligibility. No implied option Delta, short exposure, cash or total fund AUM.

## Optional enrichment contract

analyze accepts a separate public JSON enrichment object. Missing evidence is allowed and limits signals. The agent must obtain real evidence before marking verified=true; sample fixture URLs are allowed only for explicitly synthetic bundles.

```json
{
  "classification_version": "chosen-taxonomy-version",
  "classifications": {
    "CUSIP|CLASS|SH|NONE|COMMON": {
      "industry": "verified industry",
      "themes": ["verified business theme"],
      "source": "https://issuer-or-provider.example/source",
      "as_of": "2026-06-30",
      "available_at": "2026-08-01"
    }
  },
  "intervals": {
    "2026-03-31:2026-06-30": {
      "action_check": {
        "verified": true,
        "source": "https://qualified-provider.example/actions",
        "start": "2026-03-31",
        "end": "2026-06-30",
        "available_at": "2026-08-01"
      },
      "actions": {},
      "prices": {},
      "unresolved_actions": []
    }
  }
}
```

URLs above are explanatory placeholders, not usable evidence. Keys are exact security_id values returned by the reader/analysis. A complete action_check states that corporate actions were checked over the whole relevant scope; it does not mean a guessed empty actions map is safe.

An action maps an old security ID to current_id with a positive factor, effective_date, verified=true and source. Only confirmed comparable transformations belong here; complex mergers/spinoffs without a defensible mapping go in unresolved_actions. Prices require value, as_of equal to the current quarter end, available_at, verified and source. Prices are used for exited securities; missing prices never become zero.

If SDK unit diagnostics remain ambiguous, verify the original filing's monetary convention before supplying `filing_reviews`: accession → {unit: USD or USD_THOUSANDS, verified, source, available_at}. A review sets the raw-unit interpretation once, retains the original diagnostic and records its source; it does not edit the collected bundle. No review means the ambiguity remains.

For an UNKNOWN security class, `security_types` maps its original security_id to {instrument_type: COMMON/ADR/ETF/OTHER, verified, source, available_at, as_of}. Use actual public security-type evidence. It cannot change SH/PRN or put/call identity. The reviewed type becomes part of the effective security_id used for classifications; original type and row/source remain recorded.

Industry is mutually exclusive, themes may overlap and cannot be summed into total exposure. Optional size labels require a consistent market-cap basis. Optional style labels require a declared style_method with metrics, universe, buckets, source, as_of and available_at; no generic low-PE shortcut. Historical mode needs historically available classifications; freeze classification for the comparison and identify any current-classification restatement.

## Metrics and status

Compare split-adjusted quantities. Value change alone does not establish buying. The common-price allocation measure revalues both periods at the same quarter-end prices; it removes price drift but cannot identify subscriptions, mandates or intent.

Report original disclosed totals, supported-equity totals and comparable-subset coverage separately. Qualified manager votes require complete scope, verified actions and >=90% price coverage in both periods; each dimension needs >=90% classified weight. Primary breadth is manager-equal weighted.

Default RESEARCH_LEAD thresholds: >=5 independent comparable managers, >=80% enabled-unit coverage, >=60% same direction and a consistent median. These are configurable design criteria for research selection, not statistical significance or a trading rule. The current implementation exposes the fixed V1 thresholds; changing them requires a reviewed code/config extension.

Two or more adjacent changes count as persistent only on one fixed common cohort satisfying those conditions. Missing managers never vote unchanged. Small pools still produce per-manager observations. Retain opposing managers, weak magnitudes and no-signal conclusions.

Classify first appearances as NEW_REPORTED and disappearances as NO_LONGER_REPORTED within complete public coverage. They do not prove inception/liquidation of all real holdings. Pass resulting research questions to company research or local portfolio review only when requested.
