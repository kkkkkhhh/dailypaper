"""异常告警通道（design.md §4.6）。

为什么不复用 `email.send` / `lark_bot.send`：那两个接口的入参都是 `DailyReport`，
而告警场景（§6.1 的「所有数据源都不可用」「推送失败」）发生时**日报根本不存在**，
或者在逻辑上不允许作为告警载体（推送失败时不能再用同一个通道给自己发）。
所以告警需要一条不依赖日报对象的独立入口，复用推送层底层的 `send_text` 投递。

**调用方是编排层 `main.py`，不是推送模块自己**（§4.3）。若 email 自己触发飞书
告警、而 lark_bot 又触发邮件告警，两者就循环依赖了 —— 分层上也不对：
通知层只管「送达」，「送不到怎么办」是编排决策。

两条硬约束：
1. **绝不抛异常**。告警是失败路径上的最后手段，它自己再炸掉就等于没有告警。
2. **告警自身失败只记日志，不再递归告警**。否则一次网络抖动会变成告警风暴。
"""

from __future__ import annotations

from typing import Any

from shared.config import Config, load
from shared.logger import get_logger

# 这两个模块用属性访问而非 from-import 具体函数，便于测试替换
from notifier import email as email_notifier
from notifier import lark_bot

logger = get_logger("notifier.alert")

EMAIL = "email"
LARK = "lark"

#: 传 channels=None 时的投递范围。
#: 注意「默认向未失败的通道投递」需要调用方来指定 —— 告警本身无从得知
#: 哪个通道刚失败了（正是它被调用的原因）。故调用方应显式传 channels，
#: 例如邮件推送失败时传 ["lark"]。
ALL_CHANNELS = (EMAIL, LARK)


def report_alert(
    subject: str,
    detail: str,
    channels: list[str] | None = None,
    *,
    config: Config | None = None,
) -> None:
    """投递一条告警。任何情况下都不抛异常。

    Args:
        subject: 告警标题。
        detail: 告警详情（纯文本）。
        channels: 投递通道，取值 ``"email"`` / ``"lark"``。
            None 表示投递到全部通道。
        config: 配置对象，仅测试注入用；生产从 ``config.yaml`` 读取。

    Returns:
        None —— 告警的成败通过日志体现，不向调用方返回。
        这样调用方（编排层）不必在失败路径上再处理一次失败。
    """
    # 必须区分 None 与 []：None 表示「用默认范围」，[] 表示「什么都不发」。
    # 若写成 `if channels else`，空列表会被当成缺省值，变成一个静默地
    # 向所有通道群发告警的 bug —— 与本意正好相反。
    selected = list(ALL_CHANNELS) if channels is None else list(channels)

    try:
        cfg = config or load()
    except Exception as exc:  # noqa: BLE001 —— 告警路径不允许任何异常逃逸
        logger.error(
            "告警无法发出：配置读取失败",
            extra={"fields": {"error": str(exc), "subject": subject}},
        )
        return

    if not selected:
        logger.error("告警未投递：channels 为空列表", extra={"fields": {"subject": subject}})
        return

    for channel in selected:
        _deliver(channel, subject, detail, cfg)


def _deliver(channel: str, subject: str, detail: str, cfg: Config) -> None:
    """向单个通道投递，失败只记日志。"""
    try:
        if channel == EMAIL:
            recipients = list(cfg.smtp.recipients)
            if not recipients:
                logger.error(
                    "告警未投递：邮件通道未配置收件人",
                    extra={"fields": {"subject": subject}},
                )
                return
            delivered = email_notifier.send_text(subject, detail, recipients)

        elif channel == LARK:
            chat_id = cfg.lark.chat_id
            if not chat_id:
                logger.error(
                    "告警未投递：飞书通道未配置 chat_id",
                    extra={"fields": {"subject": subject}},
                )
                return
            delivered = lark_bot.send_text(_compose_lark_text(subject, detail), chat_id)

        else:
            logger.error(
                "告警未投递：未知通道",
                extra={"fields": {"subject": subject, "channel": channel}},
            )
            return

    except Exception as exc:  # noqa: BLE001 —— 告警路径不允许任何异常逃逸
        logger.error(
            "告警投递抛出异常（不再递归告警）",
            extra={
                "fields": {
                    "subject": subject,
                    "channel": channel,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            },
        )
        return

    fields: dict[str, Any] = {"subject": subject, "channel": channel}
    if delivered:
        logger.info("告警已投递", extra={"fields": fields})
    else:
        # 关键：这里只记日志，不回头再发一次告警 —— 否则告警风暴
        logger.error("告警投递失败（不再递归告警）", extra={"fields": fields})


def _compose_lark_text(subject: str, detail: str) -> str:
    """飞书没有邮件那样的主题行，把标题并进正文首行。"""
    return f"【告警】{subject}\n\n{detail}" if detail else f"【告警】{subject}"
