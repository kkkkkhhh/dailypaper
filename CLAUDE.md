# 智能日报生成器 — 项目约定

本项目的实施依据是《**SDD 实战：规范驱动开发之道**》（黄佳著，人民邮电出版社）第 4–7 章的 SDD 六阶段工作流：**先写规范，再写代码**。

## 全局上下文（改任何代码前必读）

| 文件 | 回答 | 何时查 |
|------|------|--------|
| [specs/proposal.md](specs/proposal.md) | 做什么 / 不做什么 | 判断某个功能是否在范围内 |
| [specs/design.md](specs/design.md) | 怎么做 | 查模块边界、数据模型、接口契约、ADR、非功能约束 |
| [specs/tasks.md](specs/tasks.md) | 按什么顺序做 | 查当前任务的验收标准 |

**规范是权威，代码服从规范。** 若发现规范本身有缺陷，不要用代码里的临时方案绕过 —— 回到规范层修改（第 7 章：在代码层打补丁无异于在裂缝上糊墙纸）。

## 变更规则（design.md §7）

| 变更类型 | 从哪里开始改 | 影响链 |
|---------|-------------|--------|
| 需求变更 | `proposal.md` | proposal → design → tasks → 代码 → 测试 |
| 架构变更 | `design.md` | design → tasks → 代码 → 测试 |
| 任务变更 | `tasks.md` | tasks → 代码 → 测试 |
| 缺陷修复 | 代码 | 代码 → 测试（可能反向更新 design.md） |

- **ADR 只追加，不修改**。推翻旧决策时新建 ADR 并标注「取代 ADR-XXX」。
- **先提交规范，后提交代码**；规范变更与代码变更保持可追溯。

## 架构约束（design.md §1、§2）

管道式架构，每层只认识相邻层的接口：

```
collector/ ──▶ generator/ ──▶ notifier/          all ──▶ shared/
(取数据)      (聚合+生成)     (推送+告警)                (配置/日志/异常/模型/存储/日历)
```

| 模块 | 只负责 | **不负责**（这条比上面更重要） |
|------|--------|------------------------------|
| `collector/` | 调外部 API 拿原始数据 | 不做格式化、不去重、不推送、**不做跨源聚合** |
| `generator/` | 按映射聚合 + 生成 Markdown/HTML | 不采集、不调 API、不推送 |
| `notifier/` | SMTP 发信、飞书推送、告警 | 不处理数据、不生成日报、不采集 |
| `shared/` | 配置、日志、异常、数据模型、存储、日历 | — |
| `main.py` | 编排、全局异常、执行状态记录 | **不含业务逻辑**（聚合属生成层） |

违反「不负责」列比漏做功能更严重 —— 那意味着模块边界被侵蚀。

## 关键契约（design.md §3、§4）

- 全部数据模型定义在 **`shared/models.py`**，是唯一定义处。各模块**不得自行定义**这些类型。
- 采集层统一返回 **`CollectResult[T]`**（5 字段：`source`/`success`/`records`/`error`/`failed`），**不抛异常**。
  - `success` 采「全部子项成功才为 True」；部分失败时 `records` **保留已采到的，不丢弃**。
  - 成功但无数据（`records=[]`） ≠ 失败。日报文案分别是「今日无记录」与「数据获取失败」。
- 失败信息必须逐层透传到生成层：`CollectResult → main.py 提取 → generate(failed_sources=...) → 正文标注`。

## 硬性约束

**安全（design.md §6.2）**
- 凭证**只从环境变量读取**，`config.yaml` 里没有承载密钥的键。**任何情况下不得硬编码密钥**。
- 飞书敏感词（薪资/绩效/裁员）在**采集阶段**过滤，不进返回值也不进日志。
- 日报只含提交统计，**不含代码 diff 内容**。

**时区（design.md §6.5）**
- 「当日」以配置的 `Asia/Shanghai` 为界。
- 内部 `datetime` 一律 tz-aware。GitHub 给 UTC ISO8601，飞书给毫秒时间戳，边界处归一化。
- **禁止 naive datetime 参与比较。**

**错误处理（design.md §6.1）**
- 超时重试与限流重试是**两个独立计数器**，各 3 次上限。
- 采集层失败不阻塞生成层；生成层失败不阻塞推送层。
- 推送渠道互为告警通道；告警自身失败只记日志，不递归告警。

## 代码约定

- Python ≥3.11，依赖：`httpx`、`pyyaml`、`jinja2`。**新增依赖需先在 design.md 记 ADR。**
- 全部命令用 `uv run`：`uv run pytest`、`uv run python main.py`。
- 测试放 `tests/test_<模块>.py`，**一律使用 Mock，不打真实网络**。
  - HTTP 用 `httpx.MockTransport`
  - 环境变量用 `monkeypatch.setenv` / `delenv`
  - 存储用 `tmp_path`，不要污染真实库
- 中文注释，解释**为什么**而非**做了什么**。公开函数写 docstring。
- 单个函数不超过 30 行；类型标注齐全。

## 当前进度

- [x] Task 1 项目结构与基础配置
- [x] Task 2 共享基础层（`shared/`：models/config/logger/errors/storage/calendar）
- [ ] Task 3 GitHub 采集模块
- [ ] Task 4 飞书任务采集模块
- [ ] Task 5 飞书消息采集模块
- [ ] Task 6 聚合与日报生成模块
- [ ] Task 7 邮件推送模块
- [ ] Task 8 飞书推送模块
- [ ] Task 9 主编排入口
- [ ] Task 10 集成测试
