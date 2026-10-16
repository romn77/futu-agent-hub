# Format and reference reuse implementation record

日期：2026-10-16；起点 `0b9e469`，分支 `codex/portfolio-advisor-quality`。
依据 [spec](../../../docs/superpowers/specs/2026-10-16-portfolio-format-and-reuse.md)。
用户已要求继续未完成任务；本轮不改变上一轮与历史 AC-17 的验收结论。

## Baseline and reproduced failures

- R1 的 205 个跟踪组件文件已冻结于 `/private/tmp/portfolio-format-reuse-r6ekvo3_/baseline`，
  baseline-manifest.json 保存 Git SHA 和逐文件哈希。真实账户、凭据与根目录 Visa 产物未纳入。
- 原 16 份报告回执中，14 份在 helper 之前重复完整读取研究/输出规则。已取得完整正文，
  不是截断补读；不能从回执推断隐藏压缩状态或模型内部原因。
- Q02 `run-456c00bca88bd675` 使用加粗冒号标签并把诊断结论放在范围之前；原失败保持不变。
  输出规则同时要求 H2 又说明加粗标题可算章节，本轮将消除这种产品指令歧义。

## Evaluator section contract

- 6 个完整账户/完整读取回执的反例，分别替换加粗标题、冒号标签、漏章节、乱序、重复、
  改名；原 producer 均错误返回 evidence_pass=true，测试真实 RED（True is False）。
- 新 check_sections 在评测端检查精确中英 H2 标题、顺序和条件期权章节，跳过代码围栏；
  保留旧宽容提取作诊断，不改写报告。期权条件依据持有类型，工具降级不取消章节要求。
- producer 1.0.9 增加 required_section_headings 检查。旧别名仍可提取，但不能满足精确格式。
  原缺盈亏回归仍明确验证数值失败，并补全其他章节，避免新门槛掩盖旧反例。
- 聚焦验证 139 项通过，涉及 Python 文件 Ruff check/format 通过。尚未据此声称模型行为修复。
- 单独 historical-replay-1.0.9.json 回放旧 16 份报告，只标记已知 Q02 格式失败；
  原 events、报告、评分、原审计与归档没有改写。

## Status

独立新留出输入准备中，产品指令尚未改变。后续按 spec 验证候选，保留每个失败及实际成本。
