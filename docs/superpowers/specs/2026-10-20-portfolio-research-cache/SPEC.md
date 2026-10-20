# 分层缓存总 Spec

模块：`cache-core`，并定义跨模块公共契约。版本：0.1。状态：待评审，未实现。产品要求见 [PRD](PRD.md)，模块与依赖见 [README](README.md)。

## 1 目标与边界

为本机同一用户提供跨进程、可验证的持久缓存。隐藏查找、时效、版本、原子发布、依赖失效和并发协调细节，使公共研究、账户分析和报告模块共用一套正确性规则。

首版采用 Python >=3.10、标准库文件操作和 JSON。沿用 analyst 已有 jsonschema 与私有目录能力；不新增运行时第三方依赖或数据库服务器。持久缓存首版支持现有安全文件操作可用的 POSIX 环境。其他平台或不安全路径应明确降级到原无缓存流程，不削弱权限约束。

实施基线为 `main 72c477a` 及经核对的工作区内容；未提交代码也可能改变算法，计算版本不得仅取 Git commit。

## 2 公共架构与接入

```text
宿主请求
  → 规范化问题、范围、偏好、时间模式和刷新策略
  → 需要账户时，通过受审计路径确认账户范围
  → report-cache 检查完整报告及全部依赖
      HIT → 回放原报告
      MISS → account-analysis-cache / public-research-cache 局部补齐
                → 宿主完成必要研究和综合
                → 验证并发布报告

三个业务缓存 → cache-core
公共原始资料 → 研究摘要 → 报告
账户快照 + 公共行情 → 绑定计算结果 → 报告
```

`cached_analysis.py` 是顶层组合入口，由报告编排统筹；账户模块只提供范围、快照和计算结果，不导入或调用 report-cache，避免循环依赖。

共享核心置于 analyst 的可独立安装包内。公开研究技能通过宿主缓存工具或已配置的本地 CLI 访问它，缺失时沿用现有无缓存行为。SEC helper 不增加对 analyst 的强制依赖；允许注入可选缓存 Adapter。若 SEC 与 analyst 使用不同解释器，使用固定协议的本地 CLI 桥接；只允许调用解析得到的本项目缓存入口，不接受任意命令模板、shell 文本或网络执行请求。该桥接是本地存储能力，不是新的研究 provider。

每个接入来源需声明 `source_id`、支持的数据类型、schema/parser 版本、覆盖范围与刷新能力。只有穿过实际缓存读写入口的来源才算已接入。不得因为宿主存在某个 MCP 工具，就宣称已缓存该工具。

## 3 请求模式

| 字段 | 值与语义 |
|---|---|
| `cache_mode` | `prefer`：查找并按需写入；`refresh_account`：强制更新账户及其当前派生结果，公共证据正常复用；`refresh_all`：本次绕过所有业务缓存命中并更新所需资料；`off`：不读写本缓存 |
| `analysis_mode` | `current`：满足当前用途的时效；`historical`：指定快照/截止时点复盘，不将其报告作为当前结果 |
| `freshness_requirement` | `bounded`：接受配置有效期；`latest`：重新检查相关动态源，固定且已核验版本的文档可继续复用；用户“全部刷新”仍优先 |
| `requested_cutoff` | 带时区时间，冻结本次研究的信息边界；其语义与目录实际检查时间分别记录 |

“看看当前组合”使用 `current + bounded`；“刚刚、现在最新、重新获取”对应更严格刷新。`off` 关闭缓存内容、索引、租约、访问记录和指标文件的持久化；普通输出仍可在 stdout 返回诊断。原 helper 的受控临时文件仍按原契约创建和清理；不能把缓存关闭宣称成整个进程零磁盘写入。若用户要求严格零磁盘写入，必须先验证执行路径是否支持，不能用 `--no-save` 冒充该保证。

