# 智能日报生成器 — 架构设计（design.md）

| 项 | 值 |
|----|----|
| 文档版本 | v0.5 |
| 状态 | 已评审 |
| 上游规范 | `specs/proposal.md` v0.5 |
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
┌──────────────┐  │   ┌───────────┐   ┌───────────┐   ┌─────────────┐   ┌────────────┐
│ 飞书任务 API │──┼──▶│ collector │──▶│ generator │──▶│  notifier   │──▶│ SMTP /     │
└──────────────┘  │   └───────────┘   └───────────┘   └─────────────┘   │ 飞书机器人 │
┌──────────────┐  │         │          (聚合+生成)     (推送+告警)      └────────────┘
│ 飞书消息 API │──┘         │               │                │
└──────────────┘            └───────────────┴────────────────┘
                                            │
                                    ┌───────────────┐
                                    │    shared/    │  配置·日志·异常·存储·日历
                                    └───────────────┘
```

**数据流向**：`外部 API → collector（CollectResult[Record]） → aggregator（list[MemberReport]） → generator（DailyReport） → notifier（投递） + storage（存档）`

由 `main.py` 编排，每层只认识相邻层的接口。

> **失败信息的传递路径**（关键）：采集失败的信息必须**逐层透传**到生成层，否则日报无法标注「数据获取失败」。传递链为 `CollectResult.failed / success → aggregate() 透传 → generate(failed_sources=...) → 日报正文标注`。这条链上任何一环断开，proposal.md §3.2 的验收标准都无法实现。

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
| `collector/` | `github.py`<br>`lark_task.py`<br>`lark_msg.py` | 从外部 API 获取原始数据：<br>· GitHub Commit 数据<br>· 飞书任务状态数据<br>· 飞书群消息数据 | · 不做数据格式化<br>· 不去重判断<br>· 不做推送<br>· **不做跨源聚合** |
| `generator/` | `aggregator.py`<br>`formatter.py`<br>`template.py` | 把原始数据组织为日报：<br>· **按成员映射聚合跨源记录**<br>· 数据整理<br>· Markdown 生成<br>· 日报模板管理 | · 不做数据采集<br>· 不调 API<br>· 不做推送 |
| `notifier/` | `email.py`<br>`lark_bot.py`<br>`alert.py` | 将日报推送给目标：<br>· SMTP 邮件发送<br>· 飞书机器人消息推送<br>· **异常告警投递** | · 不做数据处理<br>· 不做日报生成<br>· 不做数据采集 |
| `shared/` | `config.py`<br>`logger.py`<br>`errors.py`<br>`storage.py`<br>`calendar.py` | 跨模块共享能力：<br>· 配置读取和校验<br>· 统一日志格式<br>· 自定义异常 + 错误处理策略<br>· SQLite 存储（日报历史）<br>· **工作日/节假日判定** | — |
| （入口） | `main.py` | 编排入口：按顺序调用各层，处理全局异常，记录执行状态 | · **不含业务逻辑**（聚合属生成层，见 §4.5）<br>· 不直接调用外部 API |

> **`main.py` 边界的澄清**：编排层只做「按顺序调用 + 异常兜底 + 执行状态记录」，不承担任何数据变换。§3.4 的成员聚合规则因而**不属于** `main.py`，而是由 `generator/aggregator.py` 承接（§4.5）。

## 3. 数据模型

**定义原则**：只承载语义，不绑定实现。以下明确「一条记录应包含哪些字段」，但未规定「用 dataclass 还是 Pydantic 表示」——后者属实现细节，留给实现方裁量。

### 3.1 采集记录

| CommitRecord（代码提交记录） | 类型 | 说明 |
|---|---|---|
| `author` | str | 提交者（GitHub 用户名） |
| `message` | str | 提交信息（Commit Message） |
| `timestamp` | datetime | 提交时间（tz-aware） |
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
| `updated_at` | datetime | 变更时间（tz-aware） |

**共 5 个字段。**

| MessageRecord（消息记录） | 类型 | 说明 |
|---|---|---|
| `sender` | str | 发送者（飞书用户名） |
| `content` | str | 消息内容（纯文本） |
| `timestamp` | datetime | 发送时间（tz-aware） |
| `chat_name` | str | 群名称 |

**共 4 个字段。**

### 3.2 采集结果封装（ADR-003 引入，ADR-004 扩展）

```python
CollectResult[T]:
    source: str               # 数据源标识："github" / "lark_task" / "lark_msg"
    success: bool             # 全部子项均成功才为 True（见下方语义）
    records: list[T]          # 已采集到的记录；部分失败时保留已采到的，不丢弃
    error: str | None         # 失败原因摘要，人类可读；全部成功时为 None
    failed: list[str]         # 失败子项明细（如具体仓库名）；全部成功时为空列表
