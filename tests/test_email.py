"""邮件推送测试（design.md §4.3、§6.1，tasks.md Task 7）。

全程用 FakeSMTP 替换 smtplib.SMTP / smtplib.SMTP_SSL —— **不联网、不发真邮件**。

断言落在 EmailMessage 对象与登录凭证上，而不是只看返回值：只看 True/False
的话，把 HTML 发成纯文本、漏掉一个收件人、用错端口、密码传错，全都测不出来。
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import smtplib
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timezone
from email.message import EmailMessage

import pytest

import notifier.email as email_module
from notifier.email import send, send_text
from shared.config import SmtpConfig
from shared.models import DailyReport
from tests.conftest import VALID_CONFIG

TEAM_NAME = "研发一组"
REPORT_DATE = date(2026, 10, 11)
GENERATED_AT = datetime(2026, 10, 11, 18, 0, tzinfo=timezone.utc)

PASSWORD = "smtp_auth_code_secret"
RECIPIENTS = ["leader@company.com", "ops@company.com"]

HTML_BODY = (
    "<html><body><h1>研发一组 日报</h1>"
    "<p>张三：完成 3 个提交（+120 行 / -30 行）</p></body></html>"
)

SMTP_CFG = SmtpConfig(
    host="smtp.qq.com",
    port=465,
    sender="sender@qq.com",
    recipients=RECIPIENTS,
    password=PASSWORD,
)


def make_report() -> DailyReport:
    return DailyReport(
        date=REPORT_DATE,
        team_name=TEAM_NAME,
        members=[],
        failed_sources=[],
        failed_details={},
        generated_at=GENERATED_AT,
        markdown=f"# {TEAM_NAME} 日报",
        html=HTML_BODY,
    )


@pytest.fixture
def report() -> DailyReport:
    return make_report()


# --------------------------------------------------------------------------
# SMTP 替身
# --------------------------------------------------------------------------


class FakeSMTP:
    """smtplib 客户端的替身：记录连接参数、登录凭证与投递的邮件对象。"""

    def __init__(
        self,
        harness: SMTPHarness,
        ssl: bool,
        host: str,
        port: int,
        timeout: float | None,
    ) -> None:
        self.harness = harness
        self.ssl = ssl
        self.host = host
        self.port = port
        self.timeout = timeout
        self.login_calls: list[tuple[str, str]] = []
        self.messages: list[EmailMessage] = []
        self.starttls_calls = 0
        self.quit_calls = 0

    def login(self, sender: str, password: str) -> None:
        self.login_calls.append((sender, password))
        if self.harness.login_error is not None:
            raise self.harness.login_error
        if self.harness.login_failures > 0:
            self.harness.login_failures -= 1
            raise smtplib.SMTPServerDisconnected("瞬时故障")

    def starttls(self) -> None:
        self.starttls_calls += 1

    def send_message(self, message: EmailMessage) -> None:
        self.messages.append(message)
        if self.harness.send_error is not None:
            raise self.harness.send_error

    def quit(self) -> None:
        self.quit_calls += 1

    def close(self) -> None:
        pass


class SMTPHarness:
    """把 smtplib 的两个客户端类换成 FakeSMTP，并支持注入各类故障。

    - ``connect_error``：构造连接时抛（连接拒绝 / 超时）
    - ``login_error``：每次 login 都抛（认证失败）
    - ``login_failures``：前 N 次 login 抛（模拟瞬时故障，用于验证重试能成功）
    - ``send_error``：send_message 时抛（收件人被拒 / 连接中断）
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.instances: list[FakeSMTP] = []
        self.connect_attempts = 0
        self.connect_error: Exception | None = None
        self.login_error: Exception | None = None
        self.login_failures = 0
        self.send_error: Exception | None = None
        monkeypatch.setattr(smtplib, "SMTP", self._factory(ssl=False))
        monkeypatch.setattr(smtplib, "SMTP_SSL", self._factory(ssl=True))

    def _factory(self, ssl: bool) -> Callable[..., FakeSMTP]:
        def build(host: str, port: int, timeout: float | None = None) -> FakeSMTP:
            self.connect_attempts += 1
            if self.connect_error is not None:
                raise self.connect_error
            fake = FakeSMTP(self, ssl=ssl, host=host, port=port, timeout=timeout)
            self.instances.append(fake)
            return fake

        return build

    @property
    def ssl_instances(self) -> list[FakeSMTP]:
        return [instance for instance in self.instances if instance.ssl]

    @property
    def plain_instances(self) -> list[FakeSMTP]:
        return [instance for instance in self.instances if not instance.ssl]

    def sent_message(self) -> EmailMessage:
        """最近一次被投递的邮件对象 —— 断言主题/正文/收件人的主入口。"""
        messages = [m for instance in self.instances for m in instance.messages]
        assert messages, "没有任何邮件被发送"
        return messages[-1]


