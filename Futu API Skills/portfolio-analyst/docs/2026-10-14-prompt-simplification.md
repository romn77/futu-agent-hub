# Portfolio Analyst 顾问质量与指令精简实施记录

日期：2026-10-14。状态：实施中。依据根目录 spec 修订 3 及 PS/RQ 任务清单。
此文件记录本轮真实结果；未运行项目不借用旧版通过记录。

## 实施基线

- 用户授权：在确认三类顾问需求后，要求给出计划并按任务执行开发。
- 开始时分支 `main`，提交 `9e9c491`；现开发分支 `codex/portfolio-advisor-quality`。
- 开始时只有本轮 spec/plan 未跟踪、`tasks/plan.md` 与 `tasks/todo.md` 修改。
- 组件版本保持 `0.8.0`。`.agents/skills/portfolio-analyst` 是指向本组件的现有符号链接。
- B0 冻结目录：`/private/tmp/portfolio-advisor-quality-ctbrfxpd/baseline`，177 个白名单源文件。
  相邻 `baseline-manifest.json` 记录起点提交与逐文件 SHA-256，`baseline-git-status.txt` 记录初始状态。
  未复制真实账户、凭据、用户报告或输出目录。
- 默认必读基线：12 文件、1,105 行、10,743 词、77,875 字符。最终上限 6,900 词/54,512 字符，
  计入所有默认要求读取的内容；顾问条件路径另外统计。
- 本地 Codex CLI `0.154.0`；质量对照将对两个版本显式指定同一模型和推理配置，并保留参数。

## 验证记录

| 阶段 | 实际命令/操作 | 当前结果 |
|---|---|---|
| 基线回归 | 组件 `.venv/bin/python -m pytest` | 1325 passed；1 个回环监听测试因 sandbox 的 bind 权限失败，18.57 秒 |
| 基线环境复核 | `.venv/bin/python -m pytest tests/test_doctor.py::test_worker_rejects_unrelated_tcp_listener_when_handshake_fails`，允许本机回环绑定 | 1 passed，0.02 秒；未连接账户或外网 |
| 加载/预算新测试 | `tests/test_prompt_loading.py` | 旧版 RED 4 failed/10 passed；最终 14 项全部通过 |
| 9 个行为场景 | 保留现有 3 核心 + 5 研究 + 1 点名场景 | 尚未执行 |
| 质量对照 | Q01–Q09，2 版本 × 2 重复 | 9 案已冻结，251 次公共 reader 本地兼容检查通过；模型执行待明确外发批准 |

## 规则迁移与验证对应

以下为实施前建立的迁移索引；状态只有实际完成并核对后更新。

| 不变量/内容 | 原详细位置 | 目标位置 | 主要验证 | 状态 |
|---|---|---|---|---|
| INV-01 账户只读 | SKILL/workflow/tool contracts | 保留入口短边界与工作流 | safety/manifest/helper | 已迁移（本地验证） |
| INV-02 单次快照 | SKILL/workflow/evidence | workflow + evidence 字段绑定 | helper/模型回执 | 已迁移（本地验证） |
| INV-03 账户选择 | SKILL/workflow/snapshot contract | workflow + snapshot contract | 账户选择回归 | 已迁移（本地验证） |
| INV-04 NAV/null | portfolio/evidence/snapshot contract | portfolio + snapshot 字段契约 | snapshot/缺失值回归 | 已迁移（本地验证） |
| INV-05 完整持仓 | SKILL/portfolio/output | portfolio + output | 37 持仓场景 | 已迁移（本地验证） |
| INV-06 深研/预算 | SKILL/research/intent | research；FR-13/17 显式调整 | 默认/扩围/机会场景 | 已迁移（本地验证） |
| INV-07 六类研究/期间 | research/evidence | research + 通用 evidence | 协作/财报样本 | 已迁移（本地验证） |
| INV-08 复用与来源 | workflow/research/core research | research + core research | 协作/降级样本 | 已迁移（本地验证） |
| INV-09 隐私与不可信内容 | SKILL/evidence/core research | evidence + 请求前短提醒 | 隐私/注入样本 | 已迁移（本地验证） |
| INV-10 fatal/局部失败 | workflow/failure | failure + 工作流结果分支 | helper 回归 | 已迁移（本地验证） |
| INV-11 计算与估值 | evidence | 通用 evidence + 条件 valuation | 有效/无效估值样本 | 已迁移（本地验证） |
| INV-12 期权状态/缺口 | options policy/contract/output | 保留并补单位异常核验 | 期权/质量 Q07 | 已迁移（本地验证） |
| INV-13 条件流程 | SKILL/intent/recommendation | intent + 条件 recommendation | 路由/模拟/顾问场景 | 已迁移（本地验证） |
| INV-14 报告/同行范围 | output | output | parser/完整报告 | 已迁移（本地验证） |
| INV-15 阈值非动作 | portfolio/recommendation | portfolio + recommendation | 模型语义审查 | 已迁移（本地验证） |
| 文件清理实现 | workflow/failure | implementation/owned-state | 原文迁移、helper 测试 | 已迁移（本地验证） |
| snapshot worker/兼容实现 | core/snapshot | implementation/snapshot | 原文迁移、链接测试 | 已迁移（本地验证） |
| technical 算法/重放 | core/technical | implementation/technical | 原文迁移、technical 回归 | 已迁移（本地验证） |

