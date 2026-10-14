# Prompt quality and simplification tasks

日期：2026-10-14。修订 3：共 18 个任务，用户已授权实施；分支 `codex/portfolio-advisor-quality`。
需求：[spec](../docs/superpowers/specs/2026-10-14-portfolio-analyst-prompt-simplification.md)。
设计与依赖：[plan](../docs/superpowers/plans/2026-10-14-portfolio-analyst-prompt-simplification.md)。

除特别注明外，文件路径与命令工作目录均相对 `Futu API Skills/portfolio-analyst/`。
统一验证入口使用现有 `.venv/bin/python`；不安装依赖。任务完成时在本节记录实际命令、
结果与证据路径，不使用下方旧版记录代替。每个任务连同相关测试作为一个可审查切片。

## PS-01 固定规则与测试基线

依赖：无。规模：S，2 个文件。对应 spec：基线、INV-01–15、AC-02/12。

- [x] 记录 `main@9e9c491` 起点与初始规划改动；B0 的 177 个源文件和哈希已冻结于本地临时目录。
- [x] 实施记录已建立 INV-01–15 与估值/实现细节的原位置→目标位置→验证对应，迁移状态待逐项更新。
- [x] 基线 1325 passed，唯一回环端口权限失败的测试经对应权限复核后 1 passed；未修改账户/交易代码。

验证：`.venv/bin/python -m pytest`；`git status --short`；`git diff --check`。
读取量按 spec 的 12 个 D0 文件分别执行 `len(text.split())` / `len(text)` 汇总；新增默认依赖一并计数。
文件：新增 `docs/2026-10-14-prompt-simplification.md`；根目录 `tasks/todo.md` 的本轮记录。

## RQ-01 冻结研究质量对照基线

依赖：PS-01；必须先于任何生产 prompt 修改。规模：M，3 个文件及本地冻结副本。对应 spec：6.1、AC-17。

- [x] 冻结 B0 完整指令、运行代码、模型明确版本/配置、数据、权限及执行预算，确定 B1 唯一允许差异是本轮指令。
- [x] 为 Q01–Q09 准备独立合成请求/证据和隐藏的评分锚点，覆盖顾问适配、单位异常、明确机会/仓位及新候选/策略；不复制用户真实报告。
- [x] 写清隔离准备、随机顺序、每场景两次双版本执行、盲评解盲与全部门槛；锁定输入后不得按结果反向选题。

验证：`python3 -m json.tool evals/quality-fixtures/cases.json`；按 `evals/research-quality.md` 的准备步骤
检查每案的账户绑定、数据和允许替代结论；检查模型工作区不含评分/答案文件，两个版本除指令外相同。
本任务不调用模型；旧版可在 RQ-04 中从冻结副本运行。
文件：新增 `evals/research-quality.md`、`evals/quality-fixtures/cases.json`、
`docs/2026-10-14-prompt-simplification.md`。冻结副本放新的本地临时目录，不纳入真实账户或凭据。

## PS-02 拆出按需估值规则

依赖：RQ-01。规模：M，4 个文件。对应 spec：FR-02/04，INV-11，AC-06/07。

- [x] 创建 `valuation-policy.md`，完整迁移模型适用性、FCFF/FCFE、符号、折现/终值、股权桥接和敏感性规则。
- [x] evidence 保留通用财务口径及实际计算要求；research/evidence 明确在目标价/区间计算之前加载新文档。
- [x] 用引用和触发条件检查验证迁移；无计算的背景研究不被要求先读完整估值细则。

验证：`.venv/bin/python -m pytest tests/test_prompt_architecture.py`；按原文逐项核对估值迁移表。
文件：`references/evidence-policy.md`、`references/valuation-policy.md`、`references/research-policy.md`、
`tests/test_prompt_architecture.py`。

## PS-03 缩短一次采集工作流

依赖：RQ-01；顺序整合 PS-02 的共享测试文件。规模：M，4 个文件。对应 spec：FR-01/03，INV-02/10。

- [x] workflow 保留账户选择、一次 helper 调用、结果检查、研究与报告步骤，移出清理实现过程。
- [x] 新维护参考保留权限、device/inode、白名单、逐次校验及 TOCTOU 限制，不改变任何程序保证。
- [x] workflow/failure 仍明确 fatal 停止、组件局部降级、清理失败保留状态和报告路径；不指示模型重写清理代码。

