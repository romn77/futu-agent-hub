# 分层缓存实施与验收记录

日期：2026-10-20。分支：`feature/portfolio-research-cache`，基线 `72c477a0d51c83bb39df3193ad975e1f97a4ce34`，发布版本 `0.9.0`。

实现位于独立 worktree `/Users/yaoma/.t3/worktrees/futu-agent-hub/feature-portfolio-research-cache`。T3 绑定已核验。批准文档的 12 文件交接清单及归档哈希已核验迁入；原主目录未提交内容保持原样，未在原目录继续业务开发、reset、stash、提交或合并 main。

[批准设计入口](../superpowers/specs/2026-10-20-portfolio-research-cache/README.md) · [验收原规范](../superpowers/specs/2026-10-20-portfolio-research-cache/ACCEPTANCE.md) · [任务记录](../../tasks/todo.md)

## 已实现行为

| 层 | 实现 | 当前用途与失效 |
|---|---|---|
| cache-core | 标准库文件存储、权限/路径校验、内容摘要、原子发布、跨进程锁、租约 fencing、依赖图、容量/保留期、诊断 | 读取不续期；精确到期、未来时间、已观察到的回拨拒绝当前命中；历史读取不能降低时钟基准 |
| public-research-cache | SEC 目录/全文/财务事实/标准化13F、行情OHLCV、带引用与反证的摘要、本地登记CLI | 目录默认1h；固定 accession/内容版本复用；目录内容不变时保留 generation，摘要不必重做；新披露/修订只失效实际依赖 |
| account-analysis-cache | 先真实只读账户发现，再稳定HMAC范围；快照、risk、technical绑定复用 | 默认5min；保留OpenD NAV及原snapshot_id/as_of；身份/成员/事件/方法/配置变化失效 |
| report-cache | `cached_analysis.py` prepare/finalize/abort、请求匹配、宿主上下文、报告校验、完整依赖复核 | 完整命中直接回放；研究超过快照期限最多补刷新一次；再过期返回historical_only |

默认公共区512MiB、私有区128MiB，私有保留最多24h。有效期、保留期、显式历史互不替代。TTL到期不启动后台任务。

`RESEARCH_CACHE_*` 的明确环境配置支持TTL、容量和私有保留期；完整字段见[缓存协议](../../Futu%20API%20Skills/portfolio-analyst/references/cache-workflow.md)。有效期配置产生独立的 public/private `policy_version`，扩大TTL不会使旧记录复活。报告期限取“快照期限”和“报告生成时间+报告TTL”的较早者；原时间不可改写。

`prefer` 先查缓存；`refresh_account` 刷账户；`refresh_all` 刷本次相关证据；`latest` 重新检查当前账户及动态来源，并在单次请求内共享刷新后的行情。`off` 不读写缓存或建立身份密钥。旧 `current_analysis.py` 与 `--no-save` 保持显式旧语义，不反向依赖报告模块。

SEC collect 仍重查管理人申报链，按实际manager/accession缺项增量取正文；分析使用本次完整bundle/enrichment，分类变化不复用旧计算。目录覆盖按纽约日期判断，包含跨纽约午夜的可得性边界。风险和技术共享相同市场、周期、复权、时段、来源版本的OHLCV；范围不足、周线未结束或重核验到期会重取。

## 验证环境与结果

- 复用已有 Python 3.12 / pytest 9.1.1 环境，明确设置新 worktree 的 `PYTHONPATH`，验证导入 `__file__` 指向新代码；没有安装或升级运行依赖。
- 原基线实际收集：portfolio **1444** 项、SEC **69** 项。新增缓存测试与原测试一起运行，最终数量以本节末尾记录为准。
- 全部取数使用合成账户/可控公开源；未连接真实OpenD账户或公开网络。
- 原doctor测试需要本机回环临时端口；最终完整portfolio回归经沙箱审批执行。没有绕过源权限或隐藏失败。
- 安装验证在仓库外临时目录构建两份wheel，用临时venv、`--no-index --no-deps`安装本地构建物。实际导入安装包；新cache子包、两份schema、政策资源、3个CLI help及独立SEC→已安装cache CLI桥接通过。
- 两个独立进程竞争仅一次采集；实际进程`os._exit`崩溃后接管；旧所有者不能覆盖新结果。
- 三个独立CLI进程完成prepare→finalize→report_hit；后续命中无snapshot/risk/technical源命令，正文逐字一致。另有两个独立SEC客户端进程读取同一全文不同分页，第二次无正文取源。
- 独立core/public/SEC审查、account/report/workflow审查均关闭全部Required/Critical。修复包括锁内租约/依赖校验、配额失败不得发布、错配ref、公共载荷边界、metadata容量/epoch、SEC覆盖、latest行情、缓存故障不重复取源或丢正文、时钟历史交错、策略版本。
- 变异验证：将lookup精确到期条件`>=`改为`>`，`test_expiry_is_exact_and_reads_do_not_renew`确实失败；随后逐字节恢复，正常测试通过。

