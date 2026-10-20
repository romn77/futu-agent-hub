# 组合分析与研究缓存架构排查报告

日期：2026-10-20。审查基线：本地 `main` 的 `72c477a` 加当前工作区修改。

当前系统没有接入跨 session 的自动分析缓存。它主要依靠本次会话中的证据复用、SEC Reader 的实例内字典和执行结束即清理的临时文件。重复发起完整分析时，会重新获取快照、执行分析命令，并由宿主重新组织研究与报告。用户观察到的重复工作，与代码和技能规则一致。

建议沿用 10 月 16 日的分层缓存草案，先落地公共证据与带引用研究摘要，再接入短期账户结果和整份报告复用。优化目标需要同时覆盖外部取数、重复计算和模型重新阅读；单纯打开 SDK 缓存或保存 JSON 不足以解决问题。

本次新增排查报告和离线复现材料，未修改业务实现、技能规则或原有未提交内容。验证不连接真实账户或外部数据源；下面的调用次数是本地命令或方法调用次数，不是网络请求数或性能收益测量。

## 当前执行路径

普通组合分析的路径是：

```text
用户发起完整分析
  → 宿主加载 portfolio-analyst 规则
  → run_current_analysis
      → 新快照 snapshot（REAL / USD / --no-save）
      → risk（同一快照；可能查询历史日 K 线）
      → technical（同一快照；分别查询日 K、周 K）
      → options（有持仓期权时）
      → 返回结果，清理本次临时文件
  → 宿主按研究规则选择公司、SEC、新闻及估值工具
  → 宿主综合生成报告
```

这条路径没有“先查有效报告或分析结果”的分支。`current_analysis` 只返回快照和计算结果，不拥有后续公司研究或最终模型报告，因此仅在该函数中加缓存也无法阻止宿主重新研究。

依据：[技能入口](</Users/yaoma/project/futu-agent-hub/Futu API Skills/portfolio-analyst/SKILL.md:35>)、[执行编排](</Users/yaoma/project/futu-agent-hub/Futu API Skills/portfolio-analyst/portfolio_analyst/current_analysis.py:405>)、[研究能力契约](</Users/yaoma/project/futu-agent-hub/Futu API Skills/portfolio-analyst/references/tool-contract/core/research.md:5>)。

| 层次 | 当前实现 | 能否供下次完整分析自动复用 |
|---|---|---|
| 技能和研究上下文 | 当前运行内避免重复读取已加载内容、重复查询合格证据 | 新 session 没有持久索引保障 |
| 账户快照 | 每次新取；临时保存用于绑定后续计算 | 否 |
| 风险、技术、期权结果 | 每次执行；临时 JSON 在 finally 中清理 | 否 |
| 历史行情 | risk 与 technical 各自创建 provider，经受审计 Adapter 读取 | 仓库包装层没有结果缓存 |
| SEC 原文 | Reader 内缓存 company、filing、content；SDK 使用临时目录且关闭 HTTP 缓存 | 只在同一 Reader 生命周期内复用 |
| 机构研究 | collect 遍历配置管理人和期间内申报；analyze 可显式读取 bundle | 可手工复用 bundle，无自动命中或增量采集 |
| 快照历史与报告文件 | 显式保存后可按指定路径或历史命令读取 | 是存档能力，没有自动时效与请求匹配 |
| Futu 辅助缓存 | 环境检查、加密货币券商探测，TTL 各 1 小时 | 不缓存研究、行情或分析结论 |
| 最终研究报告 | 宿主生成，没有集成的报告索引和依赖验证 | 否 |

不同 T3 thread、Git worktree 或新 Python 进程不会自动补上这些缓存。Git worktree 隔离代码目录；未来如需同一用户跨 thread 复用，应使用仓库之外的受控缓存目录，并按代码与数据版本区分兼容性。

## 主要发现

### 1 重复分析由现行契约和实现共同决定

`SKILL.md` 要求每次完整分析取得 fresh 快照，并明确不默认持久化快照、报告或研究。`research-policy.md` 将复用限制在本次运行或允许的临时状态。代码无条件调用 snapshot、risk、technical，有期权再调用 options，最后清理自己创建的文件。

这是功能尚未接入造成的重复工作；没有证据表明存在一套本应命中的持久缓存正在随机失效。[研究复用限制](</Users/yaoma/project/futu-agent-hub/Futu API Skills/portfolio-analyst/references/research-policy.md:91>)、[临时清理](</Users/yaoma/project/futu-agent-hub/Futu API Skills/portfolio-analyst/portfolio_analyst/current_analysis.py:494>)。

实际账户读取还显式传入 `refresh_cache=True` 查询资金及持仓，不能假定 OpenD 内部缓存会替代应用层短期快照复用。[账户读取](</Users/yaoma/project/futu-agent-hub/Futu API Skills/futuapi/scripts/trade/get_portfolio.py:63>)。

