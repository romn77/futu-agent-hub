# Spec 共享 SEC 证据能力

模块：`sec-evidence`。状态：设计草案。范围与依赖见[总设计](README.md)。

## 1. 目标

为三个研究流程提供可追溯、时间一致、范围明确的公开证据。复用现有 EdgarTools MCP／SDK，补充当前摘要接口缺少的完整读取和确定性归一化能力。成功标准是同一证据能被重复核对，且消费者能识别未知、部分、冲突、截断和真正的零。

## 2. 能力选择

| 需求 | 首选现有能力 | 必须验证的边界 |
|---|---|---|
| 公司／机构身份、申报定位 | `edgar_company`、`edgar_search`、SDK filings | CIK、证券类别、申报期间；分页／历史范围 |
| 指定文件与章节 | `edgar_filing` → `edgar_read` | 固定 accession，识别截断；不反复用 latest 漂移 |
| 财务附注 | `edgar_notes`、指定 filing 的 SDK 对象 | notes 接口若仅支持最新文件，必须核对实际 filing；截止日不符时改用指定文件 |
| 多期财务 | `edgar_trends`／公司 financials 作为线索，具体 filing／facts 核验 | 单季／累计、日期、币种、单位、修订；不得仅信 periods 参数 |
| 内容搜索 | `edgar_text_search` | 搜索结果是定位线索，未读原文不能支持关键结论 |
| 机构持仓概览 | `edgar_ownership` | 显示上限与实际期间；不得当全量源 |
| 指定期间完整 13F | SDK 指定 filing 的原始信息表和封面／摘要 | 原始行、选定 accession、金额单位、修订链、报告类型、总额对账 |

MCP 满足当前问题时直接使用。完整 13F、全文补读或财务期间核验存在实际缺口时，才使用拟议 `sec_read.py` 调用 SDK。工具提示中的 next_steps 不授予新范围或操作权限。

## 3. 输入与时间

所有跨模块请求具有公开对象、研究问题、范围及 `knowledge_cutoff`。截止时点使用带时区时间；13F 另有 `report_period`，财务另有 `period_start/period_end`。`retrieved_at` 只表示读取时间。

- 默认截止时点为本次研究开始时点；同次运行不随请求推进移动。
- 历史研究只使用截止时点前公开的信息，包括修订、分类和辅助指标；没有历史可得性证明就注明无法重建该时点，不以今天的数据冒充当时已知。
- SEC 接收时间与公开可得时间不能无条件等同；优先保留公开发布时间，至少保留 acceptance 时间与语义。仅有日期时，只能证明日期粒度，不能宣称精确到盘中或开盘前已知。
- 公司研究可以比较非自然财年；13F 按明确自然季度末对齐。两者的季度标签不得直接混用。
- 运行截止后出现的文件留待下次运行；不污染已冻结的证据集合。

## 4. 最小数据契约

以下是内部语义字段，不要求普通对话报告展示原始 JSON，也不修改现有 portfolio schemas。V1 在序列化交接或计算边界验证；自然语言结果只展示对结论有用的字段。

| 记录 | 核心字段 |
|---|---|
| `EvidenceRef` | `evidence_id`、provider/interface、公开对象、issuer/manager CIK、accession、form、report_period、filed/accepted/publicly_available 时间及精度、retrieved_at、source_url、section/row locator、coverage |
| `FinancialFact` | concept／定义、value、currency/unit、period_start/end、instant/duration、fiscal_year/period、standalone/cumulative、reported/derived、accounting_basis、EvidenceRef；推导值另含公式与输入引用 |
| `ResearchFinding` | question、claim、fact/inference/scenario、supporting_refs、contrary_refs、limitations、applicable_period、observable_followup |
| `ResearchLead` | public securities、问题、来源 signal/finding IDs、优先理由、截止时点、推荐验证步骤、未完成项；无账户字段、无自动交易动作 |

`EvidenceRef` 的定位信息按证据类型适用：证券分类可能没有 SEC accession，但必须有真实 provider、对象、有效期间和定位；不能为凑字段伪造 SEC 来源。申报事实则必须绑定实际 accession。

状态分开记录：

- `availability`: `COMPLETE / PARTIAL / UNAVAILABLE`，描述声明范围内的取得情况。
- `comparability`: `VERIFIED / LIMITED / UNVERIFIED`，描述能否用于相应比较。
- `issues[]`: 如 `TRUNCATED`、`IDENTITY_AMBIGUOUS`、`PERIOD_UNVERIFIED`、`UNITS_AMBIGUOUS`、`CONFLICT`、`CUTOFF_UNVERIFIED`。
- `not_run_reason`: 预算、未触发或不适用，与数据错误不同。

COMPLETE 只证明声明范围的资料完整，不证明经济结论正确、覆盖真实全部资产或具有预测能力。