验证：`.venv/bin/python -m pytest tests/test_prompt_architecture.py tests/test_current_analysis_helper.py`。
文件：`references/workflow.md`、`references/implementation/owned-state.md`、
`references/failure-and-degradation.md`、`tests/test_prompt_architecture.py`。

## Checkpoint A

- [x] PS-02/03 验证通过；迁移前后所有估值/清理约束均有去向，运行时短提醒足够指向原规则。
- [x] 未修改 helper/生产 Python；有新发现先修复对应切片再继续。

## PS-04 拆出快照与技术算法说明

依赖：PS-03。规模：M，5 个文件。对应 spec：FR-03，INV-03/04，AC-01/12。

- [x] snapshot contract 保留账户选择、NAV、null、绑定和重要字段语义；worker 与兼容实现说明移到维护参考。
- [x] technical contract 保留选择/上限、周期、截止时间、可用性和输出意义；精度与推导重放移到维护参考。
- [x] 原细节可由相对链接访问、完整 skill 复制包含新文件；日常路径不要求读取算法全文。

验证：`.venv/bin/python -m pytest tests/test_prompt_architecture.py tests/test_technical_analysis.py tests/test_technical_cli.py tests/test_codex_eval_producer.py`。
文件：`references/tool-contract/core/snapshot.md`、`references/tool-contract/core/technical.md`、
`references/implementation/snapshot.md`、`references/implementation/technical.md`、`tests/test_prompt_architecture.py`。

## PS-05 统一研究与失败规则归属

依赖：PS-02、PS-04。规模：M，5 个文件。对应 spec：规则归属、INV-06–10。

- [x] 研究选择、8/2 预算、六类问题、证据复用只在 research-policy 详细定义，相关文件使用短摘要/引用。
- [x] portfolio-policy 保留组合解释/阈值；工具契约只管能力/许可；failure 统一纠错、拒绝、局部与 fatal 处理。
- [x] 缺 skill 不阻断、真实拒绝不可绕过、日历窗口和来源复用语义保留；不增加必填状态或许可流程。

验证：`.venv/bin/python -m pytest tests/test_prompt_architecture.py`；按规则归属表与 INV 人工对照。
文件：`references/research-policy.md`、`references/portfolio-analysis-policy.md`、
`references/tool-contract/core/research.md`、`references/failure-and-degradation.md`、`tests/test_prompt_architecture.py`。

## Checkpoint B

- [x] PS-04/05 验证通过；维护参考完整可达，各文件没有相互冲突的预算、触发条件或错误处理。
- [x] 检查必读量的阶段变化，剩余差距在当前任务内定位；不以删减不变量换取长度达标。

## PS-06 精简入口和报告说明

依赖：PS-05。规模：M，4 个文件。对应 spec：FR-01/02，INV-05/13–15。

- [x] SKILL 保留角色、简短边界和默认/条件入口，intent-routing 统一明确意图；D0 路径与必读地位不变。
- [x] output 保留十部分顺序、条件期权、完整表列、中文/英文标题、同行范围和状态标签。
- [x] 不强制动作标签；建议与模拟分离；不因简化报告而删除未研究持仓或反证/来源/缺口。

验证：`.venv/bin/python -m pytest tests/test_prompt_architecture.py tests/test_codex_eval_producer.py tests/test_eval_runner.py`。
文件：`SKILL.md`、`references/intent-routing.md`、`references/output-contract.md`、`tests/test_prompt_architecture.py`。

## PS-07 明确技术覆盖差异

依赖：PS-06。规模：M，4 个文件。对应 spec：FR-05，AC-09–11。

- [x] 研究与技术契约明确使用实际 R/T 集合，区分未入选、非适用类型、数据缺失和组件失败。
- [x] 重点研究旁展示该标的技术缺口，末节汇总实际名称；工具状态和原报告章节不变。
- [x] 扩围或点名不触发第二次 helper、另跑 technical 或外部补算；不承诺已透传点名参数。