```

**为什么需要它**：proposal.md §3.3 要求「当日无记录显示『今日无记录』」，§3.2 要求「采集失败显式标注『数据获取失败』」。而裸 `list[T]` 的空列表**无法区分**这两种语义 —— 是「今天真没数据」还是「API 挂了」？两者会产出完全相同的日报，直接导致验收标准无法实现。故采集层接口统一返回 `CollectResult[T]`。决策记录见 ADR-003。

**`success` 的判定语义（ADR-004 明确）**

`success` 反映**整个数据源是否完全正常**，采用全部成功判定：

| 情形 | `success` | `records` | `failed` |
|------|-----------|-----------|----------|
| 全部子项成功，有数据 | `True` | 记录列表 | `[]` |
| 全部子项成功，无数据 | `True` | `[]` | `[]` |
| 部分子项失败（如 3 个仓库中 1 个 404） | `False` | **保留成功子项已采到的记录** | `["owner/repoB"]` |
| 全部子项失败 | `False` | `[]` | 全部子项名 |

**为什么保留已采到的记录**：丢弃等于让一个坏仓库连坐其他仓库的数据，与 proposal.md §3.2「单一数据源失败不应阻断其他数据源的采集」的**降级精神**相悖 —— 只不过这条原则在这里被应用到了子项粒度。

**日报如何呈现**：`failed` 为空即不标注；非空时在对应板块标注「`<数据源>` 数据获取失败（`<failed 明细>`）」。因此**部分失败不会退化成整个板块不可用**。

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
| `failed_sources` | list[str] | 本次采集失败的数据源标识；用于正文标注 |
| `generated_at` | datetime | 生成时间（tz-aware） |
| `markdown` | str | 完整的 Markdown 格式日报 |
| `html` | str | 完整的 HTML 格式日报 |

> `DailyReport.failed_sources` 是 ADR-004 新增字段，承载 §1.1 所述失败信息传递链的终点。

### 3.4 跨系统身份映射

`CommitRecord.author` 是 GitHub 用户名，`TaskRecord.assignee` 是飞书用户名 —— 同一人在两个系统中用户名完全不同。**没有映射表，就无法把三个独立数据源的数据按「人」聚合。**

映射表存放在 `config.yaml` 中：

```yaml
members:
  - name: "张三"
    github: "zhangsan"
    lark: "zhangsan@company.com"       # 邮箱形式
  - name: "李四"
    github: "lisi-dev"
    lark_open_id: "ou_xxxxxxxxxxxx"    # open_id 形式
  - name: "王五"
    github: "wangwu"
    lark: "wangwu@company.com"
```

**两种飞书标识写法（ADR-004 引入）**

| 写法 | 值来源 | 前提 |
|------|--------|------|
| `lark` | 企业邮箱 | 需 `contact:user.id:readonly` 权限，运行时用 `contact/v3/users/batch_get_id` 反查为 open_id |
| `lark_open_id` | 飞书 open_id（`ou_` 开头） | 无需额外权限，直接使用 |

两者**择一填写**；同时填写时以 `lark_open_id` 为准。原因是飞书开放平台的绝大多数接口（消息、任务）返回的是 `open_id` 而非邮箱，而读取用户邮箱需要额外权限 —— 若不支持 open_id 写法，在权限受限的租户中身份映射将完全不可用。

**聚合规则**：以 `name` 为主键。采集到的记录分别用 `github` 字段与飞书标识（`lark_open_id`，或由 `lark` 反查得到的 open_id）做匹配，匹配到同一 `name` 的记录归入同一个 `MemberReport`。**不在配置映射表中的账号，其记录一律丢弃**（design.md §6.2「3. 访问控制」）。

**聚合的执行者**：由 `generator/aggregator.py` 执行，接口见 §4.5 —— 而非 `main.py`，原因见 §2 的边界澄清。

## 4. 接口契约

**信息隐藏原则**：每个模块把复杂性封装在接口背后。采集层不需要感知推送层的存在，推送层也不需要关心数据的来源。

### 4.1 采集层接口

```python
# collector/github.py
def collect(
    repos: list[str],          # 仓库列表（owner/repo 形式）
    since: datetime,           # 起始时间（tz-aware）
    until: datetime,           # 结束时间（tz-aware）
) -> CollectResult[CommitRecord]: ...
# 每个仓库独立采集；单个仓库失败不影响其他仓库（见 §3.2 语义表）

