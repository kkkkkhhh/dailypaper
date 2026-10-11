# 智能日报生成器 — 任务清单（tasks.md）

## 元信息
- 关联规范：`specs/proposal.md` v0.5、`specs/design.md` v0.5
- 文档版本：v0.5
- 任务总数：11（书中 10 个 + 新增 Task 2.5）
- 预计总执行时间：3 到 4h（包含测试）
- 执行策略：按依赖关系顺序执行，独立任务可并行执行
- 依据：《SDD 实战：规范驱动开发之道》（黄佳著，人民邮电出版社）第 6 章

### 本文件相对书中的补充与修订清单

书中 §6.5 的 tasks.md 是示例文本；本文件在其基础上做了以下**显式**修订。凡修订处均在正文以 `> 补充` 或 `> 修订` 标注。

| # | 位置 | 修订内容 | 依据 |
|---|------|---------|------|
| 1 | Task 1 第 3 条 | 书中写「`uv sync` 成功安装所有依赖」，保持原文（`uv` 实测可用，v0.12.18） | 第 6 章原文 |
| 2 | Task 1 | 新增 `.env.example` 与 `.gitignore` 验收 | design.md §6.2 |
| 3 | Task 2 | 新增 `ConfigError`（书只列了三个异常）；新增环境变量读取与缺失报错的验收 | design.md §4.4、§6.2 |
| 4 | Task 2 | 新增 `shared/calendar.py` | design.md §4.4、§6.6 |
| 5 | Task 3/4/5 描述行 | 由「返回 XRecord 列表」改为「返回 `CollectResult[XRecord]`」 | design.md §3.2、ADR-003 |
| 6 | Task 3 | 由「API 超时重试 3 次」的单一重试，扩展为**超时重试与限流重试各自独立计数**；限流判定由 `403` 扩展为 `403/429` | design.md §6.1、ADR-004 |
| 7 | Task 3/4/5 | 失败分支由书的「返回空列表 + 错误日志」改写为「返回 `CollectResult(success=False)`」——**这一条与 ADR-003 直接冲突，必须改** | design.md §3.2 |
| 8 | Task 3/4/5 | 新增 `failed` 字段与多仓库部分失败的验收 | design.md §3.2、ADR-004 |
| 9 | Task 6 | 新增 `failed_sources` 入参与对应验收（书里缺此通道，导致原验收无法实现） | design.md §4.2、ADR-004 |
| 10 | Task 6 | 新增聚合职责与 `aggregator.py` | design.md §4.5、ADR-004 |
| 11 | Task 6 | 新增「HTML 转义」验收 | tasks 层新增的安全加固（design.md §6.2 只要求不外泄 diff 内容，未要求转义） |
| 12 | Task 7/8 | 新增告警通道调用验收 | design.md §4.6、§6.1、ADR-004 |
| 13 | Task 9 | 新增节假日判定、环境变量检查、聚合调用方式 | design.md §4.4、§6.2、§6.5、§6.6 |
| 14 | Task 9 | 新增 SQLite 写入验收 | design.md §4.4、ADR-002 |
| 15 | 全文 | 新增时区约定 | design.md §6.5 |
| 16 | Task 2 | 新增 `shared/models.py` 作为数据模型的唯一定义处（原规范定义了数据模型但未指定归属文件） | design.md §2、§3 |
| 17 | Task 2.5 | 新增任务：飞书 API 客户端作为 Task 4/5 的共享前置。二者都需要令牌管理与邮箱反查，若各自实现会违反表 6-2「两个任务修改同一个文件不可以并行」，也会产生两份行为不一致的令牌逻辑 | design.md §2、§4.1 |

## 任务依赖图（DAG）

```
Task 1 (项目初始化)
   ↓
Task 2 (共享基础层)
   ↓
Task 2.5 (飞书 API 客户端)
   ↓
┌──────────┬──────────┬──────────┐
│ Task 3   │ Task 4   │ Task 5   │   ← 三个采集模块可以并行执行
│ GitHub   │ 飞书任务 │ 飞书消息 │
└──────────┴──────────┴──────────┘
   ↓
Task 6 (聚合 + 日报生成模块)
   ↓
┌─────────────┬─────────────┐
│ Task 7      │ Task 8      │        ← 两个推送模块可以并行执行
│ 邮件推送    │ 飞书推送    │
└─────────────┴─────────────┘
   ↓
Task 9 (主编排入口)
   ↓
Task 10 (集成测试)
```

