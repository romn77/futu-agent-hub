# Pending fixes: review and integration

Date: 2026-10-14. User authorized reviewing and integrating the unmerged commits and
the 21 pending working-tree files. Integration starts at main `d823b98` in a separate
worktree; original pending files were snapshotted before any edits.

## Branch reconciliation

`codex/portfolio-v080` contains four commits after the shared `a50e3e1` base:

- `d10e896`: earlier evaluation receipt parsing.
- `45a5601`: earlier research collaboration implementation and repository skill link.
- `4fa748f`: safe metadata subprocess runtime and JSON/log isolation.
- `65f6927`: SDK-compatible DRVT metadata and explicitly bound single-leg option quotes.

The merge preserves main's newer research policies, five research scenarios, fixed report
headings, strict shell-command checks and peer-comparison handling. It retains the older
branch's useful regressions, historical acceptance record, synthetic specimens and valid
repository skill symlink. Producer 1.0.6 additionally accepts the tested Chinese option-status
label with 为; producer behavior is not rolled back to the older 1.0.3 implementation.

Independent review found no required issues in the two SDK fixes. The metadata workers reuse
the existing safe environment allowlist and keep SDK output off the JSON channel. The SDK
uses DRVT for derivatives and does not echo code in its single-leg quote result. The audited
request binding supplies that provenance without inventing a vendor identity echo; malformed,
unbound, conflicting and prebound source responses remain invalid, including during replay.

## Pending working-tree changes

The trade validation group requires a valid explicitly selected account for REAL writes,
rejects non-finite/fractional or invalid numeric inputs, and records API rejections with a
single final cleanup. Existing trading enablement and confirmation controls remain intact.
The read-only analyst manifest pins the changed common module and its new pure validation
dependency together. No actual order, modification, cancellation or unlock was performed.

The quote-identity group retains actual SH/SZ source codes when normalization uses reporting
market CN. History requests use evidenced exchange identity; ambiguous legacy/conflicting
identity remains unavailable rather than guessing an exchange. Old snapshots retain their
serialized identity when the optional source field is absent.

Review found and fixed two concrete edge cases before integration:

1. In performance attribution, three unavailable CN identities previously counted as source
   failures and suppressed a later valid US history. A None result now leaves the source
   failure streak unchanged; real empty responses and exceptions still trip the circuit.
2. A canonical CN.* builder input previously populated source_code with a value forbidden by
   its SH/SZ schema. The builder now retains source_code only when the raw input actually
   names SH or SZ; canonical CN.* round-trips without fabricated exchange evidence.

Five focused regressions cover those cases and the preserved empty/error circuit behavior.
Three tests failed before the fixes; all 163 targeted tests passed afterward. The trade review
passed 22 offline tests and in-memory mutation checks. SDK review passed 186 focused tests and
binding-condition mutation checks. Reviewer changes were confined to the integration worktree.

## Integrated verification

From the integration component, using the existing component interpreter:

```bash
python -m pytest tests ../futuapi/tests -o addopts='' -q --tb=short
```

Result: **1348 passed, 91 subtests passed in 18.64s**. Portfolio component Ruff check and
format check passed (104 files); staged and unstaged whitespace checks passed. The restored
old option-status regression first failed against main and then passed with the narrow parser
compatibility correction; the focused producer/prompt/validator suites passed 116 tests.

A subsequent real, read-only owned-helper verification completed in 11 seconds. It invoked
the helper once, retained OPEND_TOTAL_ASSETS NAV authority, and validated all component source
IDs/times against the same snapshot. Option status was COMPLETE, quote and metadata reads
were SUCCESS, and guarded cleanup completed without component-call degradation. Real account
payloads, account identifiers, monetary amounts, holding lists and snapshot IDs were not saved
in this record, the repository, fixtures or an additional model evaluation.

Snapshot, risk and technical quality remain PARTIAL. Classification gaps and unavailable OTC
history remain real limitations; the US correlation reader's existing circuit behavior was
not silently changed as part of the separate CN/performance fix. User configuration was not
modified. COMPLETE option quotes do not imply every portfolio dataset is complete or establish
quote freshness beyond the existing UNVERIFIED label.
