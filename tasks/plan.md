# Current plan: Portfolio Analyst prompt quality and simplification

日期：2026-10-14。修订 3 已获用户开发授权；实施中，分支 `codex/portfolio-advisor-quality`，起点 `main@9e9c491`。

- Requirements: [Prompt simplification spec](../docs/superpowers/specs/2026-10-14-portfolio-analyst-prompt-simplification.md).
- Implementation plan: [Dated plan](../docs/superpowers/plans/2026-10-14-portfolio-analyst-prompt-simplification.md).
- Task status: [PS-01–PS-10 与 RQ-01–RQ-08](todo.md)，共 18 项。
- Scope: 精简指令、顾问适配、鲜明机会判断、条件组合方案及显式请求的新候选/策略；账户与计算引擎保留。
- Research budget: 普通持仓深研 8、完整估值 2；主动顾问可另研究最多 2 个新候选和 1 种特殊策略，估值共享原 2 个名额。
- Size target: 默认必读从 10,743 词 / 77,875 字符降至不超过 6,900 词 / 54,512 字符。
- Implementation order: PS-01 → RQ-01 → PS-02–PS-07 → RQ-02 → RQ-03 → RQ-05–RQ-08 → PS-08–PS-10 → RQ-04.
- Verification: 结构/引用检查、Python 回归、9 次行为回归，以及 9 场景 × 2 版本 × 2 重复的 36 份质量报告盲评。
- Quality target: 关键错误为零；新版 ≥3.0/4、提升 ≥0.4；五维均值不降；至少 6/9 场景改善且无单场景降幅超过 0.25；证据充分仍泛泛观察不能通过。
- 当前实施记录：[基线、迁移与验证](<../Futu API Skills/portfolio-analyst/docs/2026-10-14-prompt-simplification.md>)。
- 执行按依赖分工；主开发者维护共享入口/研究规则/整合，独立任务准备评测输入和加载测试。

下方保留已完成的 v0.8.0 实施计划与原始依据。其历史测试与授权记录不作为新工作的通过证据。

---

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
