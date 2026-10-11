"""飞书群消息采集（design.md §3.1 / §3.2 / §4.1 / §6.2 / §6.5，tasks.md Task 5）。

**接口形状（2026-10-11 按官方文档原文核实，非凭记忆）**

- ``GET /open-apis/im/v1/messages``（获取会话历史消息）
  来源：https://open.feishu.cn/document/server-docs/im-v1/message/list
  - ``container_id_type=chat``、``container_id=<oc_...>``（必填）；
  - ``start_time`` / ``end_time`` 是**秒级** Unix 时间戳字符串（文档示例
    ``1608594809``）—— 别和响应体里的毫秒混了；
  - ``sort_type``：``ByCreateTimeAsc``（默认，升序）/ ``ByCreateTimeDesc``，
    使用 ``page_token`` 翻页期间不可改换；
  - ``page_size`` 1~50（默认 20）、``page_token`` 翻页、``has_more`` 指示是否继续；
  - 响应 ``data.items[]``：
    - ``msg_type``：text / post / image / file / audio / media / sticker /
      interactive（卡片）/ system / …（见「接收消息内容」文档）；
    - ``create_time`` / ``update_time``：**毫秒级**时间戳字符串；
    - ``deleted``：撤回或删除标记（撤回消息 content 固定为
      "This message was recalled"）；
    - ``sender``：``{id, id_type, sender_type}``。应用发的消息为
      ``id_type="app_id"``、``sender_type="app"``（id 是 ``cli_`` 应用 ID），
      用户发的消息为 ``open_id`` / ``"user"``；
    - ``body.content``：**JSON 序列化后的字符串**，text 类型形如
      ``{"text": "..."}`` —— 必须二次解析；
  - 常见错误码：230002 机器不在群、230006 未启用机器人能力、230027 权限不足。

- ``GET /open-apis/im/v1/chats/:chat_id``（获取群信息）
  来源：https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/chat/get
  - 响应 ``data.name`` 即群名称，用于 ``MessageRecord.chat_name``。

**安全红线（design.md §6.2「2. 数据安全」）**

敏感词过滤在本模块**采集阶段**执行：命中 ``filters.sensitive_keywords``
的消息直接丢弃 —— 原始内容不进返回值，也**不进日志**（日志只记录条数）。
过滤顺序为敏感词在前、关键词在后，不可颠倒。

**身份字段口径**

``MessageRecord.sender`` 存 ``sender.id``（open_id）而非显示名：聚合层
（design.md §3.4 / §4.5）以 ``lark_identifier``（open_id）匹配记录，显示名
由配置映射表提供 —— 这里放显示名会让聚合整体失配。

**时间口径（§6.5）**

``since`` / ``until`` 必须 tz-aware；毫秒时间戳按 UTC 解析后转
``since.tzinfo``（采集窗口时区，编排层按 schedule.timezone 构造）。
查询参数的 start_time / end_time 是秒级，响应 create_time 是毫秒级字段，
两者单位不同，别混。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from datetime import datetime, timezone, tzinfo
from typing import Any

import httpx

from collector.lark_client import LarkClient
from shared import config as config_module
from shared.config import Config
from shared.logger import get_logger
from shared.models import CollectResult, MessageRecord

logger = get_logger("collector.lark_msg")

SOURCE = "lark_msg"

_MESSAGES_PATH = "/open-apis/im/v1/messages"
_CHAT_PATH = "/open-apis/im/v1/chats/{chat_id}"

_SORT_TYPE = "ByCreateTimeAsc"  # 升序返回，记录天然按时间排序
_PAGE_SIZE = 50  # 文档取值范围 1~50

# 「超时重试 3 次」（design.md §6.1）= 首次请求之外最多再试 3 次。
# 口径与 LarkClient「令牌失效后重试 1 次」（首次 + 1 次 = 2 次请求）一致。
_MAX_TIMEOUT_RETRIES = 3
_RETRY_INTERVAL_SECONDS = 5.0  # 测试里改 0 以避免真等待


class _LarkMsgError(Exception):
    """内部信号：接口不可用或重试耗尽。消息面向人类，不含消息内容。"""


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------


def _get_with_retry(
    client: LarkClient, path: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """GET 一次飞书接口；超时/连接类故障按 design.md §6.1 重试。

    只重试网络类失败 —— HTTP 4xx/5xx 与业务错误码重试解决不了。令牌管理
    全程交给 LarkClient（含失效自动刷新），本模块不碰任何令牌逻辑。
    """
    last_error: httpx.TransportError | None = None
    for attempt in range(_MAX_TIMEOUT_RETRIES + 1):
        try:
            return client.get(path, params=params)
        except httpx.TransportError as exc:
            last_error = exc
            if attempt < _MAX_TIMEOUT_RETRIES:
                time.sleep(_RETRY_INTERVAL_SECONDS)
    raise _LarkMsgError(
        f"飞书接口连续 {_MAX_TIMEOUT_RETRIES + 1} 次超时/连接失败：{last_error}"
    )


def _fetch_chat_name(client: LarkClient, chat_id: str) -> str:
    """群名称（MessageRecord.chat_name）。

    单次请求、失败即回退 chat_id：群名只影响展示，不该因为一次附加请求
    失败（如未开通 im:chat 权限）就丢掉整批消息，也不占用消息采集的
    重试预算。
    """
    try:
        payload = client.get(_CHAT_PATH.format(chat_id=chat_id))
    except Exception as exc:  # noqa: BLE001 — 附加信息，失败即降级
        logger.error(
            "获取飞书群信息失败，chat_name 回退为 chat_id",
            extra={"fields": {"chat_id": chat_id, "error": str(exc)}},
        )
        return chat_id

    if payload.get("code") != 0:
        logger.error(
            "获取飞书群信息返回错误，chat_name 回退为 chat_id",
            extra={
                "fields": {
                    "chat_id": chat_id,
                    "code": payload.get("code"),
                    "msg": payload.get("msg"),
                }
            },
        )
        return chat_id

    name = (payload.get("data") or {}).get("name")
    return str(name) if name else chat_id


def _fetch_items(
    client: LarkClient, chat_id: str, since: datetime, until: datetime
) -> tuple[list[dict[str, Any]], str | None]:
    """分页取回 [since, until] 内的原始消息，返回 (已取到的消息, 失败原因)。

    失败原因非 None 表示采集**未完整完成**（例如第二页超时）：已取到的页
    原样返回、由调用方以 ``CollectResult(success=False)`` 表达，不丢弃
    （design.md §3.2：部分失败时保留已采到的记录）。
    """
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    seen_tokens: set[str] = set()

    while True:
        params: dict[str, Any] = {
            "container_id_type": "chat",  # 文档核实：chat 容器含单聊与群聊
            "container_id": chat_id,
            # 查询参数是秒级时间戳（文档核实），与响应体的毫秒级字段不同
            "start_time": str(int(since.timestamp())),
            "end_time": str(int(until.timestamp())),
            "sort_type": _SORT_TYPE,
            "page_size": _PAGE_SIZE,
        }
        if page_token:
            params["page_token"] = page_token

        try:
            payload = _get_with_retry(client, _MESSAGES_PATH, params)
        except _LarkMsgError as exc:
            return items, str(exc)
        except Exception as exc:  # noqa: BLE001 — 含 HTTP 状态错误/令牌获取失败等
            return items, f"飞书消息接口请求失败：{exc}"

        if payload.get("code") != 0:
            return items, (
                f"飞书消息接口返回错误：code={payload.get('code')} "
                f"msg={payload.get('msg')}"
            )

        data = payload.get("data") or {}
        items.extend(data.get("items") or [])

        next_token = data.get("page_token")
        if not data.get("has_more") or not next_token:
            return items, None
        if next_token in seen_tokens:
            # 防御：令牌重复说明服务端分页异常，就此打住并如实报告未完成
            return items, "飞书消息分页令牌重复，采集提前终止"
        seen_tokens.add(next_token)
        page_token = next_token


def _is_bot(item: dict[str, Any], bot_open_id: str | None) -> bool:
    """机器人/应用发的消息不进讨论记录。

    两个判据互补、缺一不可：飞书对应用发的消息返回
    ``sender_type="app"``（id 是 ``cli_`` 应用 ID，见官方响应示例），与
    配置里的 ``FEISHU_BOT_OPEN_ID``（``ou_`` 形式）比对不上；而按 open_id
    比对可覆盖部分以用户身份返回的场景。命中任一即排除 —— 否则前一日
    推送的日报会被当作今日的讨论记录回采，形成回环。
    """
    sender = item.get("sender") or {}
    if sender.get("sender_type") == "app":
        return True
    return bool(bot_open_id) and sender.get("id") == bot_open_id


def _load_content(item: dict[str, Any]) -> dict[str, Any] | None:
    """解析 body.content（JSON 序列化字符串）为 dict；非法/缺失返回 None。"""
    raw = (item.get("body") or {}).get("content")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _collect_text_nodes(node: Any) -> list[str]:
    """递归收集富文本节点里的 text 字段（text / a / at / code_block / md 标签）。"""
    if isinstance(node, dict):
        text = node.get("text")
        if isinstance(text, str) and text:
            return [text]
        return [chunk for value in node.values() for chunk in _collect_text_nodes(value)]
    if isinstance(node, list):
        return [chunk for child in node for chunk in _collect_text_nodes(child)]
    return []


def _post_text(payload: dict[str, Any]) -> str | None:
    """富文本（post）→ 纯文本：标题 + 正文各段的 text 节点。

    ``content`` 与 ``content_v2`` 可能同时返回且内容重叠，只取先有文本的一个。
    """
    parts: list[str] = []
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        parts.append(title.strip())
    for key in ("content", "content_v2"):
        chunks = _collect_text_nodes(payload.get(key))
        if chunks:
            parts.extend(chunks)
            break
    text = "\n".join(part for part in parts if part)
    return text or None


def _extract_text(item: dict[str, Any]) -> str | None:
    """取消息的纯文本；无纯文本可采（图片/文件/卡片等）时返回 None。

    text 与 post 是仅有的两种含纯文本的类型；其余 msg_type
    （image / file / audio / media / sticker / interactive / system / …）
    合理跳过 —— MessageRecord.content 是纯文本。
    """
    payload = _load_content(item)
    if payload is None:
        return None

    msg_type = item.get("msg_type")
    if msg_type == "text":
        text = payload.get("text")
        return str(text).strip() if isinstance(text, str) and text.strip() else None
    if msg_type == "post":
        return _post_text(payload)
    return None


def _parse_timestamp(raw: Any, tz: tzinfo) -> datetime:
    """毫秒级 Unix 时间戳字符串 → tz-aware datetime（design.md §6.5）。

    按 UTC 解析后转目标时区（本模块取 ``since.tzinfo`` —— 采集窗口的时区，
    即编排层按 schedule.timezone 构造的那个；采集层不反向读 config）。
    毫秒拆成「秒 + 微秒」两级换算，避免浮点除法在毫秒位上的精度损失。
    """
    millis = int(raw)
    seconds, remainder = divmod(millis, 1000)
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
        microsecond=remainder * 1000
    )
    return moment.astimezone(tz)


def _matches(text: str, needles: Iterable[str]) -> bool:
    """大小写不敏感的子串命中判断（敏感词同样不敏感 —— 漏杀比误杀危险）。"""
    lowered = text.lower()
    return any(needle and needle.lower() in lowered for needle in needles)


def _filter_items(
    items: list[dict[str, Any]],
    *,
    keywords: list[str],
    sensitive_keywords: list[str],
    bot_open_id: str | None,
    chat_id: str,
    chat_name: str,
    tz: tzinfo,
    since: datetime,
    until: datetime,
) -> tuple[list[MessageRecord], int]:
    """原始 items → MessageRecord，两层过滤顺序不可颠倒：

    1. **敏感词**（安全红线，§6.2）：命中即丢弃，只计数，绝不落日志；
    2. **关键词**：只保留命中之一的消息（keywords 为空表示不做关键词过滤）。

    撤回消息、非文本消息、机器人自身消息在此一并剔除。返回
    (记录, 敏感词剔除条数)。日志只允许出现条数，不允许出现内容。
    """
    records: list[MessageRecord] = []
    sensitive_dropped = 0

    for item in items:
        if item.get("deleted") or _is_bot(item, bot_open_id):
            continue

        text = _extract_text(item)
        if text is None:
            continue

        try:
            timestamp = _parse_timestamp(item.get("create_time"), tz)
        except (TypeError, ValueError):
            # 注意：异常信息不含消息内容，日志安全
            logger.error(
                "跳过时间戳非法的飞书消息",
                extra={"fields": {"chat_id": chat_id}},
            )
            continue

        # 客户端兜底窗口校验：服务端按秒过滤，边界上可能多带消息
        if not since <= timestamp <= until:
            continue

        if _matches(text, sensitive_keywords):
            sensitive_dropped += 1  # 只记条数：内容不得进日志（§6.2）
            continue

        if keywords and not _matches(text, keywords):
            continue

        records.append(
            MessageRecord(
                sender=str((item.get("sender") or {}).get("id") or ""),
                content=text,
                timestamp=timestamp,
                chat_name=chat_name,
            )
        )

    return records, sensitive_dropped


# --------------------------------------------------------------------------
# 采集入口（design.md §4.1）
# --------------------------------------------------------------------------


def collect(
    chat_id: str,
    keywords: list[str],
    since: datetime,
    until: datetime,
    *,
    client: LarkClient | None = None,
    config: Config | None = None,
) -> CollectResult[MessageRecord]:
    """采集指定群在 [since, until] 内的消息，经敏感词 + 关键词两层过滤返回。

    签名遵循 design.md §4.1；``client`` / ``config`` 是可选注入点：
    测试经 ``client`` 注入基于 MockTransport 的 LarkClient（不打真实网络），
    编排层可复用已加载的 Config 与已创建的 LarkClient（令牌只换一次）。

    任何失败都以 ``CollectResult(success=False)`` 表达，**不抛异常**给调用方
    （design.md §3.2 / ADR-003）。
    """
    if since.tzinfo is None or until.tzinfo is None:
        return CollectResult.fail(
            SOURCE,
            "since/until 必须是 tz-aware datetime（design.md §6.5）",
            failed=[chat_id],
        )
    if since > until:
        return CollectResult.fail(SOURCE, "采集窗口非法：since 晚于 until", failed=[chat_id])

    try:
        cfg = config if config is not None else config_module.load()
    except Exception as exc:  # noqa: BLE001 — 采集层以返回值表达失败
        logger.error(
            "飞书消息采集的配置加载失败",
            extra={"fields": {"chat_id": chat_id, "error": str(exc)}},
        )
        return CollectResult.fail(SOURCE, f"配置加载失败：{exc}", failed=[chat_id])

    lark = client
    own_client = lark is None
    try:
        if lark is None:
            lark = LarkClient(cfg.lark.app_id, cfg.lark.app_secret)
        # 时间归一化目标时区取采集窗口的时区（since.tzinfo），采集层不反向读 config
        tz = since.tzinfo or timezone.utc
        chat_name = _fetch_chat_name(lark, chat_id)
        items, fetch_error = _fetch_items(lark, chat_id, since, until)
        records, sensitive_dropped = _filter_items(
            items,
            keywords=keywords,
            sensitive_keywords=cfg.filters.sensitive_keywords,
            bot_open_id=cfg.lark.bot_open_id,
            chat_id=chat_id,
            chat_name=chat_name,
            tz=tz,
            since=since,
            until=until,
        )
    except Exception as exc:  # noqa: BLE001 — 兜底：绝不把异常抛给调用方
        logger.error(
            "飞书消息采集失败",
            extra={"fields": {"chat_id": chat_id, "error": str(exc)}},
        )
        return CollectResult.fail(SOURCE, f"飞书消息采集失败：{exc}", failed=[chat_id])
    finally:
        if own_client and lark is not None:
            lark.close()

    logger.info(
        "飞书群消息采集完成",
        extra={
            "fields": {
                "chat_id": chat_id,
                "fetched": len(items),
                # 只记条数：命中的原始内容绝不落日志（design.md §6.2）
                "sensitive_dropped": sensitive_dropped,
                "matched": len(records),
            }
        },
    )

    if fetch_error is not None:
        return CollectResult.fail(SOURCE, fetch_error, records=records, failed=[chat_id])
    return CollectResult.ok(SOURCE, records)
