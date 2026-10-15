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
