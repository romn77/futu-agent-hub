# SEC 研究 V1 实现与验收

日期：2026-10-16。开发分支：`codex/sec-research`；已完成合并到 `main` 的验证。设计基线见[总设计](../../../docs/superpowers/specs/2026-10-16-sec-research/README.md)。

## 已实现

| 部分 | 入口与职责 |
|---|---|
| 独立公司研究 | company-research Skill；公开公司问题、财报／附注、论点与反证，不要求账户 |
| 机构配置调研 | institutional-style Skill；用户指定 JSON／YAML 机构池、季度持仓、可复核线索 |
| 共享读取 | scripts/sec_read.py；申报索引、指定 13F、正文分页、指定申报财务事实、有界批量采集 |
| 共享计算 | 原始信息表对账、修订合并、证券身份、公司行动、同价配置、集中度、样本广度与持续性 |
| Analyst 接入 | 条件引用、按事实域选择来源、同次证据复用、本地组合关联；账户 CLI 与引擎未改动 |

两个新 Skill 通过 `.agents/skills/` 相对链接注册；开发在独立 worktree 完成，合并后主工作目录也包含这些入口，全局 Skill 安装未修改。核心工具默认 stdout，只有显式 --output 才创建文件且不覆盖已有文件。JSON 离线分析只依赖标准库；YAML 是可选依赖。

## 验证结果

| 验证 | 实际结果 |
|---|---|
| 核心 pytest | **69 passed**，覆盖有效／错误输入、修订、缺失、单位、价格漂移、共同机构、分类时点、CLI 与资源恢复 |
| 现有 analyst 全套回归 | **1,398 项通过**；一项本地 TCP 测试在允许临时 loopback 监听的环境运行，未删改或跳过 |
| main 合并验证 | SEC **69 项通过**；包含主线新增宿主上下文测试的 analyst **1,444 项通过**；无合并冲突 |
| Skill 结构 | 两个 SKILL.md 均通过 quick_validate；引用和相对链接有效 |
| 构建 | 离线生成 sec_research_core-0.1.0-py3-none-any.whl，无新增依赖安装 |
| 默认 analyst 上下文 | 保持 12 份默认文档及既有字数／字符预算；独立安装仍可用 |
| 代码独立复核 | 两轮具体发现已对应反例测试与修复：管理人重叠、单位、时点、修订缺口、零价格、信号 ID／覆盖、SDK 恢复 |
| 变异检查 | 临时副本取消“零价格不可用”条件后，既有价格覆盖测试失败；原工作区源码未修改 |
| 独立 Skill 行为 | 四个合成问题得到可检查报告；利润现金反证、FPI 累计数、价格漂移、历史增补披露均正确区分 |
| Analyst 联合行为 | 一份三持仓合成报告；执行记录为 doctor 一次、current_analysis.py 一次，无第二次快照／技术调用或模拟 |

行为样例及原始输出：

- [公司研究案例](../../company-research/evals/cases.json)及其 `evals/results/2026-10-16/`。
- [机构研究案例](../../institutional-style/evals/cases.json)及其 `evals/results/2026-10-16/`。
- [组合联合报告](../../portfolio-analyst/evals/sec-integration/2026-10-16-report.md)与[执行记录](../../portfolio-analyst/evals/sec-integration/2026-10-16-execution.jsonl)。临时 fixture 文件 hash 在前后核对中未变化。

上述是合成行为验证，不是实际投资建议、真实账户联调或收益有效性证明。

## 真实只读 SDK 验证

使用现有 edgartools 5.62.0 解释器及已配置身份；未安装／升级包，未输出身份值，未访问证券账户。测试名单仅是临时技术样例，不是用户的研究名单。

- CIK 1067983 的 2026Q2 索引返回精确 accession `0001193125-26-352200`，覆盖状态完整。
- collector 在同一 cutoff 下读到 2026Q1 的 **90 行**和 Q2 的 **89 行**，两份文件 discovery_complete=true；本次读取响应体计 **81,716 bytes**。
- 其后 analyze 成功产生一组季度比较，并正确输出 **PARTIAL、0 条合格研究线索**。实际缺口包括金额单位歧义、未知证券类型、公司行动未核验与可比价格覆盖不足；没有将这些缺口填零或忽略。
- AAPL 指定年报 MD&A 返回总长 **18,238 字符**；200 字符测试窗口明确给出 truncated=true、next_offset=200。
- 指定 AAPL 年报的收入事实返回三个年度期间及其真实日期、单位、accession；没有误标成四个季度。

公开回执摘要与原始回执 hash 保留在[public-read-summary.json](../tests/fixtures/public-read-summary.json)。原始申报可从其中的 SEC 链接定位。完整临时运行材料可能由系统清理，不作为唯一复现方式。

## 使用与运行边界

详细命令见[工具合同](../references/tools.md)；机构池可从空的 [JSON](../../institutional-style/config/managers.example.json) 或 [YAML](../../institutional-style/config/managers.example.yaml) 模板开始，加入用户指定的 CIK。没有默认真实机构池。

SDK 读取使用现有独立解释器。离线分析不需要 EDGAR 身份、网络、OpenD 或账户。读取器是同步 CLI 运行路径；同一进程重叠 SDK session 会被拒绝。服务拒绝、时间／字节耗尽及清理失败均保留为错误或局部缺口。

申报单位、证券类型、公司行动、历史价格、分类和管理人关系需要实际证据。已提供受 cutoff 约束的 filing_reviews、security_types 和其他 enrichment 接口，保留来源而不改写原始 bundle。未取得这些证据时交付观察和缺口，不能生成伪完整信号。

V1 使用固定的研究选题门槛；没有收益预测模型、调度／消息推送、数据库或交易执行。后续调整门槛需经过明确的代码／配置设计变更。

## 复现命令

以下变量应指向已经验证、已安装相应工具的解释器；不隐式安装依赖。

```bash
cd "Futu API Skills/sec-research-core"
"$SEC_RESEARCH_TEST_PYTHON" -m pytest -q -o addopts=''
```

```bash
cd "Futu API Skills/portfolio-analyst"
"$PORTFOLIO_TEST_PYTHON" -m pytest -q
```

SDK 的公开 smoke 输入 accession、期间和 cutoff 已包含在回执摘要及工具示例中。构建采用现有 packaging 环境的 setuptools，禁用索引、依赖安装和构建隔离；测试环境本身不要求 pip。
