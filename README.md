# 智能日报生成器

自动收集团队成员的 GitHub 提交、飞书任务状态与群消息，生成结构化日报，并通过邮件与飞书推送给团队 Leader。

本项目按《**SDD 实战：规范驱动开发之道**》（黄佳著，人民邮电出版社）第 4–7 章的方法论实施：**先写规范，再写代码**。

## 规范

三份规范位于 [`specs/`](specs/)，是本文档之上位的约束基准。**改代码前先看规范**。

| 文件 | 回答的问题 | 内容 |
|------|-----------|------|
| [specs/proposal.md](specs/proposal.md) | 做什么 / 不做什么 | 背景目标、功能范围、验收标准、术语定义 |
| [specs/design.md](specs/design.md) | 怎么做 | 管道式架构、模块职责、数据模型、接口契约、ADR、非功能约束 |
| [specs/tasks.md](specs/tasks.md) | 按什么顺序做 | 10 个任务、依赖 DAG、逐条验收标准 |

**变更规则**：需求变更从 `proposal.md` 起改，架构变更从 `design.md` 起改，缺陷修复从代码起改。ADR 遵循「只追加，不修改」。

## 架构

```
GitHub API ─┐
飞书任务 API ─┼→ collector/ → generator/ → notifier/ → SMTP / 飞书
飞书消息 API ─┘        ↑          ↑            ↑
                       └──────────┴────────────┴→ shared/
```

| 模块 | 职责 |
|------|------|
| `collector/` | 从外部 API 获取原始数据，统一封装为 `CollectResult[T]` |
| `generator/` | 按身份映射聚合记录，生成 Markdown / HTML 日报 |
| `notifier/` | SMTP 邮件推送、飞书机器人推送、异常告警 |
| `shared/` | 配置、日志、异常、SQLite 存储、工作日判定 |
| `main.py` | 编排入口（不含业务逻辑） |

## 快速开始

```bash
# 1. 安装依赖
uv sync

# 2. 配置
cp config.yaml.example config.yaml   # 填团队、仓库、群 ID、收件人
cp .env.example .env                 # 填 Token / Secret / 授权码

# 3. 校验配置与连接
uv run python main.py --check

# 4. 试跑（采集 + 生成，但不推送）
uv run python main.py --dry-run

# 5. 正式执行
uv run python main.py
```

## 测试

```bash
uv run pytest              # 全量
uv run pytest tests/test_github.py -v   # 单个模块
```

单元测试全程使用 Mock，**不打真实网络**。

## 运行方式

基于 Cron 的定时任务，每个工作日 18:00 触发，不需要常驻后台服务：

```cron
0 18 * * 1-5  cd /path/to/project && uv run python main.py >> logs/cron.log 2>&1
```

非工作日（周末及法定节假日）由程序自行识别并跳过。

## 安全

- 所有密钥仅从 **环境变量** 读取，`.env` 已被 `.gitignore` 排除
- 仅采集配置文件中明确列出的仓库与群组
- 飞书消息中的敏感词（薪资、绩效等）在采集阶段即被过滤
- 日报只含提交统计，不含代码 diff 内容
