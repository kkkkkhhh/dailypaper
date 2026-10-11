# 智能日报生成器 — 架构设计（design.md）

| 项 | 值 |
|----|----|
| 文档版本 | v1.0 |
| 状态 | 已评审 |
| 上游规范 | `specs/proposal.md` v1.0 |
| 依据 | 《SDD 实战：规范驱动开发之道》（黄佳著，人民邮电出版社）第 5 章 |

**一致性约束**：本文档不得与 proposal.md 冲突。proposal.md 明确「不做智能摘要」，本文档中就不应出现 NLP 模块；proposal.md 规定运行方式为「定时任务」，本文档就不应设计常驻 HTTP 服务。

---

## 1. 系统架构

### 1.1 架构选型

采用**管道式架构（Pipeline Architecture）**：数据如同水流，从源头注入，经过层层处理，最终以成品形态流出。

```
┌──────────────┐
│  GitHub API  │──┐
└──────────────┘  │
┌──────────────┐  │   ┌───────────┐   ┌─────────────┐   ┌────────────┐   ┌──────────┐
│ 飞书任务 API │──┼──▶│ collector │──▶│  generator  │──▶│  notifier  │──▶│ SMTP /   │
└──────────────┘  │   └───────────┘   └─────────────┘   └────────────┘   │ 飞书机器人│
┌──────────────┐  │         │               │                │          └──────────┘
│ 飞书消息 API │──┘         │               │                │
└──────────────┘            └───────────────┴────────────────┘
                                            │
                                    ┌───────────────┐
                                    │    shared/    │  配置 · 日志 · 异常 · SQLite
                                    └───────────────┘
```

**数据流向**：`外部 API → collector（标准化 Record） → generator（DailyReport） → notifier（投递） + storage（存档）`

由 `main.py` 编排，每层只认识相邻层的接口。

### 1.2 为什么不选其他架构

| 候选架构 | 否决理由 |
|---------|---------|
| 微服务 | proposal.md §2.3 明确「不需要常驻服务」，且团队规模为 5～15 人。此体量下引入微服务只增复杂性，得不偿失 |
| 事件驱动 | 数据流呈清晰的线性、同步特征，不存在复杂的事件分发与异步解耦需求 |

**黄金法则**：选择能满足需求的最简架构 —— 「一切应尽可能简单，但不能过于简单」。

## 2. 模块职责

单一职责原则：每个模块应当有且仅有一个引发变更的理由。

> 「不负责」一列比「负责」更关键。若缺乏对模块边界的负面约束，实现时容易「就近」把格式化逻辑塞进 collector、把数据查询塞进 notifier，导致模块边界模糊。

| 模块 | 文件 | 负责 | **不负责** |
|------|------|------|-----------|
| `collector/` | `github.py`<br>`lark_task.py`<br>`lark_msg.py` | 从外部 API 获取原始数据：<br>· GitHub Commit 数据<br>· 飞书任务状态数据<br>· 飞书群消息数据 | · 不做数据格式化<br>· 不去重判断<br>· 不做推送 |
| `generator/` | `formatter.py`<br>`template.py` | 将原始数据组织为日报：<br>· 数据整理<br>· Markdown 生成<br>· 日报模板管理 | · 不做数据采集<br>· 不调 API<br>· 不做推送 |
| `notifier/` | `email.py`<br>`lark_bot.py` | 将日报推送给目标：<br>· SMTP 邮件发送<br>· 飞书机器人消息推送 | · 不做数据处理<br>· 不做日报生成<br>· 不做数据采集 |
| `shared/` | `config.py`<br>`logger.py`<br>`errors.py`<br>`storage.py` | 跨模块共享能力：<br>· 配置读取和校验<br>· 统一日志格式<br>· 自定义异常 + 错误处理策略<br>· SQLite 存储（日报历史） | — |
| （入口） | `main.py` | 编排入口：按顺序调用三层，处理全局异常，记录执行状态 | · 不含业务逻辑<br>· 不直接调用外部 API |