最终完整回归：**portfolio 1526 passed；SEC 80 passed**。默认12份核心文档仍在原6900词/54512字符预算内，未放宽测试阈值。安装与CLI验证通过。

复现（从worktree根目录，用已有测试解释器运行，以下`python`代表该解释器）：

```bash
PYTHONPATH="$PWD/Futu API Skills/portfolio-analyst" python -m pytest 'Futu API Skills/portfolio-analyst/tests' -q -o addopts='' -p no:cacheprovider
PYTHONPATH="$PWD/Futu API Skills/sec-research-core" python -m pytest 'Futu API Skills/sec-research-core/tests' -q -o addopts='' -p no:cacheprovider
python 'Futu API Skills/portfolio-analyst/evals/cache/benchmark.py'
```

安装验证脚本`evals/cache/verify_installation.py`使用已有setuptools/wheel及已安装运行依赖，不下载依赖。测试旧helper的合成宿主评估继续使用原fixture；没有将其伪装成新缓存宿主验收。

## 46项验收逐项记录

**PASS指本地受控功能验收通过，不表示真实金融来源、真实网络性能或模型质量已验收。** 44项PASS，2项NOT_RUN；无已知FAIL。RP01的独立进程/CLI部分已通过，但两个真实LLM宿主session部分未执行，因此整项不冒报PASS。X02已完成离线基准，真实模型成本与网络端到端性能未执行。

下表文件名位于`portfolio-analyst/tests/`；SEC标记位于`sec-research-core/tests/`。每项证据均为本次实现测试，未挪用排查阶段35项旧测试。

| ID | 状态 | 实际证据 |
|---|---|---|
| C01 | PASS | test_cache_core：second_process_reads_original_evidence，独立解释器原文/原时间一致 |
| C02 | PASS | test_cache_core：exact expiry；读取不续期，TTL边界变异可捕获 |
| C03 | PASS | test_cache_core：future/rollback、读后回拨及historical交错；test_cache_policy策略变更 |
| C04 | PASS | core损坏载荷；review_regressions损坏租约、错配ref；不得成功命中 |
| C05 | PASS | core软链接根/对象及权限拒绝；processes清理保留非缓存历史 |
| C06 | PASS | core disabled；report off完整流程无缓存/身份密钥；policy off忽略未用配置 |
| C07 | PASS | processes配额淘汰/保留期；review_regressions metadata配额、过期租约回收及epoch |
| C08 | PASS | processes missing dependency不命中；对象缺失不标已核实 |
| C09 | PASS | processes两个真实进程并发仅一次源调用 |
| C10 | PASS | processes租约到期fencing、实际崩溃进程接管、旧发布拒绝 |
| C11 | PASS | processes bounded wait，LEASE_BUSY而非无限等待 |
| C12 | PASS | market写失败仅一次源读取；report发布/预留失败仍保留正文；失败写入不可见 |
| P01 | PASS | SEC test_cache：两个独立客户端分页命中，第二进程正文调用0 |
| P02 | PASS | SEC index_refresh_keeps_generation；processes unchanged discovery保留摘要ref/原生成时间 |
| P03 | PASS | processes changed evidence/新发现generation失效；SEC collect扩展只取缺项 |
| P04 | PASS | SEC cutoff拒绝晚披露、历史范围不足刷新、纽约午夜边界 |
| P05 | PASS | SEC collect由1管理人1季度扩至2管理人2季度，4份正文各读1次，目录重核验 |
| P06 | PASS | SEC same_universe_recomputes_changed_classification；原始filings未变 |
| P07 | PASS | market风险/技术同口径OHLCV超集共享，一次取源 |
| P08 | PASS | market周/日独立、close-only拒绝、覆盖和口径键隔离 |
| P09 | PASS | market较晚cutoff/重核验到期刷新；report latest刷新一次并请求内共享 |
| P10 | PASS | public摘要包含真实依赖、反证/缺口，命中返回原摘要 |
| P11 | PASS | processes依赖变化失效摘要、发现内容不变不重写；report已知SEC事件持续失效 |
| P12 | PASS | public私人字段/文本拒绝；review_regressions SEC伪装载荷、嵌套对象及summary绕过拒绝 |
| P13 | PASS | public拒绝source error/未知动态时间；partial不升级；core零/null/空数组不互换 |
| P14 | PASS | processes双SEC预算进程间隔受控；SEC transport请求钩子接入共享预算；独立MCP明确不覆盖。真实HTTP性能未测 |
| A01 | PASS | account真实select_accounts独立发现；稳定范围测试；report命中仍调用resolver |
| A02 | PASS | account同endpoint不同账户/成员HMAC不同，多账户未选失败；原账户选择回归 |
| A03 | PASS | account t+180命中/t+300刷新，原snapshot_id/as_of保持 |
| A04 | PASS | account known trade提前失效，report event期间重绑；不清空公共材料 |
| A05 | PASS | account新snapshot/方法版本；policy账户TTL变更；技术绑定risk ref |
| A06 | PASS | account UNVERIFIED期权报价不回放，report不具备完整复用资格 |
| A07 | PASS | 原helper/临时清理回归；report source失败不被release错误覆盖；guard失败不重复取源 |
| A08 | PASS | account事件guard旧记录失效；source失败保留；processes只有显式historical能读取旧记录 |
| A09 | PASS | report历史缺失明确失败，不以当前快照重建 |
| RP01 | NOT_RUN | 两独立合成宿主进程及三个实际CLI进程已PASS：命中不重跑源/引擎/fixture研究；两个真实LLM宿主session未执行 |
| RP02 | PASS | report snapshot deadline；policy同时约束snapshot/report TTL，不按晚生成无限延长 |
| RP03 | PASS | report新问题复用下层但不命中整报告；偏好/上下文/version错配拒绝 |
| RP04 | PASS | report缺持仓cell拒绝；必需研究不完整、无discovery或失效依赖不发布完整报告 |
| RP05 | PASS | report long_generation最多一次补刷新，第二次过期historical_only |
| RP06 | PASS | report生成期间account/report guard变化；core锁内再次验证依赖和lease |
| RP07 | PASS | report_hit原Markdown、as_of及report_created_at保留；host_flow逐字一致 |
| X01 | PASS | report/core/market四模式与latest；真实CLI回执；旧helper及--no-save完整回归 |
| X02 | NOT_RUN | 三案例×五组离线冷/热实测完成，详见下一节；真实网络、模型输入/输出token及正文输入未取得 |
| X03 | PASS | 两wheel实际安装、模块/CLI/Schema/政策资源、可选SEC桥接；独立无SDK离线测试；无依赖安装或扩权限 |
| X04 | PASS | prompt architecture/loading与原完整回归；Skills/contracts同步；缓存不引入账户写操作/后台调度/默认历史归档 |