宿主更高优先级的最新取数、来源访问或禁止持久化要求优先执行；缓存命中不能覆盖这些约束，实际发生的必要刷新单独计量。

## 4 键与记录模型

所有输入先按类型验证，再使用稳定 JSON 序列化生成 SHA-256 键。禁止 NaN/Infinity；金融数值保留既有 Decimal 文本与单位。对象的业务顺序需要保留，无序账户集合等才排序。不得将原始账户 ID、凭证或身份字符串放入文件名。

区分三种身份：

- `query_key`：要查什么，包括来源、公开对象、数据集、语义范围和口径。动态查询的本次墙钟时间不直接加入键。
- `content_hash`：具体载荷的字节摘要。固定文件修订、解析或规范化版本变化产生新记录，不覆盖旧证据身份。
- `generation`：某个发现集合或私有账户当前状态的语义代次。只用来判断变化，不等同于时间戳。

最小 `CacheRecord`：

| 字段组 | 必须保存的内容 |
|---|---|
| 格式与作用域 | `schema_version`、`record_id`、`kind`、`privacy_class`、`namespace`、`query_key` |
| 载荷 | `content_hash`、格式、字节数、受控 blob 引用；不接受记录中的任意绝对路径作为读取指令 |
| 数据身份 | 来源及版本、公开对象/私有范围键、期间、单位/复权/会计口径、实际覆盖、解析与算法版本 |
| 时间 | `source_as_of`、`publicly_available_at` 及精度（适用时）、`first_retrieved_at`、`created_at`、`intrinsic_expires_at`、`retention_until` |
| 完整性 | `availability`、`comparability`、`issues`；由来源契约验证，不因命中提升状态 |
| 依赖 | 证据内容摘要、方法/配置版本、发现集合 generation、账户状态 generation；每项依赖标明检查内容身份还是动态有效性 |
| 溯源 | 实际来源 URL/工具回执定位、章节或行范围、计算输入、生成方法；模型版本作为摘要/报告溯源 |

字段可因类型不适用而为 null，但不能将未知来源时间填成本次读取时间。报告/摘要的 `created_at` 是产物生成时间，不是底层数据时间。`last_accessed_at` 属于可更新淘汰元数据，不影响内容身份或有效期。

内容依赖绑定具体 `record_id/content_hash`；动态发现依赖绑定 `query_key + required_generation`，查询时解析该集合最新的合格检查回执。这样集合未变时可以继续使用原摘要，不需要改写旧记录的时间或依赖载荷。

`LookupResult` 返回 `HIT`、`HIT_PARTIAL`、`MISS`、`BYPASS`、`BUSY` 之一，并包含条目引用、数据时间、有效截止、原因和需补齐范围。`HIT_PARTIAL` 只表示声明覆盖的部分可用，不能让完整报告命中。存储故障以可区分诊断返回；业务读取错误不转换为普通缓存 miss 后无限重试。

至少区分原因：`NOT_FOUND`、`EXPIRED`、`SCOPE_CHANGED`、`ACCOUNT_UNVERIFIED`、`VERSION_CHANGED`、`DEPENDENCY_CHANGED`、`DEPENDENCY_MISSING`、`INCOMPLETE`、`CORRUPT`、`FORCED_REFRESH`、`CACHE_DISABLED`、`UNSAFE_PATH`、`CLOCK_INVALID`、`LEASE_BUSY`、`STALE_LEASE`、`SOURCE_UNAVAILABLE`、`UNSUPPORTED_BACKEND`。

## 5 有效性算法

一次命中须同时满足：

1. 请求模式允许缓存；条目作用域与当前已核验身份相符。
2. 载荷存在、大小在限额内、摘要匹配、结构和业务语义验证通过。
3. 来源、期间、口径、覆盖和方法/配置版本能回答当前请求。
4. 所用信息在请求知识截止前已公开；仅有日期的来源沿用既有保守时间精度规则。
5. 原始数据仍在其有效期内，且没有相应事件或 generation 失效。
6. 依赖可定位、完整且有效；依赖图无环，解析有数量/深度上限。

