# Portfolio Analyst v0.8.0 execution plan

Approved scope: the user requested implementation after review of the
[revision 6 PRD](../portfolio-analyst-v0.8.0-prd-rev6.md) and
[T1–T8 implementation plan](../docs/superpowers/plans/2026-10-10-portfolio-analyst-v0.8.0.md).
Those documents remain the requirements and design authority. This file indexes execution;
[todo.md](todo.md) is the task status record, not a second specification.

## Baseline and boundaries

- Start: `a50e3e1`, product `0.7.1`; branch `codex/portfolio-v080-research`.
- Existing untracked PRD and plan belong to the user and are retained.
- Same-session baseline: 1227 tests passed; the one loopback-listener test passed when
  retried with local socket permission. Live doctor also passed with normal SDK log access.
- Preserve the account helper, audited Futu adapter, schemas and risk/technical/options engines.
  Do not read a real account for synthetic research acceptance.
- Production work is skill instructions and references; change evaluation code only for a
  demonstrated observation gap. No new research provider, default dependency or service.
- The old plan's Superpowers runners are unavailable. Execute its approved scope using
  spec-driven-development, planning-and-task-breakdown and incremental-implementation.
- Local development and verification are authorized. No remote push or release is implied.

## Capability ownership and build order

| Slice | Responsibility | Depends on | Original task |
|---|---|---|---|
| baseline | Runtime and regression evidence | — | T1 |
| research-collaboration | One public research question uses an available skill without changing accounts | baseline | T2 |
| research-routing | Six question routes, source reuse and Futu output limitations | research-collaboration | T3 |
| evidence-quality | Traceable facts, correct periods and reproducible valuation | research-collaboration | T4 |
| portfolio-synthesis | Single report, stable headings, thesis and shared portfolio drivers | research-routing, evidence-quality | T5 |
| research-validation | Representative inputs, observed behavior and a public-data smoke run | portfolio-synthesis | T6–T7 |
| version-integration | Version consistency, distribution checks and verified limitations | research-validation | T8 |

Routing and evidence references can be authored separately after the common contract is fixed.
The primary developer owns the shared entry point, workflow, tests and final integration.
Checkpoints follow collaboration, synthesis, and final validation.

## Verification strategy

Use the component `.venv/bin/python` for pytest and the root interpreter for SDK diagnostics.
An existing cached Ruff binary is available; no installation is required. Keep the three offline
account/report cases isolated. New research cases use synthetic account context and public or
fixed synthetic research data with retained host receipts. Existing test passes do not prove
model behavior; public research access does not prove real-account end-to-end coverage.

Primary risks: contradictory old web-only wording; model self-reported success; source-period
mixing; over-expansion into provider code; alternate report headings. Focus verification on those
behaviors and retain failed runs without rewriting their evidence.
