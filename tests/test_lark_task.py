"""飞书任务采集测试（design.md §3.1 / §3.2 / §4.1 / §6.5，tasks.md Task 4）。

全程使用 `httpx.MockTransport` 构造 `LarkClient` 替身，**不打真实网络**。
参考 `tests/test_lark_client.py` 的 FakeFeishu 写法。
"""

from __future__ import annotations

import inspect
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, get_type_hints

import httpx
import pytest

from collector import lark_task
from collector.lark_client import LarkClient
from collector.lark_task import collect
from shared.models import CollectResult, TaskRecord

TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
TASKLISTS_PATH = "/open-apis/task/v2/tasklists"

# 固定偏移时区：避免依赖 tzdata（本机 Windows 未安装，ZoneInfo 会报错），
# 同时保持与 config.yaml 默认 schedule.timezone 相同的 +08:00 口径。
SHANGHAI = timezone(timedelta(hours=8), "CST")

WINDOW_START = datetime(2026, 10, 11, 0, 0, tzinfo=SHANGHAI)
WINDOW_END = datetime(2026, 10, 11, 18, 0, tzinfo=SHANGHAI)
INSIDE = datetime(2026, 10, 11, 10, 30, tzinfo=SHANGHAI)
GUID = "cc371766-6584-cf50-a222-c22cd9055004"


# --------------------------------------------------------------------------
# 替身与夹具
# --------------------------------------------------------------------------


def tasks_path(guid: str) -> str:
    return f"/open-apis/task/v2/tasklists/{guid}/tasks"


def iso_ms(moment: datetime) -> str:
    """datetime → 毫秒级 Unix 时间戳字符串（飞书返回的格式）。"""
    return str(int(moment.timestamp() * 1000))


def page(
    items: list[dict[str, Any]],
    has_more: bool = False,
    page_token: str | None = None,
) -> httpx.Response:
    data: dict[str, Any] = {"items": items, "has_more": has_more}
    if page_token is not None:
        data["page_token"] = page_token
    return httpx.Response(200, json={"code": 0, "msg": "success", "data": data})


def api_error(code: int = 1470404, msg: str = "tasklist not found") -> httpx.Response:
    return httpx.Response(200, json={"code": code, "msg": msg})