## 结果解释与边界

本轮生产范围为 skill 指令与参考；测试/评测只为真实验证需要调整。账户/风险/技术/期权
计算引擎、schema 和 Futu 上游不在修改范围。实例和质量输入使用独立合成数据。
研究质量需符合 spec 的双版本盲评门槛；词数或 Python 测试通过不能替代该结论。
未推送、发布或改变全局配置。回滚限定本轮分支的指令、评测和文档，不涉及账户数据迁移。

## 当前实现结果

- 默认必读 **6,753 词 / 52,698 字符**，相比原 10,743 词下降 **37.14%**；12 个默认入口不变。
- 估值细则按计算触发；清理、snapshot worker 与 technical 算法详情放入维护参考。
- 主动顾问支持鲜明机会、条件仓位与收益风险情景，以及明确意图下 2 个新候选/1 种特殊策略；
  全报告 2 个完整估值名额共享，既有会话偏好复用。期限适配、研究资源与单位异常核验已加入。
- 新增 `named-technical-gap`，扩围样本分开检查未选中与已选中但无数据。
- 核心账户/计算代码逐字节保持：49 个 runtime、2 个 scripts、13 个 schemas 文件与 B0 相同。
- 本地最终完整回归（允许测试所需回环绑定）：**1340 passed in 18.13s**。
  重点组合测试此前 **142 passed**；仅本轮三个 Python 测试文件 Ruff check/format 均通过。
- Skill `quick_validate.py`、隔离复制/链接/加载预算和 `git diff --check` 均通过。
- 独立静态审查指出两项一致性问题，均已修复：模拟需明确意图且目标可由用户确认；
  显著风险/复杂/点名基金不能被简单储备工具快捷路径排除。估值数学原段落完整迁移。

## 模型验证状态与审批边界

冻结质量输入位于 `/private/tmp/portfolio-advisor-quality-ctbrfxpd/quality-inputs`。
`cases.json` SHA-256：`b54c3d558bc28578176a7290652589248fe23c0a80145a081894c42d526c4932`。
本地兼容回执：`/private/tmp/quality-fixture-validated-zaqh1821`。操作者规则与答案不进入模型工作区。

基线链路探测 `baseline-core-probe-01` 在沙箱初始化 app-server 时因权限失败，
结果为 `EXECUTION_UNAVAILABLE`，没有行为通过结论。为正常初始化而请求的 probe-02
被自动审批审查拒绝，实际进程未开始：用户虽授权开发/评测，仍需明确允许向 Codex/OpenAI
外发具体 analyst 指令、实现/schema 与纯合成输入。未尝试其他工具或代理绕过拒绝。

用户随后明确回复“允许上述外发和合成评测”，授权范围包括已准备的 45 次及定位失败后的
同范围必要复测。授权记录在本地 `external-approval.json`；正式评测现在按冻结输入开始。
先前的初始化失败与审批拒绝记录继续保留，不重写为成功。

45 套具体输入位于 `/private/tmp/portfolio-advisor-quality-ctbrfxpd/prepared`，其中的
`approval-summary.md`、`payload-inventory.json`、`schedule.json` 和 `validation.json`
给出范围/哈希/随机配对顺序及本地检查结果。18 对的用户请求、公共数据和非指令文件一致，
全部 45 个账户 envelope 通过语义与身份验证；B1 指令聚合哈希为
`60ea15b24e81613daeb6faaba9d29843b24ead24b1b0a417c9158516c3810a07`。

45 次正式模型执行、独立语义审查及质量提升结论仍待实际结果。
实际模型请求将固定 `gpt-6-astra`、reasoning `medium`、CLI `0.154.0`、600 秒/8 MiB 上限；
后端具体 revision 尚未从执行回执取得，不能宣称冻结了不可见的服务端权重。
未执行的场景标为未执行，不记作通过；当前只能声明实现与本地回归完成。