验证：`.venv/bin/python -m pytest tests/test_prompt_architecture.py tests/test_technical_cli.py tests/test_current_analysis_helper.py`；
检查 spec FR-05 各行的报告表述能由当前结果支持。
文件：`references/research-policy.md`、`references/tool-contract/core/technical.md`、
`references/output-contract.md`、`tests/test_prompt_architecture.py`。

## Checkpoint C

- [x] 现有章节/持仓/账户门禁通过；明确 9 个深研与技术最多 8 个不是同一个完成承诺。
- [x] 点名缺口、全组件失败、单指标不足分别有明确且不造数的处理。

## RQ-02 加强风险排序与关键传导

依赖：PS-07。规模：M，4 个文件。对应 spec：FR-08/09、AC-13/14。

- [x] 三个风险槽位按组合重要性讨论，说明共同驱动、仓位/时效和优先理由；不只按浮亏、热度或阈值排。
- [x] 每个重点标的围绕 1–2 个关键问题连接事实、经营、盈利/现金流、预期/估值和组合意义，标出不能证明的环节。
- [x] 保留工具 top_risks/状态/数字，最终报告风险排序不反向改技术选择；深研按 FR-13 分配，不回写计算或重采账户。

验证：`.venv/bin/python -m pytest tests/test_prompt_architecture.py tests/test_codex_eval_producer.py`；
用不属于正式验收集的简短开发例子审查共同暴露与“经营好不等于价格便宜”的解释要求。
最终效果由 RQ-04 验证，不能用文档出现关键词证明研究提升。
文件：`references/portfolio-analysis-policy.md`、`references/research-policy.md`、
`references/output-contract.md`、`tests/test_prompt_architecture.py`。

## RQ-03 加强反证与决策参考

依赖：RQ-02。规模：M，4 个文件。对应 spec：FR-10/11、AC-15/16。

- [x] 核心观点回应实质反证，区分已经取得的相反事实和未来失效条件；无历史理由仍标本次研究假设。
- [x] 一句话结论和观察提供主要判断、证据限制及具体复核触发，不将通用风险提示当作完成论点检验。
- [x] 明确建议请求给出组合依据、取舍和适用/失效条件；普通诊断不越界，缺目标不自动问卷或模拟。

验证：`.venv/bin/python -m pytest tests/test_prompt_architecture.py tests/test_eval_runner.py tests/test_codex_eval_producer.py`；
用开发例子检查重要反证加入后规则是否要求回应、证据不足是否允许保留判断。
文件：`references/research-policy.md`、`references/recommendation-policy.md`、
`references/output-contract.md`、`tests/test_prompt_architecture.py`。

## RQ-05 顾问适配与研究资源分配

依赖：RQ-03。规模：M，4 个文件。对应 spec：FR-08/12/13、AC-18/19。

- [x] 区分资金兑现期限与复核窗口；缺用户风险参数时限制个性化规模，但继续方向判断，不发起全面问卷。
- [x] 风险按组合量级排序，事件窗口不重复占位，小额复杂工具说明实际影响；主题纳入/排除口径一致。
- [x] 深研保留 8 个上限和候选来源，按影响/机会/分歧分配；简单储备工具可在概览核验，主要判断有相应研究支持。

验证：`.venv/bin/python -m pytest tests/test_prompt_architecture.py tests/test_codex_eval_producer.py`；
用开发例子检查储备资产较大时的研究分配，区分总覆盖与公司深研覆盖，确认技术 T 未因选择变化而被改写。
文件：`references/portfolio-analysis-policy.md`、`references/research-policy.md`、
`references/output-contract.md`、`tests/test_prompt_architecture.py`。

## RQ-06 鲜明机会判断与条件组合方案

依赖：RQ-05。规模：M，5 个文件。对应 spec：FR-15/16、AC-21/22。

- [x] 主动顾问请求及已有会话偏好触发建议规则，无需反复确认；有证据时给机会优先级、调整方向、资金取舍和失效条件。
- [x] 给出已知约束或明确假设下的仓位方案；按需实际计算基准/上行/下行，不捏造概率、市场共识或用户承受力。
- [x] 区分估值敏感性与调仓模拟，完整估值共享默认 2 个模型；提出建议不自动触发 simulate/交易或默认动用全部现金。