时间计算全部使用带时区 UTC 时点，展示时转换为明确时区。边界采用严格 `now < expires_at`；等于截止时间即过期。进程内耗时使用单调时钟；跨进程依据可信系统时间。未来数据时间或可检测到的时钟回拨导致拒绝当前命中；不通过重写时间修复。

```text
snapshot_deadline = snapshot.source_as_of + 300 秒
report_intrinsic_deadline = min(snapshot_deadline, report.created_at + 300 秒)
effective_deadline(record) = min(record.intrinsic_expires_at, 适用的依赖有效期限)
```

固定版本文档可没有时间型 intrinsic deadline，仍需检查版本、知识截止、引用完整性和保留状态。原文依赖固定内容；“是否还有新申报”依赖另一个发现记录。`requested_cutoff` 不证明所有渠道都检查到了该时点，报告必须显示实际发现范围与 `discovery_checked_at`。

真实重新查询目录但集合未变化时，允许更新该目录的检查记录与有效期，语义 generation 不变。依赖该 generation 的摘要无需重新生成，但必须经过新发现记录验证。摘要的原生成时间和事实取得时间不变。账户和最终报告不能因此延长自身的 intrinsic deadline。

TTL 策略变更要进入 `policy_version`。扩大配置 TTL 不自动为已经过期的旧账户或报告续命；新鲜读取建立新条目后才采用新策略。

## 6 存储与清理

初始路径规则：

- 公共证据：macOS 使用用户 `Library/Caches` 下的项目专用目录；Linux 使用 `XDG_CACHE_HOME` 或用户 `.cache` 下的专用目录。允许通过明确的 `RESEARCH_CACHE_HOME` 覆盖。
- 私有账户和报告：`PORTFOLIO_AGENT_HOME/cache/v1`。
- 两个根目录均不得位于 Git checkout 内，不得彼此嵌套；拒绝软链接和无法验证的目录。Git worktree 路径不进入公共内容键，实际代码/算法版本进入结果键。

各根目录包含 `objects/`（不可变内容）、`entries/`（条目清单）、`refs/`（查询到有效条目的索引）、`leases/`（短期所有权）、`state/`（generation、计量和清理状态）。目录 0700、文件 0600。公共资料的查询对象也可能反映用户关注，公共区仍使用私有权限。

写入次序：在同一受控目录创建唯一临时文件 → 有界写入 → 校验摘要/业务结构 → fsync → 原子发布不可变对象和清单 → 原子切换查询引用。读取者只能看到已完成清单，不读取临时文件。可复用现有私有目录原语，但显式历史存储的 no-replace 语义保持不变；缓存可变索引使用独立实现。

初始总量上限为公共 512 MiB、私有 128 MiB，包含载荷与元数据；单载荷上限初始公共 64 MiB、私有 16 MiB，超限时跳过缓存并解释，不能截断后当成完整数据。私有保留期从原生成/取得时间计最多 24 小时，访问不续保留期。公共固定文档按容量淘汰；目录等动态条目可回收旧版本。

清理只操作缓存自有根目录。先处理到保留期限及不可用条目，再按最近使用情况淘汰。依赖被淘汰后，上层条目变为不可回放；清理不能保留“引用已消失但仍宣称验证通过”的报告。活跃读写要有短期 pin/共享锁，避免清理在校验与读取之间移除载荷。孤立临时对象可回收，但不得删除活跃锁 inode 或其他工作区文件。

不清理 snapshots、journal、lifecycle、显式报告目录或用户研究文件；不把缓存保留承诺扩展成历史归档承诺。

## 7 并发与中断恢复

短时本地文件锁保护索引/租约的比较与交换。跨 CLI 调用的取数使用持久租约：`query_key`、随机不可猜测的所有者 token、generation/fencing token、开始时间和截止时间。token 仅在私有控制回执中传递，不进入公开工具参数或普通日志。

