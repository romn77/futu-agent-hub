# Conditional load-plan implementation record

日期：2026-10-16；继续用户要求修复的读取复用问题。
依据 [R3 spec](../../../docs/superpowers/specs/2026-10-16-portfolio-conditional-load-plan.md)；
[R2 原始结果](2026-10-16-format-and-reuse.md) 与归档保持不变。

## Baseline and independent inputs

- R2 源冻结于 `/private/tmp/portfolio-load-plan-nwlt3q65/baseline`，Git 起点 `437b290`；
  后续 `f7cac8b` 只记录R2结果和下一轮范围，没有改变产品指令。
- 新独立准备者仅读取接口、公共reader、模型/分类/规范化定义和common-prompt；不读以前
  的数据、答案、评分或候选指令。三个场景分别28、6、7持仓，均通过不变build_fixture，
  504次本地reader正反例检查通过；冻结后19份准备文件只读。
- 新数据保存在独立证据包的independent-inputs/holdout.json，SHA-256
  `483775912b7d0a74888218b8bb44aefa93f4b33ddcd55a81cd329ff561ec2dc0`。
  独立操作者量表operator-only-rubric.json，SHA-256
  `0aaf27cb24a2aa3b2f87410579002e8e02887f14627cefd1e71606542cca6229`，不进入模型工作区。

## Candidate and bounded probes

- 输入冻结后，修改SKILL与recommendation两份产品文件。按完整成功回执维护当前分析的
  已读路径集合，每次读取前检查；条件路线只扩展缺失路径，不重置基础覆盖。保留补读缺失/
  截断内容，默认文件集合和必读要求不变，无新增缓存、持久化或生产脚本。
- D0为6890词/53,543字符；27项加载/架构/标题检查和quick_validate通过。
- 候选全量离线回归1398 passed in 18.77s；这不替代实际模型行为验证。
- 已通过宿主审批启动两个旧失败输入探针：均使用R2中发生六次重读的allocation_fork
  输入，采用R3候选、新进程和原600秒/8MiB预算。数据与原模型prompt不改，报告不覆盖。
- 另16个正式槽位预登记，R2/R3各两次新留出共12份，Q02/Q05各两份R3共4份。
  目前只准备六份R2基线，等待探针后冻结最终候选；尚未运行新留出。

## Status

R3候选未采用，产品指令已恢复到R2。两个开发探针均通过章节、42个数值字段合计和
采集边界检查，但完整重读分别为0和6，仍会在条件策略后重新读取相同六份基础规则。
没有稳定消除已知失败，按预登记停止条件不再执行额外16份正式对照，也不挑选成功探针。
正式评测为0/16、未启动，不声称留出可靠性或质量改善通过；新独立输入未被模型观察，
原件保留在证据包，可供获得授权后的新加载机制验证。

仅回退本轮自有的SKILL与recommendation改动，回退前确认工作区字节等于冻结候选，
回退后确认等于R2基线。未触及用户改动、原R2修复、计算引擎、schemas或账户权限。
rollback-receipt.json保留前后SHA-256；失败候选和两份报告仍完整留存。

原spec限制不改模型工具目录、生产缓存或持久化。保持零完整重读硬门槛需要另行验证
宿主加载边界：由宿主提供带哈希的完整规则上下文，明确模型还能请求哪些规则访问；
不能靠删除测试文件或把成功报告替换失败报告实现表面通过。是否扩展此范围已提交用户选择，
在答复前不开展依赖该范围的实现或外部评测。历史AC-17及R2未通过项保持原结论。

证据：[development-evidence.tar.gz](../output/conditional-load-plan-20261016/development-evidence.tar.gz)，
校验：[archive-receipt.json](../output/conditional-load-plan-20261016/archive-receipt.json)。
