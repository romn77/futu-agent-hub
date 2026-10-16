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
- 本阶段全量离线测试 1395 passed in 18.15s；localhost 测试使用原有回环端口权限。
- 单独 historical-replay-1.0.9.json 回放旧 16 份报告，只标记已知 Q02 格式失败；
  原 events、报告、评分、原审计与归档没有改写。

## Independent freeze and candidate

- 独立准备者完成三个新案例，分别 28、6、7 持仓；未读取旧评测内容或候选指令。
  数据经不变 build_fixture 验证，319 次本地 public-reader 请求成功，9 次预期失败；
  326 处 URL 均为 .invalid。模型上下文沿用 common-prompt，未加测试专用格式/复用提示。
- 冻结数据：evals/format-reuse-fixtures/holdout.json，SHA-256
  `ee7552b09840e95c5181fa87e825cb22b72b2a28e1dfc2435be5f7a74ea3a2db`。
  操作者量表单独保存为 operator-only-rubric.json，SHA-256
  `013fb9ebf05b219e97a0a7f14d0d9c3c84c3cbf365e2fd21071ff85e1542a92b`，不进入模型工作区。
- 输入冻结后，仅修改 SKILL、workflow、output 三份产品文件：明确成功完整读取的复用，
  研究/输出放到必读末尾，给出字面 H2 标题并区分段落标签，发送前核对最终章节。
  默认文件集合、先完整读取后采集、条件加载及账户边界均不变。
- D0 6887 词 / 53,539 字符；加载/架构 26 项及 quick_validate 通过。
- 产品改动后的全量回归暴露旧测试将示例中的 `##` 再次添加的问题（1394 pass / 1 fail）；
  仅修正测试构造，补充中英文示例经过严格检查的断言。最终 1395 passed in 18.42s，
  Ruff 通过；候选产品文件和模型工作区未因此改变。
- 两份旧失败样本开发探针均通过严格章节、账户字段和读取完整性检查：Q02 格式、H01
  重读各一份；完整重读都由旧运行 2 次变为 0，边界审计无问题。探针只作开发诊断，
  不纳入新留出比较。清单和原报告在 development-01/，未使用剩余两个可选开发探针。
- 此候选随后冻结为 candidate/，35 份产品指令与仓库相同；16 槽位顺序预登记，
  每案非指令文件和 prompt 哈希相同。16 份正式评测通过宿主审批启动；之后不调整 R2。

## Status

16 份正式评测运行中；独立盲评及总体验收尚未完成。保留每个失败及实际成本。
