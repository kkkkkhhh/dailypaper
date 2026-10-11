"""飞书开放平台客户端（design.md §4.1）。

Task 4（任务采集）与 Task 5（消息采集）都需要这三件事：
1. 获取 tenant_access_token
2. Token 失效时自动刷新
3. 把配置里的邮箱反查成 open_id

所以它必须是**一份**代码 —— 若两个采集模块各自实现，就会出现两份行为
不一致的令牌逻辑，而且都要碰同一个文件，违反表 6-2 的并行规则。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from shared.logger import get_logger

logger = get_logger("collector.lark_client")

BASE_URL = "https://open.feishu.cn"

_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
_BATCH_GET_ID_PATH = "/open-apis/contact/v3/users/batch_get_id"

# 令牌失效类错误码。命中后刷新令牌并重试一次 —— 这类失败重试是有意义的，
# 与网络超时的重试是两回事（design.md §6.1 要求两者独立计数）。
_TOKEN_INVALID_CODES = frozenset({99991661, 99991663, 99991664, 99991668})

# 提前 60s 视为过期，避免「取出时还有效、发出去已失效」的边界
_EXPIRY_SKEW_SECONDS = 60


class LarkClient:
    """飞书 API 客户端。

    Args:
        app_id: 应用 App ID。
        app_secret: 应用 App Secret。
        timeout: 单次请求超时（秒）。
        transport: 可选的 httpx transport，**仅供测试注入 MockTransport**。
            生产代码不应传它。
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._client = httpx.Client(
            timeout=timeout,
            base_url=BASE_URL,
            transport=transport,
        )
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_refresh_count = 0

    # ------------------------------------------------------------------
    # 令牌管理
    # ------------------------------------------------------------------

    @property
    def token_refresh_count(self) -> int:
        """令牌被刷新的次数（不含首次获取）—— 供测试断言「自动刷新 1 次」。"""
        return self._token_refresh_count

    def tenant_token(self) -> str:
        """返回当前可用的 tenant_access_token，必要时先获取。"""
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        return self._fetch_token()

    def _fetch_token(self) -> str:
        response = self._client.post(
            _TOKEN_PATH,
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        response.raise_for_status()
        payload = response.json()

        if payload.get("code") != 0:
            raise RuntimeError(
                f"获取飞书 tenant_access_token 失败："
                f"code={payload.get('code')} msg={payload.get('msg')}"
            )

        self._token = str(payload["tenant_access_token"])
        expire = int(payload.get("expire") or 7200)
        self._token_expires_at = time.monotonic() + max(expire - _EXPIRY_SKEW_SECONDS, 0)
        return self._token

    def _refresh_token(self) -> str:
        """令牌失效时强制重新获取。"""
        self._token = None
        self._token_expires_at = 0.0
        self._token_refresh_count += 1
        return self._fetch_token()

    # ------------------------------------------------------------------
    # 请求
    # ------------------------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发起 GET 请求，返回飞书响应的 JSON。

        遇令牌失效错误码时自动刷新并重试 **1 次**；再失败则原样返回错误响应，
        由调用方决定如何处理（采集层以 CollectResult 表达失败，不抛异常）。
        """
        return self._request("GET", path, params=params)

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = self._send(method, path, self.tenant_token(), params, json_body)

        if payload.get("code") in _TOKEN_INVALID_CODES:
            logger.info(
                "飞书令牌失效，刷新后重试",
                extra={"fields": {"path": path, "code": payload.get("code")}},
            )
            token = self._refresh_token()
            payload = self._send(method, path, token, params, json_body)

        return payload

    def _send(
        self,
        method: str,
        path: str,
        token: str,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        response = self._client.request(
            method,
            path,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            json=json_body,
        )
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------------
    # 身份反查
    # ------------------------------------------------------------------

    def resolve_open_ids(self, emails: list[str]) -> dict[str, str]:
        """邮箱 → open_id 反查（design.md §3.4 的 `lark` 邮箱写法依赖它）。

        飞书消息与任务接口返回的是 open_id，而配置里可能写的是企业邮箱，
        中间差的这一层解析就在这里补上。

        Returns:
            邮箱到 open_id 的映射。**查询失败或部分邮箱查不到时，
            返回已完成的部分**，不抛异常 —— 缺失的邮箱由调用方按
            「未映射」处理（design.md §3.4：不在映射表中的记录被丢弃）。
        """
        targets = [email for email in emails if email]
        if not targets:
            return {}

        try:
            payload = self._request(
                "POST",
                _BATCH_GET_ID_PATH,
                params={"user_id_type": "open_id"},
                json_body={"emails": targets},
            )
        except httpx.HTTPError as exc:
            logger.error(
                "邮箱反查 open_id 请求失败",
                extra={"fields": {"error": str(exc)}},
            )
            return {}

        if payload.get("code") != 0:
            logger.error(
                "邮箱反查 open_id 返回错误",
                extra={
                    "fields": {
                        "code": payload.get("code"),
                        "msg": payload.get("msg"),
                    }
                },
            )
            return {}

        resolved: dict[str, str] = {}
        for item in (payload.get("data") or {}).get("user_list") or []:
            user_id = item.get("user_id")
            email = item.get("email") or item.get("enterprise_email")
            if user_id and email:
                resolved[str(email)] = str(user_id)
        return resolved

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LarkClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