- **关键路径**：Task 1 → Task 2 → Task 3/4/5 → Task 6 → Task 7/8 → Task 9 → Task 10
- **最大并行度**：3（Task 3 + Task 4 + Task 5 同时执行）

> **修订（相对书图 6-4）**：Task 6 的标题由「日报生成模块（formatter + template）」扩为「聚合 + 日报生成模块」，因其新增了 `aggregator.py`（见修订清单 #10）。依赖关系不变。

### 并行执行的安全边界

| 条件 | 并行执行 | 原因 |
|------|---------|------|
| 两个任务产出不同的文件 | 可以 | 没有文件写入冲突 |
| 两个任务读取同一个模块但不修改 | 可以 | 只读操作不会产生冲突 |
| 两个任务修改同一个文件 | **不可以** | 可能产生代码合并冲突 |
| 一个任务的输出是另一个任务的输入 | **不可以** | 存在数据依赖关系 |

---

## Task 1: 创建项目结构和基础配置

**描述**：初始化项目目录结构，创建 pyproject.toml，安装核心依赖，创建配置文件模板

**输入**：`specs/design.md`（模块职责定义 §2）
**输出**：项目根目录结构 + `pyproject.toml` + `config.yaml.example` + `.env.example`
**依赖**：无

**验收标准**
- [ ] 目录结构符合 design.md §2 的模块划分，且文件清单与之一致：
      `collector/`（github.py、lark_client.py、lark_task.py、lark_msg.py）、
      `generator/`（aggregator.py、formatter.py、template.py）、
      `notifier/`（email.py、lark_bot.py、alert.py）、
      `shared/`（models.py、config.py、logger.py、errors.py、storage.py、calendar.py）、
      `tests/`
- [ ] `pyproject.toml` 包含 httpx、pyyaml、jinja2 依赖
- [ ] `uv sync` 成功安装所有依赖
- [ ] `config.yaml.example` 包含 design.md §4.4 定义的**完整配置结构**，其中 members 映射表模板同时演示 `lark`（邮箱）与 `lark_open_id` 两种写法
- [ ] `.env.example` 列出 design.md §4.4 的全部环境变量名，值为空占位符
- [ ] `.gitignore` 包含 `.env`、`*.db`、`__pycache__/`、`.venv/`

## Task 2: 实现共享基础层

**描述**：实现数据模型、配置读取、日志记录、错误处理、SQLite 存储和工作日判定 6 个共享模块

**输入**：`config.yaml.example`（配置格式）、design.md §3（数据模型）、design.md §4.4（共享层接口契约）、design.md §6（非功能约束）
**输出**：`shared/models.py`、`shared/config.py`、`shared/logger.py`、`shared/errors.py`、`shared/storage.py`、`shared/calendar.py`
**依赖**：Task 1

**验收标准**
- [ ] `models.py` 定义 design.md §3 的全部数据模型：`CommitRecord`(7 字段)、`TaskRecord`(5 字段)、`MessageRecord`(4 字段)、`CollectResult[T]`(5 字段)、`MemberReport`(5 字段)、`DailyReport`(7 字段)、`MemberConfig`
- [ ] 各模型字段名、类型、顺序与 design.md §3 的表一致
- [ ] `config.py` 能读取 `config.yaml` 并返回配置对象
- [ ] `config.py` 在配置缺少必填字段时抛出 `ConfigError`，且**错误信息包含缺失的字段名**
- [ ] `logger.py` 输出 JSON lines 格式日志
- [ ] `errors.py` 定义 `CollectorError`、`GeneratorError`、`NotifierError`、`ConfigError` 四个自定义异常
- [ ] `storage.py` 能创建 SQLite，写入和查询日报记录
- [ ] `storage.py` 的 `init_db()` 被调用前，`save_report()` 不产生未定义行为
- [ ] `calendar.py::is_workday()` 正确实现 design.md §6.6 的**三级判定顺序**（workdays → holidays → 周末）
- [ ] 所有模块包含对应的单元测试，测试通过