## 冷热测量

原始逐轮数据：[离线基准JSON](2026-10-20-cache-offline-benchmark.json)。代码：[benchmark.py](../../Futu%20API%20Skills/portfolio-analyst/evals/cache/benchmark.py)。每例5组配对，独立临时缓存、合成账户/源、受控时间2026-10-20T14:00:00Z；代码摘要记录在JSON中。

| 案例 | 冷中位数 | 热中位数 | 热路径实际调用 |
|---|---:|---:|---|
| 仅股票 | 76.92ms | 10.36ms | 身份发现1；snapshot/risk/technical 0；fixture报告渲染0 |
| 含期权 | 73.97ms | 58.39ms | 复用snapshot/risk/technical；options 1；report_uncached，重新渲染fixture报告 |
| SEC正文 | 3.07ms | 1.08ms | company/index/text调用均0；保留原正文事实，返回本地切片 |

这里的“渲染”是离线固定模板，不是LLM生成；源方法次数也不是HTTP次数。JSON的HTTP、实际送入模型字节、真实输入/输出token均为null。数据仅证明本地执行路径减少了工作并在该合成负载下降低耗时，**不能据此宣布真实网络端到端或模型成本目标完成**。关键事实/报告digest跨冷热一致。

## 已知限制与未验收项目

1. 两个真实LLM宿主session的完整跳过行为、真实token和真实网络性能为NOT_RUN。新宿主契约与CLI路由已实现并有实际离线进程回执；真实宿主遵循指令仍需环境验收。
2. 现有options接口将quote_freshness标为UNVERIFIED，因此含期权组合不能完整回放当前报告；不得用账户缓存掩盖该来源能力缺口。
3. 自动适配覆盖现有受控SEC CLI和Futu历史行情。其它授权MCP/web工具由宿主通过本地公开证据/摘要协议登记，未接入工具不能声称自动命中。独立SEC MCP请求不在共享HTTP预算内。
4. 公共shape/来源/范围校验不能证明任意文本的真实来源；宿主必须核验实际源回执，禁止把组合私人内容投到公共区。报告校验不能认证任意自然语言论证质量。
5. 没有推送/事件通知时，只提供TTL内的bounded freshness，不保证立刻发现外部交易或新申报。手动事件/latest/refresh_*可提前刷新。
6. 缓存会被容量/保留期淘汰，不是日终档案。旧当前helper和显式历史导出保持独立。当前实现未合并main、未部署、未创建外部PR或后台任务。