## 3. 数据模型

**定义原则**：只承载语义，不绑定实现。以下明确「一条记录应包含哪些字段」，但未规定「用 dataclass 还是 Pydantic 表示」——后者属实现细节，留给实现方裁量。

### 3.1 采集记录

| CommitRecord（代码提交记录） | 类型 | 说明 |
|---|---|---|
| `author` | str | 提交者（GitHub 用户名） |
| `message` | str | 提交信息（Commit Message） |
| `timestamp` | datetime | 提交时间 |
| `repo` | str | 仓库名称 |
| `additions` | int | 新增行数 |
| `deletions` | int | 删除行数 |
| `files_changed` | int | 变更文件数 |

**共 7 个字段。**

| TaskRecord（任务变更记录） | 类型 | 说明 |
|---|---|---|
| `assignee` | str | 负责人（飞书用户名） |
| `title` | str | 任务标题 |
| `status_from` | str | 原状态 |
| `status_to` | str | 新状态 |
| `updated_at` | datetime | 变更时间 |

**共 5 个字段。**

| MessageRecord（消息记录） | 类型 | 说明 |
|---|---|---|
| `sender` | str | 发送者（飞书用户名） |
| `content` | str | 消息内容（纯文本） |
| `timestamp` | datetime | 发送时间 |
| `chat_name` | str | 群名称 |

**共 4 个字段。**

### 3.2 采集结果封装（ADR-003 引入）

```python
CollectResult[T]:
    source: str            # 数据源标识，如 "github" / "lark_task" / "lark_msg"
    success: bool          # 本次采集是否成功
    records: list[T]       # 成功时返回的记录；失败时为空列表
    error: str | None      # 失败原因；成功时为 None
```

**为什么需要它**：proposal.md §3.3 要求「当日无记录显示『今日无记录』」，§3.2 要求「采集失败显式标注『数据获取失败』」。而裸 `list[T]` 的空列表**无法区分**这两种语义 —— 是「今天真没数据」还是「API 挂了」？两者会产出完全相同的日报，直接导致验收标准无法实现。故采集层接口统一返回 `CollectResult[T]`。决策记录见 ADR-003。

### 3.3 日报模型

| MemberReport（成员报告） | 类型 | 说明 |
|---|---|---|
| `name` | str | 成员姓名 |
| `github_username` | str | GitHub 用户名 |
| `commits` | list[CommitRecord] | 代码提交记录 |
| `tasks` | list[TaskRecord] | 任务变更记录 |
| `messages` | list[MessageRecord] | 相关消息记录 |

| DailyReport（每日报告） | 类型 | 说明 |
|---|---|---|
| `date` | date | 日报日期 |
| `team_name` | str | 团队名称 |
| `members` | list[MemberReport] | 各成员的日报段落 |
| `generated_at` | datetime | 生成时间 |
| `markdown` | str | 完整的 Markdown 格式日报 |
| `html` | str | 完整的 HTML 格式日报 |

### 3.4 跨系统身份映射

`CommitRecord.author` 是 GitHub 用户名，`TaskRecord.assignee` 是飞书用户名 —— 同一人在两个系统中用户名完全不同。**没有映射表，就无法把三个独立数据源的数据按「人」聚合。**

映射表存放在 `config.yaml` 中：

```yaml
members:
  - name: "张三"
    github: "zhangsan"
    lark: "zhangsan@company.com"
  - name: "李四"
    github: "lisi-dev"
    lark: "lisi@company.com"
  - name: "王五"
    github: "wangwu"
    lark: "wangwu@company.com"
```

**聚合规则**：以 `name` 为主键。采集到的记录分别用 `github` / `lark` 字段做匹配，匹配到同一 `name` 的记录归入同一个 `MemberReport`。**不在配置映射表中的账号，其记录一律丢弃**（proposal.md 安全约束 §6.1 访问控制）。

## 4. 接口契约