> **补充（对齐 design.md §6.2「1. 密钥管理」）**
- [ ] 敏感凭证（`GITHUB_TOKEN`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`SMTP_PASSWORD`）**仅从环境变量读取**，`config.yaml` 只承载非敏感配置
- [ ] 环境变量已设置时，`Config` 对应字段取到该值
- [ ] 环境变量缺失时抛出 `ConfigError`，且**错误信息包含缺失的变量名**
- [ ] 单元测试使用 `monkeypatch.setenv` / `delenv` 覆盖以上两条分支

> **补充（对齐 design.md §6.6）**
- [ ] `is_workday()` 测试覆盖：普通工作日、普通周末、法定假日落在工作日、调休补班日落在周末

## Task 2.5: 实现飞书 API 客户端（共享前置）

**描述**：封装飞书开放平台的令牌管理与身份反查，供 Task 4 与 Task 5 共用

**输入**：design.md §4.1（采集层接口契约之 `LarkClient`）、design.md §3.4（身份映射）、design.md §6.1（错误处理）
**输出**：`collector/lark_client.py` + `tests/test_lark_client.py`
**依赖**：Task 2

> **为什么单独成为一个任务**：Task 4 与 Task 5 都需要 tenant_access_token 获取、过期自动刷新、邮箱反查 open_id 这三件事。若各自实现一份，并行开发时必然出现两份行为不一致的令牌逻辑 —— 违反单一职责，也让「怎么用一份代码」变成两个任务都要碰同一个文件。按表 6-2 的并行规则，**两个任务修改同一个文件就不可以并行**，故先把它单独交付，Task 4/5 之后只读不写。

**验收标准**
- [ ] `LarkClient.__init__` 与 `get()`、`tenant_token()`、`resolve_open_ids()` 签名符合 design.md §4.1
- [ ] 首次调用 `tenant_token()` 时向 `/open-apis/auth/v3/tenant_access_token/internal` 换取令牌
- [ ] 令牌在有效期内被复用，不重复换取（断言 HTTP 调用次数）
- [ ] 模拟 Token 失效（接口返回令牌无效错误码）时自动刷新并重试 **1 次**，`token_refresh_count` 加一
- [ ] 刷新后仍失败则不无限重试，向上报告错误
- [ ] `resolve_open_ids()` 用 `contact/v3/users/batch_get_id` 完成邮箱到 open_id 的映射
- [ ] `resolve_open_ids()` 在部分邮箱查不到时返回已完成的部分，不抛异常
- [ ] 使用 Mock 数据的单元测试全部通过，全程不打真实网络

## Task 3: 实现 GitHub 采集模块

**描述**：调用 GitHub API 获取指定仓库在采集窗口内的 Commit 记录，返回 `CollectResult[CommitRecord]`

**输入**：design.md §3.1（CommitRecord 数据模型）、design.md §3.2（CollectResult 语义）、design.md §4.1（采集层接口契约）、design.md §6.5（时区约定）
**输出**：`collector/github.py` + `tests/test_github.py`
**依赖**：Task 2

**验收标准**
- [ ] `collect()` 函数签名符合 design.md §4.1 的接口定义
- [ ] 返回的每个 CommitRecord 包含全部 7 个字段且类型正确
- [ ] 支持分页查询（GitHub API 默认每页 30 条）
- [ ] **超时重试** 3 次（间隔 5s），若仍失败则返回 `CollectResult(success=False)`
- [ ] **限流**（HTTP 403 或 429）时，读取 `X-RateLimit-Reset` 并等待至该时刻后重试，上限 3 次
- [ ] 超时重试与限流重试**使用独立计数器**，互不消耗配额
- [ ] 使用 Mock 数据的单元测试全部通过

