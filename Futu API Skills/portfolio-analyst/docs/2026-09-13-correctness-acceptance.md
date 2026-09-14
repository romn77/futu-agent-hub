# Portfolio Analyst 0.7.1 修复与验收记录

日期：2026-09-13\
开发分支：`codex/portfolio-correctness`\
开发工作树：仓库 `.worktrees/portfolio-correctness`；本地合并目标为 `main`\
基线提交：`41ea411`

## 已实施

- 风险及组合报告升级到 1.1，保持 1.0 报告读取及旧指标口径标签；软件版本为 0.7.1。
- 分类约束显式保留已知／未知 NAV 权重和上下界；UNKNOWN 不再作为真实行业、地区或题材触发集中风险。
- 持仓内部 HHI 和有效持仓数按相对持仓权重计算，NAV、现金、Top-N 权重及调仓模拟原 NAV-HHI 口径保持一致。
- 风险生产、独立语义校验、组合报告绑定、CLI/Markdown 展示和模型输出规范同步更新。
- SDK 包安装、导入和 OpenD 分阶段诊断，保留版本、解释器和已确认状态；检查必需行情方法，保留失败前已确认的 TCP 连通证据。
- 修正空仓加风险上限的校验冲突，以及 500/1000 等大规模持仓的精度容差问题。
- 日志首次创建改为排他创建，其他写入者打开已发布文件后加锁；所有路径仍保留 no-follow 和权限检查。
- 测试区分 macOS `/tmp` 符号链接与真实临时目录；子进程测试使用有界等待验证 SIGKILL 后退出，保留输出预算和后代进程退出断言。

## 测试结果

在工作树 `Futu API Skills/portfolio-analyst` 目录运行：

```sh
"/Users/yaoma/project/futu-agent-hub/Futu API Skills/portfolio-analyst/.venv/bin/python" -m pytest -o addopts='' -q --tb=short
```

结果：**1228 passed in 16.85s**，macOS，Python 3.12.4。网络监听测试在沙箱外执行；数据均为本地合成数据。

已完成以下针对性验证：

- 未知 90% / 上限 60% → UNKNOWN，不能生成 UNKNOWN 行业 HIGH 结论。
- 已知科技 70% / 未知 20% / 上限 60% → 已知科技 BREACHED。
- 已知科技 40% / 未知 30% / 上限 60% → UNKNOWN，不能误报通过。
- 科技 30% / 医疗 20% / 未知 10% / 上限 60% → 保守上界 40%，WITHIN_LIMIT。
- 股票 10% / 现金 90% → 有效持仓数 1；两只各 25% → 有效持仓数 2。
- 空仓以及六种配置上限组合的风险和组合报告均可验证。
- 500/1000 持仓回归通过；独立审查另验证了 10,000 持仓，篡改有效持仓数会被拒绝。
- 保留旧版完整 risk/portfolio 合成 fixture，验证旧口径和新旧版本隔离。
- 日志并发实测连续 30 轮，每轮 8 条记录全部保留，哈希链验证通过。
- SDK 缺失、权限错误、接口不兼容、连接拒绝、超时及 worker 非正常退出的诊断回归通过。

Ruff 0.15.12 离线检查通过；格式检查通过，Git diff 空白检查通过。规格审查与独立最终代码审查均通过，无待修审查问题；审查另以 200 个随机有效合成组合验证了分类、题材和策略状态的一致性。

## 实际运行诊断

```sh
"/Users/yaoma/project/futu-agent-hub/.venv/bin/python" scripts/portfolio.py doctor --json --no-save
```

结果：Python 3.13.3；futu-api **10.10.7008**；installed/imported/quote_api_available 均为 true；SDK status 为 ready。默认 `127.0.0.1:11111` 的 OpenD 状态为 **connection_refused**，reachable/available 为 false，命令退出码 1。

这证明 SDK 与连接失败已正确区分，不代表真实账户端到端通过。未获取任何实际账户持仓。

## 尚未完成的外部验收

| 验收项 | 状态与原因 |
|---|---|
| 真实账户/OpenD 全流程 | 未验证。默认 OpenD 没有运行，无法取得真实快照；后续需可用 OpenD 及明确账户范围 |
| Linux 测试 | 未执行。已检查本机 Docker，但 daemon 不可用；本次没有启动现有容器服务或访问外部服务器 |
| 真实模型合成评测 | 已获授权并完成三轮调用；第三轮执行证据通过、正式断言 15/18，状态仍为 ASSERTIONS_FAILED。剩余为重点研究章节识别限制，详见独立评测记录 |

本次模型案例是 `comprehensive-many-holdings`，使用仓库 `evals/run_codex_evals.py`。已授权的发送内容为隔离后的 portfolio-analyst 技能/引用文档/实现代码及该案例的合成数据，不包括真实账户数据。自动审批拒绝理由是技能代码外发的目的地和 payload 尚未被认定具有明确用户授权。用户随后明确回复“可以”，本次已按原评测入口执行三轮；全部原始失败结果保留，未以人工核对或模型自报结果代替正式验证。

本次评测发现并修复了标题边界、部分中文标题和等价时区时间的解析问题，新增反例回归；详细结果及原始证据见 [真实模型合成评测记录](2026-09-13-model-evaluation.md)。

因此可以声明本地核心修复与 macOS 合成测试通过；真实模型调用已完成但正式行为验收未通过，全部平台与真实账户验收也尚未完成。