**信息隐藏原则**：每个模块把复杂性封装在接口背后。采集层不需要感知推送层的存在，推送层也不需要关心数据的来源。

### 4.1 采集层接口

```python
# collector/github.py
def collect(
    repos: list[str],          # 仓库列表（owner/repo 形式）
    since: datetime,           # 起始时间
    until: datetime,           # 结束时间
) -> CollectResult[CommitRecord]: ...

# collector/lark_task.py
def collect(
    project_id: str,           # 飞书项目 ID
    since: datetime,           # 起始时间
    until: datetime,           # 结束时间
) -> CollectResult[TaskRecord]: ...

# collector/lark_msg.py
def collect(
    chat_id: str,              # 群 ID
    keywords: list[str],       # 过滤关键词
    since: datetime,           # 起始时间
    until: datetime,           # 结束时间
) -> CollectResult[MessageRecord]: ...
```

### 4.2 生成层接口

```python
# generator/formatter.py
def generate(
    members: list[MemberReport],   # 各成员数据
    date: date,                    # 日报日期
    team_name: str,                # 团队名称
) -> DailyReport: ...
```

### 4.3 推送层接口

```python
# notifier/email.py
def send(
    report: DailyReport,       # 日报对象
    recipients: list[str],     # 收件人邮箱列表
) -> bool: ...                 # 成功/失败

# notifier/lark_bot.py
def send(
    report: DailyReport,       # 日报对象
    chat_id: str,              # 目标群 ID
) -> bool: ...                 # 成功/失败
```

**注意**：推送层接口返回 `bool` 而非 `CollectResult` —— 推送失败不产生「部分成功」的语义歧义，且失败信息通过日志与告警通道传递（见 §6.1）。

### 4.4 共享层接口

```python
# shared/config.py
def load(path: str = "config.yaml") -> Config: ...
# Config 含：members / github / lark / smtp / schedule / keywords / sensitive_keywords
# 缺少必填字段时抛 ConfigError，错误信息须指明缺失的字段名

# shared/logger.py
def get_logger(name: str) -> Logger: ...
# 输出 JSON lines 格式

# shared/errors.py
class CollectorError(Exception): ...
class GeneratorError(Exception): ...
class NotifierError(Exception): ...
class ConfigError(Exception): ...

# shared/storage.py
def init_db(path: str) -> None: ...
def save_report(report: DailyReport) -> None: ...
def get_report(date: date) -> DailyReport | None: ...
```

## 5. 技术选型（ADR）

> ADR 遵循「只追加，不修改」原则。如需推翻旧决策，不直接修改原文，而应新建一个 ADR 并在其中明确标注「取代 ADR-XXX」。

### ADR-001：使用 httpx 作为 HTTP 客户端

**状态**：已采纳

**背景**：智能日报生成器需要调用多个外部 API（如 GitHub API、飞书开放平台 API）来获取数据。需要选择一个稳定、高效的 HTTP 客户端库来发送这些请求。

**选项**

| 选项 | 优点 | 缺点 |
|------|------|------|
| requests | 社区最广泛，文档丰富，团队熟悉 | 不支持原生异步，连接池管理较弱 |
| **httpx** | 同时支持同步和异步，API 兼容 requests，支持 HTTP/2 | 相对较新，部分边缘场景文档不足 |
| aiohttp | 成熟的异步 HTTP 库 | 仅支持异步，API 风格与 requests 差异大 |

**决策**：使用 httpx

**理由**
1. proposal.md 明确要求「单次日报生成 <60s」，且三个数据源可并发采集，异步能力是实现性能目标的关键
2. httpx 的同步 API 与 requests 几乎完全一致，团队迁移成本极低
3. 采集层当前使用同步模式，未来可平滑切换至异步模式，不需要重写代码
4. 之所以不选 aiohttp，是因为我们不需要强制异步 —— 在同步模式下程序也必须能正常运行

### ADR-002：使用 SQLite 存储日报历史