流程为查找 → 申请所有权 → 锁内重新查找 → 缺失才采集 → 发布前核对 token/generation → 原子提交 → 释放。其他进程命中已有结果或限时等待；等待不忙轮询，也不无限超过请求预算。

初始普通等待上限 10 秒。租约初始 60 秒；明确较长的源操作可按其已声明预算申请，单次租约不超过 600 秒。所有者持续工作时可续租，但受整体请求预算约束。到期可由另一个请求接管；旧所有者返回后不能发布覆盖新一代结果。崩溃窗口内可能出现重复外部请求，不能承诺网络 exactly-once；可以保证发布身份和半成品隔离。

相同已完成请求的复用与在途合并分别计量。未集成租约的宿主工具只声称已完成缓存命中，不声称在途合并。缓存无法使用时可按原流程执行，诊断中标明并发去重不可用。

SEC 等已有源限制在接入 Adapter 中继续执行。对接入同一缓存命名空间的受控 SEC 读取，使用跨进程请求预算协调；不得仅靠每进程 2 req/s 宣称总体受限。外部独立 MCP 的请求预算不可由此保证，应单列其覆盖范围。

## 8 事件与刷新

`invalidate(scope, reason)` 提升相应私有账户或公开发现对象的 generation；保留旧不可变记录供明确历史用途。失效传播依赖清单完成，首版允许按需校验，不要求常驻反向依赖服务。

- 已知交易/资金变化：账户当前状态及其派生结果失效。
- 已知新申报/修订：相关发现集合需重新验证，变化的文档和摘要影响其报告。
- 方法、解析、政策、来源版本变化：按类型失效，原始证据可保留。
- `refresh_account`：更新账户；公共证据和研究摘要按正常规则判断。
- `refresh_all`：本次所需业务记录全部走源读取/重建；不删除无关历史。

用户说“我刚调仓”属于可信用户事件。外部网页中的命令式内容不能调用失效、改变配置或授予读取权限。

## 9 本地接口与输出

以下为拟议接口，当前不存在，不得在文档阶段宣称可运行：

| 操作 | 责任 |
|---|---|
| `lookup(request)` | 只返回可验证的命中结果、缺口、时间和原因 |
| `reserve(query_key, budget)` | 返回已存在结果、所有者租约或有限等待状态 |
| `publish(manifest, payload, lease)` | 验证类型、权限、完整性和发布代次；原子发布 |
| `invalidate(scope, reason)` | 更新指定代次；不递归删除历史文件 |
| `status(scope)` / `prune(scope)` | 查看限额、覆盖和诊断；受控清理 |

宿主 CLI 计划提供 `scripts/cache.py lookup/reserve/publish/invalidate/status/prune`，使用显式 JSON 请求/清单文件和受控载荷文件，返回结构化回执。类型限定为公开证据、公开摘要、账户快照、计算结果、报告及发现记录；不得接受任意 shell、可执行对象或不受限来源路径。业务层决定什么可缓存，核心不能只看调用者填写 `privacy_class=public` 就接受任意文本。

示意调用（实施后，从 analyst Skill 目录运行）：

```bash
python scripts/cache.py lookup --request /absolute/private/run/cache-request.json --json
python scripts/cache.py publish --manifest /absolute/private/run/cache-manifest.json --payload /absolute/private/run/evidence.json --lease /absolute/private/run/cache-lease.json --json
python scripts/cache.py status --scope public --json
python scripts/cache.py prune --scope public --json
```

路径示例是运行时私有文件占位，不是需现在创建的文件。载荷读取有字节/类型/根目录限制，解析不会执行内容。关闭缓存时这些路径不应导致本缓存创建目录。