# collector/lark_task.py
def collect(
    project_id: str,           # 飞书任务清单 GUID
    since: datetime,           # 起始时间（tz-aware）
    until: datetime,           # 结束时间（tz-aware）
) -> CollectResult[TaskRecord]: ...
# project_id 为空字符串时表示「不限定清单，采集全部任务」

# collector/lark_msg.py
def collect(
    chat_id: str,              # 群 ID
    keywords: list[str],       # 过滤关键词
    since: datetime,           # 起始时间（tz-aware）
    until: datetime,           # 结束时间（tz-aware）
) -> CollectResult[MessageRecord]: ...
```

### 4.2 生成层接口

```python
# generator/formatter.py
def generate(
    members: list[MemberReport],   # 各成员数据（已聚合）
    failed_sources: list[str],     # 采集失败的数据源标识，用于正文标注
    date: date,                    # 日报日期
    team_name: str,                # 团队名称
) -> DailyReport: ...
```

> `failed_sources` 是 ADR-004 新增入参。**没有它，proposal.md §3.2「采集失败显式标注」与 tasks.md Task 6 的对应验收标准在物理上无法实现** —— 生成层若拿不到失败信息，就无从区分「今日无记录」与「数据获取失败」。

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

**注意**：推送层接口返回 `bool` 而非 `CollectResult` —— 推送失败不产生「部分成功」的语义歧义，且失败信息通过日志与告警通道传递（见 §4.5）。

**关于 `lark_bot` 的通道选择**：接口签名的参数是 `chat_id` 而非 webhook URL，因此实现走**应用机器人**通道（`POST /open-apis/im/v1/messages`，`receive_id_type=chat_id`），而非自定义机器人的 webhook。二者都可向群推送消息，但只有前者以 `chat_id` 定位目标；自定义机器人的 webhook 地址本身即目标，没有 `chat_id` 参数，与本文档的契约不符。

### 4.4 共享层接口

```python
# shared/config.py
def load(path: str = "config.yaml") -> Config: ...
# 缺少必填字段时抛 ConfigError，错误信息须指明缺失的字段名
# 敏感凭证（API 密钥、授权码）不来自本文件，仅从环境变量读取（见 §6.2）

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
# 建立模块级 SQLite 连接，由 main.py 在流程开始时调用一次
def save_report(report: DailyReport) -> None: ...
def get_report(date: date) -> DailyReport | None: ...

# shared/calendar.py
def is_workday(d: date, holidays: list[date], workdays: list[date]) -> bool: ...
# 判定顺序：workdays（调休补班）优先 → holidays（法定假日）→ 周末
# 详见 §6.6
```

**完整配置结构**（`config.yaml`）

```yaml
team:
  name: "研发一组"

members:                      # 身份映射表，见 §3.4
  - name: "张三"
    github: "zhangsan"
    lark: "zhangsan@company.com"

github:
  repos:                      # 采集范围（安全约束：仅采集此处列出的仓库）
    - "owner/repo"
  # 凭证来自环境变量 GITHUB_TOKEN

lark:
  chat_id: "oc_xxxxxxxx"      # 目标群 / 消息采集范围
  project_id: ""              # 任务清单 GUID，留空则采集全部任务
  # 凭证来自环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET

smtp:
  host: "smtp.qq.com"
  port: 465
  sender: "sender@qq.com"
  recipients:                 # 日报收件人
    - "leader@company.com"
  # 授权码来自环境变量 SMTP_PASSWORD