**状态**：已采纳

**背景**：虽然 proposal.md 明确要求「数据存储：本地 SQLite」，但为确保架构合理性，仍需对这一选择进行正式评估，确认其是否真的适合本场景。

**选项**

| 选项 | 优点 | 缺点 |
|------|------|------|
| **SQLite** | 零部署、零运维、Python 内置支持、文件级备份 | 不支持高并发写入、数据量极大时查询性能下降 |
| PostgreSQL | 功能完整、支持高并发、强大的全文搜索能力 | 需要独立部署和运维、对 5 人团队日报场景过于沉重 |
| JSON 文件 | 实现最简单、不需要任何依赖 | 缺乏复杂查询能力、并发写入不安全、数据量增长后难以管理 |

**决策**：使用 SQLite

**理由**
1. proposal.md 明确运行方式为「定时任务，不需要常驻服务」，这意味着不存在并发写入场景（每天仅执行一次）
2. 5 人团队一年的日报数据约为 1250 条，SQLite 处理起来绰绰有余
3. 零运维高度符合项目定位 —— 不想为了一个简单的日报工具去维护独立的数据库服务
4. Python 标准库 `sqlite3` 开箱即用，不需要引入额外的第三方依赖

### ADR-003：采集层返回 CollectResult 而非裸列表

**状态**：已采纳
**取代**：design.md v0.5 中「采集层接口返回 `list[Record]`」的定义

**背景**：design.md 初稿将采集层接口定义为 `collect(...) -> list[Record]`。在编写 tasks.md 时发现该契约存在缺陷：Task 6 的验收标准要求「若某数据源采集失败，日报中需要标注『数据获取失败』」，但裸 `list[Record]` 无法区分「今日确实无数据」（空列表，正常）与「API 调用失败」（空列表，异常）—— 这两种截然不同的情况在现有契约下产出完全相同的日报。这是实现前从规范层暴露出的设计缺陷。

**选项**

| 选项 | 优点 | 缺点 |
|------|------|------|
| 裸 `list[Record]` | 签名简单 | 无法区分空数据与失败；验收标准无法实现 |
| 失败时抛异常 | 语义直接 | 单个数据源失败会中断整个采集流程，违背「优雅降级」原则 |
| **`CollectResult[T]`** | 显式携带 success 标志与错误信息；调用方自行决定降级策略；兼容优雅降级 | 调用方需要多解一层包装 |

**决策**：使用 `CollectResult[T]`（定义见 §3.2）

**理由**
1. 它是唯一能同时满足 proposal.md §3.2「单一数据源失败不阻断其他源」与 §3.3「无记录显示『今日无记录』」这两个约束的方案
2. 失败作为**返回值**而非异常，使「部分失败」成为一等公民，而非需要 try/except 兜底的意外路径
3. 该修订证实了 SDD 的价值：缺陷在规范层被发现并修正，而不是等代码写完后靠临时 hack 打补丁

**代价**：所有采集模块的调用方都必须显式处理 `success=False` 分支。这是有意的 —— 强制调用方直面失败，而非静默吞掉。

## 6. 非功能性约束

proposal.md 已用一句话界定了性能与重试要求；本节将其展开为可实现的策略。

### 6.1 错误处理策略（优雅降级）

航空工程师早已深谙此道：单台发动机故障不应导致整架飞机坠毁。

| 场景 | 处理方式 |
|------|---------|
| GitHub API 超时 | 重试 3 次（间隔 5s），若仍失败则标记「数据获取失败」 |
| 飞书 Token 过期 | 自动刷新 Token 后重试 1 次 |
| 单个数据源完全不可用 | 跳过该数据源，日报中标注「xx 数据源暂不可用」，其他数据源正常采集 |
| 所有数据源都不可用 | 记录错误日志，发送告警邮件，**不生成空日报** |
| 邮件发送失败 | 重试 2 次，若仍失败则记录日志和飞书消息告警 |
| 飞书推送失败 | 重试 2 次，若仍失败则记录日志和邮件告警 |