验证：`.venv/bin/python -m pytest tests/test_prompt_architecture.py tests/test_eval_runner.py tests/test_codex_eval_producer.py`；
检查“观点更鲜明/主动调整/新机会”意图与普通诊断的区别，核对条件方案不会被表述为已确认执行指令。
文件：`references/recommendation-policy.md`、`references/valuation-policy.md`、
`references/intent-routing.md`、`references/output-contract.md`、`tests/test_prompt_architecture.py`。

## RQ-07 金额单位与经济含义核验

依赖：RQ-05；与 RQ-06 顺序整合共享测试。规模：M，4 个文件。对应 spec：FR-14、AC-20。

- [x] 明确 COMPLETE 不能替代数量/报价单位/乘数/市值等交叉核验，异常须定位原字段语义。
- [x] 区分已证实基础不可靠和待核实局部歧义；不凭猜测乘除 100，不重采账户或更改权威 NAV。
- [x] 合成案例覆盖重复乘数、报价单位歧义和局部未知；保留既有 fatal 与局部降级处理。

验证：`.venv/bin/python -m pytest tests/test_prompt_architecture.py tests/test_current_analysis_helper.py tests/test_option_cli.py`；
逐条核对异常处理不要求修改账户/期权计算引擎，实际产品数值缺陷另行记录。
文件：`references/evidence-policy.md`、`references/options-policy.md`、
`references/tool-contract/core/options.md`、`tests/test_prompt_architecture.py`。

## RQ-08 新候选与事件进攻策略研究

依赖：RQ-06；与 RQ-07 顺序整合。规模：M，5 个文件。对应 spec：FR-17、AC-23。

- [x] 明确请求下研究最多 2 个未持有候选、1 种特殊策略，持有深研仍最多 8，全报告完整估值共享 2 个名额。
- [x] 新机会解释相对现有持仓的增量价值、催化/价格条件和反证；策略说明结构、期限、收益/损失来源与缺失的量化输入。
- [x] 无主动顾问偏好的普通诊断不扩围；候选不进入账户表或 T，不虚构合约/行情/保证金，不把 held-options 输出当新策略验证。

验证：`.venv/bin/python -m pytest tests/test_prompt_architecture.py tests/test_eval_runner.py tests/test_codex_eval_producer.py`；
检查普通诊断的原有 unheld-candidate 门禁保留，Q09 的显式扩围由独立手工质量场景验收，不篡改原场景期望。
文件：`references/research-policy.md`、`references/tool-contract/core/research.md`、
`references/recommendation-policy.md`、`references/output-contract.md`、`tests/test_prompt_architecture.py`。

## Checkpoint Q

- [x] FR-08–17 有明确归属；观点、调整建议、新机会均有实质内容，交易执行、技术引擎及账户边界未扩展。
- [x] 开发样本与正式 Q01–Q09 分离；Q08 有充分证据仍只观察不能通过，Q05 合理保留判断不被误罚。

## PS-08 验证加载关系和文字预算

依赖：RQ-07、RQ-08。规模：M，4 个文件。对应 spec：FR-07，AC-01/02。

- [x] 在测试内检查 D0、条件估值、维护参考和相对链接；无需生产 manifest、注册中心或递归自动加载。
- [x] 含新增研究质量规则的基础必读 ≤ 6,900 词且 ≤ 54,512 字符；所有默认依赖计入，另记条件路径成本。
- [x] 断言检查规范语义的结构位置与关键约束；更新旧定位但不删除安全/账户门禁，完成规则迁移复核。

验证：`.venv/bin/python -m pytest tests/test_prompt_loading.py tests/test_prompt_architecture.py tests/test_codex_eval_producer.py`。
`test_prompt_loading.py` 是本任务新增的加载/链接/预算测试，规划阶段尚不存在。
文件：`tests/test_prompt_loading.py`、`tests/test_prompt_architecture.py`、`evals/README.md`、
`docs/2026-10-14-prompt-simplification.md`。

## PS-09 准备扩围与点名缺口样本

依赖：PS-07。规模：M，最多 5 个文件。对应 spec：AC-06/09/10。

- [x] expanded-scope 的独立判据比较 fixture 实际 R/T，不假定第九个名称就是遗漏对象；保留 9 深研/3 估值请求。
- [x] 新 named-technical-gap 优先使用已核实的 US.NVDA，明确只对它深研；复核其持有、未入 T、有研究输入，完整表格不变。
- [ ] 更新手工样本准备白名单、预期回执顺序与报告判据；用操作者侧反例确认漏读估值/伪造技术不会被通过。

