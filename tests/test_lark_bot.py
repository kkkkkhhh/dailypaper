"""飞书推送模块测试（design.md §4.3 / §6.1，tasks.md Task 8）。

全程使用 ``httpx.MockTransport``（参照 tests/test_lark_client.py 的 FakeFeishu）
构造成 `LarkClient` 注入到模块级 ``_client``，**绝不真的发消息、不打真实网络**。

三组重点断言，分别对应 Task 8 补充段的三条硬性要求：
1. 走**应用机器人**通道（``im/v1/messages`` + ``receive_id_type=chat_id``），
   而不是自定义机器人的 webhook
2. 令牌管理**复用** `LarkClient` —— 本模块内不存在换取令牌的代码路径
3. 失败**不抛异常**、**不触发告警**，只返回 False + ERROR 日志
"""

from __future__ import annotations

import inspect
import json
import logging
from datetime import date, datetime, timezone

import httpx
import pytest

import notifier.lark_bot as lark_bot
from collector.lark_client import LarkClient
from shared.models import DailyReport
from tests.conftest import VALID_CONFIG

TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
MESSAGES_PATH = "/open-apis/im/v1/messages"

CHAT_ID = "oc_test_group_0001"
MARKDOWN = (
    "# 研发一组 日报 · 2026-10-11\n"
    "\n"
    "## 代码提交\n"
    "\n"
    "### 张三\n"
    "\n"
    "- `owner/repo` 修复登录（+10 行 / -2 行，3 个文件）\n"
)
TEXT = "【告警】飞书推送失败：已重试 2 次"


# --------------------------------------------------------------------------
# 测试替身与夹具
# --------------------------------------------------------------------------