**关键原则**
1. 采集层的失败不应阻塞生成层
2. 生成层的失败不应阻塞推送层（需要推送错误报告）
3. 任何失败必须有日志记录
4. 推送渠道应互为备份告警通道

### 6.2 安全约束

**1. 密钥管理**
- 所有 API 密钥均通过**环境变量**注入
- 严禁在代码或配置文件中硬编码密钥
- 加入 `.env` 文件则**必须**加入 `.gitignore`

**2. 数据安全**
- 对飞书消息中的敏感关键词（如薪资、绩效、裁员等）进行过滤（配置黑名单）
- 日报中**不包含代码差异的具体内容**（仅包含统计数据，如 +10 行 / -5 行）

**3. 访问控制**
- 仅采集配置文件中明确列出的仓库或群组
- 不采集配置范围之外的任何数据

### 6.3 可运维性设计

**1. 日志**
- 格式：采用 JSON lines（便于日志分析工具解析）
- 级别：分为 INFO（正常流程）+ ERROR（异常）
- 每次执行记录：开始时间、结束时间、各数据源采集条数及推送结果

**2. 健康检查**
- 提供 `main.py --check` 模式：验证所有 API 连接是否正常、验证邮件配置是否正确、验证飞书机器人权限是否正常
- 建议部署后每日执行一次

**3. 配置热更新**
- 成员映射表（`config.yaml`）修改后下次执行即可自动生效，不需要重启服务
- 新增或删除成员时不需要修改代码

### 6.4 性能约束

| 指标 | 目标 | 来源 |
|------|------|------|
| 单次日报生成耗时（5 人团队） | <60s | proposal.md §3.2 |
| GitHub Commit 统计 | 列表接口不返回增删行数，需对每条 commit 追加一次详情请求 | 实现约束 |

> **实现提示**：GitHub 的提交列表接口（`GET /repos/{owner}/{repo}/commits`）**不返回** `additions` / `deletions` / `files_changed`，这三个字段只有单条提交详情接口（`GET /repos/{owner}/{repo}/commits/{sha}`）才返回。因此采集每条 commit 需要 1 + N 次请求。在 5 人团队单日提交量级下可接受，本期不做缓存优化（对应 proposal.md §2.2「不做历史搜索与统计图表分析」）。

## 7. 规范版本与变更管理

**三条原则**
1. **规范与代码同步提交** —— 规范的变更应与代码变更保持在同一个 Commit 中，以确保文档与实现的一致性
2. **先提交规范，后提交代码** —— 进行复杂变更时，可将规范变更与代码实现分成两步提交
3. **ADR 只追加，不修改** —— 不得删除或篡改旧 ADR。如需推翻旧决策，应新建 ADR 并标注「取代 ADR-XXX」

**版本演进节奏**
- v0.1（最小可行规范）：proposal.md 仅含最核心的功能定义和项目边界
- v0.5（基线规范）：proposal.md + design.md + tasks.md 三者构成完整规范集合，足以支撑正式编码
- v1.0（首版完成）：代码实现完成并通过所有测试，规范文档与代码实现完全对齐
- v1.1、v1.2……（持续演进）：每次功能新增或缺陷修复，规范与代码同步更新

**变更影响链**

| 变更类型 | 起点 | 影响链 |
|---------|------|--------|
| 需求变更 | proposal.md | proposal.md → design.md → tasks.md → 代码 → 测试 |
| 架构变更 | design.md | design.md → tasks.md → 代码 → 测试 |
| 任务变更 | tasks.md | tasks.md → 代码 → 测试 |
| 缺陷修复 | 代码 | 代码 → 测试（可能反向更新 design.md） |

判断标准：审视变更究竟改变了「做什么」（proposal.md）、「怎么做」（design.md），还是仅仅影响「如何实现」（代码）。找到受影响的最上游层级，从那里开始修改。