### 2 同一次分析中的行情请求也存在重叠

风险分析查询前复权日线；技术分析另外查询前复权日线和周线。两个入口各自创建 provider/Adapter，没有共享行情结果。风险路径在本次计算内会按证券身份去重，但这份内存结果没有传给技术路径。

默认风险回看 120 个价格点，查询起点为 cutoff 前 240 天；技术日线查询起点为 cutoff 前 200 天。日期、复权和交易时段一致时，两段原始 OHLCV 有复用机会。但风险结果只保留所需收盘序列，不能直接把风险报告当技术分析的完整行情源。需要保存合格的原始序列、覆盖范围和口径，再按需裁剪。

依据：[风险行情读取](</Users/yaoma/project/futu-agent-hub/Futu API Skills/portfolio-analyst/portfolio_analyst/price_history.py:36>)、[技术行情读取](</Users/yaoma/project/futu-agent-hub/Futu API Skills/portfolio-analyst/portfolio_analyst/technical.py:150>)、[风险本次去重](</Users/yaoma/project/futu-agent-hub/Futu API Skills/portfolio-analyst/portfolio_analyst/risk.py:618>)。

每次穿过现有 Futu Adapter 还会校验并复制受审计脚本到临时目录、启动子进程。缓存命中若发生在这个步骤之前，可同时减少启动工作；其耗时占比尚未测量，不建议为提速取消信任校验。[Adapter 执行](</Users/yaoma/project/futu-agent-hub/Futu API Skills/portfolio-analyst/portfolio_analyst/futu_adapter.py:484>)。

### 3 SEC 缓存只活在一次 Reader 内

`EdgarReader` 的三个字典分别按 CIK、CIK 与 accession、accession 与 section 复用对象或文本。每条 live CLI 命令都创建新的 Reader；SDK session 使用新临时目录，HTTP manager 设置 `cache_enabled=False`。

因此将同一篇申报拆成两条正文分页命令，会重复进入底层全文读取路径。`facts()` 和 `holdings()` 没有自建的结果缓存；SDK 对象内部是否另有缓存不在本次验证范围内。

依据：[Reader 实例缓存](</Users/yaoma/project/futu-agent-hub/Futu API Skills/sec-research-core/sec_research/reader.py:124>)、[每次新建 Reader](</Users/yaoma/project/futu-agent-hub/Futu API Skills/sec-research-core/sec_research/cli.py:131>)、[SDK 临时状态](</Users/yaoma/project/futu-agent-hub/Futu API Skills/sec-research-core/sec_research/transport.py:62>)。

机构 `collect` 每次重新遍历管理人和申报，没有“查目录差异，仅补新 accession”的增量路径。`analyze --bundle` 能读取已有采集回执，但要求 cutoff 一致、名单版本匹配，并重新计算；它没有自动搜索旧报告的能力。不能通过删除 cutoff 校验让旧材料冒充新截止日的完整证据。[采集](</Users/yaoma/project/futu-agent-hub/Futu API Skills/sec-research-core/sec_research/cli.py:68>)、[bundle 复用条件](</Users/yaoma/project/futu-agent-hub/Futu API Skills/sec-research-core/sec_research/cli.py:158>)。

### 4 存档和缓存需要不同的有效性判断

现有 SnapshotRepository、显式报告输出和 `output/` 文件解决“保存了什么”，没有解决“这次问题能否直接使用它”。当前主路径也不会查询这些历史产物来跳过分析。

默认 no-save 快照使用随机生成的 EPHEMERAL 账户别名；snapshot_id 还包含数据时间和内容摘要。因此即使持仓未交易，新取快照也不能自然得到一个稳定的跨 session 查找键。缓存必须先可靠识别账户范围，再查找原始快照，而不是先完整拉取持仓、生成新 snapshot_id 后才尝试命中。

依据：[账户别名选择](</Users/yaoma/project/futu-agent-hub/Futu API Skills/portfolio-analyst/portfolio_analyst/service.py:214>)、[随机临时别名](</Users/yaoma/project/futu-agent-hub/Futu API Skills/portfolio-analyst/portfolio_analyst/account_alias.py:34>)、[快照身份](</Users/yaoma/project/futu-agent-hub/Futu API Skills/portfolio-analyst/portfolio_analyst/identity.py:50>)。

### 5 原文缓存不能单独消除模型重复研究

当前没有集中管理研究产物的入口。宿主按技能选择网页、SDK、MCP 等不同工具；新的分析仍需解释、对比和综合资料。即便原文读取命中，如果将全文再次送入模型，阅读与推理成本仍会发生。