filters:
  keywords:                   # 消息采集的包含关键词
    - "项目"
  sensitive_keywords:         # 敏感词黑名单，命中即剔除（见 §6.2）
    - "薪资"
    - "绩效"
    - "裁员"

schedule:
  timezone: "Asia/Shanghai"   # 采集窗口时区，见 §6.5
  db_path: "data/daily_report.db"
  log_dir: "logs"
  holidays:                   # 法定假日（YYYY-MM-DD），见 §6.6
    - "2026-10-01"
    - "2026-10-02"
  workdays:                   # 调休补班日（周末但上班），见 §6.6
    - "2026-10-11"
```

**环境变量清单**（`.env`，不进版本库）

| 变量 | 用途 |
|------|------|
| `GITHUB_TOKEN` | GitHub API 凭证 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书应用凭证 |
| `SMTP_PASSWORD` | 邮箱授权码 |

### 4.5 聚合层接口（ADR-004 新增）

```python
# generator/aggregator.py
def aggregate(
    commits: CollectResult[CommitRecord],     # GitHub 采集结果
    tasks: CollectResult[TaskRecord],         # 飞书任务采集结果
    messages: CollectResult[MessageRecord],   # 飞书消息采集结果
    members: list[MemberConfig],              # 身份映射表
) -> list[MemberReport]: ...
```

**职责**：按 §3.4 的规则，把三个独立数据源的记录按「人」聚合为 `list[MemberReport]`；丢弃不在映射表中的账号的记录。

**为什么单独定义这个接口**：§3.4 的聚合规则原先只有描述、没有接口、没有属主，导致实现者要么违反 §2 对 `main.py` 的边界约束，要么自创契约外的接口。接口化后，「谁执行聚合」与「聚合产出什么」都有了明确定义。

**注意**：本接口**只读不改**采集结果 —— 传入的 `CollectResult` 不被消费，失败信息由 `main.py` 另行透传给 `generate()`。

### 4.6 告警接口（ADR-004 新增）

```python
# notifier/alert.py
def report_alert(
    subject: str,                     # 告警标题
    detail: str,                      # 告警详情（纯文本）
    channels: list[str] | None = None # 投递通道，None = 按 §6.1 默认策略
) -> None: ...
```

**为什么需要独立于 `email.send` / `lark_bot.send`**：这两个接口的入参都是 `DailyReport`，而告警场景（§6.1 的「所有数据源都不可用」「推送失败」）发生时**日报根本不存在**，或在逻辑上不允许作为告警载体（推送失败时不能再依赖同一通道）。因此告警需要一条不依赖 `DailyReport` 的独立通道。

**投递策略**：默认向**未失败**的通道投递；告警本身失败时仅记录日志，不再递归告警（避免告警风暴）。

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
**取代**：design.md v0.5 初稿中「采集层接口返回 `list[Record]`」的定义

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

### ADR-004：补齐失败信息传递链与聚合属主

**状态**：已采纳
**取代**：无（本 ADR 是 ADR-003 的**延伸**，不推翻任何既有决策）

**背景**：规范审查（2026-10-11）发现三处阻塞性缺陷，编码前必须修复：

1. **ADR-003 的修复只覆盖了半条链路。** 它解决了 `collector → generator` 之间的失败语义，但 `generator.generate()` 自身的入参（`members`、`date`、`team_name`）没有任何位置承载失败信息，`MemberReport` / `DailyReport` 的字段里也没有。结果是 tasks.md Task 6 的第 1 条验收（「`generate()` 签名符合 §4.2」）与第 5 条验收（「采集失败须标注『数据获取失败』」）**互相矛盾**，任何合法实现都无法同时满足 —— 同一个缺陷在下游原样复现。
2. **聚合环节有规则、无接口、无属主。** §3.4 定义了聚合规则，但 §4 无接口承接；tasks.md 将其派给 `main.py`，而 §2 明确规定 `main.py` 不含业务逻辑。
3. **告警通道缺失。** §6.1 承诺「发送告警邮件」「推送渠道互为备份告警通道」，但 §4.3 的两个接口都以 `DailyReport` 为入参，而告警发生时日报并不存在。

**决策**

1. `generate()` 新增入参 `failed_sources: list[str]`；`DailyReport` 新增字段 `failed_sources: list[str]`（§4.2、§3.3）
2. `CollectResult` 新增字段 `failed: list[str]`，并明确 `success` 采用「全部子项成功才为 True」的判定；部分失败时保留已采到的记录（§3.2）
3. 新增 `generator/aggregator.py::aggregate()` 作为聚合的正式接口与属主（§4.5）
4. 新增 `notifier/alert.py::report_alert()` 作为独立于日报的告警通道（§4.6）
5. 新增 `shared/calendar.py::is_workday()`（§4.4、§6.6）
6. 身份映射表新增 `lark_open_id` 写法，作为 `lark` 邮箱写法的对等替代（§3.4）
7. 明确限流重试与超时重试使用**独立计数器**（§6.1）

**选项**

| 选项 | 优点 | 缺点 |
|------|------|------|
| 让 `collector` 直接返回 `list[MemberReport]` | 一步到位 | 采集层被迫承担聚合职责，违反 §2 单一职责；失败信息仍无处安放 |
| 把失败信息塞进 `MemberReport` | 少一个入参 | 数据模型被污染：成员维度不应承载数据源维度的事实；且「全部数据源失败」时无成员可依附 |
| **独立 `failed_sources` 入参 + 独立聚合接口**（采纳） | 失败信息与成员数据解耦；各层职责单一；不破坏 ADR-003 | 编排层需要多传一个参数 |

**理由**

1. 失败是**数据源维度**的事实，成员是**人的维度**的实体 —— 把前者塞进后者的模型会造成概念污染，且「所有数据源都失败」时前者无处依附
2. 聚合是**数据变换**，属生成层职责；编排层只应编排。接口化后任务归属无歧义
3. 告警必须独立于日报契约 —— 否则「推送失败」这一场景无法告警（不能用失败的通道发出关于自己失败的告警）
4. 三条缺陷同源：ADR-003 把修复做窄了。本次修订补全整条链，而非在末端打补丁 —— 这正是第 7 章「规范缺陷只能通过修改规范来修复」的实践

**代价**：`generate()` 与 `aggregate()` 的签名比书中示例更宽；编排层代码略增。换取的是 Task 6/9 的验收标准从「物理上无法完成」变为「可测试」。

## 6. 非功能性约束

proposal.md 已用一句话界定了性能与重试要求；本节将其展开为可实现的策略。

### 6.1 错误处理策略（优雅降级）

航空工程师早已深谙此道：单台发动机故障不应导致整架飞机坠毁。

| 场景 | 处理方式 |
|------|---------|
| GitHub API 超时 | **超时重试** 3 次（间隔 5s），若仍失败则标记为失败源 |
| GitHub API 限流（403/429） | **限流重试**：读取 `X-RateLimit-Reset`，等待至 reset 时刻后重试，上限 3 次。**与超时重试使用独立计数器**，互不消耗配额 |
| 飞书 Token 过期 | 自动刷新 Token 后重试 1 次 |
| 单个数据源完全不可用 | 跳过该数据源，日报中标注「xx 数据源获取失败」，其他数据源正常采集 |
| **单个数据源部分失败**（多仓库场景） | 保留成功部分的数据；在对应板块标注失败的子项明细 |
| 所有数据源都不可用 | 记录错误日志，**调用 `report_alert()` 发送告警**，不生成空日报 |
| 邮件发送失败 | 重试 2 次，若仍失败则记录日志并**经飞书通道告警** |
| 飞书推送失败 | 重试 2 次，若仍失败则记录日志并**经邮件通道告警** |
| 告警自身失败 | 仅记录日志，**不再递归告警**（防告警风暴） |

**重试计数口径（ADR-004 明确）**：超时重试与限流重试是两类独立的重试，各自有 3 次上限。原因：限流是服务端的流量控制（等待后大概率成功），超时是网络或服务异常（重试是唯一手段），把二者合并计数会导致限流场景挤占超时重试的配额。

**关键原则**
1. 采集层的失败不应阻塞生成层
2. 生成层的失败不应阻塞推送层（需要推送错误报告）
3. 任何失败必须有日志记录
4. 推送渠道应互为备份告警通道

### 6.2 安全约束

**1. 密钥管理**
- 所有 API 密钥均通过**环境变量**注入（清单见 §4.4）
- 严禁在代码或配置文件中硬编码密钥 —— **`config.yaml` 只承载非敏感配置**（仓库名、群 ID、收件人等）
- 加入 `.env` 文件则**必须**加入 `.gitignore`
- **可测试性要求**：单元测试须能断言「环境变量缺失时抛出 `ConfigError` 且错误信息包含缺失的变量名」

**2. 数据安全**
- 对飞书消息中的敏感关键词（如薪资、绩效、裁员等）进行过滤（配置黑名单 `filters.sensitive_keywords`）
- **过滤归属**：由 `collector/lark_msg.py` 在采集时执行 —— 敏感内容不应流入后续任何环节，包括日志
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
- **环境变量缺失时给出明确的逐项报错**（而非笼统失败）
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

### 6.5 时间窗口与时区

**采集窗口**：**当日 00:00 至执行时刻**（口径以 proposal.md §4 术语表为准）。18:00 触发时覆盖 00:00–18:00。

**时区规则**
- 「当日」的日界以 `config.yaml` 的 `schedule.timezone` 为准（默认 `Asia/Shanghai`）
- 所有**内部** `datetime` 一律 tz-aware；`since` / `until` 在跨越模块边界前必须带 tzinfo
- **API 边界处的归一化**：
  - GitHub 返回 UTC ISO8601（如 `2026-10-11T02:33:00Z`）→ 转 `Asia/Shanghai` 后取日期，参与「当日」判断
  - 飞书消息返回**毫秒级 Unix 时间戳**（字符串）→ 按 UTC 解析后转目标时区
- 禁止 naive datetime 参与比较；比较前统一换算到 `schedule.timezone`

**为什么必须写死**：GitHub 用 UTC、飞书用毫秒时间戳、本机时区又可能是任意值。三者在没有约定时会各自按本地时钟解释「今天」，导致同一次执行采到的数据集与测试断言口径不一致 —— 而且是**静默**的不一致。

### 6.6 工作日与节假日判定

proposal.md §3.3 要求「识别非工作日（周末及法定节假日），并自动跳过日报生成」。

**判定顺序**（`shared/calendar.py::is_workday`）

1. 若日期在 `schedule.workdays` 中 → **是工作日**（调休补班日，即使它是周末）
2. 若日期在 `schedule.holidays` 中 → **不是工作日**（法定假日，即使它是周一）
3. 否则：周一至周五为工作日，周六周日不是

**为什么需要 `workdays` 清单**：中国的法定节假日安排包含**调休补班**——某些周六周日是法定工作日。只判周末会把补班日误判为休息而漏发日报，只判 `holidays` 则会把补班日误判为休息。两个清单缺一不可。

**清单的维护**：由人工按年度国务院安排更新 `config.yaml`，本期不引入第三方节假日库（对应 proposal.md §2.3「不做…」所体现的最小依赖取向）。**清单过期不会导致程序错误**，只会按规则 3 退化为「周末休息、工作日上班」的朴素判断。

### 6.7 非工作日的行为

命中非工作日时，`main.py` **不执行采集与生成**，直接记录 INFO 日志（含跳过原因与判定依据）后退出，退出码为 0（非工作日跳过属于正常流程，不是错误）。`--dry-run` 与 `--check` 模式不受此约束，以便在任意日期验证链路。

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

> **当前状态**：三份规范处于 **v0.5**。代码尚未实现，故不可标记为 v1.0。

**变更影响链**

| 变更类型 | 起点 | 影响链 |
|---------|------|--------|
| 需求变更 | proposal.md | proposal.md → design.md → tasks.md → 代码 → 测试 |
| 架构变更 | design.md | design.md → tasks.md → 代码 → 测试 |
| 任务变更 | tasks.md | tasks.md → 代码 → 测试 |
| 缺陷修复 | 代码 | 代码 → 测试（可能反向更新 design.md） |

判断标准：审视变更究竟改变了「做什么」（proposal.md）、「怎么做」（design.md），还是仅仅影响「如何实现」（代码）。找到受影响的最上游层级，从那里开始修改。