## 5. 数值与正文规则

### 5.1 财务

- 保留原值与单位；`null` 不变为零；不能用 `value or 0`。
- 四季度基线必须是四个连续、不重叠的独立期间。累计数只有在范围、口径、币种及修订版本匹配时才差分。
- 年度减前三季累计推导 Q4 时标记 derived；现金流适用的相减不能机械用于 EPS、加权平均股数、利润率或存量数据。
- 同时保留财报时点和申报时点；重述选择注明规则，不静默混用新旧会计口径。
- GAAP、调整后指标、管理层指引、市场共识分别标记。
- SEC Company Facts 不能覆盖所有自定义标签或分部维度；关键业务指标需回到具体申报与附注。[SEC API 范围](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)

### 5.2 正文

- 检查服务端截断、宿主截断、缺页／附件缺失；输出长度不代表原文长度。
- 阅读以问题所需段落、表格和附注为单位。补读到支撑关键判断的范围即可，不机械倾倒整份 10-K。
- 跨期变化必须读两侧同义章节；“未在片段中发现”不能写成“公司不再披露”。
- 每个关键结论引用实际读取的原文位置；管理层解释是有归属的陈述，不能自动认定为客观因果。

### 5.3 13F

共享层负责固定申报、原始行、单位和读取完整性；修订后的有效持仓与跨期算法由[机构 spec](SPEC-institutional-style.md)负责。两层使用同一份 provenance，不各自抓取一份 latest。

## 6. 公开证据复用与保存

复用键包含 provider／接口版本、对象、accession 或数据版本、期间、口径、截断范围。更完整证据可替换同一文件的摘要，但不能覆盖原始来源关系。

- 默认在当前运行或宿主允许的临时状态内复用；独立研究报告不自动写入 thesis/lifecycle。
- 用户明确保存时，分别保存公共证据、研究结果；私有组合关联仍归现有 analyst 保存路径。
- SDK 普通缓存遵循宿主政策；首次接入核对实际写入位置、身份配置及缓存行为，不承诺 helper 清理范围外的文件也会被删除。
- 机构配置路径和列表备注不发送给第三方；逐机构请求仅发送公开 CIK。与持仓相关的公司请求逐公司发送，账户数量、权重、NAV 等只在本地关联。
- SEC 所需调用身份由既有宿主／SDK 身份配置提供，不把它写进研究 payload、示例或报告，也不打印配置值。

## 7. 辅助程序边界与接口草案

必要时增加的 `sec_read.py` 只负责受控公开读取：

| 子能力 | 输入 | 输出 |
|---|---|---|
| `filing-index` | 单一 CIK、forms、期间范围、cutoff、有限分页预算 | 申报列表、覆盖范围、是否穷尽 |
| `filing-content` | 精确 accession、section/attachment、读取范围 | 有定位的正文／表格、截断信息 |
| `financial-facts` | 精确 filing 集合、概念、期间 | 保留实际期间与单位的原始／推导事实 |
| `holdings-13f` | 精确 accession | 封面、摘要、原始信息表、总额、单位诊断与溯源 |

这些是拟议接口，**不是当前存在的命令**。只接受公开标识及明确预算；不接受任意 Python、shell、任意 URL 主机或私人账户文件。补读 URL 必须由已定位文件的 SEC 元数据产生并验证来源。

重试只处理临时错误，最多两次附加尝试；身份／权限拒绝不循环重试。按当前 SEC 和 SDK 限流规则节流，有限指数退避，整体预算超限返回局部 PARTIAL。缓存和并发不能绕过服务限制。

实现风格沿用项目 Python、`snake_case`、显式可空值、边界校验。例如以下仅示意派生数值语义：

```python
def change(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    if current is None or previous is None:
        return None
    return current - previous
```

本地已存在的环境核验命令：

```bash
/Users/yaoma/.local/share/uv/tools/edgartools/bin/python -c 'import importlib.metadata as m; print(m.version("edgartools"))'
```

拟议实现后的离线测试命令，当前不可执行：

```bash
"$SEC_RESEARCH_PYTHON" -m pytest "Futu API Skills/sec-research-core/tests" -q
```

`SEC_RESEARCH_PYTHON` 是部署时显式指定的已验证解释器，不是当前环境已有配置承诺。

## 8. 验收与不做事项

验收对应 [SEC-01 至 SEC-08](ACCEPTANCE.md#2-共享证据)。要求截断、累计季度、时点泄漏、单位误判和错误身份均能被识别；原始来源能够定位；同次运行不重复拉取相同已合格证据。

不构建新的 SEC 网络解析器、通用行情平台、全市场数据仓库、强制远程服务；不改变账户 CLI 或安装／升级本机依赖。实现和安装是后续任务，当前仅定义契约。