需要同时保存带引用的研究摘要，包含研究问题、支持与反对证据、缺口、适用期间和依赖版本。新问题可以复用证据或摘要，但不一定能回放旧结论。只有全部请求条件与依赖均匹配时，才适合直接返回最终报告。未接入缓存路径的外部工具，应明确列为未覆盖，不能计入缓存命中率。

### 6 并行 thread 不会自动合并相同研究

SEC 的 `threading.Lock` 只保护单个进程内 SDK 全局状态；冲突时返回 busy。每个进程分别限制请求速度，不存在仓库级的跨进程相同请求合并。多个 thread 同时查同一份文件，仍可能各自执行采集。

缓存的原子写只能避免半文件，不能阻止多个进程同时请求同一数据。后续需分别解决原子发布、同键在途请求合并和跨进程请求预算。[SEC session 管理](</Users/yaoma/project/futu-agent-hub/Futu API Skills/sec-research-core/sec_research/transport.py:13>)。

另外，技术和期权报告校验会用嵌入证据进行本地语义重放。观察到多次分析函数调用时，应区分这种正确性验证与重新取数，不应直接关闭校验来降低调用数。[技术报告校验](</Users/yaoma/project/futu-agent-hub/Futu API Skills/portfolio-analyst/portfolio_analyst/schema.py:478>)。

## 离线验证结果

使用真实编排函数、行情 provider 和 SEC CLI，替换外部读取为合成回执。两次运行在同一进程、同一临时私有目录中进行；连这种最容易复用的情形也没有跳过重复命令。

| 验证场景 | 第一次 | 第二次 | 结论 |
|---|---|---|---|
| 同一快照，无期权 | snapshot、risk、technical | 再次调用全部三项 | 无分析结果命中分支 |
| 同一快照，有期权 | 上述三项及 options | 再次调用全部四项 | 期权结果同样不复用 |
| 每次运行结束后的自有临时文件 | 0 | 0 | 清理符合现有契约 |
| 同一股票的 risk 日线与 technical 日周线 | 3 次 Adapter 调用 | 再次 3 次 | provider 包装层没有缓存 |
| 同一 SEC Reader 读取两页 | Company、get_filings、text 各 1 次，覆盖两页 | — | 实例缓存有效 |
| 两次 SEC CLI execute 各读取一页 | 三种方法各 1 次 | 三种方法各再 1 次 | CLI 调用之间不复用 Reader |

现有 `test_current_analysis_helper.py`、`test_price_history_adapter.py`、`test_technical_cli.py` 共 35 项通过。它们验证当前行为，不代表新缓存已实现。

材料：[复现脚本](</Users/yaoma/project/futu-agent-hub/output/cache-architecture-audit-2026-10-20/probe.py>)、[原始结果](</Users/yaoma/project/futu-agent-hub/output/cache-architecture-audit-2026-10-20/probe-results.json>)、[验证命令](</Users/yaoma/project/futu-agent-hub/output/cache-architecture-audit-2026-10-20/verification.txt>)。

未测量真实账户与公开源请求量、模型 token 或端到端耗时；不能据此宣称某个百分比的提速。宿主工具与 OpenD 内部缓存也不等同于本仓库的自动分析复用。

## 优化候选与实施顺序

建议保持本机单用户方案，先复用已有 Python 运行环境、私有目录与原子持久化能力；无需以数据库服务器或远程缓存作为第一步。现有独立安装的 portfolio-analyst 不应被迫依赖 SEC sibling。

### 第一阶段 公共证据与研究摘要

**公共证据 Module。** 涉及 `price_history.py`、`technical.py`、SEC `reader.py`、`cli.py` 和研究规则。把查找、覆盖范围判断、来源、版本、更新与失效集中在一个 Implementation 中，由现有取数 Adapter 接入相应 Seam。外部 Interface 只需要表达要什么证据及其时间和口径要求。这样一次修复可同时减少多处重复请求，形成 Locality，并在同一 Interface 上验证冷读、热读、过期和损坏回退。

先缓存精确相同的公开请求与固定 accession 文档，再增加日线覆盖范围复用和机构增量采集。风险与技术需要原始 OHLCV，而不是互相借用不完整结果。行情条目须保留市场、证券身份、周期、复权、交易时段、覆盖区间、来源和修订依据；已结束的交易日可以优先复用，未结束的日线与周线需要单独刷新。公司行动可能改写前复权历史，不能将它视为永不变化的事实。

**研究摘要 Module。** 涉及公司、机构和组合研究的 workflow 与输出契约。把已验证事实、引用、反证、问题范围及依赖有效性集中管理，提供跨研究流程的 Leverage。依赖未变时复用摘要；新申报、新闻、估值日期或研究问题变化时，只重做受影响部分。验收既检查事实与反证保留，也检查模型重新读入的原文字节或 token 是否下降。

这一阶段需要同步消除现有“默认不跨 session 复用”的规则冲突；只改代码或只改提示词都不构成完整接入。