> **补充（对齐 design.md §3.2 / ADR-003 / ADR-004）**
- [ ] 返回值包含 `CollectResult` 的**全部 5 个字段**，且 `source == "github"`
- [ ] 全部成功且有数据时：`success=True`、`records` 非空、`failed=[]`、`error=None`
- [ ] 全部成功但无数据时：`success=True`、`records=[]`、`failed=[]` —— **区别于失败**
- [ ] 多仓库部分失败（如 2 个仓库中 1 个 404）：`success=False`、`records` **保留成功仓库的记录**、`failed` 含失败仓库名
- [ ] 全部仓库失败时：`success=False`、`records=[]`、`failed` 含全部仓库名
- [ ] 任何情况下**不抛出**异常给调用方（失败一律以返回值表达）
- [ ] 时间过滤基于 tz-aware datetime，GitHub 返回的 UTC 时间正确归一化到配置时区（design.md §6.5）

## Task 4: 实现飞书任务采集模块

**描述**：调用飞书开放平台 API 获取指定任务清单中在采集窗口内发生状态变更的任务，返回 `CollectResult[TaskRecord]`

**输入**：design.md §3.1（TaskRecord 数据模型）、design.md §3.2（CollectResult 语义）、design.md §4.1（采集层接口契约：`LarkClient` 与 `lark_task.collect`）、design.md §6.5（时区约定）
**输出**：`collector/lark_task.py` + `tests/test_lark_task.py`
**依赖**：Task 2、Task 2.5

**验收标准**
- [ ] `collect()` 函数签名符合 design.md §4.1 的接口定义
- [ ] 返回的每个 TaskRecord 包含全部 5 个字段且类型正确
- [ ] `project_id` 为空字符串时不限定清单，采集全部任务
- [ ] 复用 `LarkClient` 的令牌管理，**不自建令牌逻辑**
- [ ] 超时重试 3 次，若仍失败则返回 `CollectResult(success=False)`
- [ ] 使用 Mock 数据的单元测试全部通过

> **补充（对齐 design.md §3.2 / ADR-003）**
- [ ] 返回值包含 `CollectResult` 的**全部 5 个字段**，且 `source == "lark_task"`
- [ ] 成功/失败分支与 Task 3 一致（含 `source` / `success` / `records` / `failed` / `error` 五个字段的取值）
- [ ] **不抛出**异常给调用方
- [ ] 飞书返回的**毫秒级时间戳**正确归一化为 tz-aware datetime（design.md §6.5）

## Task 5: 实现飞书消息采集模块

**描述**：调用飞书开放平台 API 获取指定群的消息记录，按关键词过滤，返回 `CollectResult[MessageRecord]`

**输入**：design.md §3.1（MessageRecord 数据模型）、design.md §3.2（CollectResult 语义）、design.md §4.1（采集层接口契约：`LarkClient` 与 `lark_msg.collect`）、design.md §6.2（安全约束）、design.md §6.5（时区约定）
**输出**：`collector/lark_msg.py` + `tests/test_lark_msg.py`
**依赖**：Task 2、Task 2.5

**验收标准**
- [ ] `collect()` 函数签名符合 design.md §4.1 的接口定义
- [ ] 返回的每个 MessageRecord 包含全部 4 个字段且类型正确
- [ ] 关键词过滤正确，只返回包含指定关键词的消息
- [ ] 敏感关键词（薪资、绩效等）被正确过滤，不出现在结果中
- [ ] 复用 `LarkClient` 的令牌管理，**不自建令牌逻辑**
- [ ] 使用 Mock 数据的单元测试全部通过

> **补充（对齐 design.md §3.2 / ADR-003 与 §6.2）**
- [ ] 返回值包含 `CollectResult` 的**全部 5 个字段**，且 `source == "lark_msg"`
- [ ] 成功/失败分支与 Task 3 一致；**不抛出**异常给调用方
- [ ] 敏感词过滤在**采集阶段**执行（design.md §6.2「2. 数据安全」）——命中敏感词的原始内容不进入返回值，也不写入日志
- [ ] 机器人自身发送的消息被排除（避免上一日推送的日报被当作讨论记录回采；机器人 open_id 见 `.env` 的 `FEISHU_BOT_OPEN_ID`）

## Task 6: 实现聚合与日报生成模块

**描述**：按身份映射表聚合各数据源的采集结果，再按「代码提交 → 任务进展 → 协作沟通」组织日报内容，输出 Markdown 和 HTML 格式

