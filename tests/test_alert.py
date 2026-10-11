"""告警通道测试（design.md §4.6，tasks.md Task 8.5）。

两条必须钉死的约束：
1. 告警**绝不抛异常** —— 它是失败路径上的最后手段，自己再炸就等于没有告警
2. 告警自身失败**只记日志、不再递归告警** —— 否则一次网络抖动会变成告警风暴
"""

from __future__ import annotations

import logging

import pytest

from notifier import alert
from notifier import email as email_notifier
from notifier import lark_bot
from shared.config import (
    Config,
    FiltersConfig,
    GithubConfig,
    LarkConfig,
    ScheduleConfig,
    SmtpConfig,
)

SUBJECT = "智能日报生成失败"
DETAIL = "所有数据源均不可用：github、lark_task、lark_msg"


def make_config(recipients=("leader@company.com",), chat_id="oc_test"):
    return Config(
        team_name="研发一组",
        members=[],
        github=GithubConfig(repos=["owner/repo"], token="gh_token"),
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
        filters=FiltersConfig(),
        schedule=ScheduleConfig(
            timezone="Asia/Shanghai", db_path="d.db", log_dir="logs"
        ),
    )


class Recorder:
    """替换 email / lark_bot 的 send_text，记录调用并可控地失败。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.result = True
        self.raises: Exception | None = None

    def __call__(self, *args):
        self.calls.append(args)
        if self.raises is not None:
            raise self.raises
        return self.result


@pytest.fixture
def email_recorder(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(email_notifier, "send_text", recorder)
    return recorder


@pytest.fixture
def lark_recorder(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(lark_bot, "send_text", recorder)
    return recorder


# --------------------------------------------------------------------------
# 正常投递
# --------------------------------------------------------------------------


def test_email_channel_calls_email_send_text(email_recorder, lark_recorder):
    alert.report_alert(SUBJECT, DETAIL, ["email"], config=make_config())

    assert email_recorder.calls == [(SUBJECT, DETAIL, ["leader@company.com"])]
    assert lark_recorder.calls == []


def test_lark_channel_calls_lark_bot_send_text(email_recorder, lark_recorder):
    alert.report_alert(SUBJECT, DETAIL, ["lark"], config=make_config())

    assert email_recorder.calls == []
    assert len(lark_recorder.calls) == 1

    text, chat_id = lark_recorder.calls[0]
    assert chat_id == "oc_test"
    assert SUBJECT in text
    assert DETAIL in text


def test_default_channels_hits_both(email_recorder, lark_recorder):
    alert.report_alert(SUBJECT, DETAIL, config=make_config())

    assert len(email_recorder.calls) == 1
    assert len(lark_recorder.calls) == 1


def test_alert_does_not_need_a_daily_report(email_recorder, lark_recorder):
    """告警不依赖 DailyReport —— 日报不存在时（全部数据源失败）也要能发。

    这正是它区别于 email.send / lark_bot.send 的地方：那两者的入参是报告对象。
    """
    alert.report_alert("全挂了", "没有日报可发", ["email"], config=make_config())

    args = email_recorder.calls[0]
    assert len(args) == 3  # subject, text, recipients —— 没有 report


def test_multiple_recipients_passed_through(email_recorder, lark_recorder):
    config = make_config(recipients=("a@x.com", "b@x.com"))

    alert.report_alert(SUBJECT, DETAIL, ["email"], config=config)

    assert email_recorder.calls[0][2] == ["a@x.com", "b@x.com"]


def test_success_is_logged(email_recorder, lark_recorder, caplog):
    with caplog.at_level(logging.INFO, logger="notifier.alert"):
        alert.report_alert(SUBJECT, DETAIL, ["email"], config=make_config())

    assert any("告警已投递" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# 不递归告警（design.md §4.6）
# --------------------------------------------------------------------------


def test_single_channel_failure_does_not_retrigger_alert(
    email_recorder, lark_recorder, caplog
):
    """告警投递失败只记日志，绝不回头再发一次。"""
    email_recorder.result = False

    with caplog.at_level(logging.ERROR, logger="notifier.alert"):
        alert.report_alert(SUBJECT, DETAIL, ["email"], config=make_config())

    # 恰好一次调用 —— 若递归，这里会变成 2 次、3 次……直到栈溢出
    assert len(email_recorder.calls) == 1
    assert len(lark_recorder.calls) == 0
    assert any("不再递归告警" in r.getMessage() for r in caplog.records)


def test_exception_in_channel_is_swallowed(email_recorder, lark_recorder, caplog):
    email_recorder.raises = ConnectionError("smtp down")

    with caplog.at_level(logging.ERROR, logger="notifier.alert"):
        alert.report_alert(SUBJECT, DETAIL, ["email"], config=make_config())

    assert len(email_recorder.calls) == 1
    assert any("不再递归告警" in r.getMessage() for r in caplog.records)


def test_one_channel_failure_does_not_block_the_other(
    email_recorder, lark_recorder
):
    """推送渠道互为备份（§6.1）—— 邮件挂了不该连累飞书。"""
    email_recorder.result = False

    alert.report_alert(SUBJECT, DETAIL, config=make_config())

    assert len(email_recorder.calls) == 1
    assert len(lark_recorder.calls) == 1


def test_exception_in_first_channel_does_not_block_second(
    email_recorder, lark_recorder
):
    email_recorder.raises = RuntimeError("boom")

    alert.report_alert(SUBJECT, DETAIL, config=make_config())

    assert len(lark_recorder.calls) == 1


# --------------------------------------------------------------------------
# 绝不抛异常
# --------------------------------------------------------------------------


def test_config_load_failure_is_swallowed(monkeypatch, caplog):
    def failing_load(*args, **kwargs):
        raise RuntimeError("config.yaml 不存在")

    monkeypatch.setattr(alert, "load", failing_load)

    with caplog.at_level(logging.ERROR, logger="notifier.alert"):
        alert.report_alert(SUBJECT, DETAIL)  # 不应抛

    assert any("配置读取失败" in r.getMessage() for r in caplog.records)


def test_never_raises_whatever_channels_look_like(email_recorder, lark_recorder):
    """任何 channels 取值都不该让它抛异常。"""
    config = make_config()

    for channels in ([], ["email"], ["lark"], ["nonsense"], ["email", "nonsense"], None):
        alert.report_alert(SUBJECT, DETAIL, channels, config=config)


def test_unknown_channel_is_logged_not_silently_ignored(
    email_recorder, lark_recorder, caplog
):
    with caplog.at_level(logging.ERROR, logger="notifier.alert"):
        alert.report_alert(SUBJECT, DETAIL, ["sms"], config=make_config())

    assert email_recorder.calls == []
    assert lark_recorder.calls == []
    assert any("未知通道" in r.getMessage() for r in caplog.records)


def test_empty_channels_list_is_logged(email_recorder, lark_recorder, caplog):
    with caplog.at_level(logging.ERROR, logger="notifier.alert"):
        alert.report_alert(SUBJECT, DETAIL, [], config=make_config())

    assert email_recorder.calls == []
    assert any("channels 为空列表" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# 配置缺失的通道
# --------------------------------------------------------------------------


def test_missing_recipients_skips_email_channel(email_recorder, lark_recorder, caplog):
    with caplog.at_level(logging.ERROR, logger="notifier.alert"):
        alert.report_alert(SUBJECT, DETAIL, ["email"], config=make_config(recipients=()))

    assert email_recorder.calls == []
    assert any("未配置收件人" in r.getMessage() for r in caplog.records)


def test_missing_chat_id_skips_lark_channel(email_recorder, lark_recorder, caplog):
    with caplog.at_level(logging.ERROR, logger="notifier.alert"):
        alert.report_alert(SUBJECT, DETAIL, ["lark"], config=make_config(chat_id=""))

    assert lark_recorder.calls == []
    assert any("未配置 chat_id" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# 结构约束
# --------------------------------------------------------------------------


def test_signature_matches_design_section_4_6():
    import inspect

    signature = inspect.signature(alert.report_alert)
    params = list(signature.parameters)

    assert params[0] == "subject"
    assert params[1] == "detail"
    assert params[2] == "channels"
    assert signature.parameters["channels"].default is None
    assert signature.return_annotation in (None, "None")


def test_alert_reuses_push_layer_send_text():
    """告警必须复用推送层的底层发送能力，而非另写一套 SMTP / HTTP。

    否则「告警通道」会有第二份传输实现，与推送层的行为逐渐分叉。
    """
    import ast
    from pathlib import Path

    source = Path(alert.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "notifier":
            imported.update(alias.name for alias in node.names)

    assert imported == {"email", "lark_bot"}

    # 不应出现自建传输的痕迹
    assert "smtplib" not in source
    assert "httpx" not in source