### 第二阶段 短期账户与分析结果

**当前分析 Module。** 涉及 `current_analysis.py`、账户身份处理、Settings、结果校验及技能入口。在取得新快照之前识别账户范围并查缓存；命中则保留原 snapshot_id 和 as_of，未命中才调用既有受审计读取路径。

沿用现有草案的 5 分钟作为可调整起点，用户要求最新、已知调仓、账户切换或数据不合格时立即失效。稳定账户身份需要沿用现有本地密钥能力或等价受控机制；不能复用 EPHEMERAL 别名或仅凭 OpenD 地址判断账户。必要的只读账户发现单独计数，不宣称零网络请求。

计算结果还须匹配快照、行情或报价证据、算法版本、风险政策与技术参数。新快照可复用合格公共证据，不能把旧风险报告改一个时间就绑定到新快照。期权报价按其更严格时效处理。缓存写入与显式历史存档分开；保留原 `--no-save` 语义，不能让底层命令暗中持久化。

### 第三阶段 最终报告复用和并发协作

**报告复用 Module。** 涉及宿主研究工作流及最终报告产物，而不止 Python helper。按账户、问题范围、实际偏好、语言、方法版本和完整依赖匹配已完成报告。完整命中时直接返回现有报告并标注原数据时间；仅部分命中时补齐差异。此处才有机会跳过整轮模型综合分析。

同步完善同键在途请求合并、跨进程锁超时、原子发布和容量清理。多个 Git worktree 可共享兼容的公开证据，但不同算法版本不得误复用结果。私有缓存留在 `PORTFOLIO_AGENT_HOME` 内，公共证据也使用用户私有缓存目录；两者都放在 Git 仓库之外。

每阶段都应输出命中层次、未命中原因、原数据时间、源调用次数、取回字节、模型输入输出量和阶段耗时。用这些指标区分必要刷新与重复工作，不用单一“命中率”代替实际收益。

## 草案中需要特别落实的条件

已有[分层缓存草案](</Users/yaoma/project/futu-agent-hub/docs/superpowers/specs/2026-10-16-portfolio-research-cache.md:3>) 与本次发现基本一致，明确标注“尚未实现”。其账户快照和报告复用范围已经写明，无需重新讨论是否只缓存公共数据；具体 TTL 仍需验证。

1. **固定文档与最新发现分开。** 固定 accession 原文按版本复用；申报目录按时效重新检查。新截止日发现了修订或新申报后再更新相关分析，旧材料的知识截止日不能直接改写。
2. **原始证据与摘要分开。** 原始缓存命中减少取数；合格摘要命中才减少重复阅读。摘要必须保留引用、反证和缺口，不能成为无来源的新事实。
3. **报告过期从底层时间算起。** 若深度研究超过 5 分钟，报告生成时原快照可能已经过期。这种报告可以留作历史材料，但不能重新起算 5 分钟；应通过阶段耗时和分层复用改善收益。
4. **共享缓存不等于共享在途任务。** 原子文件写入不足以防止并行采集；需要独立验证多进程同键只产生一次有效采集，以及持锁者崩溃后的恢复。
5. **机构名单版本不是完整结果版本。** `universe_version` 描述成员关系，分析结果还依赖完整配置、分类与 enrichment、问题、季度和方法版本。不能只按名单版本复用最终结果。[名单版本实现](</Users/yaoma/project/futu-agent-hub/Futu API Skills/sec-research-core/sec_research/universe.py:67>)。
6. **先完整性后回放。** PARTIAL 证据可复用已覆盖部分；权限拒绝、截断、来源不明或严重不完整的报告不得变成成功命中。清理缓存不能删除显式保存的账户历史。

## 验收标准

- 两个独立进程使用同一缓存目录：第二次读取有效固定文档不再请求正文，重复分析不再重复取相同历史行情。
- 风险与技术读取相同证券和口径时复用足够覆盖的原始序列；不同交易时段、复权版本、周期或截止日不得误命中。
- 新 SEC 申报或修订只补采缺项；正文分页复用已取得的完整文本；历史研究不混入截止日之后的信息。
- 同账户、同问题、有效期内的完整报告可直接回放；保留原数据时间，必要账户发现与真正取持仓分开计数。
- 只刷新账户仍复用合格公开材料；新问题复用底层证据但不直接返回旧答案。
- 账户变化、时间边界、报价过期、配置或方法变化、损坏和部分数据均有明确失效或降级行为。
- 两个并发进程、异常退出、不可写目录及容量淘汰不产生半文件、串账户或重复成功回执；无持久化模式不创建缓存。
- 用同一组冷、热研究案例比较真实源调用数、模型 token、总耗时及关键事实质量。缓存命中但端到端成本不降时，性能目标仍未完成。
