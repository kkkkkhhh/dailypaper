"""跨模块共享的数据模型（design.md §3）。

本模块是全部数据模型的**唯一定义处**。采集层、生成层、推送层与存储层
共用同一份定义 —— 若各模块自行定义，并行开发时必然出现类型不匹配。

字段名、类型、顺序均与 design.md §3 的表格严格对应。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Generic, TypeVar

T = TypeVar("T")


# --------------------------------------------------------------------------
# §3.1 采集记录
# --------------------------------------------------------------------------


@dataclass
class CommitRecord:
    """代码提交记录（design.md §3.1，共 7 个字段）。"""

    author: str  # 提交者（GitHub 用户名）
    message: str  # 提交信息（Commit Message）
    timestamp: datetime  # 提交时间（tz-aware）
    repo: str  # 仓库名称
    additions: int  # 新增行数
    deletions: int  # 删除行数
    files_changed: int  # 变更文件数


@dataclass
class TaskRecord:
    """任务变更记录（design.md §3.1，共 5 个字段）。"""

    assignee: str  # 负责人（飞书用户名）
    title: str  # 任务标题
    status_from: str  # 原状态
    status_to: str  # 新状态
    updated_at: datetime  # 变更时间（tz-aware）


@dataclass
class MessageRecord:
    """消息记录（design.md §3.1，共 4 个字段）。"""

    sender: str  # 发送者（飞书用户名）
    content: str  # 消息内容（纯文本）
    timestamp: datetime  # 发送时间（tz-aware）
    chat_name: str  # 群名称


# --------------------------------------------------------------------------
# §3.2 采集结果封装
# --------------------------------------------------------------------------


@dataclass
class CollectResult(Generic[T]):
    """采集结果（design.md §3.2，共 5 个字段）。

    success 反映**整个数据源是否完全正常**，采用「全部子项成功才为 True」
    的判定。部分失败时 records 保留成功子项已采到的记录，不丢弃 —— 否则
    一个坏仓库会连坐其他仓库的数据，与优雅降级原则相悖。
    """

    source: str  # "github" / "lark_task" / "lark_msg"
    success: bool
    records: list[T] = field(default_factory=list)
    error: str | None = None
    failed: list[str] = field(default_factory=list)

    @classmethod
    def ok(cls, source: str, records: list[T]) -> CollectResult[T]:
        """全部子项成功（records 可为空 —— 空列表表示「今日无数据」，非失败）。"""
        return cls(source=source, success=True, records=list(records))

    @classmethod
    def fail(
        cls,
        source: str,
        error: str,
        records: list[T] | None = None,
        failed: list[str] | None = None,
    ) -> CollectResult[T]:
        """存在失败子项。records 保留已采到的部分，failed 记录失败子项明细。"""
        return cls(
            source=source,
            success=False,
            records=list(records or []),
            error=error,
            failed=list(failed or []),
        )


# --------------------------------------------------------------------------
# §3.3 日报模型
# --------------------------------------------------------------------------


@dataclass
class MemberReport:
    """成员报告（design.md §3.3，共 5 个字段）。"""

    name: str  # 成员姓名
    github_username: str  # GitHub 用户名
    commits: list[CommitRecord] = field(default_factory=list)
    tasks: list[TaskRecord] = field(default_factory=list)
    messages: list[MessageRecord] = field(default_factory=list)


@dataclass
class DailyReport:
    """每日报告（design.md §3.3，共 8 个字段）。"""

    date: date  # 日报日期
    team_name: str  # 团队名称
    members: list[MemberReport]  # 各成员的日报段落
    failed_sources: list[str]  # 采集失败的数据源标识；决定哪些板块显示失败标注
    failed_details: dict[str, list[str]]  # 数据源 → 失败子项明细；整源失败时值为 error 摘要
    generated_at: datetime  # 生成时间（tz-aware）
    markdown: str  # 完整的 Markdown 格式日报
    html: str  # 完整的 HTML 格式日报


# --------------------------------------------------------------------------
# §3.4 身份映射项
# --------------------------------------------------------------------------


@dataclass
class MemberConfig:
    """身份映射表的一项（design.md §3.4）。

    飞书标识二选一：lark（企业邮箱）或 lark_open_id（open_id）。
    两者同时填写时以 lark_open_id 为准。
    """

    name: str
    github: str
    lark: str | None = None
    lark_open_id: str | None = None

    @property
    def lark_identifier(self) -> str:
        """返回用于匹配飞书记录的标识，优先 open_id。"""
        return self.lark_open_id or self.lark or ""
