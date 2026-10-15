# Report reliability implementation record

日期：2026-10-15。起点 `846f378`；分支 `codex/portfolio-advisor-quality`。
本轮依据 [reliability spec](../../../docs/superpowers/specs/2026-10-15-portfolio-report-reliability.md)。
历史 AC-17 保持未通过，本文件不替代上一轮评测记录。

## Baseline

- 开发前同会话复跑：1340 passed in 18.62s；远端 main 为 `9e9c491`。
- R0 的 196 个 Git 跟踪组件文件冻结在 `/private/tmp/portfolio-reliability-2bbvf488/baseline`；
  相邻 baseline-manifest.json 保存完整 SHA-256。未复制真实账户、凭据、忽略输出或 Visa 研究。
- 生产账户/风险/技术/期权代码、scripts、schemas、上游和版本号保持不变。

## R-01 field integrity

- 真实缺口复现：代码齐全但漏掉盈亏的表格仍得到 execution.evidence_pass=true；新集成测试
  在原 producer 上失败（`assert True is False`），不是仅凭缺少新函数得到 RED。
- 新增评测专属 report_checks.py，对同一 snapshot 的权重、市值、盈亏及行列完整性核对。
  中英文/换列/舍入/明确单位/损失括号/未知与零/合并同证券等 21 项回归通过。
- 更新旧成功 mock 为真实完整字段表，保留重复代码等原失败断言；未放宽任何旧门槛。
- 聚焦验证：test_report_integrity + test_codex_eval_producer + test_eval_runner，125 passed。
  四个涉及 Python 文件 Ruff check 通过，format 已应用。
- 本阶段只证明评测能发现这些错误；尚不证明模型已不再生成错误，也不证明研究质量提高。
- 后续单位反例先 RED 后 GREEN：中文港元/人民币不能当作美元，百分比不能当作金额。
  将新核对器应用到原始 Q05-repeat1 两份归档报告，准确定位 A 的 US.SPY COLUMN_COUNT，
  完整 B 表 VERIFIED（9 个数值字段）。原报告和原评分均未改写；新回放结果单独保留。

## R-03 observed loading cost

- 新增 host 回执观测：完整读取次数、完整重复字符、原始读取输出字符/字节、失败/截断
  尝试、重复/冲突 item ID，以及 turn.completed 实际 token usage。缺字段不填零。
- 6 项新用例先 RED（缺观测函数）后 GREEN；与字段及 producer 回归共 123 passed。
- 复用原 required_reference_reads_complete 的相同完整正文证据；账户和模拟回执条件不变。
  指标仅定位无效读取，不能自动判定所有重读不合理，也不证明费用必然下降。

## Product instruction correction

- 四个产品文件做小范围修正：SKILL 按可用上下文只补缺失规则；evidence 明确现金余额
  与期间 CFO/CapEx/FCF；output 在发送前核对最终行/列而非仅总额；research 默认把已核实
  的简单储备基金放在概览，将研究容量用于下一个重要公司问题。点名/复杂/不确定基金例外保留。
- D0 为 6,874 词 / 53,468 字符，仍低于原 6,900 / 54,512 上限。加载/架构 26 项通过。
- 三组新留出数据及量表在修改产品指令前由独立准备者冻结；未向被测模型提供操作者答案。
  R0 源码保持 846f378；R1 指令冻结于本轮临时目录 candidate，之后未基于留出输出调参。
- 本地全量回归：1374 passed in 18.31s；没有据此声明模型行为或质量改善通过。
- 本地审查在首次模型执行前明确 CapEx 为正现金流出、遵从来源定义与符号，R1 待执行
  指令及其清单同步更新并留痕；留出输入/量表不变。最终 quick_validate.py 通过。

## Frozen prospective inputs and execution status

- 独立准备者在未看被测输出前完成三个留出场景：29 持仓/储备基金与公司暴露、
  财务期间/币种/存量流量、带反证的主动配置比较。全部为独立合成数据。
- holdout.json SHA-256：`ae6e51003ef182d12c8b7477dd1997cb5e09e6d94a5c0edefaf67305ab629219`。
  operator rubric SHA-256：`794e65805c4210cc5e2a8120fe29b2c4b3a9fb16a6c4cc4dbd82f85d595320cc`。
- prepare_reliability.py 复用原隔离准备流程和不变计算库，写出真实重建的绑定快照，
  不手改 ID/hash，不复制操作者量表到模型工作区。4 个准备/隔离/拒绝覆盖回归通过。
- 16 个槽位已准备：新留出 3×2版本×2重复=12；旧 Q02/Q05 各两份 R1=4，仅作缺陷回归。
  每对非产品指令文件和用户 prompt 哈希一致，随机顺序提前记录，目录名不含版本标签。
- 配置：Codex CLI 0.154.0，已解析固定启动路径及 SHA-256，gpt-6-astra / medium，
  600 秒 / 8 MiB 上限，只读且忽略用户配置。服务端模型 revision 未知，不能声称严格同权重对照。
- 首个 probe 的宿主请求被自动审批审查拒绝，理由为尚未明确授权将这批仓库指令、实现、
  schemas 发送至 Codex/OpenAI。模型进程未开始；没有使用代理、其他工具或传输路径绕过。
  已向用户提出具体 payload 的确认，等待答复；当前生成报告数为 0/16，行为/盲评仍未完成。
- 请求范围与清单、16 份输入、配置和计划保留于 `/private/tmp/portfolio-reliability-2bbvf488`。
  主目录 approval-summary.json 是待确认范围，prepared/*/inputs.json 是逐文件清单。
- 数值核对的临时副本将 `<=` 反转为 `>=` 后 5 项测试失败，证明比较逻辑变坏会被捕获；
  原源码未修改。最终所有本地代码及测试 Ruff check/format 通过，差异检查无空白错误。