class FakeFeishu:
    """记录请求并按脚本作答的飞书服务端替身（参照 tests/test_lark_client.py）。

    ``script`` 逐次决定消息接口的行为：

    - ``dict`` → 以 HTTP 200 返回该 JSON
    - ``Exception`` → 抛出（模拟网络故障）
    - ``httpx.Response`` → 原样返回（模拟 HTTP 400 / 500 等）
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.tokens_issued = 0
        self.message_calls = 0
        self.script: list = []
        self.token_response: dict | None = None
        self.default: dict = {
            "code": 0,
            "msg": "success",
            "data": {"message_id": "om_test"},
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == TOKEN_PATH:
            self.tokens_issued += 1
            return httpx.Response(
                200,
                json=self.token_response
                or {
                    "code": 0,
                    "tenant_access_token": f"t-{self.tokens_issued}",
                    "expire": 7200,
                },
            )

        if path == MESSAGES_PATH:
            self.message_calls += 1
            step = self.script.pop(0) if self.script else self.default
            if isinstance(step, Exception):
                raise step
            if isinstance(step, httpx.Response):
                return step
            return httpx.Response(200, json=step)

        return httpx.Response(200, json={"code": 0, "data": {}})

    def client(self) -> LarkClient:
        return LarkClient(
            "cli_test", "secret_test", transport=httpx.MockTransport(self.handler)
        )

    def message_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == MESSAGES_PATH]

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


@pytest.fixture
def fake() -> FakeFeishu:
    return FakeFeishu()


@pytest.fixture
def bot(fake, monkeypatch) -> FakeFeishu:
    """把 FakeFeishu 的客户端注入模块，并把重试间隔置 0（不真等待）。

    注入走模块级 ``_client`` 而非函数参数：design.md §4.3 的签名
    ``send(report, chat_id)`` / ``send_text(text, chat_id)`` 不接收 client 参数。
    """
    monkeypatch.setattr(lark_bot, "_client", fake.client())
    monkeypatch.setattr(lark_bot, "_RETRY_INTERVAL_SECONDS", 0.0)
    return fake


def make_report(markdown: str = MARKDOWN) -> DailyReport:
    """构造最小合法日报（字段以 shared/models.py 的 8 字段定义为准）。"""
    return DailyReport(
        date=date(2026, 10, 11),
        team_name="研发一组",
        members=[],
        failed_sources=[],
        failed_details={},
        generated_at=datetime(2026, 10, 11, 18, 0, tzinfo=timezone.utc),
        markdown=markdown,
        html="<p>日报</p>",
    )


def body_of(request: httpx.Request) -> dict:
    """取出请求体（content 是 JSON 字符串，与线上形状一致）。"""
    return json.loads(request.content)


def content_of(request: httpx.Request) -> dict:
    return json.loads(body_of(request)["content"])


def logged_fields(caplog) -> list[dict]:
    return [getattr(record, "fields", {}) for record in caplog.records]


# --------------------------------------------------------------------------
# 通道与请求形状
# --------------------------------------------------------------------------


def test_send_goes_to_app_bot_message_endpoint(bot):
    """应用机器人通道：POST /open-apis/im/v1/messages?receive_id_type=chat_id。"""
    assert lark_bot.send(make_report(), CHAT_ID) is True

    request = bot.message_requests()[0]
    assert request.method == "POST"
    assert request.url.path == MESSAGES_PATH
    assert request.url.params["receive_id_type"] == "chat_id"


def test_send_does_not_use_custom_bot_webhook(bot):
    """契约入参是 chat_id → 不走自定义机器人 webhook（design.md §4.3）。"""
    lark_bot.send(make_report(), CHAT_ID)

    url = str(bot.message_requests()[0].url)
    assert "/open-apis/im/v1/messages" in url
    assert "/bot/v2/hook/" not in url  # 自定义机器人的 webhook 地址


def test_send_targets_the_given_chat_id(bot):
    lark_bot.send(make_report(), CHAT_ID)

    assert body_of(bot.message_requests()[0])["receive_id"] == CHAT_ID


def test_send_ships_report_markdown_in_post_md_tag(bot):
    """日报以 Markdown 送达：post 富文本的 md 标签，内容即 report.markdown。"""
    lark_bot.send(make_report(), CHAT_ID)

    body = body_of(bot.message_requests()[0])
    assert body["msg_type"] == "post"
    md = content_of(bot.message_requests()[0])["zh_cn"]["content"][0][0]
    assert md["tag"] == "md"
    assert md["text"] == MARKDOWN


def test_send_text_sends_plain_text(bot):
    """send_text 走 text 消息类型 —— 告警读的是纯文本，不需渲染 Markdown。"""
    assert lark_bot.send_text(TEXT, CHAT_ID) is True

    request = bot.message_requests()[0]
    assert body_of(request)["msg_type"] == "text"
    assert content_of(request) == {"text": TEXT}


def test_send_text_uses_same_channel_and_chat(bot):
    lark_bot.send_text(TEXT, CHAT_ID)

    request = bot.message_requests()[0]
    assert request.method == "POST"
    assert request.url.path == MESSAGES_PATH
    assert request.url.params["receive_id_type"] == "chat_id"
    assert body_of(request)["receive_id"] == CHAT_ID


def test_success_is_logged_with_push_result(bot, caplog):
    """design.md §6.3 要求日志含推送结果。"""
    with caplog.at_level(logging.INFO, logger="notifier.lark_bot"):
        assert lark_bot.send(make_report(), CHAT_ID) is True

    assert any("成功" in record.getMessage() for record in caplog.records)
    assert any(fields.get("message_id") == "om_test" for fields in logged_fields(caplog))


# --------------------------------------------------------------------------
# 失败重试（design.md §6.1）
# --------------------------------------------------------------------------


def test_transient_failure_retries_twice_then_returns_false(bot, caplog):
    """重试 2 次 = 初试 + 2 次重试 = 最多 3 次请求；最终 False + ERROR 日志。"""
    bot.script = [httpx.ConnectError("connection refused")] * 3

    with caplog.at_level(logging.ERROR, logger="notifier.lark_bot"):
        assert lark_bot.send(make_report(), CHAT_ID) is False

    assert bot.message_calls == 3
    assert any(record.levelno >= logging.ERROR for record in caplog.records)
    assert any("仍不成功" in record.getMessage() for record in caplog.records)


def test_retry_recovers_when_a_later_attempt_succeeds(bot):
    """重试不是走过场：第 2 次成功即返回 True，不再多打请求。"""
    bot.script = [httpx.ReadTimeout("slow"), bot.default]

    assert lark_bot.send(make_report(), CHAT_ID) is True
    assert bot.message_calls == 2


def test_http_5xx_is_transient_and_retried(bot):
    """5xx 是服务端故障 → 属瞬时错误，即使响应体带 code 也要重试。"""
    bot.script = [httpx.Response(500, json={"code": 500, "msg": "internal"})] * 3

    assert lark_bot.send(make_report(), CHAT_ID) is False
    assert bot.message_calls == 3


def test_business_error_on_http_200_is_not_retried(bot, caplog):
    """230002 机器人不在群 —— 重试解决不了（design.md §6.1）。"""
    bot.script = [{"code": 230002, "msg": "The bot can not be outside the group."}]

    with caplog.at_level(logging.ERROR, logger="notifier.lark_bot"):
        assert lark_bot.send(make_report(), CHAT_ID) is False

    assert bot.message_calls == 1
    assert any(fields.get("code") == 230002 for fields in logged_fields(caplog))


def test_business_error_carried_by_http_400_is_not_retried(bot):
    """真实世界里 230002 走 HTTP 400 + 响应体带 code —— 同样只调 1 次。"""
    bot.script = [
        httpx.Response(400, json={"code": 230027, "msg": "Lack of necessary permissions."})
    ]

    assert lark_bot.send_text(TEXT, CHAT_ID) is False
    assert bot.message_calls == 1


# --------------------------------------------------------------------------
# 频率限制是业务码里的例外（design.md §6.1）
# --------------------------------------------------------------------------


def test_rate_limit_code_is_retried(bot):
    """230020 触发频率限制 —— 虽走 code != 0，但等待后可恢复，按瞬时故障处理。

    若像 230002 那样一次就放弃，一次偶发限流就会让当天日报彻底发不出去。
    """
    # script 是逐次 pop 的，要模拟「一直限流」就得把每次应答都排上
    bot.script = [{"code": 230020, "msg": "triggered rate limit"}] * 3

    assert lark_bot.send(make_report(), CHAT_ID) is False
    assert bot.message_calls == 3  # 初试 + 2 次重试


def test_rate_limit_recovers_on_retry(bot):
    """限流后重试成功 —— 这正是把它当可恢复错误的收益。"""
    bot.script = [
        {"code": 230020, "msg": "triggered rate limit"},
        {"code": 0, "msg": "success", "data": {"message_id": "om_ok"}},
    ]

    assert lark_bot.send(make_report(), CHAT_ID) is True
    assert bot.message_calls == 2


def test_rate_limit_carried_by_http_400_is_also_retried(bot):
    """限流同样可能以 HTTP 400 形态出现，不能因为状态码就改判。"""
    bot.script = [
        httpx.Response(400, json={"code": 230020, "msg": "rate limit"})
        for _ in range(3)
    ]

    assert lark_bot.send_text(TEXT, CHAT_ID) is False
    assert bot.message_calls == 3


def test_rate_limit_exhaustion_is_logged_as_retry_failure(bot, caplog):
    bot.script = [{"code": 230020, "msg": "rate limit"}] * 3

    with caplog.at_level(logging.ERROR, logger="notifier.lark_bot"):
        lark_bot.send_text(TEXT, CHAT_ID)

    messages = [record.getMessage() for record in caplog.records]
    assert any("重试" in m and "仍不成功" in m for m in messages)


# --------------------------------------------------------------------------
# 令牌通道：走 LarkClient 的公开 post()，不用私有方法
# --------------------------------------------------------------------------


def test_message_is_sent_through_public_post_not_private_request():
    """推送层不该依赖 LarkClient 的私有 `_request`。

    私有方法被外部当成公开接口用，等于把内部实现冻结成了契约 ——
    将来 LarkClient 重构就会被无辜破坏。这条测试钉住这个边界。
    """
    import ast
    from pathlib import Path

    source = Path(lark_bot.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    private_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith("_")
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"client", "lark_client"}
    ]

    assert private_calls == [], (
        "推送层调用了 LarkClient 的私有方法："
        f"{[n.func.attr for n in private_calls]}"
    )


# --------------------------------------------------------------------------
# 边界：空 chat_id、网络异常、令牌失败 —— 一律返回 False 不抛
# --------------------------------------------------------------------------


def test_empty_chat_id_returns_false_without_any_request(bot, caplog):
    with caplog.at_level(logging.ERROR, logger="notifier.lark_bot"):
        assert lark_bot.send(make_report(), "") is False

    assert bot.message_calls == 0
    assert bot.requests == []  # 连令牌请求都不该发生
    assert any("chat_id" in record.getMessage() for record in caplog.records)


def test_whitespace_chat_id_returns_false_without_any_request(bot):
    assert lark_bot.send_text(TEXT, "   ") is False
    assert bot.requests == []


def test_network_error_returns_false_without_raising(bot):
    """断言本身就是「没有异常逃逸到调用方」（design.md §4.3）。"""
    bot.script = [httpx.ConnectError("boom")] * 3

    assert lark_bot.send_text(TEXT, CHAT_ID) is False


def test_token_exchange_failure_returns_false(bot):
    """LarkClient 换取令牌失败（RuntimeError）也收敛为 False，不向上抛。"""
    bot.token_response = {"code": 10003, "msg": "app not found"}

    assert lark_bot.send(make_report(), CHAT_ID) is False
    assert bot.message_calls == 0  # 没拿到令牌，消息请求根本不该发出


# --------------------------------------------------------------------------
# 分层：复用 LarkClient、不触发告警、签名符契约
# --------------------------------------------------------------------------


def test_message_request_is_authenticated_by_lark_client(bot):
    """消息请求头里的令牌由 LarkClient 换取并附加 —— 本模块不自己拼鉴权。"""
    lark_bot.send(make_report(), CHAT_ID)

    assert bot.message_requests()[0].headers["authorization"] == "Bearer t-1"
    # 鉴权接口只被 LarkClient 访问一次（换取后缓存），本模块没有第二条令牌路径
    assert bot.paths().count(TOKEN_PATH) == 1


def test_token_is_fetched_once_across_send_and_send_text(bot):
    """复用同一 LarkClient 实例 → 令牌缓存跨调用生效。"""
    assert lark_bot.send(make_report(), CHAT_ID) is True
    assert lark_bot.send_text(TEXT, CHAT_ID) is True

    assert bot.tokens_issued == 1
    assert bot.message_calls == 2


def test_module_has_no_token_exchange_code():
    """本模块内不存在换取 / 刷新令牌的代码路径（Task 8 硬约束）。"""
    source = inspect.getsource(lark_bot)

    assert "/open-apis/auth" not in source
    assert "tenant_access_token" not in source
    assert "httpx.Client" not in source  # 不另起一套 HTTP 客户端
    assert lark_bot.LarkClient is LarkClient  # 用的就是 collector 那一份


def test_module_never_triggers_alerts():
    """推送层只如实返回成败；是否告警由编排层决定（design.md §4.3）。"""
    source = inspect.getsource(lark_bot)

    assert "report_alert" not in source
    assert "notifier.alert" not in source
    assert "notifier.email" not in source


def test_signatures_match_design_section_4_3():
    assert list(inspect.signature(lark_bot.send).parameters) == ["report", "chat_id"]
    assert list(inspect.signature(lark_bot.send_text).parameters) == ["text", "chat_id"]


def test_get_client_builds_from_shared_config(env_set, tmp_path, monkeypatch):
    """未注入时按 shared.config 的凭证惰性构造并复用（不读裸环境变量）。"""
    monkeypatch.setattr(lark_bot, "_client", None)
    (tmp_path / "config.yaml").write_text(VALID_CONFIG, encoding="utf-8")

    client = lark_bot._get_client()

    assert isinstance(client, LarkClient)
    assert lark_bot._get_client() is client  # 惰性构造后复用同一实例
    client.close()
    # monkeypatch 收尾时会把 _client 复原为 None，不把真实客户端留给后续测试
