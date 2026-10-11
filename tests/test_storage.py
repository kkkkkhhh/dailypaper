"""SQLite 存储测试（design.md §4.4、ADR-002）。

验收标准：「storage.py 能创建 SQLite，写入和查询日报记录」以及
「init_db() 被调用前，save_report() 不产生未定义行为」。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from shared.models import (
    CommitRecord,
    DailyReport,
    MemberReport,
    MessageRecord,
    TaskRecord,
)
from shared import storage

TS = datetime(2026, 10, 11, 18, 0, tzinfo=timezone.utc)
REPORT_DATE = date(2026, 10, 11)


@pytest.fixture(autouse=True)
def clean_connection():
    """每个测试结束后关闭模块级连接，避免测试之间互相污染。"""
    yield
    storage.close_db()


def make_report() -> DailyReport:
    return DailyReport(
        date=REPORT_DATE,
        team_name="研发一组",
        members=[
            MemberReport(
                name="张三",
                github_username="zhangsan",
                commits=[
                    CommitRecord(
                        author="zhangsan",
                        message="feat: 新增日报模板",
                        timestamp=TS,
                        repo="owner/repo",
                        additions=120,
                        deletions=8,
                        files_changed=3,
                    )
                ],
                tasks=[
                    TaskRecord(
                        assignee="zhangsan",
                        title="日报模板设计",
                        status_from="进行中",
                        status_to="已完成",
                        updated_at=TS,
                    )
                ],
                messages=[
                    MessageRecord(
                        sender="zhangsan",
                        content="模板已提交，请评审",
                        timestamp=TS,
                        chat_name="研发一组",
                    )
                ],
            ),
            MemberReport(name="李四", github_username="lisi-dev"),
        ],
        failed_sources=["lark_task"],
        failed_details={"lark_task": ["项目清单 A", "项目清单 B"]},
        generated_at=TS,
        markdown="# 日报\n\n## 代码提交\n\n- feat: 新增日报模板",
        html="<h1>日报</h1><h2>代码提交</h2>",
    )


def test_init_db_creates_file(tmp_path):
    db_path = tmp_path / "data" / "daily_report.db"

    storage.init_db(db_path)

    assert db_path.exists()
    assert Path(db_path).is_file()


def test_init_db_creates_parent_directory(tmp_path):
    db_path = tmp_path / "nested" / "deeper" / "report.db"

    storage.init_db(db_path)

    assert db_path.exists()


def test_roundtrip_preserves_every_field(tmp_path):
    storage.init_db(tmp_path / "report.db")
    original = make_report()

    storage.save_report(original)
    loaded = storage.get_report(REPORT_DATE)

    assert loaded is not None
    assert loaded.date == original.date
    assert loaded.team_name == original.team_name
    assert loaded.failed_sources == ["lark_task"]
    assert loaded.failed_details == {"lark_task": ["项目清单 A", "项目清单 B"]}
    assert loaded.generated_at == original.generated_at
    assert loaded.markdown == original.markdown
    assert loaded.html == original.html

    assert len(loaded.members) == 2

    first = loaded.members[0]
    assert first.name == "张三"
    assert first.github_username == "zhangsan"

    commit = first.commits[0]
    assert commit.author == "zhangsan"
    assert commit.message == "feat: 新增日报模板"
    assert commit.timestamp == TS
    assert commit.repo == "owner/repo"
    assert commit.additions == 120
    assert commit.deletions == 8
    assert commit.files_changed == 3

    task = first.tasks[0]
    assert task.assignee == "zhangsan"
    assert task.status_from == "进行中"
    assert task.status_to == "已完成"
    assert task.updated_at == TS

    message = first.messages[0]
    assert message.sender == "zhangsan"
    assert message.content == "模板已提交，请评审"
    assert message.chat_name == "研发一组"

    # 无记录的成员也要完整往返（对应「今日无记录」）
    second = loaded.members[1]
    assert second.name == "李四"
    assert second.commits == []
    assert second.tasks == []
    assert second.messages == []


def test_get_missing_date_returns_none(tmp_path):
    storage.init_db(tmp_path / "report.db")

    assert storage.get_report(date(1999, 1, 1)) is None


def test_saving_same_date_overwrites(tmp_path):
    """同一天重复执行（如手动重跑）应覆盖而非报主键冲突。"""
    storage.init_db(tmp_path / "report.db")

    storage.save_report(make_report())

    second = make_report()
    second.markdown = "# 重跑后的日报"
    storage.save_report(second)

    loaded = storage.get_report(REPORT_DATE)
    assert loaded is not None
    assert loaded.markdown == "# 重跑后的日报"


def test_saving_twice_does_not_duplicate(tmp_path):
    storage.init_db(tmp_path / "report.db")
    storage.save_report(make_report())
    storage.save_report(make_report())

    conn = storage._conn()
    count = conn.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0]
    assert count == 1


def test_multiple_dates_are_independent(tmp_path):
    storage.init_db(tmp_path / "report.db")

    first = make_report()
    storage.save_report(first)

    other_date = date(2026, 10, 12)
    other = make_report()
    other.date = other_date
    other.markdown = "# 次日的日报"
    storage.save_report(other)

    assert storage.get_report(REPORT_DATE).markdown == first.markdown
    assert storage.get_report(other_date).markdown == "# 次日的日报"


def test_use_before_init_raises_clear_error(tmp_path):
    """init_db() 未调用时的行为必须**明确定义**，而不是 None 上的属性错误。"""
    storage.close_db()

    with pytest.raises(RuntimeError) as exc:
        storage.save_report(make_report())

    assert "init_db" in str(exc.value)

    with pytest.raises(RuntimeError):
        storage.get_report(REPORT_DATE)


def test_init_db_can_be_called_twice(tmp_path):
    storage.init_db(tmp_path / "a.db")
    storage.init_db(tmp_path / "b.db")

    storage.save_report(make_report())

    assert storage.get_report(REPORT_DATE) is not None
    assert (tmp_path / "b.db").exists()


def test_chinese_content_survives_roundtrip(tmp_path):
    """中文与 emoji 不应被转义损坏。"""
    storage.init_db(tmp_path / "report.db")

    report = make_report()
    report.markdown = "# 日报 📊\n\n- 完成「考勤数据接入」"
    storage.save_report(report)

    assert "📊" in storage.get_report(REPORT_DATE).markdown
    assert "「考勤数据接入」" in storage.get_report(REPORT_DATE).markdown
