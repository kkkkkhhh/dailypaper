"""飞书 API 客户端测试（design.md §4.1，tasks.md Task 2.5）。

全程使用 httpx.MockTransport，不打真实网络。
"""

from __future__ import annotations

import httpx
import pytest

from collector.lark_client import BASE_URL, LarkClient

TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
BATCH_PATH = "/open-apis/contact/v3/users/batch_get_id"


class FakeFeishu:
    """记录请求并按脚本作答的飞书服务端替身。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.tokens_issued = 0
        self.token_response: dict = {}
        self.route_handler = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)

        if request.url.path == TOKEN_PATH:
            self.tokens_issued += 1
            token = self.token_response or {
                "code": 0,
                "tenant_access_token": f"t-{self.tokens_issued}",
                "expire": 7200,
            }
            return httpx.Response(200, json=token)

        if self.route_handler is not None:
            return self.route_handler(request)

        return httpx.Response(200, json={"code": 0, "data": {}})

    def client(self, **kwargs) -> LarkClient:
        return LarkClient(
            "cli_test",
            "secret_test",
            transport=httpx.MockTransport(self.handler),
            **kwargs,
        )

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def auth_headers(self) -> list[str | None]:
        return [request.headers.get("authorization") for request in self.requests]


@pytest.fixture
def feishu() -> FakeFeishu:
    return FakeFeishu()


# --------------------------------------------------------------------------
# 令牌获取与缓存
# --------------------------------------------------------------------------


def test_token_fetched_on_first_use(feishu):
    client = feishu.client()

    assert client.tenant_token() == "t-1"
    assert feishu.paths() == [TOKEN_PATH]


def test_token_request_carries_credentials(feishu):
    client = feishu.client()
    client.tenant_token()

    request = feishu.requests[0]
    assert request.method == "POST"
    assert b"cli_test" in request.content
    assert b"secret_test" in request.content


def test_token_is_cached_within_validity(feishu):
    """令牌在有效期内被复用 —— 重复换取会浪费配额，也可能触发限流。"""
    client = feishu.client()

    for _ in range(5):
        client.tenant_token()

    assert feishu.tokens_issued == 1


def test_expired_token_is_refetched(feishu):
    client = feishu.client()
    client.tenant_token()

    client._token_expires_at = 0.0  # 模拟过期

    assert client.tenant_token() == "t-2"
    assert feishu.tokens_issued == 2


def test_fetch_failure_raises_with_message(feishu):
    feishu.token_response = {"code": 10003, "msg": "app not found"}
    client = feishu.client()

    with pytest.raises(RuntimeError) as exc:
        client.tenant_token()

    assert "10003" in str(exc.value)


# --------------------------------------------------------------------------
# GET 请求：令牌注入与自动刷新
# --------------------------------------------------------------------------


def test_get_attaches_bearer_token(feishu):
    client = feishu.client()

    client.get("/open-apis/im/v1/chats")

    assert feishu.auth_headers()[-1] == "Bearer t-1"


def test_get_passes_params(feishu):
    client = feishu.client()

    client.get("/open-apis/im/v1/chats", params={"page_size": 50})

    assert "page_size=50" in str(feishu.requests[-1].url)


def test_invalid_token_triggers_refresh_and_retry_once(feishu):
    """令牌失效错误码 → 刷新后重试一次，且只重试一次。"""
    calls = {"n": 0}

    def route(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"code": 99991663, "msg": "token invalid"})
        return httpx.Response(200, json={"code": 0, "data": {"items": [1, 2]}})

    feishu.route_handler = route
    client = feishu.client()

    payload = client.get("/open-apis/im/v1/chats")

    assert payload["code"] == 0
    assert payload["data"]["items"] == [1, 2]
    assert calls["n"] == 2
    assert client.token_refresh_count == 1
    assert feishu.tokens_issued == 2  # 首次 + 刷新


@pytest.mark.parametrize("code", [99991661, 99991663, 99991664, 99991668])
def test_all_token_invalid_codes_trigger_refresh(feishu, code):
    calls = {"n": 0}

    def route(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"code": code, "msg": "invalid"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    feishu.route_handler = route
    client = feishu.client()

    assert client.get("/open-apis/im/v1/chats")["code"] == 0
    assert client.token_refresh_count == 1


def test_refresh_does_not_loop_forever(feishu):
    """刷新后仍失败则不再重试 —— 否则令牌持续失效会打死循环。"""
    calls = {"n": 0}

    def route(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"code": 99991663, "msg": "still invalid"})

    feishu.route_handler = route
    client = feishu.client()

    payload = client.get("/open-apis/im/v1/chats")

    assert payload["code"] == 99991663
    assert calls["n"] == 2  # 首次 + 刷新后一次，到此为止
    assert client.token_refresh_count == 1


def test_non_token_error_is_not_retried(feishu):
    """非令牌类错误不触发刷新 —— 重试解决不了权限不足。"""
    calls = {"n": 0}

    def route(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"code": 99991672, "msg": "permission denied"})

    feishu.route_handler = route
    client = feishu.client()

    payload = client.get("/open-apis/im/v1/chats")

    assert payload["code"] == 99991672
    assert calls["n"] == 1
    assert client.token_refresh_count == 0


# --------------------------------------------------------------------------
# POST 请求（推送层发消息需要它 —— 没有公开的 post 就只能去调私有 _request）
# --------------------------------------------------------------------------


def test_post_uses_post_method(feishu):
    client = feishu.client()

    client.post("/open-apis/im/v1/messages")

    assert feishu.requests[-1].method == "POST"


def test_post_attaches_bearer_token(feishu):
    client = feishu.client()

    client.post("/open-apis/im/v1/messages")

    assert feishu.auth_headers()[-1] == "Bearer t-1"


def test_post_sends_json_body(feishu):
    client = feishu.client()

    client.post(
        "/open-apis/im/v1/messages",
        params={"receive_id_type": "chat_id"},
        json_body={"receive_id": "oc_x", "msg_type": "text"},
    )

    request = feishu.requests[-1]
    assert "receive_id_type=chat_id" in str(request.url)
    assert b'"receive_id"' in request.content
    assert b'"msg_type"' in request.content


def test_post_shares_token_cache_with_get(feishu):
    """post 与 get 用同一套令牌缓存，不应各自换取。"""
    client = feishu.client()

    client.get("/open-apis/im/v1/chats")
    client.post("/open-apis/im/v1/messages")

    assert feishu.tokens_issued == 1


def test_post_also_refreshes_on_invalid_token(feishu):
    calls = {"n": 0}

    def route(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"code": 99991663, "msg": "token invalid"})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_1"}})

    feishu.route_handler = route
    client = feishu.client()

    payload = client.post("/open-apis/im/v1/messages", json_body={"a": 1})

    assert payload["code"] == 0
    assert client.token_refresh_count == 1
    assert calls["n"] == 2


# --------------------------------------------------------------------------
# HTTP 错误里的飞书业务码
# --------------------------------------------------------------------------


def test_http_error_message_carries_feishu_business_code(feishu):
    """飞书把业务错误塞在 HTTP 400 的响应体里，异常消息必须带上它。

    否则调用方只看到「400 Bad Request」，而真正有用的信息 ——
    缺的是哪个权限 —— 恰恰在响应体里。这是实测时踩到的：
    消息接口报 400，排查后才发现响应体里写着 `need scope: im:message.group_msg`。
    """
    feishu.route_handler = lambda request: httpx.Response(
        400,
        json={
            "code": 230027,
            "msg": "Lack of necessary permissions, ext=need scope: im:message.group_msg",
        },
    )
    client = feishu.client()

    with pytest.raises(httpx.HTTPStatusError) as exc:
        client.get("/open-apis/im/v1/messages")

    message = str(exc.value)
    assert "400" in message
    assert "230027" in message
    assert "im:message.group_msg" in message


def test_http_error_still_raises_when_body_is_not_json(feishu):
    """响应体不是 JSON 时也不能吞掉错误。"""
    feishu.route_handler = lambda request: httpx.Response(
        502, text="<html>bad gateway</html>"
    )
    client = feishu.client()

    with pytest.raises(httpx.HTTPStatusError) as exc:
        client.get("/open-apis/im/v1/messages")

    assert "502" in str(exc.value)


def test_http_error_keeps_the_response_attached(feishu):
    """响应对象要挂在异常上 —— 推送层靠 `exc.response` 读业务码决定要不要重试。"""
    feishu.route_handler = lambda request: httpx.Response(
        400, json={"code": 230020, "msg": "rate limit"}
    )
    client = feishu.client()

    with pytest.raises(httpx.HTTPStatusError) as exc:
        client.post("/open-apis/im/v1/messages")

    assert exc.value.response.status_code == 400
    assert exc.value.response.json()["code"] == 230020


# --------------------------------------------------------------------------
# 邮箱反查 open_id
# --------------------------------------------------------------------------


def test_resolve_open_ids_maps_emails(feishu):
    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "user_list": [
                        {"user_id": "ou_aaa", "email": "a@company.com"},
                        {"user_id": "ou_bbb", "email": "b@company.com"},
                    ]
                },
            },
        )

    feishu.route_handler = route
    client = feishu.client()

    resolved = client.resolve_open_ids(["a@company.com", "b@company.com"])

    assert resolved == {"a@company.com": "ou_aaa", "b@company.com": "ou_bbb"}


def test_resolve_uses_open_id_type(feishu):
    client = feishu.client()
    client.resolve_open_ids(["a@company.com"])

    request = feishu.requests[-1]
    assert request.url.path == BATCH_PATH
    assert "user_id_type=open_id" in str(request.url)
    assert b"a@company.com" in request.content


def test_resolve_returns_partial_when_some_missing(feishu):
    """部分邮箱查不到时返回已完成的部分，不抛异常。

    缺失的邮箱由调用方按「未映射」处理（design.md §3.4）。
    """

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "user_list": [
                        {"user_id": "ou_aaa", "email": "a@company.com"},
                        {"email": "missing@company.com"},  # 查不到，无 user_id
                    ]
                },
            },
        )

    feishu.route_handler = route
    client = feishu.client()

    resolved = client.resolve_open_ids(["a@company.com", "missing@company.com"])

    assert resolved == {"a@company.com": "ou_aaa"}


def test_resolve_error_code_returns_empty(feishu):
    feishu.route_handler = lambda request: httpx.Response(
        200, json={"code": 99991672, "msg": "permission denied"}
    )
    client = feishu.client()

    assert client.resolve_open_ids(["a@company.com"]) == {}


def test_resolve_network_error_returns_empty(feishu):
    def route(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    feishu.route_handler = route
    client = feishu.client()

    assert client.resolve_open_ids(["a@company.com"]) == {}


def test_resolve_empty_input_makes_no_request(feishu):
    client = feishu.client()

    assert client.resolve_open_ids([]) == {}
    assert feishu.requests == []


def test_resolve_accepts_enterprise_email_field(feishu):
    feishu.route_handler = lambda request: httpx.Response(
        200,
        json={
            "code": 0,
            "data": {
                "user_list": [
                    {"user_id": "ou_ccc", "enterprise_email": "c@company.com"}
                ]
            },
        },
    )
    client = feishu.client()

    assert client.resolve_open_ids(["c@company.com"]) == {"c@company.com": "ou_ccc"}


# --------------------------------------------------------------------------
# 生命周期
# --------------------------------------------------------------------------


def test_base_url_is_open_feishu():
    assert BASE_URL == "https://open.feishu.cn"


def test_context_manager_closes(feishu):
    with feishu.client() as client:
        client.tenant_token()

    assert client._client.is_closed
