"""数据模型测试（design.md §3）。

核对字段名、类型与**数量** —— 这些数量被 tasks.md 逐条写进了验收标准
（CommitRecord 7 个、TaskRecord 5 个、MessageRecord 4 个、CollectResult 5 个、
MemberReport 5 个、DailyReport 7 个），任何一处增减都会破坏契约。
"""

from __future__ import annotations

from dataclasses import fields
from datetime import date, datetime, timezone

from shared.models import (
    CollectResult,
    CommitRecord,
    DailyReport,
    MemberConfig,
    MemberReport,
    MessageRecord,
    TaskRecord,
)

TS = datetime(2026, 10, 11, 9, 30, tzinfo=timezone.utc)


def field_names(cls) -> list[str]:
    return [f.name for f in fields(cls)]


def test_commit_record_has_seven_fields_in_order():
    assert field_names(CommitRecord) == [
        "author",
        "message",
        "timestamp",
        "repo",
        "additions",
        "deletions",
        "files_changed",
    ]


def test_task_record_has_five_fields_in_order():
    assert field_names(TaskRecord) == [
        "assignee",
        "title",
        "status_from",
        "status_to",
        "updated_at",
    ]


def test_message_record_has_four_fields_in_order():
    assert field_names(MessageRecord) == ["sender", "content", "timestamp", "chat_name"]


def test_collect_result_has_five_fields_in_order():
    assert field_names(CollectResult) == [
        "source",
        "success",
        "records",
        "error",
        "failed",
    ]


def test_member_report_has_five_fields_in_order():
    assert field_names(MemberReport) == [
        "name",
        "github_username",
        "commits",
        "tasks",
        "messages",
    ]


def test_daily_report_has_seven_fields_in_order():
    assert field_names(DailyReport) == [
        "date",
        "team_name",
        "members",
        "failed_sources",
        "generated_at",
        "markdown",
        "html",
    ]


# --------------------------------------------------------------------------
# CollectResult 的语义（design.md §3.2 的 4 行语义表）
# --------------------------------------------------------------------------


def test_ok_carries_records():
    record = CommitRecord("zhangsan", "feat: x", TS, "owner/repo", 10, 2, 3)
    result = CollectResult.ok("github", [record])

    assert result.source == "github"
    assert result.success is True
    assert result.records == [record]
    assert result.error is None
    assert result.failed == []


def test_ok_with_empty_records_is_not_a_failure():
    """全部成功但无数据 —— 这是「今日无记录」，不是「数据获取失败」。"""
    result = CollectResult.ok("github", [])

    assert result.success is True
    assert result.records == []
    assert result.error is None
    assert result.failed == []


def test_fail_keeps_partial_records():
    """部分失败时保留已采到的记录 —— 一个坏仓库不应连坐其他仓库的数据。"""
    kept = CommitRecord("lisi", "fix: y", TS, "owner/good", 1, 1, 1)
    result = CollectResult.fail("github", "1/2 个仓库采集失败", records=[kept], failed=["owner/bad"])

    assert result.success is False
    assert result.records == [kept]
    assert result.failed == ["owner/bad"]
    assert result.error == "1/2 个仓库采集失败"


def test_fail_defaults_to_empty_lists():
    result = CollectResult.fail("lark_msg", "boom")

    assert result.success is False
    assert result.records == []
    assert result.failed == []


# --------------------------------------------------------------------------
# MemberConfig
# --------------------------------------------------------------------------


def test_member_config_accepts_email_form():
    member = MemberConfig(name="张三", github="zhangsan", lark="z@company.com")
    assert member.lark_identifier == "z@company.com"


def test_member_config_accepts_open_id_form():
    member = MemberConfig(name="李四", github="lisi", lark_open_id="ou_abc")
    assert member.lark_identifier == "ou_abc"


def test_open_id_wins_when_both_present():
    member = MemberConfig(
        name="王五", github="wangwu", lark="w@company.com", lark_open_id="ou_xyz"
    )
    assert member.lark_identifier == "ou_xyz"


def test_daily_report_construction():
    report = DailyReport(
        date=date(2026, 10, 11),
        team_name="研发一组",
        members=[],
        failed_sources=["lark_task"],
        generated_at=TS,
        markdown="# 日报",
        html="<h1>日报</h1>",
    )
    assert report.failed_sources == ["lark_task"]
    assert report.generated_at.tzinfo is not None
