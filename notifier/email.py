"""邮件推送（design.md §4.3）。

职责：把日报的 HTML 正文（以及告警的纯文本）经 SMTP 送达收件人。

三条边界，越过任何一条都会破坏分层：

1. **不触发告警**。本模块只如实返回成败，是否告警由编排层（main.py）决定。
   若这里自己发飞书告警、而飞书推送模块又反过来发邮件告警，两者就循环依赖了
   —— 「送不到怎么办」是编排决策，不是通知层的事。
2. **不抛异常**。连接拒绝、认证失败、超时、收件人被拒……全部收敛为 False +
   ERROR 日志 —— 接口按契约返回 bool，调用方不必为推送失败包 try。
3. **不泄漏授权码**。密码只用于 login()，绝不写进任何日志或异常消息。

SMTP 主机/端口/发件人与授权码同属「运行环境」而非调用参数，故不进签名，
由实现从 shared.config.load() 读取（design.md §4.1 的同款做法）；``smtp=``
是仅关键字注入点，供测试替换，生产代码不传。
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from shared.config import SmtpConfig, load
from shared.logger import get_logger
from shared.models import DailyReport

logger = get_logger("notifier.email")

# 465（QQ 邮箱默认）是隐式 TLS 端口，直接走 SMTP_SSL；587 等其他端口先明文
# 连接再用 STARTTLS 升级。按端口自动选择，配置里不必再写一个模式开关 ——
# 模式与端口不匹配（如 465 却发 STARTTLS）只会得到握手失败。
_SSL_PORT = 465

# 没有超时的话，遇到网络黑洞时 connect/login 可能挂住整个日报流程。
_TIMEOUT_SECONDS = 30.0

# 「重试 2 次」按 design.md §6.1 的计数口径理解（该节明确「超时重试 3 次」
# 即初试 + 3 次重试）= 初试 + 2 次重试 = 最多 3 次尝试。邮件失败多为瞬时
# 网络故障，重试很可能成功；再失败则由编排层决定是否告警。
_MAX_ATTEMPTS = 3


def send(
    report: DailyReport,
    recipients: list[str],
    *,
    smtp: SmtpConfig | None = None,
) -> bool:
    """把日报以 HTML 邮件发送给收件人（design.md §4.3）。

    Args:
        report: 日报对象，正文取 ``report.html``。
        recipients: 收件人邮箱列表；为空时直接判失败，不发空邮件。
        smtp: 仅测试注入；生产代码不传，由 shared.config.load() 读取。

    Returns:
        True 表示 SMTP 服务器已接受邮件；任何失败返回 False，**不抛异常**。
    """
    subject = f"{report.team_name} 日报 · {report.date.isoformat()}"
    return _deliver(subject, report.html, recipients, subtype="html", smtp=smtp)


def send_text(
    subject: str,
    text: str,
    recipients: list[str],
    *,
    smtp: SmtpConfig | None = None,
) -> bool:
    """发送纯文本邮件，供告警通道（notifier/alert.py）复用（design.md §4.3）。

    告警场景下日报并不存在，故这里不依赖 DailyReport。与 send() 一样只返回
    成败，绝不自行触发告警（见模块 docstring 第 1 条）。
    """
    return _deliver(subject, text, recipients, subtype="plain", smtp=smtp)


def _deliver(
    subject: str,
    body: str,
    recipients: list[str],
    *,
    subtype: str,
    smtp: SmtpConfig | None,
) -> bool:
    """两个公开入口共用的投递流程：校验收件人 → 构造邮件 → 重试发送。"""
    if not recipients:
        # 空收件人几乎总是配置漏填。直接判失败而不是「发送成功」，否则
        # 编排层会把一封没发给任何人的邮件记成推送成功。
        logger.error("收件人列表为空，不发送邮件", extra={"fields": {"subject": subject}})
        return False

    try:
        config = smtp or load().smtp
        message = _build_message(config.sender, subject, body, recipients, subtype)
    except Exception as exc:
        # 配置缺失（ConfigError）或构造异常同样按「投递失败」处理，不向调用方抛
        logger.error(
            "邮件构造失败，无法发送",
            extra={"fields": {"subject": subject, "error": str(exc)}},
        )
        return False

    return _send_with_retry(config, message, subject, len(recipients))


def _send_with_retry(
    config: SmtpConfig,
    message: EmailMessage,
    subject: str,
    recipient_count: int,
) -> bool:
    """尝试投递，失败重试（_MAX_ATTEMPTS - 1 次）；返回最终成败。"""
    last_error: Exception | None = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            _transmit(config, message)
        except Exception as exc:
            last_error = exc
            if attempt < _MAX_ATTEMPTS:
                # 中间失败只记 INFO：最终失败才升级为 ERROR，否则一次网络
                # 抖动会留下多条 ERROR，掩盖真正需要人工介入的失败
                logger.info(
                    "邮件发送失败，准备重试",
                    extra={
                        "fields": {
                            "subject": subject,
                            "attempt": attempt,
                            "error": str(exc),
                        }
                    },
                )
            continue
        logger.info(
            "邮件已发送",
            extra={"fields": {"subject": subject, "recipients": recipient_count}},
        )
        return True

    logger.error(
        f"邮件发送失败，重试 {_MAX_ATTEMPTS - 1} 次后放弃",
        extra={
            "fields": {
                "subject": subject,
                "recipients": recipient_count,
                "error": str(last_error),
            }
        },
    )
    return False


def _build_message(
    sender: str,
    subject: str,
    body: str,
    recipients: list[str],
    subtype: str,
) -> EmailMessage:
    """构造邮件：主题/收件人走标准头字段，正文按 subtype 编码为 UTF-8。

    中文主题与正文由 EmailMessage 自动做 RFC 2047 编码 —— 手动拼头部
    容易在非 ASCII 上踩坑。
    """
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body, subtype=subtype, charset="utf-8")
    return message


def _transmit(config: SmtpConfig, message: EmailMessage) -> None:
    """单次投递；失败以异常形式抛给 _send_with_retry，由它统一记录并重试。"""
    if config.port == _SSL_PORT:
        client = smtplib.SMTP_SSL(config.host, config.port, timeout=_TIMEOUT_SECONDS)
    else:
        client = smtplib.SMTP(config.host, config.port, timeout=_TIMEOUT_SECONDS)

    try:
        if config.port != _SSL_PORT:
            client.starttls()
        client.login(config.sender, config.password)
        client.send_message(message)
    finally:
        _close_quietly(client)


def _close_quietly(client: smtplib.SMTP) -> None:
    """收尾连接，且不让收尾阶段的失败改写投递结果。

    走到 quit() 时邮件已被服务器接受；此时断链只说明会话没能优雅结束，
    不该把一次成功的投递判成失败。
    """
    try:
        client.quit()
    except Exception:
        client.close()
