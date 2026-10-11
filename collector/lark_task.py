"""飞书任务采集（design.md §3.1 / §3.2 / §4.1 / §6.5，tasks.md Task 4）。

接口契约（design.md §4.1）::

    collect(project_id: str, since: datetime, until: datetime) -> CollectResult[TaskRecord]

    project_id 为空字符串时表示「不限定清单，采集全部任务」。

令牌管理**完全复用** `collector/lark_client.py`：本模块不换取、不缓存、不刷新
任何令牌，只调用 ``LarkClient.get()``（design.md §4.1 要求两者共用一份令牌逻辑）。

--------------------------------------------------------------------------
飞书接口形状（2026-10-11 逐份核对官方文档，非凭记忆）
--------------------------------------------------------------------------

1. 列取任务列表 —— https://open.feishu.cn/document/task-v2/task/list
   ``GET /open-apis/task/v2/tasks``
   查询参数：``page_size``(1~100，默认 50)、``page_token``、``completed``(布尔)、
   ``type``（目前只支持 ``"my_tasks"``）、``user_id_type``(默认 ``open_id``)。
   响应 ``data.items[]`` 为完整 task 对象：``guid`` / ``summary`` / ``status``
   （``"todo"`` 或 ``"done"``）/ ``created_at`` / ``updated_at`` / ``completed_at``
   （毫秒时间戳字符串）/ ``members[]`` / ``tasklists[]`` / …
   **该接口的请求头要求 ``user_access_token``**，而 `LarkClient`（design.md §4.1）
   只管理 ``tenant_access_token`` —— 用 tenant 身份调用它必然失败。

2. 获取清单任务列表 —— https://open.feishu.cn/document/task-v2/tasklist/tasks
   ``GET /open-apis/task/v2/tasklists/{tasklist_guid}/tasks``
   路径参数 ``tasklist_guid``（任务清单 GUID）；查询参数 ``page_size``、
   ``page_token``、``completed``、``created_from``、``created_to``、``user_id_type``。
   接受 ``tenant_access_token`` 或 ``user_access_token``。
   响应 ``data.items[]`` 为 task_summary：``guid`` / ``summary`` /
   ``completed_at``（毫秒时间戳字符串，``"0"`` 表示未完成）/ ``start`` / ``due`` /
   ``members[{id, type, role, name}]`` / ``subtask_count``。
   注意 ``created_from`` / ``created_to`` 过滤的是**创建**时间，不是状态变更时间，
   因此本模块不使用它们做窗口过滤（否则会漏掉「窗口前创建、窗口内完成」的任务）。

3. 获取清单列表 —— https://open.feishu.cn/document/task-v2/tasklist/list
   ``GET /open-apis/task/v2/tasklists``
   查询参数 ``page_size``、``page_token``、``user_id_type``；接受
   ``tenant_access_token``。响应 ``data.items[]``：``guid`` / ``name`` / …

**project_id 为空字符串时的实现选择**：「不限定清单，采集全部任务」落在
tenant 身份下唯一可行的路径上 —— 先用接口 3 枚举**全部可读清单**，再逐个清单
用接口 2 采集其任务（清单即本模块的「子项」，与 design.md §3.2 语义表中
「多仓库部分失败」处于同一粒度）。之所以不用接口 1：它要求 ``user_access_token``，
在本项目的 `LarkClient` 契约下拿不到，会让「采集全部任务」在物理上无法完成。
已知代价：不属于任何清单的「任务中心」任务不在覆盖范围内。

--------------------------------------------------------------------------
status_from / status_to 的数据来源与限制
--------------------------------------------------------------------------

飞书任务的开放接口**只返回当前状态，不提供状态变更历史**（接口 2 的
task_summary 甚至不含 ``status`` 字段，只有 ``completed_at``）。接口 2 暴露的
时间戳里，只有 ``completed_at`` 在语义上是一次**状态变更时刻**；``start`` / ``due``
是用户设定的业务字段，不能当作变更时刻。

因此本模块的窗口判定为：``completed_at ∈ [since, until]``（闭区间）→ 产出一条
TaskRecord，对应一次**可确证的**「未完成 → 已完成」变更：

- ``status_from = "todo"``（由 completed_at 非零反推：该任务此前未完成）
- ``status_to``：优先取条目上的 ``status`` 字段（接口 1 返回它），缺省时按
  ``completed_at != "0"`` 推导出 ``"done"``（接口 2 的实际形状）
- ``updated_at = completed_at`` 归一化后的 tz-aware datetime

取不到的情况（接口能力所限，非实现缺陷）：
a) 窗口内「已完成 → 未完成」的重新打开 —— 接口把 ``completed_at`` 重置为 ``"0"``，
   变更时刻不可知，会被漏采；
b) 与完成无关的编辑（改标题、改截止时间等）不是状态变更，本就不该采集。

--------------------------------------------------------------------------
时区（design.md §6.5）
--------------------------------------------------------------------------

`collect()` 的签名不含时区参数（§4.1 定死），故目标时区取自入参
``since.tzinfo`` —— §6.5 要求 ``since`` / ``until`` 跨越模块边界前必带 tzinfo，
调用方的时区即 `schedule.timezone`。这样既满足「毫秒时间戳按 UTC 解析后转目标
时区」，又不必让采集层反向读取 config。毫秒时间戳先用 UTC 解析，再
``astimezone(target_tz)``；窗口比较统一在 UTC 下进行，禁止 naive 参与比较。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from urllib.parse import quote

import httpx

from collector.lark_client import LarkClient
from shared.config import load as load_config
from shared.logger import get_logger
from shared.models import CollectResult, TaskRecord

logger = get_logger("collector.lark_task")

#: CollectResult.source 的取值（design.md §3.2）
SOURCE = "lark_task"

# --- 飞书接口路径（核对自官方文档，见模块 docstring） ----------------------
_TASKLISTS_PATH = "/open-apis/task/v2/tasklists"
_TASKLIST_TASKS_PATH = "/open-apis/task/v2/tasklists/{guid}/tasks"

_PAGE_SIZE = 100  # 文档允许的最大值，减少请求次数
_MAX_PAGES = 100  # 分页死循环护栏：100 页 × 100 条 = 10000 条任务
_MAX_TIMEOUT_RETRIES = 3  # 超时重试 3 次（design.md §6.1、tasks.md Task 4）
_RETRY_INTERVAL_SECONDS = 5.0  # 与 GitHub 采集一致的重试间隔（§6.1）

_STATUS_TODO = "todo"
_STATUS_DONE = "done"
_ROLE_ASSIGNEE = "assignee"

#: 模块级客户端（测试注入点：monkeypatch `collector.lark_task._client`）。
#: design.md §4.1 的 collect() 签名不接收 client 参数，故由模块惰性构造并复用 ——
#: 复用同一实例也让 tenant_access_token 的缓存跨调用生效。
_client: LarkClient | None = None


class _FetchError(Exception):
    """单个子项（清单）采集失败。

    仅在模块内部流转：`collect()` 捕获它并转成 `CollectResult(success=False)`，
    **绝不外抛给调用方**（tasks.md Task 4 补充段验收标准）。
    """


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


def _get_client() -> LarkClient:
    """惰性构造并复用 `LarkClient`（凭证来自环境变量，经 shared.config 读取）。"""
    global _client
    if _client is None:
        config = load_config()
        _client = LarkClient(config.lark.app_id, config.lark.app_secret)
    return _client


# --------------------------------------------------------------------------
# 时间工具（design.md §6.5）
# --------------------------------------------------------------------------


def _parse_ms(value: Any) -> datetime | None:
    """毫秒级 Unix 时间戳（飞书返回的是**字符串**）→ UTC tz-aware datetime。

    ``None`` / ``""`` / ``"0"`` / 非法值一律返回 ``None``：
    task_summary 的 ``completed_at`` 用 ``"0"`` 表示「未完成」，它不是时间戳。
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        millis = int(text)
    except ValueError:
        return None
    if millis <= 0:
        return None
    seconds, remainder = divmod(millis, 1000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(
        milliseconds=remainder
    )


def _to_utc(value: datetime) -> datetime:
    """统一换算到 UTC 参与比较；naive 视为 UTC（§6.5 禁止 naive 参与比较）。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# 字段映射
# --------------------------------------------------------------------------


def _status_of(item: dict[str, Any]) -> str:
    """取任务当前状态。

    优先使用接口显式返回的 ``status``（接口 1 有，接口 2 无）；缺省时由
    ``completed_at`` 推导 —— 非 0 即已完成。
    """
    raw = str(item.get("status") or "").strip().lower()
    if raw in (_STATUS_TODO, _STATUS_DONE):
        return raw
    return _STATUS_DONE if _parse_ms(item.get("completed_at")) is not None else _STATUS_TODO


def _assignee_of(item: dict[str, Any]) -> str:
    """取负责人的 **open_id**。

    design.md §3.1 把该字段描述为「负责人（飞书用户名）」，但 §3.4 的聚合规则
    要求「用飞书标识（lark_open_id，或由 lark 反查得到的 open_id）做匹配」，且
    §3.4 明确写道「飞书开放平台的绝大多数接口（消息、任务）返回的是 open_id
    而非邮箱」。要能与 config.yaml 里的 ``lark_open_id`` 对上，这里必须填
    open_id；填显示名（member.name）会让聚合层无法匹配而丢弃整条记录。

    飞书任务接口的 member 对象里，``id`` 的类型由 ``user_id_type`` 决定
    （本模块显式传 ``open_id``，与文档默认值一致）。
    """
    members = item.get("members")
    if not isinstance(members, list):
        return ""
    for member in members:
        if isinstance(member, dict) and str(member.get("role") or "") == _ROLE_ASSIGNEE:
            return str(member.get("id") or "")
    # 未标注 role=assignee 时退而取第一个成员，避免整条记录丢失负责人
    for member in members:
        if isinstance(member, dict) and member.get("id"):
            return str(member["id"])
    return ""


def _build_record(
    item: dict[str, Any],
    since_utc: datetime,
    until_utc: datetime,
    target_tz: tzinfo,
) -> TaskRecord | None:
    """把一条 task_summary/task 映射为 TaskRecord；不在采集窗口内返回 None。

    窗口依据是 ``completed_at``（模块 docstring 说明了为何它是接口唯一可用作
    「状态变更时刻」的时间戳）。
    """
    changed_at = _parse_ms(item.get("completed_at"))
    if changed_at is None:
        return None  # 未完成 / 无时间戳：无法判定窗口内发生过状态变更
    if not since_utc <= changed_at <= until_utc:
        return None

    return TaskRecord(
        assignee=_assignee_of(item),
        title=str(item.get("summary") or ""),
        status_from=_STATUS_TODO,
        status_to=_status_of(item),
        updated_at=changed_at.astimezone(target_tz),
    )


# --------------------------------------------------------------------------
# 请求（超时重试 3 次）
# --------------------------------------------------------------------------


def _fetch(client: LarkClient, path: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET 一次飞书接口；超时重试 3 次，仍失败抛 `_FetchError`。

    令牌失效的刷新与重试由 `LarkClient` 内部完成（§6.1「飞书 Token 过期 →
    自动刷新后重试 1 次」），本模块**不重复实现**，也不与其重试计数混用。
    """
    last_error: Exception | None = None
    attempts = _MAX_TIMEOUT_RETRIES + 1
    for attempt in range(attempts):
        try:
            payload = client.get(path, params=params)
        except httpx.TimeoutException as exc:
            # 超时是唯一值得重试的瞬时故障（design.md §6.1）
            last_error = exc
            if attempt < _MAX_TIMEOUT_RETRIES:
                logger.warning(
                    "飞书任务接口超时，准备重试",
                    extra={
                        "fields": {
                            "path": path,
                            "attempt": attempt + 1,
                            "max_attempts": attempts,
                            "error": str(exc),
                        }
                    },
                )
                time.sleep(_RETRY_INTERVAL_SECONDS)
            continue
        except Exception as exc:
            # HTTP 4xx/5xx（raise_for_status）、连接错误、取令牌失败（RuntimeError）…
            # 重试解决不了这类问题，直接判该子项失败
            raise _FetchError(f"请求 {path} 失败：{exc}") from exc

        if payload.get("code") != 0:
            raise _FetchError(
                f"飞书返回错误（code={payload.get('code')} msg={payload.get('msg')}）"
            )
        return payload

    raise _FetchError(f"超时重试 {_MAX_TIMEOUT_RETRIES} 次后仍失败：{last_error}")


def _paginate(client: LarkClient, path: str) -> list[dict[str, Any]]:
    """按 page_token 翻页取全量 items（判断依据是 has_more，非 items 条数）。

    飞书文档明确：历史原因下当前分页可能出现 items 为空但 has_more=true 的情况。
    """
    items: list[dict[str, Any]] = []
    page_token: str | None = None

    for _ in range(_MAX_PAGES):
        params: dict[str, Any] = {
            "page_size": _PAGE_SIZE,
            "user_id_type": "open_id",  # 与 _assignee_of 的约定一致
        }
        if page_token:
            params["page_token"] = page_token

        payload = _fetch(client, path, params)
        data = payload.get("data") or {}
        page_items = data.get("items") or []
        if not isinstance(page_items, list):
            raise _FetchError(f"飞书返回的 items 不是列表：{type(page_items).__name__}")
        items.extend(item for item in page_items if isinstance(item, dict))

        if not data.get("has_more"):
            return items
        page_token = data.get("page_token") or None
        if not page_token:
            logger.warning(
                "飞书分页 has_more=true 但未返回 page_token，提前结束",
                extra={"fields": {"path": path}},
            )
            return items

    raise _FetchError(f"分页超过上限 {_MAX_PAGES} 页，疑似分页标记未推进")


# --------------------------------------------------------------------------
# 子项采集
# --------------------------------------------------------------------------


def _list_all_tasklist_guids(client: LarkClient) -> list[str]:
    """枚举全部可读清单的 guid（project_id 为空时的第一步）。"""
    guids: list[str] = []
    for item in _paginate(client, _TASKLISTS_PATH):
        guid = item.get("guid")
        if guid:
            guids.append(str(guid))
    return guids


def _collect_one_tasklist(
    client: LarkClient,
    guid: str,
    since_utc: datetime,
    until_utc: datetime,
    target_tz: tzinfo,
) -> tuple[list[TaskRecord], str | None]:
    """采集单个清单，返回 (记录, 错误摘要)；错误为 None 表示该子项成功。"""
    path = _TASKLIST_TASKS_PATH.format(guid=quote(str(guid), safe=""))
    try:
        items = _paginate(client, path)
    except _FetchError as exc:
        return [], str(exc)
    except Exception as exc:  # 兜底：宁可丢一个清单，也不能让异常冒到调用方
        return [], f"采集清单 {guid} 时发生未预期错误：{exc}"

    records = [
        record
        for record in (
            _build_record(item, since_utc, until_utc, target_tz) for item in items
        )
        if record is not None
    ]
    return records, None


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def collect(project_id: str, since: datetime, until: datetime) -> CollectResult[TaskRecord]:
    """采集采集窗口内发生状态变更的飞书任务（design.md §4.1）。

    Args:
        project_id: 飞书任务清单 GUID；**空字符串表示不限定清单**，采集全部
            可读清单中的任务。
        since: 起始时间（tz-aware，闭区间）。
        until: 结束时间（tz-aware，闭区间）。

    Returns:
        `CollectResult[TaskRecord]`。**任何失败都以返回值表达，不抛异常**
        （tasks.md Task 4 补充段验收标准）：部分清单失败时保留已采到的记录，
        失败清单的 guid 记入 ``failed``。
    """
    since_utc = _to_utc(since)
    until_utc = _to_utc(until)
    # §6.5 规定 since/until 跨模块边界必带 tzinfo；naive 时退化为 UTC
    target_tz: tzinfo = since.tzinfo or timezone.utc

    try:
        client = _get_client()
    except Exception as exc:  # 配置缺失（ConfigError）等，同样以返回值表达
        logger.error(
            "飞书任务采集无法建立客户端",
            extra={"fields": {"error": str(exc)}},
        )
        return CollectResult.fail(SOURCE, f"飞书客户端初始化失败：{exc}")

    project = str(project_id or "").strip()

    if project:
        guids = [project]
    else:
        # 「不限定清单」：枚举全部可读清单，逐个采集（见模块 docstring）
        try:
            guids = _list_all_tasklist_guids(client)
        except _FetchError as exc:
            logger.error(
                "枚举飞书任务清单失败",
                extra={"fields": {"error": str(exc)}},
            )
            # 失败发生在「子项已知之前」，故 failed 为空，诊断信息在 error 里
            return CollectResult.fail(SOURCE, f"枚举飞书任务清单失败：{exc}")
        except Exception as exc:
            logger.error(
                "枚举飞书任务清单发生未预期错误",
                extra={"fields": {"error": str(exc)}},
            )
            return CollectResult.fail(SOURCE, f"枚举飞书任务清单失败：{exc}")

    if not guids:
        # 一个可读清单都没有 == 无数据，不是失败（§3.2 语义表第 2 行）
        logger.info("飞书任务采集完成：无可用清单", extra={"fields": {"count": 0}})
        return CollectResult.ok(SOURCE, [])

    records: list[TaskRecord] = []
    failed: list[str] = []
    errors: list[str] = []

    for guid in guids:
        sub_records, error = _collect_one_tasklist(
            client, guid, since_utc, until_utc, target_tz
        )
        if error is None:
            records.extend(sub_records)
        else:
            failed.append(guid)
            errors.append(f"{guid}：{error}")
            logger.error(
                "飞书任务清单采集失败",
                extra={"fields": {"tasklist_guid": guid, "error": error}},
            )

    logger.info(
        "飞书任务采集完成",
        extra={
            "fields": {
                "project_id": project or "<全部清单>",
                "tasklists": len(guids),
                "failed_tasklists": len(failed),
                "records": len(records),
            }
        },
    )

    if failed:
        return CollectResult.fail(
            SOURCE,
            "飞书任务采集失败 —— " + "；".join(errors),
            records=records,  # 部分失败时保留成功清单已采到的记录（§3.2）
            failed=failed,
        )
    return CollectResult.ok(SOURCE, records)
