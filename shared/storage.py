"""SQLite 日报历史存储（design.md §4.4，ADR-002）。

仅用于存储日报历史记录，不做搜索与统计（proposal.md §2.2 明确排除）。
运行方式为定时任务，每天执行一次，不存在并发写入（ADR-002 的选型理由）。

连接为模块级，由 main.py 在流程开始时调用一次 init_db() 建立。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from shared.models import (
    CommitRecord,
    DailyReport,
    MemberReport,
    MessageRecord,
    TaskRecord,
)

_connection: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_reports (
    date           TEXT PRIMARY KEY,
    team_name      TEXT NOT NULL,
    generated_at   TEXT NOT NULL,
    failed_sources TEXT NOT NULL,
    failed_details TEXT NOT NULL,
    members        TEXT NOT NULL,
    markdown       TEXT NOT NULL,
    html           TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------
# 连接管理
# --------------------------------------------------------------------------


def init_db(path: str | Path) -> None:
    """建立模块级连接并建表。重复调用会先关闭旧连接。"""
    global _connection

    if _connection is not None:
        _connection.close()
        _connection = None

    db_path = Path(path)
    if db_path.parent and str(db_path.parent) not in (".", ""):
        db_path.parent.mkdir(parents=True, exist_ok=True)

    _connection = sqlite3.connect(str(db_path))
    _connection.row_factory = sqlite3.Row
    _connection.executescript(_SCHEMA)
    _connection.commit()


def close_db() -> None:
    """关闭连接。测试的 teardown 用；生产流程可不调用。"""
    global _connection
    if _connection is not None:
        _connection.close()
        _connection = None


def _conn() -> sqlite3.Connection:
    if _connection is None:
        raise RuntimeError("storage 尚未初始化，请先调用 init_db(path)")
    return _connection


# --------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------


def _record_to_dict(record: object) -> dict[str, object]:
    data = dict(vars(record))
    for key, value in data.items():
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    return data


def _member_to_dict(member: MemberReport) -> dict[str, object]:
    return {
        "name": member.name,
        "github_username": member.github_username,
        "commits": [_record_to_dict(c) for c in member.commits],
        "tasks": [_record_to_dict(t) for t in member.tasks],
        "messages": [_record_to_dict(m) for m in member.messages],
    }


def _member_from_dict(data: dict[str, object]) -> MemberReport:
    def parse_commits(items: list[dict[str, object]]) -> list[CommitRecord]:
        return [
            CommitRecord(
                author=str(i["author"]),
                message=str(i["message"]),
                timestamp=datetime.fromisoformat(str(i["timestamp"])),
                repo=str(i["repo"]),
                additions=int(i["additions"]),  # type: ignore[arg-type]
                deletions=int(i["deletions"]),  # type: ignore[arg-type]
                files_changed=int(i["files_changed"]),  # type: ignore[arg-type]
            )
            for i in items
        ]

    def parse_tasks(items: list[dict[str, object]]) -> list[TaskRecord]:
        return [
            TaskRecord(
                assignee=str(i["assignee"]),
                title=str(i["title"]),
                status_from=str(i["status_from"]),
                status_to=str(i["status_to"]),
                updated_at=datetime.fromisoformat(str(i["updated_at"])),
            )
            for i in items
        ]

    def parse_messages(items: list[dict[str, object]]) -> list[MessageRecord]:
        return [
            MessageRecord(
                sender=str(i["sender"]),
                content=str(i["content"]),
                timestamp=datetime.fromisoformat(str(i["timestamp"])),
                chat_name=str(i["chat_name"]),
            )
            for i in items
        ]

    return MemberReport(
        name=str(data["name"]),
        github_username=str(data["github_username"]),
        commits=parse_commits(data.get("commits") or []),  # type: ignore[arg-type]
        tasks=parse_tasks(data.get("tasks") or []),  # type: ignore[arg-type]
        messages=parse_messages(data.get("messages") or []),  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------


def save_report(report: DailyReport) -> None:
    """写入（或按日期覆盖）一条日报记录。"""
    conn = _conn()
    conn.execute(
        """
        INSERT INTO daily_reports
            (date, team_name, generated_at, failed_sources, failed_details,
             members, markdown, html)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            team_name      = excluded.team_name,
            generated_at   = excluded.generated_at,
            failed_sources = excluded.failed_sources,
            failed_details = excluded.failed_details,
            members        = excluded.members,
            markdown       = excluded.markdown,
            html           = excluded.html
        """,
        (
            report.date.isoformat(),
            report.team_name,
            report.generated_at.isoformat(),
            json.dumps(report.failed_sources, ensure_ascii=False),
            json.dumps(report.failed_details, ensure_ascii=False),
            json.dumps(
                [_member_to_dict(m) for m in report.members],
                ensure_ascii=False,
            ),
            report.markdown,
            report.html,
        ),
    )
    conn.commit()


def get_report(report_date: date) -> DailyReport | None:
    """按日期取回日报；不存在返回 None。"""
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM daily_reports WHERE date = ?",
        (report_date.isoformat(),),
    ).fetchone()

    if row is None:
        return None

    return DailyReport(
        date=date.fromisoformat(row["date"]),
        team_name=row["team_name"],
        members=[
            _member_from_dict(m) for m in json.loads(row["members"])
        ],
        failed_sources=json.loads(row["failed_sources"]),
        failed_details=json.loads(row["failed_details"]),
        generated_at=datetime.fromisoformat(row["generated_at"]),
        markdown=row["markdown"],
        html=row["html"],
    )
