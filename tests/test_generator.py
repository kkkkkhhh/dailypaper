"""聚合与日报生成测试（design.md §4.2、§4.5，tasks.md Task 6）。

两条关键验收：
- 「今日无记录」与「数据获取失败」是**两种不同文案**，由**不同输入**触发
- 聚合按 name 为主键，lark 与 lark_open_id 两种写法都能匹配
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pytest

from generator.aggregator import aggregate
from generator.formatter import generate
from shared.models import (
    CollectResult,
    CommitRecord,
    MemberConfig,
    MemberReport,
    MessageRecord,
    TaskRecord,
)

TZ = timezone(timedelta(hours=8))
TS = datetime(2026, 10, 11, 14, 30, tzinfo=TZ)
REPORT_DATE = date(2026, 10, 11)

ZHANG = MemberConfig(name="张三", github="zhangsan", lark="zhangsan@company.com")
LI = MemberConfig(name="李四", github="lisi-dev", lark_open_id="ou_lisi")
WANG = MemberConfig(name="王五", github="wangwu", lark_open_id="ou_wangwu")
MEMBERS = [ZHANG, LI, WANG]

# 飞书记录里的身份是 open_id 或邮箱，**不是** GitHub 用户名。
# 用 GitHub 用户名当 assignee/sender，记录会被聚合器按未映射账号正确丢弃 ——
# 那样测试就测不到想测的东西了。
ZHANG_LARK = "zhangsan@company.com"
LI_LARK = "ou_lisi"
WANG_LARK = "ou_wangwu"


def commit(author: str, message: str = "feat: x", repo: str = "owner/repo") -> CommitRecord:
    return CommitRecord(
        author=author,
        message=message,
        timestamp=TS,
        repo=repo,
        additions=10,
        deletions=2,
        files_changed=3,
    )


def task(assignee: str, title: str = "日报模板设计") -> TaskRecord:
    return TaskRecord(
        assignee=assignee,
        title=title,
        status_from="进行中",
        status_to="已完成",
        updated_at=TS,
    )


def message(sender: str, content: str = "模板已提交") -> MessageRecord:
    return MessageRecord(
        sender=sender, content=content, timestamp=TS, chat_name="研发一组"
    )


def results(commits=(), tasks=(), messages=()):
    return (
        CollectResult.ok("github", list(commits)),
        CollectResult.ok("lark_task", list(tasks)),
        CollectResult.ok("lark_msg", list(messages)),
    )


# --------------------------------------------------------------------------
# 聚合
# --------------------------------------------------------------------------


def test_aggregate_matches_github_username():
    commits, tasks, messages = results(commits=[commit("zhangsan")])

    reports = aggregate(commits, tasks, messages, MEMBERS)

    assert len(reports[0].commits) == 1
    assert reports[1].commits == []


def test_aggregate_matches_lark_email_form():
    """配置写邮箱时，用邮箱匹配（编排层须已把邮箱反查为 open_id，或直接匹配邮箱）。"""
    commits, tasks, messages = results(tasks=[task("zhangsan@company.com")])

    reports = aggregate(commits, tasks, messages, MEMBERS)

    assert len(reports[0].tasks) == 1


def test_aggregate_matches_lark_open_id_form():
    commits, tasks, messages = results(messages=[message("ou_lisi")])

    reports = aggregate(commits, tasks, messages, MEMBERS)

    assert len(reports[1].messages) == 1


def test_aggregate_drops_unmapped_accounts():
    """不在映射表中的账号，其记录一律丢弃（§6.2 访问控制）。"""
    commits, tasks, messages = results(
        commits=[commit("stranger"), commit("zhangsan")],
        tasks=[task("ou_stranger")],
        messages=[message("ou_stranger")],
    )

    reports = aggregate(commits, tasks, messages, MEMBERS)

    assert sum(len(r.commits) for r in reports) == 1
    assert sum(len(r.tasks) for r in reports) == 0
    assert sum(len(r.messages) for r in reports) == 0


def test_aggregate_preserves_config_order():
    """输出顺序由配置决定，不由「谁恰好有数据」决定。

    否则某人当天没提交就会从日报里消失，而不是显示「今日无记录」。
    """
    commits, tasks, messages = results(commits=[commit("wangwu"), commit("zhangsan")])

    reports = aggregate(commits, tasks, messages, MEMBERS)

    assert [r.name for r in reports] == ["张三", "李四", "王五"]


def test_aggregate_keeps_members_without_records():
    commits, tasks, messages = results()

    reports = aggregate(commits, tasks, messages, MEMBERS)

    assert [r.name for r in reports] == ["张三", "李四", "王五"]
    assert all(r.commits == [] and r.tasks == [] and r.messages == [] for r in reports)


def test_aggregate_records_failed_sources_are_still_not_consumed():
    """聚合只读不改采集结果 —— 失败信息由编排层另行透传。"""
    commits = CollectResult.fail("github", "1/2 仓库失败", failed=["owner/bad"])

    reports = aggregate(commits, CollectResult.ok("lark_task", []), CollectResult.ok("lark_msg", []), MEMBERS)

    assert reports[0].commits == []
    assert commits.success is False
    assert commits.failed == ["owner/bad"]


# --------------------------------------------------------------------------
# 生成：三段式结构
# --------------------------------------------------------------------------


def test_generate_contains_three_section_titles():
    commits, tasks, messages = results()
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS), [], REPORT_DATE, "研发一组"
    )

    assert "## 代码提交" in report.markdown
    assert "## 任务进展" in report.markdown
    assert "## 协作沟通" in report.markdown


def test_generate_returns_all_seven_fields():
    commits, tasks, messages = results()
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS), [], REPORT_DATE, "研发一组"
    )

    assert report.date == REPORT_DATE
    assert report.team_name == "研发一组"
    assert len(report.members) == 3
    assert report.failed_sources == []
    assert report.generated_at.tzinfo is not None
    assert report.markdown
    assert report.html


def test_generate_renders_commit_stats():
    commits, tasks, messages = results(
        commits=[commit("zhangsan", message="feat: 新增模板")]
    )
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS), [], REPORT_DATE, "研发一组"
    )

    assert "feat: 新增模板" in report.markdown
    assert "+10 行 / -2 行" in report.markdown
    assert "3 个文件" in report.markdown


def test_generate_renders_task_transition():
    commits, tasks, messages = results(tasks=[task(ZHANG_LARK, "日报模板设计")])
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS), [], REPORT_DATE, "研发一组"
    )

    assert "日报模板设计：进行中 → 已完成" in report.markdown


def test_generate_renders_message_with_time():
    commits, tasks, messages = results(messages=[message(ZHANG_LARK, "模板已提交，请评审")])
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS), [], REPORT_DATE, "研发一组"
    )

    assert "[14:30] 模板已提交，请评审" in report.markdown


# --------------------------------------------------------------------------
# 关键验收：「今日无记录」vs「数据获取失败」
# --------------------------------------------------------------------------


def test_empty_member_shows_no_records_today():
    commits, tasks, messages = results()
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS), [], REPORT_DATE, "研发一组"
    )

    assert "今日无记录" in report.markdown


def test_failed_source_shows_fetch_failure_not_no_records():
    """同一份空成员数据，加上 failed_sources 后文案必须**变**。

    这是 ADR-003 引入 CollectResult、ADR-004 补 failed_sources 入参的全部
    意义所在 —— 若两者文案相同，这两次接口修订就白做了。
    """
    commits, tasks, messages = results()
    members = aggregate(commits, tasks, messages, MEMBERS)

    without_failure = generate(members, [], REPORT_DATE, "研发一组")
    with_failure = generate(members, ["github"], REPORT_DATE, "研发一组")

    assert without_failure.markdown != with_failure.markdown
    assert "数据获取失败" not in without_failure.markdown
    assert "数据获取失败" in with_failure.markdown
    assert "GitHub 数据获取失败" in with_failure.markdown


def test_failed_source_never_falls_back_to_no_records():
    """失败板块绝不显示「今日无记录」—— 我们并不知道成员当天有没有记录。

    把这句写上去，等于用「没干活」掩盖了「采集失败」。
    """
    commits, tasks, messages = results(commits=[commit("zhangsan")])
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS), ["github"], REPORT_DATE, "研发一组"
    )

    section = report.markdown.split("## 代码提交")[1].split("## 任务进展")[0]
    assert "数据获取失败" in section
    assert "今日无记录" not in section


def test_partial_failure_still_shows_the_data_we_did_collect():
    """部分失败时，已采到的数据必须照常展示。

    否则一个坏仓库会连带把其他仓库采到的内容从日报上整个抹掉 ——
    「部分失败不退化」这句话就只剩字面意义。
    """
    commits, tasks, messages = results(commits=[commit("zhangsan", message="feat: 有数据")])
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS),
        ["github"],
        REPORT_DATE,
        "研发一组",
        failed_details={"github": ["owner/repoB"]},
    )

    section = report.markdown.split("## 代码提交")[1].split("## 任务进展")[0]
    assert "数据获取失败" in section
    assert "owner/repoB" in section
    assert "feat: 有数据" in section
    assert "数据不完整" in section


def test_whole_source_failure_without_data_renders_notice_only():
    """整源失败且一条都没采到 —— 只出告示，不列成员。"""
    commits, tasks, messages = results()
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS), ["github"], REPORT_DATE, "研发一组"
    )

    section = report.markdown.split("## 代码提交")[1].split("## 任务进展")[0]
    assert "数据获取失败" in section
    assert "张三" not in section
    assert "今日无记录" not in section


def test_other_sections_unaffected_by_one_failure():
    """单一数据源失败不阻断其他源（proposal.md §3.2）。"""
    commits, tasks, messages = results(tasks=[task(ZHANG_LARK)])
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS), ["github"], REPORT_DATE, "研发一组"
    )

    task_section = report.markdown.split("## 任务进展")[1].split("## 协作沟通")[0]
    assert "日报模板设计" in task_section


def test_report_keeps_failed_sources():
    commits, tasks, messages = results()
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS),
        ["github", "lark_msg"],
        REPORT_DATE,
        "研发一组",
    )

    assert report.failed_sources == ["github", "lark_msg"]
    assert "数据获取失败" in report.markdown


# --------------------------------------------------------------------------
# 失败子项明细（design.md §3.2 的 failed_details 承载）
# --------------------------------------------------------------------------


def test_failed_details_rendered_in_both_formats():
    """部分失败时必须显示**哪个子项**失败了，Markdown 与 HTML 都要有。

    只显示「GitHub 数据获取失败」而不说哪个仓库，读者还是不知道该找谁。
    """
    commits, tasks, messages = results(commits=[commit("zhangsan")])
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS),
        ["github"],
        REPORT_DATE,
        "研发一组",
        failed_details={"github": ["owner/repoB"]},
    )

    for produced in (report.markdown, report.html):
        assert "GitHub 数据获取失败" in produced
        assert "owner/repoB" in produced


def test_failed_details_stored_on_report_object():
    """明细要落在报告对象上 —— 否则 Task 10 只能靠字符串匹配，模板一改就误报。"""
    commits, tasks, messages = results()
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS),
        ["github"],
        REPORT_DATE,
        "研发一组",
        failed_details={"github": ["owner/repoB", "owner/repoC"]},
    )

    assert report.failed_details == {"github": ["owner/repoB", "owner/repoC"]}


def test_multiple_failed_sources_each_render_their_own_detail():
    commits, tasks, messages = results()
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS),
        ["github", "lark_task"],
        REPORT_DATE,
        "研发一组",
        failed_details={"github": ["owner/repoB"], "lark_task": ["项目清单 A"]},
    )

    github_section = report.markdown.split("## 代码提交")[1].split("## 任务进展")[0]
    task_section = report.markdown.split("## 任务进展")[1].split("## 协作沟通")[0]

    assert "owner/repoB" in github_section
    assert "项目清单 A" not in github_section

    assert "项目清单 A" in task_section
    assert "owner/repoB" not in task_section


def test_whole_source_failure_uses_error_summary_as_detail():
    """整源失败（failed 为空）时，明细位置放错误摘要。"""
    commits, tasks, messages = results()
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS),
        ["lark_msg"],
        REPORT_DATE,
        "研发一组",
        failed_details={"lark_msg": ["机器人不在群中（230002）"]},
    )

    assert "230002" in report.markdown
    assert "230002" in report.html


def test_failed_details_defaults_to_empty():
    """不传 failed_details 时行为与之前一致，只是括号内容为空。"""
    commits, tasks, messages = results()
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS), ["github"], REPORT_DATE, "研发一组"
    )

    assert report.failed_details == {}
    assert "GitHub 数据获取失败" in report.markdown
    assert "（）" not in report.markdown  # 明细为空时不留下空括号


def test_details_for_non_failed_source_are_ignored():
    """failed_details 里有、但 failed_sources 里没有的源，不该冒出来。"""
    commits, tasks, messages = results()
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS),
        [],
        REPORT_DATE,
        "研发一组",
        failed_details={"github": ["owner/repoB"]},
    )

    assert "owner/repoB" not in report.markdown
    assert "数据获取失败" not in report.markdown


# --------------------------------------------------------------------------
# 安全（design.md §6.2）
# --------------------------------------------------------------------------


def test_html_escapes_user_content():
    """提交信息里的 <script> 不能被当标签执行。"""
    commits, tasks, messages = results(
        commits=[commit("zhangsan", message="<script>alert(1)</script>")]
    )
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS), [], REPORT_DATE, "研发一组"
    )

    assert "<script>alert(1)</script>" not in report.html
    assert "&lt;script&gt;" in report.html


def test_markdown_does_not_escape_user_content():
    """Markdown 不该转义 —— 否则 & 会变成 &amp;，污染正文。"""
    commits, tasks, messages = results(
        commits=[commit("zhangsan", message="fix: A & B 的边界")]
    )
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS), [], REPORT_DATE, "研发一组"
    )

    assert "A & B" in report.markdown
    assert "&amp;" not in report.markdown


def test_markdown_contains_no_diff_content():
    """日报只含统计数字，不含代码差异内容（design.md §6.2）。"""
    commits, tasks, messages = results(
        commits=[commit("zhangsan", message="feat: 改造聚合逻辑")]
    )
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS), [], REPORT_DATE, "研发一组"
    )

    assert "diff --git" not in report.markdown
    assert "@@" not in report.markdown
    assert "+10 行" in report.markdown  # 统计数字在，diff 内容不在


def test_markdown_blocks_are_separated_by_blank_lines():
    """Markdown 的块级语法靠空行划界，少一个空行含义就变了。

    最典型的是引用块：`> ⚠️ 数据获取失败` 后面紧跟的正文行会被 CommonMark
    当作该引用的**延续**（lazy continuation），整段被渲染进引用里。
    这条测试钉住这个性质，免得将来改模板时被 trim_blocks 之类的空白处理
    悄悄破坏 —— 那种 bug 只有肉眼看渲染结果才发现得了。
    """
    commits, tasks, messages = results(commits=[commit("zhangsan")])
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS),
        ["github"],
        REPORT_DATE,
        "研发一组",
        failed_details={"github": ["owner/repoB"]},
    )

    lines = report.markdown.splitlines()

    for index, line in enumerate(lines[:-1]):
        if line.startswith(">"):
            following = lines[index + 1]
            assert following == "" or following.startswith(">"), (
                f"引用块会把 {following!r} 吞进引用里"
            )

    for index, line in enumerate(lines):
        if not line.startswith("#"):
            continue
        if index > 0:
            assert lines[index - 1] == "", f"{line!r} 前缺少空行"
        if index < len(lines) - 1:
            assert lines[index + 1] == "", f"{line!r} 后缺少空行"


def test_markdown_ends_with_single_newline():
    commits, tasks, messages = results()
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS), [], REPORT_DATE, "研发一组"
    )

    assert report.markdown.endswith("\n")
    assert not report.markdown.endswith("\n\n")


def test_html_is_wellformed_document():
    commits, tasks, messages = results()
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS), [], REPORT_DATE, "研发一组"
    )

    assert report.html.startswith("<!DOCTYPE html>")
    assert report.html.rstrip().endswith("</html>")
    assert 'charset="utf-8"' in report.html


def test_missing_records_render_as_no_records_even_in_html():
    commits, tasks, messages = results()
    report = generate(
        aggregate(commits, tasks, messages, MEMBERS), [], REPORT_DATE, "研发一组"
    )

    assert "今日无记录" in report.html


# --------------------------------------------------------------------------
# 边界
# --------------------------------------------------------------------------


def test_unknown_failed_source_is_logged_not_silently_ignored(caplog):
    """数据源标识拼错时不该静默少一个标注。

    用 caplog 而不是 capsys：logger 的 handler 在模块 import 时就绑定了
    当时的 sys.stdout，capsys 之后替换的流它看不到。
    """
    commits, tasks, messages = results()

    with caplog.at_level(logging.ERROR, logger="generator.formatter"):
        report = generate(
            aggregate(commits, tasks, messages, MEMBERS),
            ["githbu"],
            REPORT_DATE,
            "研发一组",
        )

    assert "githbu" not in report.markdown
    assert any("githbu" in record.getMessage() for record in caplog.records)


def test_generated_at_can_be_pinned_for_tests():
    committed_now = datetime(2026, 10, 11, 18, 0, tzinfo=TZ)
    commits, tasks, messages = results()

    report = generate(
        aggregate(commits, tasks, messages, MEMBERS),
        [],
        REPORT_DATE,
        "研发一组",
        now=committed_now,
    )

    assert report.generated_at == committed_now
    assert "2026-10-11 18:00" in report.markdown


@pytest.mark.parametrize("member_count", [1, 5])
def test_generate_handles_various_team_sizes(member_count):
    members = [
        MemberConfig(name=f"成员{i}", github=f"user{i}", lark_open_id=f"ou_{i}")
        for i in range(member_count)
    ]
    commits, tasks, messages = results(
        commits=[commit(f"user{i}") for i in range(member_count)]
    )

    report = generate(
        aggregate(commits, tasks, messages, members), [], REPORT_DATE, "研发一组"
    )

    assert len(report.members) == member_count

    # 只对代码提交板块断言「没有空的」—— 本用例没造任务和消息数据，
    # 那两段显示「今日无记录」是正确行为，不是缺陷
    commit_section = report.markdown.split("## 代码提交")[1].split("## 任务进展")[0]
    assert "今日无记录" not in commit_section
    for i in range(member_count):
        assert f"### 成员{i}" in commit_section