**输入**：design.md §3.3（MemberReport、DailyReport 数据模型）、design.md §3.4（跨系统身份映射）、design.md §4.2（生成层接口契约）、design.md §4.5（聚合层接口契约）
**输出**：`generator/aggregator.py`、`generator/formatter.py`、`generator/template.py` + `tests/test_generator.py`
**依赖**：Task 2

**验收标准**
- [ ] `aggregate()` 函数签名符合 design.md §4.5 的接口定义
- [ ] `generate()` 函数签名符合 design.md §4.2 的接口定义
- [ ] 输出的 `DailyReport.markdown` 包含三个部分标题
- [ ] 输出的 `DailyReport.html` 可被浏览器正确渲染
- [ ] 若某成员当日无任何记录，日报中需要显示「今日无记录」
- [ ] 若某数据源采集失败，日报中需要标注「数据获取失败」
- [ ] Markdown 到 HTML 的转换格式正确
- [ ] 使用 Mock 数据的单元测试全部通过

> **补充（对齐 design.md §4.2 / §3.4 / ADR-004）**
- [ ] 「今日无记录」与「数据获取失败」是**两种不同文案**，由**不同输入**触发，测试须分别构造：
      - `CollectResult(success=True, records=[])` + 空 `failed_sources` → 「今日无记录」
      - `failed_sources` 非空 → 「数据获取失败」+ 失败原因
- [ ] `generate()` 通过 `failed_sources` 入参获取失败信息，**不直接依赖采集层类型**
- [ ] 聚合按 `name` 为主键；`lark`（邮箱）与 `lark_open_id` 两种写法均能正确匹配
- [ ] 不在映射表中的账号，其记录被丢弃（断言其不出现在任何 `MemberReport` 中）

> **补充（对齐 design.md §6.2「2. 数据安全」）**
- [ ] 日报中**不出现**代码 diff 的具体内容，只有统计数据（`+N 行 / -M 行`）
- [ ] 生成 HTML 时对用户数据做转义，提交信息中的 `<script>` 等不被当作标签执行

## Task 7: 实现邮件推送模块

**描述**：将 HTML 格式日报通过 SMTP 发送给指定收件人

**输入**：design.md §4.3（推送层接口契约）、design.md §4.6（告警接口）
**输出**：`notifier/email.py` + `tests/test_email.py`
**依赖**：Task 6

**验收标准**
- [ ] `send()` 函数签名符合 design.md §4.3 的接口定义
- [ ] 邮件主题包含日期和团队名称
- [ ] 邮件正文为 HTML 格式的日报内容
- [ ] 发送失败时重试 2 次，仍失败则返回 `False` + 错误日志
- [ ] 使用 Mock SMTP 的单元测试通过

> **补充（对齐 design.md §6.1 / §4.6）**
- [ ] 邮件发送最终失败时，触发**飞书通道**告警（走 `report_alert()`，而非再走邮件）

## Task 8: 实现飞书推送模块

**描述**：将 Markdown 格式日报通过飞书机器人发送到指定群

**输入**：design.md §4.3（推送层接口契约）、design.md §4.6（告警接口）
**输出**：`notifier/lark_bot.py` + `tests/test_lark_bot.py`
**依赖**：Task 6

**验收标准**
- [ ] `send()` 函数签名符合 design.md §4.3 的接口定义
- [ ] 发送的消息为 Markdown 格式
- [ ] 发送失败时重试 2 次，仍失败则返回 `False` + 错误日志
- [ ] 使用 Mock 接口的单元测试通过

> **补充（对齐 design.md §4.3 的通道说明与 §6.1）**
- [ ] 实现走**应用机器人**通道（`im/v1/messages`，`receive_id_type=chat_id`），而非自定义机器人 webhook —— 因为契约的入参是 `chat_id`
- [ ] 飞书推送最终失败时，触发**邮件通道**告警（走 `report_alert()`）

## Task 9: 实现主编排入口

**描述**：创建 `main.py` 作为系统入口，按顺序编排「采集 → 聚合 → 生成 → 推送」的完整流程

**输入**：所有模块的接口
**输出**：`main.py`
**依赖**：Task 3、Task 4、Task 5、Task 6、Task 7、Task 8