验证：按 `evals/research-cases.md` 在全新临时目录准备上述 2 个样本，检查冻结结果的 T 与研究数据；
复用 fixtures 不联网、不调用真实账户、不把操作者判据放入模型 prompt。本任务只准备，PS-10 执行。
文件：`evals/research-cases.md`、`evals/research-fixtures/expanded-scope.md`、
新增 `evals/research-fixtures/named-technical-gap.md`、
必要时 `evals/research-fixtures/data.json`、`docs/2026-10-14-prompt-simplification.md`。
当前合成 financials/company-profile 已覆盖 US.NVDA，无需修改 data.json。大组合 T 中 8 条
技术记录均 UNAVAILABLE；扩围样本须分别识别 NVDA/TSLA 未入选与其他名称数据不可用。

## PS-10 集成回归与模型验收

依赖：PS-08、PS-09。规模：M，3 个记录文件；验证覆盖全组件。对应 spec：AC-01–12。

- [x] 全量 pytest、限定范围差异和实际变更 Python 的既有 Ruff 通过；不把另一路代码改动纳入本任务。
- [ ] 运行 3 个核心 + 5 个既有研究 + 1 个点名样本，保留实际读取/计算/调用回执和报告，并完成独立语义复核。
- [ ] 记录 AC-01–12、真实失败/未执行项、阅读量、能力边界与回滚；本任务通过只代表回归，质量改善由 RQ-04 验收。

验证：`.venv/bin/python -m pytest`；`git diff --check`；spec 第 5 节的 3 条模型命令；
`evals/research-cases.md` 中的 6 个研究样本流程。输出用新目录，旧失败不覆盖。
记录文件：`docs/2026-10-14-prompt-simplification.md`、`evals/README.md`、根目录 `tasks/todo.md`。
若验证发现实现问题，回到所属任务修复，不在本任务追加不明范围的代码清理。

## RQ-04 执行新旧报告盲评

依赖：PS-10；使用 RQ-01 冻结的 B0、样本和量表。规模：M，3 个记录文件，模型回执独立留存。
对应 spec：AC-13–23、6.1 的全部质量门槛。

- [ ] 固定模型、工具/数据、各案例研究和执行预算与用户请求，9 场景 × 2 版本 × 2 重复产生完整 36 份报告，随机顺序且无样本挑选。
- [ ] 评审者先盲评后解盲：五维等权，新版 ≥3.0/4、提升 ≥0.4、各维度均值不降，至少 6/9 场景改善且无单场景下降超过 0.25。
- [ ] 新版关键错误为零、其他 AC 通过；分别报告工程、行为和质量结果，未满足改善标准就明确“未证明提升”。

验证：按 `evals/research-quality.md` 的双版本准备/执行/盲评流程，逐对核验输入和参数摘要、
原始回执、报告与评分理由，检查 Q03/Q04 反证、Q07 单位歧义、Q08 主动方案及 Q09 新机会。
Q08 充分证据仍泛泛观察时决策维度最高 2 分且 AC-21 失败。生成模型自评不能单独宣布通过；
合法执行但漏答计入分数，环境失败标该对不可比较，不能删去该场景后声称全量通过。
文件：`evals/research-quality.md`、`docs/2026-10-14-prompt-simplification.md`、根目录 `tasks/todo.md`。
失败若用于调参则记录该题已转回归集，为正式验收另备未用于调参的等价题；所有失败保留。

## Final checkpoint

- [ ] AC-01–23 有当前证据，最终指令阅读量、保留的不变量、显式行为调整与质量门槛同时满足。
- [ ] 原始失败和全部重复保留；没有可比模型对照就明确研究提升未证明，不声称全套完成。
- [ ] 本轮变更范围与回滚清楚；没有真实账户读取、生产算法/schema 改动、上游修补或自动发布。

## 本轮规划记录

