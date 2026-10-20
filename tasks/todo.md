# 缓存实施任务

各任务按依赖顺序完成，验证命令使用本 worktree PYTHONPATH 与已有 Python 测试环境。

- [x] T1 安全存储、记录、TTL与禁用路径
  - 验收：C01–C06、C12；跨进程读、到期不续期、拒绝不安全文件。
  - 文件：cache/types.py、cache/storage.py、cache/store.py、test_cache_core.py。
  - 验证：pytest tests/test_cache_core.py；红→绿。
- [x] T2 依赖、租约、限额和清理
  - 依赖：T1。验收：C07–C11；依赖变化失效、竞争和旧所有者不能发布。
  - 文件：cache/store.py、cache/coordination.py、test_cache_processes.py。
  - 验证：独立进程竞争与故障注入、核心回归。
- [x] T3 公开证据/摘要与缓存CLI
  - 依赖：T2。验收：P10–P13；来源校验、摘要引用和隐私范围、结构化CLI。
  - 文件：cache/public.py、cache/cli.py、scripts/cache.py、schemas/cache_request.schema.json、test_cache_public.py。
  - 验证：公开读写和CLI subprocess tests。
- [x] T4 行情共享接入
  - 依赖：T3。验收：P07–P09；同口径覆盖复用，错误口径不命中。
  - 文件：cache/market.py、price_history.py、technical.py、test_cache_market.py。
  - 验证：新行情缓存与原行情/技术回归。
- [x] T5 SEC可选缓存与增量读取
  - 依赖：T3。验收：P01–P06、P14；正文分页、目录无变化、增量申报、共享节流。
  - 文件：sec_research/cache.py、reader.py、cli.py、transport.py、tests/test_cache.py。
  - 验证：SEC完整离线回归和跨解释器桥接。
- [ ] T6 账户身份与短期结果
  - 依赖：T2、T4。验收：A01–A09；先发现再查、事件失效、旧helper兼容。
  - 文件：repository.py、current_analysis.py、cache/account.py、test_cache_account.py。
  - 验证：账户新测试与原账户选择/helper回归。
- [ ] T7 报告契约与组合入口
  - 依赖：T3、T6。验收：RP01–RP07；完整回放、请求错配、单次补刷新。
  - 文件：cache/report.py、cache/workflow.py、scripts/cached_analysis.py、test_cache_report.py、test_cache_host_flow.py。
  - 验证：七场景和独立CLI进程端到端。
- [ ] T8 安装与账户Skill接入
  - 依赖：T7。验收：X01、X03、X04；已安装包、Skill必须先查缓存。
  - 文件：pyproject.toml、SKILL.md、references/workflow.md、references/tool-contract/core/snapshot.md、references/tool-contract/core/research.md。
  - 验证：prompt契约回归、wheel安装到临时目录、CLI smoke。
- [ ] T9 研究契约与输出同步
  - 依赖：T5、T8。验收：X04；默认复用、原时间与禁止隐式扩大范围。
  - 文件：references/research-policy.md、evidence-policy.md、output-contract.md、company-research/references/workflow.md、institutional-style/references/workflow.md。
  - 验证：静态契约与宿主回执测试。
- [ ] T10 共享SEC规则与实现记录
  - 依赖：T9。验收：覆盖说明准确，历史文档不当作新实现证据。
  - 文件：sec-research-core/references/evidence.md、tools.md、docs/reviews/2026-10-20-cache-implementation.md、缓存README。
  - 验证：链接、需求映射、来源覆盖。
- [ ] T11 完整回归、冷热评测与审查
  - 依赖：全部。验收：46项记录状态；已实现能力不得缺少测试。
  - 文件：test_cache_benchmark.py、实现记录、必要审查修复。
  - 验证：portfolio+SEC全套、安装、CLI/多进程基准；真实网络/token无法获得时明确NOT_RUN。

## 执行记录

- 2026-10-20：T3已绑定独立worktree；12份设计/证据文件迁入且SHA-256一致；原主工作区未改动。

- T1：新增8项行为测试先因缺少cache模块失败，随后全部通过；验证独立进程读取、精确到期、关闭缓存、时间异常、软链接/权限、损坏、大小和零值。

- T2：10项新测试先失败，依赖/动态目录代次、租约fencing、容量清理和真实双进程协调实现后，与T1合计18项通过。首次并发创建锁的macOS ENOENT已定位并使用同一锚定名称的安全重开修复。

- T3：公开证据/摘要和CLI新增9项测试先失败，完成后与核心合计27项通过；公共写入验证来源、时间、完整性、引用及已知私人字段，CLI新进程命中有效。

- T4：5项行情行为测试及核心/原行情/技术共33项通过；同口径日线复用、周线独立、过期重取、非完整OHLCV不污染缓存，旧入口不隐式落盘。

- T5：SEC全部74项（原69+新增5）通过。可选CLI桥接不增加强制依赖；缓存正文供分页复用、保留历史截止；collect为新的冻结cutoff执行必要目录检查，申报正文仍增量复用。后续验收需补增量13F及共享节流断言。