**验收标准**
- [ ] `python main.py` 执行完整的日报生成流程
- [ ] `python main.py --check` 验证所有 API 连接和配置
- [ ] `python main.py --dry-run` 执行采集和生成但不推送
- [ ] 若单个数据源失败，不应影响其他数据源的采集
- [ ] 若所有数据源失败，不生成空日报，记录错误日志
- [ ] 执行日志包含：开始时间、各数据源数据采集条数、推送结果、结束时间

> **补充（对齐 design.md §2 / §4.4 / §4.6 / §6.6）**
- [ ] 聚合通过调用 `generator.aggregator.aggregate()` 完成，**`main.py` 内不实现任何聚合或数据变换逻辑**
- [ ] 采集失败信息由 `main.py` 从各 `CollectResult` 提取后透传给 `generate(failed_sources=...)`
- [ ] 非工作日（周末及法定节假日）自动跳过，日志记录跳过原因与判定依据；`--dry-run` / `--check` 不受此约束
- [ ] 日报生成后调用 `storage.save_report()` 写入 SQLite；`db_path` 取自配置
- [ ] 所有数据源失败时调用 `report_alert()` 发送告警，而非生成空日报
- [ ] `--check` 对环境变量缺失给出**逐项**明确报错

## Task 10: 集成测试

**描述**：编写端到端集成测试，验证完整流程在各种场景下的行为

**输入**：`main.py` + 所有模块
**输出**：`tests/test_integration.py`
**依赖**：Task 9

**验收标准**
- [ ] 正常场景：所有数据源可用，日报生成并推送成功
- [ ] 降级场景：若某个数据源超时，日报中需要标注「数据获取失败」，其他部分正常
- [ ] 空数据场景：若某成员当日无任何记录，日报中需要显示「今日无记录」
- [ ] 全部失败场景：若所有数据源不可用，不生成日报，记录错误日志
- [ ] 执行耗时少于 60s（Mock 环境下计时）

> **补充（对齐 design.md §3.2 / §6.1）**
- [ ] 部分失败场景：多仓库中一个仓库失败，日报保留成功仓库的数据并标注失败子项
- [ ] 全部失败场景同时断言 `report_alert()` 被调用
- [ ] 非工作日场景：`main.py` 跳过采集与生成，退出码为 0
- [ ] 耗时断言使用 Mock 环境（不打真实网络），计时口径与 proposal.md §3.2 一致

---

## 执行顺序

推荐执行顺序（考虑依赖关系和并行机会）：

| 阶段 | 任务 | 并行 |
|------|------|------|
| 阶段 1 | Task 1（项目初始化） | 串行 |
| 阶段 2 | Task 2（共享基础层） | 串行 |
| 阶段 2.5 | Task 2.5（飞书 API 客户端） | 串行 |
| 阶段 3 | **Task 3 + Task 4 + Task 5**（三个采集模块） | **并行** |
| 阶段 4 | Task 6（聚合 + 日报生成模块） | 串行 |
| 阶段 5 | **Task 7 + Task 8**（两个推送模块） | **并行** |
| 阶段 6 | Task 9（主编排入口） | 串行 |
| 阶段 7 | Task 10（集成测试） | 串行 |

串行执行需 10 个步骤；并行执行仅需 7 个阶段。

## 执行方式（SDD 任务执行循环）

每个任务遵循同一闭环：**获取任务 → 实现 → 验证通过 → 获取下一个任务**。

单任务执行指令的上下文由三层构成：
- **全局上下文**：`CLAUDE.md` 中引用 `specs/proposal.md` 与 `specs/design.md`
- **任务上下文**：该任务的描述、输入输出定义及验收标准
- **前置上下文**：已完成任务的产出代码

验证手段：`python -m pytest tests/test_<模块>.py -v`。全部通过则该任务完成；失败则将具体失败信息反馈修复，修复循环通常不超过 3 轮。

**注意**：指令仅明确「做什么」（接口契约）与「做到何种程度」（验收标准），**不规定实现方式**（设计模式、分页逻辑、代码组织由实现方自主决策）。若对实现有客观要求，应将其转化为可执行的验收标准，而非主观偏好。