@pytest.fixture
def smtp_harness(monkeypatch) -> SMTPHarness:
    return SMTPHarness(monkeypatch)


def log_blob(caplog) -> str:
    """把捕获到的日志（消息 + 结构化字段）拼成可断言的一整块文本。"""
    parts: list[str] = []
    for record in caplog.records:
        parts.append(record.getMessage())
        fields = getattr(record, "fields", None)
        if fields is not None:
            parts.append(json.dumps(fields, ensure_ascii=False, default=str))
    return "\n".join(parts)


# --------------------------------------------------------------------------
# 接口契约（design.md §4.3）
# --------------------------------------------------------------------------


def test_send_signature_matches_design_contract():
    """两个入参的名称、顺序与类型逐字一致，注入点不破坏两参调用。"""
    sig = inspect.signature(send)

    assert list(sig.parameters) == ["report", "recipients", "smtp"]
    assert sig.parameters["report"].annotation == "DailyReport"
    assert sig.parameters["recipients"].annotation == "list[str]"
    assert sig.parameters["smtp"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["smtp"].default is None
    assert sig.return_annotation == "bool"


def test_send_text_signature_matches_design_contract():
    sig = inspect.signature(send_text)

    assert list(sig.parameters) == ["subject", "text", "recipients", "smtp"]
    assert sig.parameters["subject"].annotation == "str"
    assert sig.parameters["text"].annotation == "str"
    assert sig.parameters["recipients"].annotation == "list[str]"
    assert sig.parameters["smtp"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.return_annotation == "bool"


def test_module_does_not_reference_other_channels_or_new_dependencies():
    """design.md §4.3：推送层互不触发告警（避免循环依赖），且不引入新依赖。

    用 AST 检查 import 而不是搜索源码文本 —— docstring 解释分层原因时
    会提到「告警」「飞书」，文本搜索会被这些合理的注释误伤。
    """
    tree = ast.parse(inspect.getsource(email_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    assert not any(name.startswith("notifier") for name in imported)
    assert all(
        name.split(".")[0] in {"__future__", "smtplib", "email", "shared"}
        for name in imported
    )


# --------------------------------------------------------------------------
# 日报邮件：主题 / 正文 / 收件人
# --------------------------------------------------------------------------


def test_subject_contains_team_name_and_date(smtp_harness, report):
    assert send(report, RECIPIENTS, smtp=SMTP_CFG) is True

    subject = str(smtp_harness.sent_message()["Subject"])

    assert TEAM_NAME in subject
    assert REPORT_DATE.isoformat() in subject
    assert subject == f"{TEAM_NAME} 日报 · {REPORT_DATE.isoformat()}"


def test_body_is_report_html_with_utf8_text_html(smtp_harness, report):
    assert send(report, RECIPIENTS, smtp=SMTP_CFG) is True

    message = smtp_harness.sent_message()

    assert message.get_content_type() == "text/html"
    assert message.get_content_charset() == "utf-8"
    # get_content() 会为不以换行结尾的正文补一个换行（邮件正文的传输要求），
    # 故比对时忽略行尾差异；中文能原样往返，说明 UTF-8 编码正确
    assert message.get_content().rstrip("\n") == HTML_BODY.rstrip("\n")
    assert "研发一组" in message.get_content()


def test_markdown_is_not_used_as_body(smtp_harness, report):
    """正文必须是 HTML 而非 Markdown —— 两者都在 DailyReport 上，容易取错。"""
    assert send(report, RECIPIENTS, smtp=SMTP_CFG) is True

    assert report.markdown not in smtp_harness.sent_message().get_content()


def test_all_recipients_and_sender_are_in_headers(smtp_harness, report):
    assert send(report, RECIPIENTS, smtp=SMTP_CFG) is True

    message = smtp_harness.sent_message()
    to_header = str(message["To"])

    for recipient in RECIPIENTS:
        assert recipient in to_header
    assert str(message["From"]) == SMTP_CFG.sender


def test_connects_with_configured_host_port_and_timeout(smtp_harness, report):
    assert send(report, RECIPIENTS, smtp=SMTP_CFG) is True

    client = smtp_harness.instances[0]

    assert (client.host, client.port) == (SMTP_CFG.host, SMTP_CFG.port)
    # 没有超时的 SMTP 连接遇到网络黑洞会挂住整个日报流程
    assert client.timeout is not None and client.timeout > 0


# --------------------------------------------------------------------------
# 端口决定加密方式：465 → SSL，587 → STARTTLS
# --------------------------------------------------------------------------


def test_port_465_uses_ssl_and_skips_starttls(smtp_harness, report):
    assert send(report, RECIPIENTS, smtp=SMTP_CFG) is True  # SMTP_CFG.port == 465

    assert len(smtp_harness.ssl_instances) == 1
    assert smtp_harness.plain_instances == []
    assert smtp_harness.ssl_instances[0].starttls_calls == 0


def test_port_587_uses_plain_connection_then_starttls(smtp_harness, report):
    config = replace(SMTP_CFG, port=587)

    assert send(report, RECIPIENTS, smtp=config) is True

    assert smtp_harness.ssl_instances == []
    assert smtp_harness.plain_instances[0].starttls_calls == 1
    assert smtp_harness.plain_instances[0].login_calls == [
        (SMTP_CFG.sender, SMTP_CFG.password)
    ]


# --------------------------------------------------------------------------
# send_text：告警复用的纯文本入口
# --------------------------------------------------------------------------


def test_send_text_sends_plain_text_with_given_subject(smtp_harness):
    assert (
        send_text("日报推送失败", "飞书通道连续失败，请人工检查", RECIPIENTS, smtp=SMTP_CFG)
        is True
    )

    message = smtp_harness.sent_message()

    assert message.get_content_type() == "text/plain"
    assert message.get_content_charset() == "utf-8"
    assert str(message["Subject"]) == "日报推送失败"
    assert message.get_content().rstrip("\n") == "飞书通道连续失败，请人工检查"


# --------------------------------------------------------------------------
# 空收件人
# --------------------------------------------------------------------------


def test_empty_recipients_fail_without_touching_smtp(smtp_harness, report, caplog):
    caplog.set_level(logging.INFO, logger="notifier.email")

    assert send(report, [], smtp=SMTP_CFG) is False

    assert smtp_harness.connect_attempts == 0  # 发空邮件没有意义，连尝试都不该有
    assert any(record.levelno == logging.ERROR for record in caplog.records)


def test_send_text_empty_recipients_fail_without_touching_smtp(smtp_harness):
    assert send_text("告警", "正文", [], smtp=SMTP_CFG) is False
    assert smtp_harness.connect_attempts == 0


# --------------------------------------------------------------------------
# 重试与最终失败
# --------------------------------------------------------------------------


def test_retries_twice_then_returns_false(smtp_harness, report, caplog):
    """design.md §6.1：重试 2 次（初试 + 2 次重试 = 3 次尝试）后仍失败则返回 False。"""
    caplog.set_level(logging.INFO, logger="notifier.email")
    smtp_harness.login_error = smtplib.SMTPServerDisconnected("连接被重置")

    assert send(report, RECIPIENTS, smtp=SMTP_CFG) is False

    assert smtp_harness.connect_attempts == 3
    assert len(smtp_harness.instances) == 3  # 每次重试都是新连接：失败后的连接不可复用

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1  # 只有最终失败升级为 ERROR，中间重试是 INFO
    assert "重试" in errors[0].getMessage()


def test_transient_failure_succeeds_on_retry(smtp_harness, report):
    """重试的意义：第一次瞬时失败、第二次成功，整体就不算失败。"""
    smtp_harness.login_failures = 1

    assert send(report, RECIPIENTS, smtp=SMTP_CFG) is True

    assert smtp_harness.connect_attempts == 2  # 1 次失败 + 1 次成功，不再多试


@pytest.mark.parametrize(
    ("failure_field", "failure"),
    [
        pytest.param("connect_error", ConnectionRefusedError("连接被拒绝"), id="连接拒绝"),
        pytest.param("connect_error", TimeoutError("连接超时"), id="连接超时"),
        pytest.param(
            "login_error",
            smtplib.SMTPAuthenticationError(535, b"authentication failed"),
            id="认证失败",
        ),
        pytest.param(
            "send_error",
            smtplib.SMTPRecipientsRefused({"x@company.com": (550, b"no such user")}),
            id="收件人被拒",
        ),
        pytest.param(
            "send_error",
            smtplib.SMTPServerDisconnected("连接中断"),
            id="发送中断",
        ),
    ],
)
def test_failures_return_false_and_never_raise(smtp_harness, report, failure_field, failure):
    """任何 smtplib/网络异常都收敛为 False —— 推送层不向调用方抛异常。"""
    setattr(smtp_harness, failure_field, failure)

    assert send(report, RECIPIENTS, smtp=SMTP_CFG) is False

    assert smtp_harness.connect_attempts == 3  # 每个失败场景都完整重试 2 次


# --------------------------------------------------------------------------
# 授权码：来源与保密
# --------------------------------------------------------------------------


def test_login_uses_config_password_and_never_logs_it(smtp_harness, report, caplog):
    caplog.set_level(logging.INFO, logger="notifier.email")

    assert send(report, RECIPIENTS, smtp=SMTP_CFG) is True
    assert smtp_harness.instances[0].login_calls == [(SMTP_CFG.sender, PASSWORD)]

    # 失败路径会打更多日志（含异常文本）—— 一并断言不泄漏授权码
    smtp_harness.login_error = smtplib.SMTPAuthenticationError(535, b"auth failed")
    assert send_text("告警", "正文", RECIPIENTS, smtp=SMTP_CFG) is False

    assert PASSWORD not in log_blob(caplog)


def test_missing_config_fails_without_raising(isolated_env, smtp_harness, report):
    """配置缺失（ConfigError）也走「返回 False」而不是抛给调用方。"""
    assert send(report, RECIPIENTS) is False

    assert smtp_harness.connect_attempts == 0  # 都不知道往哪连，自然没有连接


def test_password_comes_from_env_through_shared_config(env_set, tmp_path, smtp_harness, report):
    """§6.2：授权码源头是环境变量 SMTP_PASSWORD，经 config.smtp.password 传给 login。

    不注入 smtp=，走真实的 shared.config.load() 路径 —— 只注入环境变量，
    验证凭证确实是从环境变量流进来的。
    """
    (tmp_path / "config.yaml").write_text(VALID_CONFIG, encoding="utf-8")

    assert send(report, ["leader@company.com"]) is True

    client = smtp_harness.instances[0]
    assert client.login_calls == [("sender@qq.com", "test_smtp_password")]
    assert (client.host, client.port) == ("smtp.qq.com", 465)
    assert len(smtp_harness.ssl_instances) == 1
