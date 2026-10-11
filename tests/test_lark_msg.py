"""飞书消息采集模块测试（design.md §3.1 / §3.2 / §4.1 / §6.2 / §6.5，tasks.md Task 5）。

全程使用 httpx.MockTransport（参照 tests/test_lark_client.py 的 FakeFeishu）
与临时目录里的 config.yaml，**不打真实网络**。

安全红线单测（test_sensitive_message_is_dropped_and_never_logged）是本文件的
重点：它同时断言「不进返回值」与「不进日志」，缺一不可。
"""

from __future__ import annotations

import inspect
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import yaml

import collector.lark_msg as lark_msg
from collector.lark_client import LarkClient
from collector.lark_msg import collect
from shared.models import MessageRecord
from tests.conftest import VALID_CONFIG

TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
MESSAGES_PATH = "/open-apis/im/v1/messages"

CHAT_ID = "oc_test_group_0001"
BOT_OPEN_ID = "ou_bot_000000000000000000000000000000"
SENDER_ALICE = "ou_alice_1111111111111111111111111111"
SENDER_BOB = "ou_bob_22222222222222222222222222222222"

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai 固定偏移（本机无 tzdata）
SINCE = datetime(2026, 10, 11, 0, 0, tzinfo=TZ)
UNTIL = datetime(2026, 10, 11, 18, 0, tzinfo=TZ)
MESSAGE_TIME = datetime(2026, 10, 11, 9, 30, tzinfo=TZ)
KEYWORDS = ["项目", "需求"]


# --------------------------------------------------------------------------
# 测试替身与夹具
# --------------------------------------------------------------------------


