"""飞书推送（design.md §4.3，tasks.md Task 8）。

接口契约（design.md §4.3）::

    send(report: DailyReport, chat_id: str) -> bool
    send_text(text: str, chat_id: str) -> bool

- ``send``：把 ``report.markdown`` 以 Markdown 格式推给 ``chat_id`` 指定的群
- ``send_text``：推送纯文本；告警通道（notifier/alert.py，Task 8.5）复用它

**通道：应用机器人，不是自定义机器人 webhook**（design.md §4.3「关于 lark_bot
的通道选择」）。契约的入参是 ``chat_id``，而 webhook 地址本身即目标、没有
chat_id 参数；只有应用机器人通道（``POST /open-apis/im/v1/messages``，
``receive_id_type=chat_id``）能按 chat_id 定位接收方。

**本模块不触发任何告警**：推送失败只如实返回 False + 日志，「送不到怎么办」是
编排层（main.py）的决策。若本模块自己发邮件告警，而 email 模块又发飞书告警，
两者就会互相引用，形成循环依赖（design.md §4.3）。

**令牌管理完全复用** `collector/lark_client.py`：本模块不换取、不缓存、不刷新
任何令牌，模块内不存在通往鉴权接口的代码路径（见 `_post`，对应断言见
tests/test_lark_bot.py）。

--------------------------------------------------------------------------
飞书接口形状（2026-10-11 用 curl 取官方文档 .md 原文逐条核对，非凭记忆）
--------------------------------------------------------------------------

来源 A（发送消息接口）：
https://open.feishu.cn/document/server-docs/im-v1/message/create
来源 B（发送消息内容结构）：
https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/im-v1/message/create_json
来源 C（卡片富文本组件，用于排除 interactive 方案）：
https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-components/content-components/rich-text

- 路径 ``POST https://open.feishu.cn/open-apis/im/v1/messages``；查询参数
  ``receive_id_type``（必填）取值 ``open_id`` / ``union_id`` / ``user_id`` /
  ``email`` / ``chat_id`` —— 本模块固定 ``chat_id``。
- 请求体三个必填字段：``receive_id``（与 receive_id_type 同类型的 ID）、
  ``msg_type``、``content``。``content`` 是 **JSON 结构序列化后的字符串**，
  不是嵌套对象 —— 本模块用 ``json.dumps`` 生成后交给 httpx 再序列化一次。
- ``msg_type`` 全部可选值：text / post / image / file / audio / media /
  sticker / interactive / share_chat / share_user / system。**没有 ``markdown``**，
  所以 Markdown 必须借道 ``post`` 富文本或 ``interactive`` 卡片。
- 成功响应 ``{"code": 0, "msg": "success", "data": {"message_id": ...}}``。
- 失败既可能是 HTTP 200 + ``code != 0``，也可能是 **HTTP 400 + 响应体里仍带
  code/msg**。文档错误码表点名：230002 机器人不在群、230027 权限不足、
  230006 未启用机器人能力、230034 receive_id 无效、230025 消息体超长
  （文本 150KB；卡片与富文本 30KB）。

**Markdown 走哪条路（本模块的决定）**：文档两处明确「如需发送 Markdown 内容，
推荐使用富文本 ``post`` 消息中的 ``md`` 标签」，支持 CommonMark 0.31 + GFM
（ATX 标题、引用、有序/无序列表、表格、围栏代码块…）。``interactive`` 卡片被
排除：卡片 JSON 1.0 的富文本组件**不支持标题、引用、表格**（来源 C 的「注意事项」），
而日报正文恰以 ``#`` / ``##`` / ``###`` 与 ``>`` 组织；卡片 JSON 2.0 结构更重，
对本场景没有额外收益。故：

- ``send`` → ``msg_type="post"``，
  ``content={"zh_cn": {"content": [[{"tag": "md", "text": markdown}]]}}``
- ``send_text`` → ``msg_type="text"``，``content={"text": text}``

--------------------------------------------------------------------------
重试口径（design.md §6.1）
--------------------------------------------------------------------------
「飞书推送失败 → 重试 2 次」按「初试 + 2 次重试 = 最多 3 次请求」实现，与 §6.1
「重试计数口径」注一致。重试只针对**瞬时故障**（网络异常、HTTP 5xx）；业务错误码
（``code != 0``，含 HTTP 400 里夹带的）**不重试** —— 机器人不在群、权限不足，
重试解决不了。**唯一例外**是 230020（触发频率限制），它虽是业务码但等待后可恢复，
按瞬时故障处理（见 ``_RETRYABLE_BUSINESS_CODES``）。
令牌失效的刷新与重试由 LarkClient 内部完成（§6.1 独立的那条），
与本模块的重试计数互不混用。
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from collector.lark_client import LarkClient
from shared.config import load as load_config
from shared.logger import get_logger
from shared.models import DailyReport

logger = get_logger("notifier.lark_bot")

# --- 飞书接口常量（形状与文档来源见模块 docstring） --------------------------
_MESSAGES_PATH = "/open-apis/im/v1/messages"
_RECEIVE_ID_TYPE = "chat_id"

#: 触发频率限制。走的是业务错误码，但语义上**可恢复** —— 等待后重试大概率成功，
#: 与「机器人不在群」「权限不足」这类重试也没用的错误性质不同（design.md §6.1 的例外条目）。
_RETRYABLE_BUSINESS_CODES = frozenset({230020})

#: 「重试 2 次」= 初试 + 2 次重试 = 最多 3 次请求（design.md §6.1 的重试计数口径）
_MAX_RETRIES = 2
#: 与采集层的重试节奏保持一致（§6.1）；测试将其置 0，不真等待
_RETRY_INTERVAL_SECONDS = 5.0

#: 模块级客户端（测试注入点：monkeypatch `notifier.lark_bot._client`）。
#: design.md §4.3 的签名不接收 client 参数，故由模块惰性构造并复用 ——
#: 复用同一实例，令牌缓存才能跨 send / send_text 生效，不必每次推送重复换取。
_client: LarkClient | None = None


# --------------------------------------------------------------------------
# 客户端与请求
# --------------------------------------------------------------------------


def _get_client() -> LarkClient:
    """惰性构造并复用 `LarkClient`（凭证经 shared.config 从环境变量读取）。"""
    global _client
    if _client is None:
        config = load_config()
        _client = LarkClient(config.lark.app_id, config.lark.app_secret)
    return _client


def _post(client: LarkClient, body: dict[str, Any]) -> dict[str, Any]:
    """经 `LarkClient` 的带鉴权通道 POST 一条消息。

    走 `LarkClient.post()` —— 它是内部唯一的「附加访问令牌 + 遇令牌失效自动
    刷新后重试 1 次」通道。若在推送层另写一份 httpx 调用与 Authorization 头，
    令牌相关逻辑就会出现第二处实现，正是 collector/lark_client.py 的模块
    docstring 反对的（一份令牌逻辑，两个飞书采集模块与推送模块共用）。
    """
    return client.post(
        _MESSAGES_PATH,
        params={"receive_id_type": _RECEIVE_ID_TYPE},
        json_body=body,
    )


# --------------------------------------------------------------------------
# 消息体构造（形状见模块 docstring 的「Markdown 走哪条路」）
# --------------------------------------------------------------------------


def _markdown_content(markdown: str) -> str:
    """日报 Markdown → ``post`` 富文本的 content 字符串（``md`` 标签独占段落）。"""
    structured = {"zh_cn": {"content": [[{"tag": "md", "text": markdown}]]}}
    return json.dumps(structured, ensure_ascii=False)


def _text_content(text: str) -> str:
    """纯文本 → ``text`` 消息的 content 字符串（告警走这条）。"""
    return json.dumps({"text": text}, ensure_ascii=False)


# --------------------------------------------------------------------------
# 失败分类
# --------------------------------------------------------------------------


def _business_rejection(response: httpx.Response) -> tuple[int, str] | None:
    """HTTP 错误响应里若夹带飞书业务错误码，取出来；取不到返回 None。

    飞书的业务失败既可能走 HTTP 200（``code != 0``），也可能走 HTTP 400
    （响应体仍是 ``{"code": 230027, "msg": ...}``）。不把后者解析出来，
    「机器人不在群」就会被当成瞬时故障白白重试两次（§6.1 要求不重试）。
    5xx 除外：那是服务端故障，即使响应体带 code 也应按瞬时错误处理。
    """
    if response.status_code >= 500:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if isinstance(code, int) and code != 0:
        return code, str(payload.get("msg") or "")
    return None


def _code_and_msg(payload: Any) -> tuple[Any, str]:
    """HTTP 200 响应体 → (code, msg)；成功时为 (0, "")。"""
    if not isinstance(payload, dict):
        return None, ""
    return payload.get("code"), str(payload.get("msg") or "")


# --------------------------------------------------------------------------
# 日志（design.md §6.3：推送结果必须进日志）
# --------------------------------------------------------------------------


def _log_success(
    chat_id: str, kind: str, body: dict[str, Any], payload: dict[str, Any], attempt: int
) -> None:
    message_id = (payload.get("data") or {}).get("message_id")
    logger.info(
        f"飞书{kind}推送成功",
        extra={
            "fields": {
                "chat_id": chat_id,
                "msg_type": body["msg_type"],
                "message_id": message_id,
                "attempt": attempt,
            }
        },
    )


def _log_rejected(chat_id: str, kind: str, code: Any, msg: str) -> None:
    logger.error(
        f"飞书{kind}推送被拒（业务错误码，重试解决不了，不重试）",
        extra={"fields": {"chat_id": chat_id, "code": code, "msg": msg}},
    )


def _log_retry(
    chat_id: str, kind: str, attempt: int, max_attempts: int, error: str
) -> None:
    logger.warning(
        f"飞书{kind}推送失败，准备重试",
        extra={
            "fields": {
                "chat_id": chat_id,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "error": error,
            }
        },
    )


# --------------------------------------------------------------------------
# 投递
# --------------------------------------------------------------------------


def _deliver(client: LarkClient, body: dict[str, Any], chat_id: str, kind: str) -> bool:
    """投递一条消息：瞬时故障重试 2 次，业务错误码立即放弃（design.md §6.1）。

    例外：`_RETRYABLE_BUSINESS_CODES` 里的错误码虽然也走 `code != 0` 通道，
    但语义上可恢复，按瞬时故障处理。
    """
    attempts = _MAX_RETRIES + 1
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            payload = _post(client, body)
        except httpx.HTTPStatusError as exc:
            rejected = _business_rejection(exc.response)
            if rejected is not None and rejected[0] not in _RETRYABLE_BUSINESS_CODES:
                _log_rejected(chat_id, kind, *rejected)
                return False
            if rejected is not None:
                last_error = f"code={rejected[0]} {rejected[1]}"
            else:
                last_error = f"HTTP {exc.response.status_code}"
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            code, msg = _code_and_msg(payload)
            if code == 0:
                _log_success(chat_id, kind, body, payload, attempt)
                return True
            if code not in _RETRYABLE_BUSINESS_CODES:
                _log_rejected(chat_id, kind, code, msg)
                return False
            last_error = f"code={code} {msg}"

        if attempt < attempts:
            _log_retry(chat_id, kind, attempt, attempts, last_error)
            time.sleep(_RETRY_INTERVAL_SECONDS)

    logger.error(
        f"飞书{kind}推送失败：重试 {_MAX_RETRIES} 次后仍不成功",
        extra={
            "fields": {"chat_id": chat_id, "attempts": attempts, "error": last_error}
        },
    )
    return False


def _push(chat_id: str, msg_type: str, content: str, kind: str) -> bool:
    """两个公开入口的公共实现。

    对调用方的唯一承诺：**任何情况下只返回 bool，不抛异常**（design.md §4.3）。
    兜底 except 是这条承诺的最后防线 —— 覆盖取配置失败、LarkClient 换取令牌
    失败（RuntimeError）、响应体不是 JSON 等非 httpx 异常。
    """
    if not chat_id or not chat_id.strip():
        logger.error(
            f"飞书{kind}推送缺少 chat_id，未发送",
            extra={"fields": {"kind": kind}},
        )
        return False

    body = {"receive_id": chat_id, "msg_type": msg_type, "content": content}
    try:
        return _deliver(_get_client(), body, chat_id, kind)
    except Exception as exc:  # noqa: BLE001 —— 见 docstring 的承诺
        logger.error(
            f"飞书{kind}推送发生未预期异常",
            extra={"fields": {"chat_id": chat_id, "error": repr(exc)}},
        )
        return False


# --------------------------------------------------------------------------
# 公开接口（design.md §4.3）
# --------------------------------------------------------------------------


def send(report: DailyReport, chat_id: str) -> bool:
    """把日报以 Markdown 格式推送到指定群。

    Returns:
        True 表示飞书确认接收；任何失败只返回 False 并记 ERROR 日志，不抛异常。
    """
    return _push(chat_id, "post", _markdown_content(report.markdown), "日报")


def send_text(text: str, chat_id: str) -> bool:
    """把纯文本推送到指定群；供告警通道复用（design.md §4.3）。

    本模块自身**不触发告警**，是否告警由编排层根据返回值决定。
    """
    return _push(chat_id, "text", _text_content(text), "文本")