- 2026-10-14：完成 spec、实施计划和 PS-01–PS-10 拆分；未开始上述实施任务。
- 2026-10-14 修订 2：按用户认可增加 FR-08–11、AC-13–17 和 RQ-01–04；先冻结旧版，再实施与盲评。
- 2026-10-14 修订 3：用户确认全部三类进取型信息；增加 FR-12–17、AC-18–23、RQ-05–08，并明确深研分配调整及候选/策略预算。
- 当前初次正式验收规模：9 次行为回归 + 36 份双版本质量报告，共 45 次模型执行；不含开发/必要复测，本轮均未运行。
- 规划基线 D0：12 文件 / 1,105 行 / 10,743 词 / 77,875 Unicode 字符。
- 下方 v0.8.0 清单在本轮开始时全部勾选，原文保留；其结果只代表历史实施。

---

# v0.8.0 implementation tasks

Scope and dependencies: [plan.md](plan.md). Status is updated only after the stated evidence exists.

## 1. Baseline (T1)
- [x] Record branch, starting commit, existing user files and same-session test/doctor results.
- [x] Run available Ruff and diff checks; map account invariants to existing tests.
- Verify: component pytest baseline; cached Ruff check/format; `git diff --check`.
- Files: execution records and evaluation README.

## 2. First research collaboration path (T2)
- [x] Update entry point, workflow and research contract consistently.
- [x] Available, relevant skills use existing authorized capabilities; already loaded content and
  evidence are reused; missing skills preserve the original analysis.
- [x] Account reads retain their exact CLI boundary; public SDK/MCP permission is independent of
  web availability, and actual denials cannot be bypassed.
- Verify: focused prompt-contract tests and manual consistency review.
- Files: `SKILL.md`, `references/workflow.md`, `references/tool-contract/core/research.md`,
  `tests/test_prompt_architecture.py`.
- Depends on: 1.

## Checkpoint: collaboration
- [x] Focused tests pass; owned helper and account implementation unchanged by this work.

## 3. Financial/thesis, earnings and valuation routes (T3)
- [x] Six routes have concrete triggers, evidence and expected results.
- [x] Eight-name/two-model defaults, explicit expansion, source reuse and public calendar windows
  are consistent; known Futu output limitations remain visible.
- Verify: routing-contract checks and representative behavior cases in task 6.
- Files: `references/intent-routing.md`, `references/research-policy.md`, optional focused
  `references/futu-public-research.md`.
- Depends on: 2.

## 4. Evidence and valuation quality (T4)
- [x] Privacy covers all public request channels; shared public market queries are allowed.
- [x] Necessary source/period/unit information supports key claims without mandatory metadata noise.
- [x] Valuation requires actual calculation, matching periods and valid assumptions; incomplete
  inputs restrict only affected conclusions.
- Verify: representative valid and invalid valuation, estimate and privacy inputs in task 6.
- Files: `references/evidence-policy.md`; optional focused valuation reference only if needed.
- Depends on: 2; may run alongside 3.

## 5. Portfolio synthesis and local degradation (T5)
- [x] One report contract uses stable headings, full holdings, thesis evidence and portfolio implications.
- [x] Missing history, optional skills, data, invalid models and budget omissions are distinct;
  preserve authoritative component status and fatal account/cleanup failures.
- [x] Advice and quantifiable simulation retain separate triggers.
- Verify: prompt tests; offline report behavior cases.
- Files: output, failure, portfolio and recommendation policies as needed.
- Depends on: 3, 4.

## Checkpoint: synthesis
- [x] Integrated contract review and focused tests pass; actual model regressions are tracked below.
- [x] No account core, Futu upstream or default dependency changes from this implementation.

## 6. Representative behavior verification (T6–T7)
- [x] Register concrete synthetic research inputs and observations for V01–V08 without weakening
  the three existing account/report cases or exposing live accounts to the runner.
- [x] Execute the three baseline model cases and representative research cases; inspect receipts,
  calculations and reports, recording exact pass/failure/unavailable status.
- [x] Exercise one authorized public research chain, recording actual coverage and source.
- Verify: real model/host tool receipts plus focused and full pytest, Ruff and diff checks.
- Files: `evals/evals.json`, `evals/README.md`, research samples; evaluation code/tests only for an
  observed gap.
- Depends on: 5; sample preparation may begin during 3–5.