class FakeFeishu:
    """记录请求并按脚本作答的飞书服务端替身（参照 tests/test_lark_client.py）。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.tokens_issued = 0
        self.page_payloads: list[dict] = []  # 消息接口逐次返回的响应体
        self.chat_payload: dict | None = None  # 群信息接口的响应体
        self.failure: Exception | None = None  # 非 None 时消息接口抛该异常（模拟超时）
        self.fail_message_calls_after: int | None = None
        self.message_http_status: int | None = None  # 非 None 时消息接口返回该状态码
        self.message_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

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

        if path == MESSAGES_PATH:
            self.message_calls += 1
            if self.message_http_status is not None:
                return httpx.Response(self.message_http_status, json={"code": 0})
            if self.failure is not None and (
                self.fail_message_calls_after is None
                or self.message_calls > self.fail_message_calls_after
            ):
                raise self.failure
            payload = (
                self.page_payloads.pop(0)
                if self.page_payloads
                else {"code": 0, "data": {"items": [], "has_more": False}}
            )
            return httpx.Response(200, json=payload)

        if path.startswith("/open-apis/im/v1/chats/"):
            return httpx.Response(
                200, json=self.chat_payload or {"code": 0, "data": {}}
            )

        return httpx.Response(200, json={"code": 0, "data": {}})

    def client(self) -> LarkClient:
        return LarkClient(
            "cli_test", "secret_test", transport=httpx.MockTransport(self.handler)
        )

    def message_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == MESSAGES_PATH]


@pytest.fixture
def fake() -> FakeFeishu:
    return FakeFeishu()


@pytest.fixture
def msg_env(env_set, tmp_path, monkeypatch):
    """隔离环境（临时目录 + 凭证环境变量）+ 完整 config.yaml + 机器人 open_id。

    配置走真实的 shared.config.load()，顺带覆盖「敏感词与机器人 open_id
    从配置进入采集」这条链路，而不是把配置对象 mock 掉。
    """
    data = yaml.safe_load(VALID_CONFIG)
    data["filters"]["keywords"] = list(KEYWORDS)
    data["filters"]["sensitive_keywords"] = ["薪资", "绩效", "裁员"]
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setenv("FEISHU_BOT_OPEN_ID", BOT_OPEN_ID)
    monkeypatch.setattr(lark_msg, "_RETRY_INTERVAL_SECONDS", 0.0)  # 重试不真等待
    return tmp_path


def page(items: list[dict], *, has_more: bool = False, page_token: str | None = None) -> dict:
    data: dict = {"items": items, "has_more": has_more}
    if page_token:
        data["page_token"] = page_token
    return {"code": 0, "data": data}


def raw_item(
    content_json: str,
    *,
    msg_type: str = "text",
    sender: str = SENDER_ALICE,
    sender_type: str = "user",
    when: datetime = MESSAGE_TIME,
    deleted: bool = False,
) -> dict:
    """构造官方文档形状的 messages item。"""
    stamp = str(int(when.timestamp() * 1000))  # 响应体里的时间戳是毫秒字符串
    return {
        "message_id": "om_test_0001",
        "msg_type": msg_type,
        "create_time": stamp,
        "update_time": stamp,
        "deleted": deleted,
        "chat_id": CHAT_ID,
        "sender": {"id": sender, "id_type": "open_id", "sender_type": sender_type},
        "body": {"content": content_json},
    }


def text_message(
    content: str,
    *,
    sender: str = SENDER_ALICE,
    sender_type: str = "user",
    when: datetime = MESSAGE_TIME,
    deleted: bool = False,
) -> dict:
    return raw_item(
        json.dumps({"text": content}, ensure_ascii=False),
        sender=sender,
        sender_type=sender_type,
        when=when,
        deleted=deleted,
    )


def run_collect(fake: FakeFeishu, *, keywords=None, since=SINCE, until=UNTIL, **kwargs):
    return collect(
        CHAT_ID,
        list(KEYWORDS if keywords is None else keywords),
        since,
        until,
        client=fake.client(),
        **kwargs,
    )


def log_blob(caplog) -> str:
    """把捕获到的日志（消息 + 结构化字段 + 异常）拼成可断言的一整块文本。"""
    parts: list[str] = []
    for record in caplog.records:
        parts.append(record.getMessage())
        fields = getattr(record, "fields", None)
        if fields is not None:
            parts.append(json.dumps(fields, ensure_ascii=False, default=str))
        if record.exc_info:
            parts.append(logging.Formatter().formatException(record.exc_info))
    return "\n".join(parts)


# --------------------------------------------------------------------------
# 接口契约与正常路径
# --------------------------------------------------------------------------


def test_collect_signature_matches_design_contract():
    """design.md §4.1：四个入参的名称、类型、顺序必须完全一致。"""
    sig = inspect.signature(collect)
    names = list(sig.parameters)

    assert names[:4] == ["chat_id", "keywords", "since", "until"]
    assert all(
        sig.parameters[name].default is inspect.Parameter.empty for name in names[:4]
    )
    assert sig.parameters["chat_id"].annotation == "str"
    assert sig.parameters["keywords"].annotation == "list[str]"
    assert sig.parameters["since"].annotation == "datetime"
    assert sig.parameters["until"].annotation == "datetime"
    assert "CollectResult" in sig.return_annotation
    # 注入点必须是仅关键字可选参数，不破坏四参调用的兼容性
    assert all(
        sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        for name in names[4:]
    )


def test_happy_path_returns_complete_collect_result(msg_env, fake):
    fake.chat_payload = {"code": 0, "data": {"name": "研发一组日报群"}}
    fake.page_payloads = [
        page(
            [
                text_message("项目 A 的进度更新"),
                text_message("需求评审下午三点开始", sender=SENDER_BOB),
            ]
        )
    ]

    result = run_collect(fake)

    # CollectResult 全部 5 个字段（tasks.md Task 5 补充段）
    assert result.source == "lark_msg"
    assert result.success is True
    assert result.error is None
    assert result.failed == []

    # 每个 MessageRecord 含全部 4 个字段且类型正确
    assert len(result.records) == 2
    first = result.records[0]
    assert isinstance(first, MessageRecord)
    assert isinstance(first.sender, str) and first.sender == SENDER_ALICE
    assert isinstance(first.content, str) and first.content == "项目 A 的进度更新"
    assert isinstance(first.timestamp, datetime) and first.timestamp.tzinfo is not None
    assert isinstance(first.chat_name, str) and first.chat_name == "研发一组日报群"
    assert result.records[1].sender == SENDER_BOB


def test_empty_items_is_success_not_failure(msg_env, fake):
    """成功但无数据 ≠ 失败（design.md §3.2：这决定日报出「今日无记录」）。"""
    fake.page_payloads = [page([])]

    result = run_collect(fake)

    assert result.source == "lark_msg"
    assert result.success is True
    assert result.records == []
    assert result.error is None
    assert result.failed == []


def test_keyword_filter_keeps_only_matching_messages(msg_env, fake):
    fake.page_payloads = [
        page(
            [
                text_message("项目进度更新"),
                text_message("需求排期确认"),
                text_message("下午茶改到三点"),
            ]
        )
    ]

    result = run_collect(fake)

    assert [r.content for r in result.records] == ["项目进度更新", "需求排期确认"]


# --------------------------------------------------------------------------
# 安全红线：敏感词过滤
# --------------------------------------------------------------------------


def test_sensitive_message_is_dropped_and_never_logged(msg_env, fake, caplog):
    """敏感内容既不进返回值，也不进日志（design.md §6.2「2. 数据安全」）。

    构造一条**同时命中关键词与敏感词**的消息：若过滤顺序颠倒（先关键词后
    敏感词），它仍会被丢弃 —— 所以额外断言日志里的 sensitive_dropped == 1，
    以此证明它确实被敏感词层拦下，而不是碰巧没命中关键词。
    """
    caplog.set_level(logging.INFO, logger="collector.lark_msg")
    marker = "绩效评估结果与薪资调整方案已发群"
    fake.page_payloads = [page([text_message(marker), text_message("项目进度更新")])]

    result = run_collect(fake)

    # 不进返回值
    assert [r.content for r in result.records] == ["项目进度更新"]
    assert all(marker not in r.content for r in result.records)

    # 不进日志：消息正文、结构化字段、异常文本一律不得出现敏感内容
    blob = log_blob(caplog)
    assert "薪资" not in blob
    assert "绩效" not in blob
    assert "裁员" not in blob
    assert marker not in blob

    # 日志只允许出现条数
    fields = [getattr(record, "fields", {}) for record in caplog.records]
    assert any(f.get("sensitive_dropped") == 1 for f in fields)


@pytest.mark.parametrize("sensitive", ["薪资", "绩效", "裁员"])
def test_each_configured_sensitive_keyword_is_dropped(msg_env, fake, sensitive):
    fake.page_payloads = [
        page([text_message(f"项目讨论：{sensitive}相关话题"), text_message("项目正常内容")])
    ]

    result = run_collect(fake)

    assert [r.content for r in result.records] == ["项目正常内容"]


def test_sensitive_keywords_are_case_insensitive(msg_env, fake):
    fake.page_payloads = [page([text_message("项目 SALARY 讨论"), text_message("项目正常内容")])]

    # 把大小写不同的词写进配置，验证漏杀防护
    config_path = Path("config.yaml")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["filters"]["sensitive_keywords"] = ["salary"]
    config_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    result = run_collect(fake)

    assert [r.content for r in result.records] == ["项目正常内容"]


# --------------------------------------------------------------------------
# 机器人自身消息排除
# --------------------------------------------------------------------------


def test_bot_messages_excluded(msg_env, fake):
    """机器人自己发的消息被排除 —— 否则前一日推送的日报会被回采成今日讨论。"""
    fake.page_payloads = [
        page(
            [
                text_message("项目日报推送", sender=BOT_OPEN_ID),
                text_message("项目来自应用", sender="cli_9f427eec54ae901b", sender_type="app"),
                text_message("项目正常人发言", sender=SENDER_ALICE),
            ]
        )
    ]

    result = run_collect(fake)

    assert [r.content for r in result.records] == ["项目正常人发言"]


def test_without_bot_open_id_configured_app_messages_still_excluded(
    msg_env, fake, monkeypatch
):
    """未配置 FEISHU_BOT_OPEN_ID 时至少还要挡住应用身份的消息。"""
    monkeypatch.delenv("FEISHU_BOT_OPEN_ID")
    fake.page_payloads = [
        page(
            [
                text_message("项目应用消息", sender="cli_9f427eec54ae901b", sender_type="app"),
                text_message("项目用户消息", sender=SENDER_ALICE),
            ]
        )
    ]

    result = run_collect(fake)

    assert [r.content for r in result.records] == ["项目用户消息"]


# --------------------------------------------------------------------------
# 时间处理（design.md §6.5）
# --------------------------------------------------------------------------


def test_millisecond_timestamp_normalized_to_window_timezone(msg_env, fake):
    """毫秒级 Unix 时间戳字符串 → tz-aware datetime，按 UTC 解析后转 since.tzinfo。"""
    fake.page_payloads = [
        page([raw_item('{"text":"项目历史记录"}', when=datetime(2021, 3, 10, 20, 49, tzinfo=TZ))])
    ]
    item = fake.page_payloads[0]["data"]["items"][0]
    item["create_time"] = "1615380573411"  # 官方文档示例值，毫秒

    result = run_collect(
        fake,
        since=datetime(2021, 3, 10, 0, 0, tzinfo=TZ),
        until=datetime(2021, 3, 10, 23, 59, 59, tzinfo=TZ),
    )

    ts = result.records[0].timestamp
    assert ts == datetime(2021, 3, 10, 20, 49, 33, 411000, tzinfo=TZ)
    assert ts.tzinfo is not None
    assert ts.utcoffset() == timedelta(hours=8)
    assert ts.microsecond == 411000  # 毫秒位不丢精度


def test_timestamp_timezone_follows_window_not_config(msg_env, fake, monkeypatch):
    """归一化时区取自 since.tzinfo —— 窗口给 UTC 就回 UTC，不反向读 config。"""
    monkeypatch.delenv("FEISHU_BOT_OPEN_ID")
    fake.page_payloads = [page([text_message("项目进度", when=datetime(2026, 10, 11, 1, 30, tzinfo=timezone.utc))])]

    result = run_collect(
        fake,
        since=datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc),
        until=datetime(2026, 10, 11, 18, 0, tzinfo=timezone.utc),
    )

    ts = result.records[0].timestamp
    assert ts.utcoffset() == timedelta(0)
    assert ts == datetime(2026, 10, 11, 1, 30, tzinfo=timezone.utc)


def test_request_uses_second_level_time_window(msg_env, fake):
    """查询参数 start_time/end_time 是**秒级**时间戳（官方文档核实）。"""
    fake.page_payloads = [page([])]

    run_collect(fake)

    params = dict(fake.message_requests()[0].url.params)
    assert params["container_id_type"] == "chat"
    assert params["container_id"] == CHAT_ID
    assert params["start_time"] == str(int(SINCE.timestamp()))
    assert params["end_time"] == str(int(UNTIL.timestamp()))
    assert params["sort_type"] == "ByCreateTimeAsc"
    assert params["page_size"] == "50"


def test_messages_outside_window_are_dropped(msg_env, fake):
    """服务端按秒过滤，边界上可能多带消息 —— 客户端兜底再校验一次。"""
    fake.page_payloads = [
        page(
            [
                text_message("项目昨日消息", when=MESSAGE_TIME - timedelta(days=1)),
                text_message("项目明日消息", when=MESSAGE_TIME + timedelta(days=1)),
                text_message("项目今日消息"),
            ]
        )
    ]

    result = run_collect(fake)

    assert [r.content for r in result.records] == ["项目今日消息"]


def test_naive_datetime_rejected_without_network(env_set, fake):
    result = collect(
        CHAT_ID,
        KEYWORDS,
        datetime(2026, 10, 11, 0, 0),
        datetime(2026, 10, 11, 18, 0),
        client=fake.client(),
    )

    assert result.success is False
    assert "tz-aware" in (result.error or "")
    assert fake.requests == []


# --------------------------------------------------------------------------
# 分页、消息类型与群名
# --------------------------------------------------------------------------


def test_pagination_follows_page_token(msg_env, fake):
    fake.page_payloads = [
        page([text_message("项目第一页")], has_more=True, page_token="tok-2"),
        page([text_message("项目第二页")]),
    ]

    result = run_collect(fake)

    assert [r.content for r in result.records] == ["项目第一页", "项目第二页"]
    requests = fake.message_requests()
    assert len(requests) == 2
    assert dict(requests[1].url.params)["page_token"] == "tok-2"


def test_deleted_and_non_text_messages_skipped(msg_env, fake):
    """撤回消息与无纯文本可采的消息合理跳过（MessageRecord.content 是纯文本）。"""
    fake.page_payloads = [
        page(
            [
                text_message("项目已撤回", deleted=True),
                raw_item(json.dumps({"image_key": "img_1"}), msg_type="image"),
                raw_item(
                    json.dumps({"file_key": "file_1", "file_name": "报告.zip"}),
                    msg_type="file",
                ),
                raw_item(json.dumps({"elements": []}), msg_type="interactive"),
                raw_item(json.dumps({"content": "{\"text\":\"x\"}"}), msg_type="system"),
                text_message("项目正常文本"),
            ]
        )
    ]

    result = run_collect(fake)

    assert [r.content for r in result.records] == ["项目正常文本"]


def test_post_richtext_is_converted_to_plain_text(msg_env, fake):
    post = {
        "title": "项目周报",
        "content": [
            [
                {"tag": "text", "text": "本周完成需求评审"},
                {"tag": "img", "image_key": "img_1"},
            ]
        ],
    }
    fake.page_payloads = [
        page([raw_item(json.dumps(post, ensure_ascii=False), msg_type="post")])
    ]

    result = run_collect(fake)

    assert len(result.records) == 1
    assert "项目周报" in result.records[0].content
    assert "本周完成需求评审" in result.records[0].content


def test_chat_name_falls_back_to_chat_id_when_lookup_fails(msg_env, fake):
    """群信息查不到（如缺 im:chat 权限）不影响消息采集本身。"""
    fake.chat_payload = {"code": 230027, "msg": "Lack of necessary permissions."}
    fake.page_payloads = [page([text_message("项目进度")])]

    result = run_collect(fake)

    assert result.success is True
    assert result.records[0].chat_name == CHAT_ID


# --------------------------------------------------------------------------
# 令牌复用与失败分支（不抛异常）
# --------------------------------------------------------------------------


def test_reuses_lark_client_token_management(msg_env, fake):
    """令牌由 LarkClient 统一管理：本模块只带着 Bearer 头调用，不碰令牌逻辑。"""
    fake.page_payloads = [page([text_message("项目进度")])]

    result = run_collect(fake)

    paths = [r.url.path for r in fake.requests]
    assert TOKEN_PATH in paths
    assert fake.tokens_issued == 1
    assert result.success is True
    assert all(
        r.headers.get("authorization") == "Bearer t-1"
        for r in fake.message_requests()
    )


def test_timeout_retries_three_times_then_fails_without_raising(msg_env, fake):
    """超时重试 3 次 = 首次之外最多再试 3 次；仍失败则以返回值表达。"""
    fake.failure = httpx.ReadTimeout("read timeout")

    result = run_collect(fake)

    assert result.success is False
    assert result.records == []
    assert result.failed == [CHAT_ID]
    assert "超时" in (result.error or "")
    assert fake.message_calls == 4  # 首次 + 3 次重试


def test_api_error_code_returns_failure(msg_env, fake):
    """业务错误码（如机器人不在群 230002）重试无意义，直接如实返回。"""
    fake.page_payloads = [{"code": 230002, "msg": "The bot can not be outside the group."}]

    result = run_collect(fake)

    assert result.success is False
    assert result.records == []
    assert result.failed == [CHAT_ID]
    assert "230002" in (result.error or "")
    assert fake.message_calls == 1  # 不重试


def test_http_error_returns_failure(msg_env, fake):
    fake.message_http_status = 500

    result = run_collect(fake)

    assert result.success is False
    assert result.records == []
    assert result.failed == [CHAT_ID]
    assert "请求失败" in (result.error or "")
    assert fake.message_calls == 1  # HTTP 错误不重试


def test_partial_pagination_failure_keeps_fetched_records(msg_env, fake):
    """第二页失败时保留第一页的记录（design.md §3.2 部分失败语义）。"""
    fake.page_payloads = [
        page([text_message("项目第一页")], has_more=True, page_token="tok-2")
    ]
    fake.failure = httpx.ReadTimeout("timeout")
    fake.fail_message_calls_after = 1

    result = run_collect(fake)

    assert result.success is False
    assert [r.content for r in result.records] == ["项目第一页"]
    assert result.failed == [CHAT_ID]
    assert fake.message_calls == 5  # 成功 1 次 + 第二页 1 次 + 重试 3 次


def test_missing_config_returns_failure_without_raising(env_set, fake):
    """config.yaml 缺失也不抛异常 —— 失败以返回值表达（ADR-003）。"""
    result = collect(CHAT_ID, KEYWORDS, SINCE, UNTIL, client=fake.client())

    assert result.success is False
    assert result.records == []
    assert result.failed == [CHAT_ID]
    assert "配置" in (result.error or "")
