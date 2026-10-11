"""主编排入口测试（tasks.md Task 9）。

编排层的职责是「按顺序调用 + 兜底 + 记录状态」，所以这里主要测**编排行为**：
什么情况下调用什么、失败如何降级、告警走哪个通道、退出码是什么。
具体的数据变换由 aggregator/formatter 自己的测试覆盖，这里直接用真实实现。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pytest

import main
from collector.lark_client import LarkClient
from shared.config import (
    Config,
    FiltersConfig,
    GithubConfig,
    LarkConfig,
    ScheduleConfig,
    SmtpConfig,
)
from shared.errors import ConfigError
from shared.models import (
    CollectResult,
    CommitRecord,
    MemberConfig,
)

TZ = timezone(timedelta(hours=8))
MONDAY = date(2026, 10, 12)  # 普通工作日
SATURDAY = date(2026, 10, 17)  # 普通周末
HOLIDAY = date(2026, 10, 1)  # 法定假日（周四）


def make_config(
    *,
    members=None,
    holidays=(),
    workdays=(),
    repos=("owner/repo",),
    recipients=("leader@company.com",),
    chat_id="oc_test",
):
    return Config(
        team_name="研发一组",
        members=members
        if members is not None
        else [MemberConfig(name="张三", github="zhangsan", lark_open_id="ou_zhang")],
        github=GithubConfig(repos=list(repos), token="gh_token"),
        lark=LarkConfig(
            chat_id=chat_id,
            project_id="",
            app_id="cli_test",
            app_secret="secret",
        ),
        smtp=SmtpConfig(
            host="smtp.qq.com",
            port=465,
            sender="sender@qq.com",
            recipients=list(recipients),
            password="smtp_password",
        ),
        filters=FiltersConfig(keywords=["项目"], sensitive_keywords=["薪资"]),
        schedule=ScheduleConfig(
            timezone="Asia/Shanghai",
            db_path="unused.db",
            log_dir="",
            holidays=list(holidays),
            workdays=list(workdays),
        ),
    )


def at(day: date, hour: int = 18) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=TZ)


def ok(source, records=()):
    return CollectResult.ok(source, list(records))


def failed(source, error="boom", records=(), failed_items=()):
    return CollectResult.fail(source, error, records=records, failed=failed_items)


def commit(author="zhangsan"):
    return CommitRecord(
        author=author,
        message="feat: x",
        timestamp=at(MONDAY, 10),
        repo="owner/repo",
        additions=5,
        deletions=1,
        files_changed=2,
    )


class Harness:
    """把编排层依赖的外部世界全部替换掉，只留下编排逻辑本身。"""

    def __init__(self, monkeypatch, config):
        self.config = config
        self.monkeypatch = monkeypatch
        self.collected: dict[str, tuple] = {}
        self.results: dict[str, CollectResult] = {
            "github": ok("github"),
            "lark_task": ok("lark_task"),
            "lark_msg": ok("lark_msg"),
        }
        self.saved: list = []
        self.alerts: list[tuple] = []
        self.emails: list = []
        self.larks: list = []
        self.email_result = True
        self.lark_result = True
        self.resolved: dict[str, str] = {}

        monkeypatch.setattr(main, "load_config", lambda *a, **k: config)
        monkeypatch.setattr(main.storage, "init_db", lambda path: None)
        monkeypatch.setattr(main.storage, "save_report", self.saved.append)

        for source, module in (
            ("github", main.github),
            ("lark_task", main.lark_task),
            ("lark_msg", main.lark_msg),
        ):
            monkeypatch.setattr(module, "collect", self._collector(source))

        monkeypatch.setattr(
            main.email_notifier, "send", self._email_send
        )
        monkeypatch.setattr(main.lark_bot, "send", self._lark_send)
        monkeypatch.setattr(main.alert, "report_alert", self._alert)
        monkeypatch.setattr(main, "_resolve_members", self._resolve)

    def _collector(self, source: str):
        def collect(*args, **kwargs):
            self.collected[source] = (args, kwargs)
            return self.results[source]

        return collect

    def _email_send(self, report, recipients):
        self.emails.append((report, recipients))
        return self.email_result

    def _lark_send(self, report, chat_id):
        self.larks.append((report, chat_id))
        return self.lark_result

    def _alert(self, subject, detail, channels=None, **kwargs):
        self.alerts.append((subject, detail, channels))

    def _resolve(self, config):
        if self.resolved:
            return [
                main.MemberConfig(
                    name=m.name, github=m.github, lark_open_id=self.resolved[m.name]
                )
                for m in config.members
            ]
        return list(config.members)


@pytest.fixture
def config():
    return make_config()


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------


def test_full_run_generates_saves_and_pushes(monkeypatch, config):
    h = Harness(monkeypatch, config)
    h.results["github"] = ok("github", [commit()])

    code = main._run_pipeline(config, push=True, now=at(MONDAY))

    assert code == main.EXIT_OK
    assert len(h.saved) == 1
    assert len(h.emails) == 1
    assert len(h.larks) == 1


def test_pushed_report_carries_the_generated_content(monkeypatch, config):
    h = Harness(monkeypatch, config)
    h.results["github"] = ok("github", [commit()])

    main._run_pipeline(config, push=True, now=at(MONDAY))

    report, recipients = h.emails[0]
    assert recipients == ["leader@company.com"]
    assert "## 代码提交" in report.markdown
    assert "feat: x" in report.markdown
    assert report.date == MONDAY


def test_dry_run_skips_pushing_but_still_saves(monkeypatch, config):
    h = Harness(monkeypatch, config)

    code = main._run_pipeline(config, push=False, now=at(MONDAY))

    assert code == main.EXIT_OK
    assert len(h.saved) == 1
    assert h.emails == []
    assert h.larks == []


def test_collectors_receive_the_configured_scope(monkeypatch, config):
    h = Harness(monkeypatch, config)

    main._run_pipeline(config, push=False, now=at(MONDAY))

    assert h.collected["github"][0][0] == ["owner/repo"]
    assert h.collected["lark_task"][0][0] == ""
    assert h.collected["lark_msg"][0][0] == "oc_test"
    assert h.collected["lark_msg"][0][1] == ["项目"]


def test_collection_window_starts_at_midnight(monkeypatch, config):
    """采集窗口是「当日 00:00 至执行时刻」（proposal.md §4 术语表）。"""
    h = Harness(monkeypatch, config)

    main._run_pipeline(config, push=False, now=at(MONDAY, 18))

    since, until = h.collected["github"][0][1], h.collected["github"][0][2]
    assert since == datetime(2026, 10, 12, 0, 0, tzinfo=TZ)
    assert until == at(MONDAY, 18)


# --------------------------------------------------------------------------
# 非工作日（design.md §6.7）
# --------------------------------------------------------------------------


def test_weekend_skips_the_whole_pipeline(monkeypatch, config):
    h = Harness(monkeypatch, config)

    code = main._run_pipeline(config, push=True, now=at(SATURDAY))

    assert code == main.EXIT_OK  # 跳过是正常流程，不是错误
    assert h.collected == {}
    assert h.saved == []


def test_holiday_skips_the_whole_pipeline(monkeypatch):
    config = make_config(holidays=[HOLIDAY])
    h = Harness(monkeypatch, config)

    code = main._run_pipeline(config, push=True, now=at(HOLIDAY))

    assert code == main.EXIT_OK
    assert h.collected == {}


def test_makeup_workday_on_a_weekend_does_not_skip(monkeypatch):
    """调休补班日即使是周末也要出日报 —— 这正是 workdays 清单存在的理由。"""
    config = make_config(workdays=[SATURDAY])
    h = Harness(monkeypatch, config)

    main._run_pipeline(config, push=True, now=at(SATURDAY))

    assert "github" in h.collected
    assert len(h.saved) == 1


def test_dry_run_ignores_weekend(monkeypatch, config):
    """--dry-run 的用途正是在任意日期验证链路。"""
    h = Harness(monkeypatch, config)

    code = main._run_pipeline(config, push=False, now=at(SATURDAY))

    assert code == main.EXIT_OK
    assert "github" in h.collected


def test_skip_reason_is_logged(monkeypatch, config, caplog):
    Harness(monkeypatch, config)

    with caplog.at_level(logging.INFO, logger="main"):
        main._run_pipeline(config, push=True, now=at(SATURDAY))

    records = [r for r in caplog.records if "非工作日" in r.getMessage()]
    assert len(records) == 1
    assert "周末" in records[0].fields["reason"]


# --------------------------------------------------------------------------
# 降级：单源失败
# --------------------------------------------------------------------------


def test_single_source_failure_does_not_block_the_rest(monkeypatch, config):
    h = Harness(monkeypatch, config)
    h.results["github"] = failed("github", "令牌过期")
    h.results["lark_task"] = ok("lark_task")

    code = main._run_pipeline(config, push=True, now=at(MONDAY))

    assert code == main.EXIT_OK
    assert len(h.saved) == 1
    assert len(h.emails) == 1


def test_partial_failure_marks_the_section_and_keeps_failed_items(
    monkeypatch, config
):
    """多仓库部分失败时，明细要精确到子项而不是笼统说「GitHub 挂了」。"""
    h = Harness(monkeypatch, config)
    h.results["github"] = failed(
        "github", "1/2 个仓库失败", records=[commit()], failed_items=["owner/repoB"]
    )

    main._run_pipeline(config, push=True, now=at(MONDAY))

    report = h.saved[0]
    assert report.failed_sources == ["github"]
    assert report.failed_details == {"github": ["owner/repoB"]}
    assert "owner/repoB" in report.markdown
    # 失败源里已采到的记录不该丢
    assert "feat: x" in report.markdown


def test_whole_source_failure_falls_back_to_error_summary(monkeypatch, config):
    h = Harness(monkeypatch, config)
    h.results["lark_msg"] = failed("lark_msg", "机器人不在群中（230002）")

    main._run_pipeline(config, push=True, now=at(MONDAY))

    report = h.saved[0]
    assert report.failed_details == {"lark_msg": ["机器人不在群中（230002）"]}


def test_successful_sources_are_not_marked(monkeypatch, config):
    h = Harness(monkeypatch, config)
    h.results["github"] = failed("github", "超时")

    main._run_pipeline(config, push=True, now=at(MONDAY))

    report = h.saved[0]
    assert report.failed_sources == ["github"]
    task_section = report.markdown.split("## 任务进展")[1].split("## 协作沟通")[0]
    assert "数据获取失败" not in task_section


# --------------------------------------------------------------------------
# 降级：全源失败
# --------------------------------------------------------------------------


def test_all_sources_failing_aborts_without_empty_report(monkeypatch, config):
    h = Harness(monkeypatch, config)
    for source in ("github", "lark_task", "lark_msg"):
        h.results[source] = failed(source, f"{source} 挂了")

    code = main._run_pipeline(config, push=True, now=at(MONDAY))

    assert code == main.EXIT_FAILED
    assert h.saved == []  # 不生成空日报
    assert h.emails == []
    assert h.larks == []


def test_all_sources_failing_triggers_alert(monkeypatch, config):
    h = Harness(monkeypatch, config)
    for source in ("github", "lark_task", "lark_msg"):
        h.results[source] = failed(source, f"{source} 挂了")

    main._run_pipeline(config, push=True, now=at(MONDAY))

    assert len(h.alerts) == 1
    subject, detail, channels = h.alerts[0]
    assert "所有数据源均不可用" in subject
    # 三个源的错误摘要都要在告警里，否则排查时还得去翻日志
    for source in ("github", "lark_task", "lark_msg"):
        assert source in detail


# --------------------------------------------------------------------------
# 推送失败 → 用另一个通道告警
# --------------------------------------------------------------------------


def test_email_failure_alerts_via_lark(monkeypatch, config):
    h = Harness(monkeypatch, config)
    h.email_result = False

    main._run_pipeline(config, push=True, now=at(MONDAY))

    assert len(h.alerts) == 1
    assert h.alerts[0][2] == [main.alert.LARK]


def test_lark_failure_alerts_via_email(monkeypatch, config):
    h = Harness(monkeypatch, config)
    h.lark_result = False

    main._run_pipeline(config, push=True, now=at(MONDAY))

    assert len(h.alerts) == 1
    assert h.alerts[0][2] == [main.alert.EMAIL]


def test_both_channels_failing_exits_nonzero(monkeypatch, config):
    h = Harness(monkeypatch, config)
    h.email_result = False
    h.lark_result = False

    code = main._run_pipeline(config, push=True, now=at(MONDAY))

    assert code == main.EXIT_FAILED
    assert len(h.alerts) == 2  # 两条各自告警


def test_single_channel_success_still_exits_ok(monkeypatch, config):
    """推送渠道互为备份 —— 只要有一个送出去了，就算送达。"""
    h = Harness(monkeypatch, config)
    h.email_result = False

    code = main._run_pipeline(config, push=True, now=at(MONDAY))

    assert code == main.EXIT_OK


# --------------------------------------------------------------------------
# 身份映射解析（design.md §3.4）
# --------------------------------------------------------------------------


def test_email_identifiers_are_resolved_to_open_ids(monkeypatch):
    config = make_config(
        members=[MemberConfig(name="张三", github="zhangsan", lark="z@company.com")]
    )
    h = Harness(monkeypatch, config)
    h.resolved = {"张三": "ou_resolved"}

    main._run_pipeline(config, push=False, now=at(MONDAY))

    # 聚合后记录应挂到该成员下（用 open_id 匹配）
    report = h.saved[0]
    assert [m.name for m in report.members] == ["张三"]


def test_skips_resolution_when_open_id_already_present(monkeypatch, config):
    """全部用 open_id 写法时不该发起反查请求 —— 那是一次白跑的网络往返。"""
    calls = {"n": 0}

    class FakeLarkClient:
        def __init__(self, *a, **k):
            calls["n"] += 1

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def resolve_open_ids(self, emails):
            return {}

    monkeypatch.setattr(main, "LarkClient", FakeLarkClient)

    resolved = main._resolve_members(config)

    assert calls["n"] == 0
    assert resolved[0].lark_open_id == "ou_zhang"


def test_resolution_fills_open_id_for_email_members(monkeypatch):
    config = make_config(
        members=[MemberConfig(name="张三", github="zhangsan", lark="z@company.com")]
    )

    class FakeLarkClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def resolve_open_ids(self, emails):
            assert emails == ["z@company.com"]
            return {"z@company.com": "ou_resolved"}

    monkeypatch.setattr(main, "LarkClient", FakeLarkClient)

    resolved = main._resolve_members(config)

    assert resolved[0].lark_open_id == "ou_resolved"


def test_unresolvable_email_is_logged_loudly(monkeypatch, caplog):
    """解析不到必须报错 —— 否则该成员的记录会被按未映射账号静默丢弃，
    日报上只显示「今日无记录」，看起来像当天没干活。"""
    config = make_config(
        members=[MemberConfig(name="张三", github="zhangsan", lark="z@company.com")]
    )

    class FakeLarkClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def resolve_open_ids(self, emails):
            return {}

    monkeypatch.setattr(main, "LarkClient", FakeLarkClient)

    with caplog.at_level(logging.ERROR, logger="main"):
        main._resolve_members(config)

    assert any("无法解析为 open_id" in r.getMessage() for r in caplog.records)


def test_resolution_failure_does_not_abort_the_run(monkeypatch, caplog):
    """反查接口挂了不该让整个流程停摆 —— GitHub 那部分数据仍然有价值。"""
    config = make_config(
        members=[MemberConfig(name="张三", github="zhangsan", lark="z@company.com")]
    )

    class ExplodingLarkClient:
        def __init__(self, *a, **k):
            raise RuntimeError("飞书不可达")

    monkeypatch.setattr(main, "LarkClient", ExplodingLarkClient)

    with caplog.at_level(logging.ERROR, logger="main"):
        resolved = main._resolve_members(config)

    assert len(resolved) == 1
    assert any("反查失败" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# 日志（design.md §6.3）
# --------------------------------------------------------------------------


def test_log_records_start_counts_and_end(monkeypatch, config, caplog):
    h = Harness(monkeypatch, config)
    h.results["github"] = ok("github", [commit()])

    with caplog.at_level(logging.INFO, logger="main"):
        main._run_pipeline(config, push=True, now=at(MONDAY))

    messages = [r.getMessage() for r in caplog.records]
    assert any("执行开始" in m for m in messages)
    assert messages.count("数据源采集完成") == 3
    assert any("执行结束" in m for m in messages)

    counts = {
        r.fields["source"]: r.fields["count"]
        for r in caplog.records
        if r.getMessage() == "数据源采集完成"
    }
    assert counts == {"github": 1, "lark_task": 0, "lark_msg": 0}


def test_log_records_push_results(monkeypatch, config, caplog):
    Harness(monkeypatch, config)

    with caplog.at_level(logging.INFO, logger="main"):
        main._run_pipeline(config, push=True, now=at(MONDAY))

    end = next(r for r in caplog.records if r.getMessage() == "执行结束")
    assert end.fields["email_pushed"] is True
    assert end.fields["lark_pushed"] is True


def test_archive_is_logged(monkeypatch, config, caplog):
    Harness(monkeypatch, config)

    with caplog.at_level(logging.INFO, logger="main"):
        main._run_pipeline(config, push=True, now=at(MONDAY))

    assert any(r.getMessage() == "日报已存档" for r in caplog.records)


# --------------------------------------------------------------------------
# main() 入口与参数
# --------------------------------------------------------------------------


def test_config_error_exits_with_dedicated_code(monkeypatch):
    def failing_load(*a, **k):
        raise ConfigError("配置不完整 —— 环境变量未设置：GITHUB_TOKEN、SMTP_PASSWORD")

    monkeypatch.setattr(main, "load_config", failing_load)

    assert main.main([]) == main.EXIT_CONFIG_ERROR


def test_config_error_message_lists_every_missing_item(monkeypatch, caplog):
    def failing_load(*a, **k):
        raise ConfigError("配置不完整 —— 环境变量未设置：GITHUB_TOKEN、SMTP_PASSWORD")

    monkeypatch.setattr(main, "load_config", failing_load)

    with caplog.at_level(logging.ERROR, logger="main"):
        main.main([])

    message = " ".join(r.getMessage() for r in caplog.records)
    assert "GITHUB_TOKEN" in message
    assert "SMTP_PASSWORD" in message


def test_dry_run_flag_reaches_the_pipeline(monkeypatch, config):
    h = Harness(monkeypatch, config)

    code = main.main(["--dry-run"])

    assert code == main.EXIT_OK
    assert h.emails == []


def test_check_flag_takes_the_check_path(monkeypatch, config):
    Harness(monkeypatch, config)
    called = {"n": 0}

    def fake_check(cfg):
        called["n"] += 1
        return main.EXIT_OK

    monkeypatch.setattr(main, "_run_check", fake_check)

    assert main.main(["--check"]) == main.EXIT_OK
    assert called["n"] == 1


# --------------------------------------------------------------------------
# --check
# --------------------------------------------------------------------------


def test_check_passes_when_everything_is_reachable(monkeypatch, config):
    h = Harness(monkeypatch, config)
    monkeypatch.setattr(main.email_notifier, "check", lambda cfg: (True, "ok"))
    monkeypatch.setattr(main.lark_bot, "check", lambda cfg: (True, "ok"))

    assert main._run_check(config) == main.EXIT_OK


def test_check_fails_when_any_probe_fails(monkeypatch, config):
    h = Harness(monkeypatch, config)
    monkeypatch.setattr(main.email_notifier, "check", lambda cfg: (False, "认证失败"))
    monkeypatch.setattr(main.lark_bot, "check", lambda cfg: (True, "ok"))

    assert main._run_check(config) == main.EXIT_FAILED


def test_check_reports_each_item_separately(monkeypatch, config, caplog):
    Harness(monkeypatch, config)
    monkeypatch.setattr(main.email_notifier, "check", lambda cfg: (False, "认证失败"))
    monkeypatch.setattr(main.lark_bot, "check", lambda cfg: (True, "ok"))

    with caplog.at_level(logging.INFO, logger="main"):
        main._run_check(config)

    text = " ".join(r.getMessage() for r in caplog.records)
    for name in ("GitHub API", "飞书任务接口", "飞书消息接口", "SMTP", "飞书机器人"):
        assert name in text


def test_check_does_not_collect_or_push(monkeypatch, config):
    h = Harness(monkeypatch, config)
    monkeypatch.setattr(main.email_notifier, "check", lambda cfg: (True, "ok"))
    monkeypatch.setattr(main.lark_bot, "check", lambda cfg: (True, "ok"))

    main._run_check(config)

    assert h.emails == []
    assert h.larks == []
    assert h.saved == []
    # 探测会调用 collect，但必须是短窗口、不代表真实采集
    since = h.collected["github"][0][1]
    until = h.collected["github"][0][2]
    assert until - since <= timedelta(minutes=1)