## 7. Version, distribution and review (T8)
- [x] Synchronize entry point, package and runtime to 0.8.0 and update version assertions.
- [x] Verify complete skill directory/relative references and actual loading in fresh isolated
  Codex sessions. Global installation is unchanged; wheel building lacks setuptools.
- [x] Record tested scope, limitations, rollback and final review; do not claim untested sources.
- Verify: final pytest, Ruff, diff and package/entry-point checks; code quality review.
- Files: version declarations/test, evaluation README and release notes.
- Depends on: 6.

## Execution notes

- Development started from the user's approved PRD and plan. No real account data is needed for
  implementation or synthetic acceptance.
- T2–T5 production instructions implemented; routing/evidence tasks authored independently and
  integrated by the primary developer. The user subsequently authorized local commits of this
  completed v0.8.0 work; unrelated concurrent trading/account changes remain outside those commits.
- Fixed-base verification in `/private/tmp/portfolio-v080-validation-01`: 1245 tests passed,
  one Git-ignore test needed actual Git metadata in the copied fixture; after `git init` and copying
  the baseline `.gitignore`, that test passed separately. Ruff check/format and skill-link validation
  passed. A deliberate peer-scope mutation was caught by the new regression, then restored.
- Another session changed Futu trading/account files during shared-workspace tests, causing audit
  digest failures. Those changes were preserved and excluded from the fixed-base research overlay.
- Public Futu financials failed twice with `Network interruption.`; authorized issuer/SEC web
  fallback, calculations and synthetic-portfolio association succeeded. No live account was read.
- User explicitly approved sending the prepared analyst code/schema and eight synthetic cases to
  Codex/OpenAI, including needed failure-fix reruns, after an initial automatic approval rejection.
- First three real model runs produced reports but failed formal assertions: 17/18 comprehensive,
  16/20 options, 14/16 simulation. Actual receipts exposed Python `-B` handling and report-format
  observation gaps. Original artifacts are retained; corrected attempts passed as recorded below.
- Five separate research host-log samples completed and passed independent review; a launched
  sample alone is not a PASS. First-attempt failures remain unchanged alongside corrected reruns.
- Wheel build was attempted in the existing interpreter but `setuptools` is absent; no dependency
  was installed. Version/schema tests and full-directory reference checks are available separately.

## Final checkpoint

- [x] Shared repository full regression: **1286 passed in 17.13s**; all owned Python files pass
  Ruff check/format and all skill references resolve. Git diff whitespace checks pass.
- [x] Three real baseline model executions passed all **54/54** assertions and independent semantic
  review: comprehensive attempt 03, options attempt 03, rebalance attempt 02.
- [x] Five research cases passed observed host-receipt review: collaboration attempt 03,
  estimates/valuation attempt 01, degradation/boundaries attempt 01, expanded/default scope attempt 02.
- [x] Known limits and rollback documented in
  `Futu API Skills/portfolio-analyst/docs/2026-10-14-v0.8.0-implementation.md`.
- No real-account end-to-end run, global installation, push or public release was performed.
  The source implementation and approved development acceptance are complete. Futu public
  financials still returned network interruption; a public issuer-web fallback was exercised.
- Follow-up diagnosis: SDK/OpenD versions both 10.10.7008 and the local handshake succeeded, but
  `qot_logined=false` and `trd_logined=false`; ordinary quotes also returned Network interruption.
  The user will handle login/connectivity later and authorized committing the tested code now.

## Follow-up: reviewed pending fixes

- [x] Review the four commits on `codex/portfolio-v080` and the 21 pending files independently.
- [x] Preserve main's newer research/evaluation contracts while integrating the SDK fixes and
  useful older regressions; retain the original branch ancestry in a real merge commit.
- [x] Review trade guards/audit cleanup and exchange-qualified quote identity separately.
- [x] Reproduce and fix identity-local failures tripping the performance source circuit, and
  canonical CN builder inputs failing their own schema round-trip.
- [x] Integrated offline verification: 1348 tests and 91 subtests passed; portfolio Ruff checks pass.
- [x] Fresh real read-only verification: one helper invocation, same-source bindings, successful
  option metadata and quote reads, COMPLETE option status and guarded cleanup. No real trades.
- Remaining classification/OTC coverage and private configuration are not silently relabeled as
  complete. Detailed scope and review evidence: component `docs/2026-10-14-pending-fixes-review.md`.