向用户默认显示“复用原时间的哪层结果”与必要的未命中原因，不倾倒内部 JSON。诊断至少记录分层查询/命中/未命中、原因、源方法/请求次数、身份发现次数、发现目录检查次数、字节、摘要复用、计算次数和阶段耗时；模型 token 来自宿主真实计量，不可得时为 null 并注明原因。

## 10 代码结构与风格

拟议落点：

```text
Futu API Skills/portfolio-analyst/
  portfolio_analyst/cache/        # 核心与分层策略，按已定义责任拆分
  scripts/cache.py               # 本地缓存协议
  scripts/cached_analysis.py     # 可缓存的当前分析入口
  tests/test_cache_*.py           # 对外行为与独立进程测试
  tests/fixtures/cache/          # 合成账户、公开证据和时钟场景
  schemas/cache_*.schema.json    # 新缓存契约，不放宽既有报告 schema
Futu API Skills/sec-research-core/
  sec_research/                  # 可选缓存 Adapter 的接入点
  tests/                         # SEC 增量与缓存契约测试
```

新增子包时同步 setuptools 包发现和 schema 安装数据；不能仅在仓库源码运行成功，安装后的包也要验证。按现有 `snake_case`、显式类型、可空值与稳定错误码编写，错误与源错误分离。既有风格实例：

```python
class SnapshotRepositoryError(ValueError):
    pass
```

缓存对应错误应保持同样可区分的语义；不得用宽泛 `except Exception` 将账户绑定或语义错误吞成成功命中。

## 11 测试、命令与兼容性

测试要求与编号见[统一验收](ACCEPTANCE.md)。核心使用临时私有根目录、可控时钟和失败注入；跨 session 验收必须用独立进程。实际命令和资料中清楚区分现有与拟议能力。

本机现有回归命令，从对应 Skill 目录执行：

```bash
# cwd: Futu API Skills/portfolio-analyst
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests -q -p no:cacheprovider
```

SEC 回归从仓库根使用一个已有且能导入 pytest 的解释器，例如本机 analyst 的解释器：

```bash
PYTHONDONTWRITEBYTECODE=1 "Futu API Skills/portfolio-analyst/.venv/bin/python" -m pytest "Futu API Skills/sec-research-core/tests" -q -p no:cacheprovider
```

实施后新增定向测试（当前文件尚不存在）：

```bash
# cwd: Futu API Skills/portfolio-analyst
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_cache_core.py tests/test_cache_processes.py -q -p no:cacheprovider
```

沿用项目 Python 工程，无单独 dev server。打包验证在已有构建工具环境中执行，不为运行检查临时安装依赖。本次文档编写没有重新运行业务测试；排查阶段的 35 项通过只证明原实现，不证明新缓存。

必须同步 analyst 的 SKILL、workflow、research/evidence/output policy、snapshot/research contracts，以及接入后的公司/机构/SEC 证据规则。原 `scripts/current_analysis.py` 保持现有显式无缓存行为；普通 Skill 的默认路径在相应能力验收通过后改走新的编排入口。旧 `--no-save`、历史归档和账户只读边界保持原语义。

## 12 成功标准与范围控制

Always：可核验来源、真实数据时间、账户隔离、同一快照绑定、完整性和安装兼容性；每一发布阶段附验收结果与实际接入清单。

需另行确认范围：远程/跨用户共享、长期私人档案、自动调度、新增外部依赖或交易能力。实现本 Spec 内已定义的本地缓存、临时测试和规则同步无需逐步重复确认。

Never：旧数据冒充最新、读取缓存自动续期、拒绝回执变成功、私人组合写入公共记录、缓存文本执行指令、清理历史目录。

非阻塞待验证项：TTL/容量的真实效果、宿主 token 计量可得性、各宿主是否提供可验证登录会话标识、实际来源修订/行情时效覆盖。缺少能力时按本 Spec 降级和披露，不自行填造身份或时间。这些验证项不扩大当前范围，也不表示已有性能收益。