def task_item(
    guid: str = "task-1",
    summary: str = "任务",
    completed_at: str = "0",
    members: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """构造 task_summary（「获取清单任务列表」返回的形状）。"""
    item: dict[str, Any] = {
        "guid": guid,
        "summary": summary,
        "completed_at": completed_at,
    }
    if members is not None:
        item["members"] = members
    item.update(extra)
    return item


def member(open_id: str, role: str = "assignee", name: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": open_id, "type": "user", "role": role}
    if name is not None:
        entry["name"] = name
    return entry


class FakeFeishu:
    """记录请求并按脚本作答的飞书服务端替身。

    `stub()` 给某条路径排定响应脚本：调用时依次取出，只剩一条时重复使用；
    脚本里放异常实例即表示该请求抛异常（如 `httpx.ReadTimeout`）。
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.tokens_issued = 0
        self.scripts: dict[str, list[Any]] = {}

    def stub(self, path: str, *outcomes: Any) -> None:
        self.scripts[path] = list(outcomes)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        script = self.scripts.get(path)
        if script:
            outcome = script.pop(0) if len(script) > 1 else script[0]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        if path == TOKEN_PATH:
            self.tokens_issued += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": f"t-{self.tokens_issued}",
                    "expire": 7200,
                },
            )

        # 未排定脚本的路径一律返回空数据集
        return page([])

    def client(self, **kwargs: Any) -> LarkClient:
        return LarkClient(
            "cli_test",
            "secret_test",
            transport=httpx.MockTransport(self.handler),
            **kwargs,
        )

    # -- 断言辅助 ----------------------------------------------------------

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def paths_except_token(self) -> list[str]:
        return [path for path in self.paths() if path != TOKEN_PATH]

    def queries(self, path: str) -> list[dict[str, str]]:
        return [dict(r.url.params) for r in self.requests if r.url.path == path]

    def auth_headers(self) -> list[str | None]:
        return [request.headers.get("authorization") for request in self.requests]


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    """把超时重试间隔置 0 —— 测试不能真等 5 秒。"""
    monkeypatch.setattr(lark_task, "_RETRY_INTERVAL_SECONDS", 0)


@pytest.fixture
def feishu() -> FakeFeishu:
    return FakeFeishu()


@pytest.fixture
def collector(feishu: FakeFeishu, monkeypatch) -> FakeFeishu:
    """把替身客户端注入模块（collect 的签名不含 client 参数，见 design.md §4.1）。"""
    monkeypatch.setattr(lark_task, "_client", feishu.client())
    return feishu


# --------------------------------------------------------------------------
# 接口契约
# --------------------------------------------------------------------------


def test_collect_signature_matches_design():
    """collect(project_id, since, until) -> CollectResult[TaskRecord]（§4.1）。"""
    signature = inspect.signature(collect)

    assert list(signature.parameters) == ["project_id", "since", "until"]
    assert get_type_hints(collect)["return"] == CollectResult[TaskRecord]


def test_collect_result_has_all_five_fields(collector):
    collector.stub(tasks_path(GUID), page([task_item(completed_at=iso_ms(INSIDE))]))

    result = collect(GUID, WINDOW_START, WINDOW_END)

    assert {field.name for field in fields(CollectResult)} == {
        "source",
        "success",
        "records",
        "error",
        "failed",
    }
    assert result.source == "lark_task"
    assert isinstance(result, CollectResult)


def test_module_does_not_implement_its_own_token_logic():
    """验收标准「复用 LarkClient，不自建令牌逻辑」的结构性证据。

    换取令牌的接口路径只应出现在 collector/lark_client.py 里；若它出现在本模块，
    就说明又写了一套令牌逻辑。
    """
    source = Path(lark_task.__file__).read_text(encoding="utf-8")

    assert TOKEN_PATH not in source


# --------------------------------------------------------------------------
# 请求形状
# --------------------------------------------------------------------------


def test_project_id_targets_tasklist_scoped_endpoint(collector):
    collector.stub(tasks_path(GUID), page([]))

    collect(GUID, WINDOW_START, WINDOW_END)

    assert collector.paths_except_token() == [tasks_path(GUID)]


def test_request_params_use_open_id_and_max_page_size(collector):
    collector.stub(tasks_path(GUID), page([]))

    collect(GUID, WINDOW_START, WINDOW_END)

    query = collector.queries(tasks_path(GUID))[0]
    assert query["user_id_type"] == "open_id"
    assert query["page_size"] == "100"


def test_requests_carry_bearer_token(collector):
    collector.stub(tasks_path(GUID), page([]))

    collect(GUID, WINDOW_START, WINDOW_END)

    assert collector.paths()[0] == TOKEN_PATH
    assert collector.auth_headers()[-1] == "Bearer t-1"


def test_token_is_fetched_once_for_the_whole_collection(collector):
    """令牌由 LarkClient 统一管理：多清单、多页也只换取一次。"""
    collector.stub(
        TASKLISTS_PATH,
        page([{"guid": "list-a"}, {"guid": "list-b"}], has_more=True, page_token="p2"),
        page([{"guid": "list-c"}]),
    )
    for guid in ("list-a", "list-b", "list-c"):
        collector.stub(tasks_path(guid), page([]))

    collect("", WINDOW_START, WINDOW_END)

    assert collector.tokens_issued == 1
    assert collector.paths().count(TOKEN_PATH) == 1


def test_guid_is_url_quoted(collector):
    """清单 GUID 参与路径拼接前先转义，避免斜杠等字符破坏 URL。"""
    collect("a/b", WINDOW_START, WINDOW_END)

    raw_path = collector.requests[-1].url.raw_path.split(b"?")[0]
    assert raw_path == b"/open-apis/task/v2/tasklists/a%2Fb/tasks"


def test_pagination_follows_page_token_until_has_more_false(collector):
    collector.stub(
        tasks_path(GUID),
        page(
            [task_item(guid="t-1", summary="t-1", completed_at=iso_ms(INSIDE))],
            has_more=True,
            page_token="page-2",
        ),
        page([task_item(guid="t-2", summary="t-2", completed_at=iso_ms(INSIDE))]),
    )

    result = collect(GUID, WINDOW_START, WINDOW_END)

    assert [record.title for record in result.records] == ["t-1", "t-2"]
    queries = collector.queries(tasks_path(GUID))
    assert len(queries) == 2
    assert "page_token" not in queries[0]
    assert queries[1]["page_token"] == "page-2"


# --------------------------------------------------------------------------
# 字段映射（TaskRecord 5 个字段）
# --------------------------------------------------------------------------


def test_record_has_all_five_fields_with_correct_types(collector):
    members = [member("ou_zhangsan", role="assignee", name="张三")]
    collector.stub(
        tasks_path(GUID),
        page(
            [
                task_item(
                    guid="t-1",
                    summary="完成周报",
                    completed_at=iso_ms(INSIDE),
                    members=members,
                )
            ]
        ),
    )

    result = collect(GUID, WINDOW_START, WINDOW_END)
    record = result.records[0]

    assert isinstance(record, TaskRecord)
    assert {field.name for field in fields(TaskRecord)} == {
        "assignee",
        "title",
        "status_from",
        "status_to",
        "updated_at",
    }
    # 负责人是 open_id，而非显示名 —— 聚合层要用它与 config 的 lark_open_id 匹配
    assert record.assignee == "ou_zhangsan"
    assert isinstance(record.assignee, str)
    assert record.title == "完成周报"
    assert record.status_from == "todo"
    assert record.status_to == "done"
    assert isinstance(record.updated_at, datetime)


def test_ms_timestamp_normalized_to_tzaware_target_timezone(collector):
    """飞书返回毫秒时间戳字符串 → tz-aware datetime，且落在目标时区（§6.5）。"""
    collector.stub(
        tasks_path(GUID),
        page([task_item(completed_at=iso_ms(INSIDE))]),
    )

    record = collect(GUID, WINDOW_START, WINDOW_END).records[0]

    assert record.updated_at.tzinfo is not None
    assert record.updated_at.utcoffset() == timedelta(hours=8)  # 目标时区取自 since
    assert record.updated_at.hour == 10
    # 与原始时间戳是同一时刻
    assert record.updated_at == INSIDE
    assert record.updated_at.astimezone(timezone.utc) == datetime(
        2026, 10, 11, 2, 30, tzinfo=timezone.utc
    )


def test_explicit_status_field_is_honored(collector):
    """接口 1 会返回 status 字段，有它就优先用（接口 2 没有，靠 completed_at 推导）。"""
    collector.stub(
        tasks_path(GUID),
        page([task_item(completed_at=iso_ms(INSIDE), status="done")]),
    )

    record = collect(GUID, WINDOW_START, WINDOW_END).records[0]

    assert record.status_to == "done"


def test_assignee_falls_back_to_first_member(collector):
    members = [member("ou_a", role="editor"), member("ou_b", role="viewer")]
    collector.stub(
        tasks_path(GUID),
        page([task_item(completed_at=iso_ms(INSIDE), members=members)]),
    )

    record = collect(GUID, WINDOW_START, WINDOW_END).records[0]

    assert record.assignee == "ou_a"


def test_assignee_is_empty_when_no_members(collector):
    collector.stub(tasks_path(GUID), page([task_item(completed_at=iso_ms(INSIDE))]))

    record = collect(GUID, WINDOW_START, WINDOW_END).records[0]

    assert record.assignee == ""


def test_missing_summary_yields_empty_title(collector):
    item = task_item(completed_at=iso_ms(INSIDE), summary="")
    collector.stub(tasks_path(GUID), page([item]))

    record = collect(GUID, WINDOW_START, WINDOW_END).records[0]

    assert record.title == ""


# --------------------------------------------------------------------------
# 采集窗口
# --------------------------------------------------------------------------


def test_only_tasks_completed_inside_window_are_collected(collector):
    """窗口闭区间；未完成与非法时间戳一律不产出记录。"""
    labels = {
        "before": WINDOW_START - timedelta(minutes=1),
        "at-start": WINDOW_START,
        "inside": INSIDE,
        "at-end": WINDOW_END,
        "after": WINDOW_END + timedelta(minutes=1),
    }
    items = [
        task_item(summary=label, completed_at=iso_ms(moment))
        for label, moment in labels.items()
    ]
    items.append(task_item(summary="still-open", completed_at="0"))
    items.append(task_item(summary="garbage", completed_at="not-a-timestamp"))
    items.append({"guid": "no-key", "summary": "no-key"})
    collector.stub(tasks_path(GUID), page(items))

    result = collect(GUID, WINDOW_START, WINDOW_END)

    assert [record.title for record in result.records] == ["at-start", "inside", "at-end"]


# --------------------------------------------------------------------------
# project_id 为空：不限定清单
# --------------------------------------------------------------------------


@pytest.mark.parametrize("project_id", ["", "   "])
def test_empty_project_id_collects_every_readable_tasklist(collector, project_id):
    collector.stub(
        TASKLISTS_PATH,
        page([{"guid": "list-a"}, {"guid": "list-b"}]),
    )
    collector.stub(
        tasks_path("list-a"),
        page([task_item(guid="a-1", summary="a-1", completed_at=iso_ms(INSIDE))]),
    )
    collector.stub(
        tasks_path("list-b"),
        page([task_item(guid="b-1", summary="b-1", completed_at=iso_ms(INSIDE))]),
    )

    result = collect(project_id, WINDOW_START, WINDOW_END)

    assert result.success is True
    assert [record.title for record in result.records] == ["a-1", "b-1"]
    assert collector.paths_except_token() == [
        TASKLISTS_PATH,
        tasks_path("list-a"),
        tasks_path("list-b"),
    ]
    # 不限定清单时不走 tasklist 专属路径
    assert TASKLISTS_PATH in collector.paths()


def test_empty_project_id_with_no_readable_tasklist_is_success(collector):
    collector.stub(TASKLISTS_PATH, page([]))

    result = collect("", WINDOW_START, WINDOW_END)

    assert result.success is True
    assert result.records == []
    assert result.failed == []
    assert result.error is None


def test_tasklist_enumeration_failure_is_reported(collector):
    collector.stub(TASKLISTS_PATH, api_error(99991672, "permission denied"))

    result = collect("", WINDOW_START, WINDOW_END)

    assert result.success is False
    assert result.records == []
    assert result.failed == []
    assert result.error is not None
    assert "99991672" in result.error


# --------------------------------------------------------------------------
# CollectResult 语义（design.md §3.2 四行语义表）
# --------------------------------------------------------------------------


def test_success_with_data(collector):
    collector.stub(tasks_path(GUID), page([task_item(completed_at=iso_ms(INSIDE))]))

    result = collect(GUID, WINDOW_START, WINDOW_END)

    assert (result.success, len(result.records), result.failed, result.error) == (
        True,
        1,
        [],
        None,
    )


def test_success_without_data_is_not_a_failure(collector):
    """「今日无记录」与「采集失败」必须能区分（ADR-003）。"""
    collector.stub(tasks_path(GUID), page([task_item(completed_at="0")]))

    result = collect(GUID, WINDOW_START, WINDOW_END)

    assert result.success is True
    assert result.records == []
    assert result.failed == []
    assert result.error is None


def test_partial_failure_keeps_records_from_successful_tasklists(collector):
    collector.stub(TASKLISTS_PATH, page([{"guid": "list-ok"}, {"guid": "list-bad"}]))
    collector.stub(
        tasks_path("list-ok"),
        page([task_item(summary="ok-1", completed_at=iso_ms(INSIDE))]),
    )
    collector.stub(tasks_path("list-bad"), api_error(1470404, "tasklist not found"))

    result = collect("", WINDOW_START, WINDOW_END)

    assert result.source == "lark_task"
    assert result.success is False
    assert [record.title for record in result.records] == ["ok-1"]  # 不连坐
    assert result.failed == ["list-bad"]
    assert result.error is not None and "list-bad" in result.error


def test_all_tasklists_failed(collector):
    collector.stub(TASKLISTS_PATH, page([{"guid": "list-a"}, {"guid": "list-b"}]))
    collector.stub(tasks_path("list-a"), api_error(1470500, "server error"))
    collector.stub(tasks_path("list-b"), api_error(1470500, "server error"))

    result = collect("", WINDOW_START, WINDOW_END)

    assert result.success is False
    assert result.records == []
    assert result.failed == ["list-a", "list-b"]


def test_single_tasklist_failure_reports_its_guid(collector):
    collector.stub(tasks_path(GUID), api_error(1470404, "tasklist not found"))

    result = collect(GUID, WINDOW_START, WINDOW_END)

    assert result.success is False
    assert result.records == []
    assert result.failed == [GUID]
    assert result.error is not None
    assert "1470404" in result.error


# --------------------------------------------------------------------------
# 失败一律以返回值表达，不抛异常
# --------------------------------------------------------------------------


def test_timeout_retries_three_times_then_reports_failure(collector):
    collector.stub(tasks_path(GUID), httpx.ReadTimeout("timed out"))

    result = collect(GUID, WINDOW_START, WINDOW_END)

    assert result.success is False
    assert result.records == []
    assert result.failed == [GUID]
    assert result.error is not None and "超时" in result.error
    # 首次失败 + 重试 3 次
    assert len(collector.queries(tasks_path(GUID))) == 4


def test_timeout_then_success_recovers(collector):
    collector.stub(
        tasks_path(GUID),
        httpx.ReadTimeout("timed out"),
        page([task_item(summary="recovered", completed_at=iso_ms(INSIDE))]),
    )

    result = collect(GUID, WINDOW_START, WINDOW_END)

    assert result.success is True
    assert [record.title for record in result.records] == ["recovered"]
    assert len(collector.queries(tasks_path(GUID))) == 2


def test_http_error_becomes_failure(collector):
    collector.stub(tasks_path(GUID), httpx.Response(500, json={"msg": "boom"}))

    result = collect(GUID, WINDOW_START, WINDOW_END)  # 不抛异常

    assert result.success is False
    assert result.failed == [GUID]
    assert result.error is not None and "500" in result.error


def test_token_acquisition_failure_becomes_failure(feishu, monkeypatch):
    feishu.stub(
        TOKEN_PATH,
        httpx.Response(200, json={"code": 10003, "msg": "app not found"}),
    )
    feishu.stub(tasks_path(GUID), page([]))
    monkeypatch.setattr(lark_task, "_client", feishu.client())

    result = collect(GUID, WINDOW_START, WINDOW_END)  # 不抛异常

    assert result.success is False
    assert result.records == []
    assert result.error is not None and "10003" in result.error


def test_unexpected_error_becomes_failure(collector):
    collector.stub(tasks_path(GUID), ValueError("boom"))

    result = collect(GUID, WINDOW_START, WINDOW_END)  # 不抛异常

    assert result.success is False
    assert result.failed == [GUID]


def test_client_build_failure_becomes_failure(monkeypatch):
    def boom():
        raise RuntimeError("config.yaml 不存在")

    monkeypatch.setattr(lark_task, "_get_client", boom)

    result = collect(GUID, WINDOW_START, WINDOW_END)  # 不抛异常

    assert result.success is False
    assert result.records == []
    assert result.error is not None and "config.yaml" in result.error
