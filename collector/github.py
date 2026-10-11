"""GitHub 提交数据采集（design.md §4.1、§6.1、§6.4、§6.5；tasks.md Task 3）。

对外契约只有一个函数 `collect(repos, since, until) -> CollectResult[CommitRecord]`
（design.md §4.1）。三处实现要点均来自规范：

1. **1 + N 次请求是规范认可的代价**（§6.4）：提交列表接口不返回
   additions / deletions / files_changed，每条提交都必须再取一次详情 ——
   本期不做缓存优化。
2. **超时重试与限流重试是两个独立计数器**（§6.1、ADR-004）：限流是服务端
   流量控制（等 reset 后大概率成功），超时是网络异常（重试是唯一手段），
   合并计数会让限流场景挤占超时重试的配额。
3. **时间一律 tz-aware**（§6.5）：GitHub 返回 UTC（"Z" 结尾），统一归一化到
   采集时区；naive datetime 直接拒绝，不进入任何比较。

失败绝不向上抛：单个仓库失败不连坐其他仓库，一切失败以 CollectResult
的返回值表达（§3.2 语义表）。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone as dt_timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from shared.logger import get_logger
from shared.models import CollectResult, CommitRecord

logger = get_logger("collector.github")

SOURCE = "github"
BASE_URL = "https://api.github.com"
TOKEN_ENV_VAR = "GITHUB_TOKEN"

# 采集窗口的日界时区（design.md §6.5）—— 对应 config.yaml schedule.timezone
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_TIMEOUT = 30.0

# 分页：显式取上限 100，减少请求次数（GitHub 默认每页仅 30 条）
PER_PAGE = 100

# §6.1：两类重试各自独立计数、各 3 次上限，互不消耗配额
MAX_TIMEOUT_RETRIES = 3
MAX_RATE_LIMIT_RETRIES = 3
RETRY_INTERVAL_SECONDS = 5.0

_RATE_LIMIT_STATUSES = (403, 429)
_ACCEPT_HEADER = "application/vnd.github+json"
_API_VERSION_HEADER = "2022-11-28"


class _CollectError(Exception):
    """模块内部错误：消息会被原样写进 CollectResult.error。"""


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------

# Windows 没有系统 IANA 时区数据库，未安装 tzdata 包时 ZoneInfo 会全部解析失败。
# Asia/Shanghai 自 1991 年起无夏令时，固定 +08:00 与 IANA 数据完全一致，
# 因此在缺库环境下以固定偏移兜底，§6.5 的归一化语义不变。
_FIXED_OFFSET_FALLBACKS: dict[str, float] = {
    "Asia/Shanghai": 8.0,
    "Asia/Chongqing": 8.0,
    "UTC": 0.0,
    "Etc/UTC": 0.0,
    "GMT": 0.0,
}


def _resolve_timezone(name: str) -> tzinfo:
    """把 IANA 时区名解析为 tzinfo；无法解析时给出可行动的错误。

    不认识的时区**不会**被静默降级成某个偏移 —— 那正是 §6.5 警告的
    「静默的不一致」；宁可让本次采集明确失败。
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        offset = _FIXED_OFFSET_FALLBACKS.get(name)
        if offset is None:
            raise _CollectError(
                f"无法解析时区 {name!r}：环境缺少 IANA 时区数据库"
                "（Windows 上需安装 tzdata 包）"
            ) from None
        return dt_timezone(timedelta(hours=offset))


def _first_naive(*named_values: tuple[str, datetime]) -> str | None:
    """返回第一个 naive datetime 的参数名；全部 tz-aware 时返回 None。"""
    for name, value in named_values:
        if value.tzinfo is None or value.utcoffset() is None:
            return name
    return None


def _to_utc_iso(value: datetime) -> str:
    """tz-aware datetime → GitHub 要求的 UTC ISO8601（秒级，Z 结尾）。"""
    return value.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_github_time(value: Any) -> datetime:
    """解析 GitHub 的 UTC ISO8601（如 2026-10-11T02:33:00Z）为 tz-aware datetime。"""
    if not value:
        raise _CollectError("提交缺少 commit.author.date 字段，无法确定提交时间")
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        # GitHub 约定返回 UTC；万一对方省了时区后缀，按 UTC 解释 ——
        # 若按本地时钟解释，日期归属会随部署机器漂移（§6.5）。
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def _is_rate_limited(response: httpx.Response) -> bool:
    """限流判定：状态码 403/429 **且** 配额已用尽。

    只看状态码不够：403 也可能是权限不足（重试解决不了），所以以
    `X-RateLimit-Remaining: 0` 作为限流的特征。
    """
    return (
        response.status_code in _RATE_LIMIT_STATUSES
        and response.headers.get("X-RateLimit-Remaining") == "0"
    )


def _rate_limit_wait_seconds(response: httpx.Response) -> float:
    """距 X-RateLimit-Reset（Unix 秒）还有多久；缺失或非法时退化为固定间隔。"""
    reset = response.headers.get("X-RateLimit-Reset")
    try:
        return max(0.0, float(reset) - time.time())  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return RETRY_INTERVAL_SECONDS


def _has_next_page(response: httpx.Response, item_count: int) -> bool:
    """是否还有下一页。

    优先信 Link 头（GitHub 的权威信号）；没有 Link 头时退化为
    「本页取满 per_page 条 ⇒ 可能还有下一页」，多取一页（返回空数组）即终止。
    """
    link = response.headers.get("Link")
    if link is not None:
        return 'rel="next"' in link
    return item_count >= PER_PAGE


def _describe_error(exc: Exception) -> str:
    """把异常翻译成人类可读的失败原因（进入 CollectResult.error）。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} {exc.response.reason_phrase}"
    if isinstance(exc, httpx.HTTPError):
        return f"{exc.__class__.__name__}: {exc}"
    return str(exc) or exc.__class__.__name__


def _failure_result(
    records: list[CommitRecord], failed: list[str], reasons: list[str]
) -> CollectResult[CommitRecord]:
    """组装部分/全部失败的结果（§3.2：保留已采到的记录、failed 记明细）。"""
    logger.error(
        "GitHub 采集存在失败仓库",
        extra={"fields": {"failed": failed, "records": len(records)}},
    )
    return CollectResult.fail(SOURCE, "；".join(reasons), records=records, failed=failed)


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


class GithubCollector:
    """GitHub REST API 客户端（对外契约见模块级 `collect()`）。

    Args:
        token: GitHub 凭证（Bearer）。
        timeout: 单次请求超时（秒）。
        timezone: IANA 时区名，CommitRecord.timestamp 归一化到该时区（§6.5）。
        transport: 仅测试注入 httpx.MockTransport 用；生产代码不应传它。
        base_url: API 根地址，仅测试覆盖用。
        sleep: 等待函数，仅测试注入（避免测试真等重试间隔）。
    """

    def __init__(
        self,
        token: str,
        timeout: float = DEFAULT_TIMEOUT,
        timezone: str = DEFAULT_TIMEZONE,
        transport: httpx.BaseTransport | None = None,
        base_url: str = BASE_URL,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._timezone = _resolve_timezone(timezone)
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": _ACCEPT_HEADER,
                "X-GitHub-Api-Version": _API_VERSION_HEADER,
            },
        )

    # ------------------------------------------------------------------
    # 采集入口
    # ------------------------------------------------------------------

    def collect(
        self,
        repos: list[str],
        since: datetime,
        until: datetime,
    ) -> CollectResult[CommitRecord]:
        """逐个仓库独立采集（design.md §3.2 语义表）。

        任何异常都被收敛为返回值：单个仓库失败不连坐其他仓库，全部成功
        才 success=True；部分失败时 records 保留成功仓库的记录。
        """
        naive = _first_naive(("since", since), ("until", until))
        if naive is not None:
            return CollectResult.fail(
                SOURCE,
                f"{naive} 必须是 tz-aware datetime（design.md §6.5 禁止 naive datetime 参与比较）",
            )

        records: list[CommitRecord] = []
        failed: list[str] = []
        reasons: list[str] = []

        for repo in repos:
            try:
                records.extend(self._collect_repo(repo, since, until))
            except Exception as exc:  # noqa: BLE001 —— 采集层对调用方的承诺是不抛异常
                failed.append(repo)
                reasons.append(f"{repo}：{_describe_error(exc)}")
                logger.error(
                    "GitHub 仓库采集失败",
                    extra={"fields": {"repo": repo, "error": _describe_error(exc)}},
                )

        if failed:
            return _failure_result(records, failed, reasons)

        logger.info(
            "GitHub 采集完成",
            extra={"fields": {"repos": len(repos), "records": len(records)}},
        )
        return CollectResult.ok(SOURCE, records)

    # ------------------------------------------------------------------
    # 单个仓库：列表分页 + 逐条详情
    # ------------------------------------------------------------------

    def _collect_repo(
        self, repo: str, since: datetime, until: datetime
    ) -> list[CommitRecord]:
        """采集单个仓库；中途失败则整个仓库作为失败子项（不返回半份数据）。"""
        records: list[CommitRecord] = []
        for item in self._list_commits(repo, since, until):
            sha = str(item.get("sha") or "")
            if not sha:
                raise _CollectError("提交列表项缺少 sha 字段")
            detail = self._commit_detail(repo, sha)
            records.append(self._build_record(repo, item, detail))
        return records

    def _list_commits(
        self, repo: str, since: datetime, until: datetime
    ) -> list[dict[str, Any]]:
        """翻页取完窗口内的提交列表（§6.4 入口：列表不含增删行数）。"""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self._get(
                f"/repos/{repo}/commits",
                params={
                    "since": _to_utc_iso(since),
                    "until": _to_utc_iso(until),
                    "per_page": PER_PAGE,
                    "page": page,
                },
            )
            page_items = response.json()
            if not isinstance(page_items, list):
                raise _CollectError("提交列表响应不是 JSON 数组")
            items.extend(page_items)
            if not _has_next_page(response, len(page_items)):
                return items
            page += 1

    def _commit_detail(self, repo: str, sha: str) -> dict[str, Any]:
        """单条提交详情：additions / deletions / files 只在这里返回（§6.4）。"""
        response = self._get(f"/repos/{repo}/commits/{sha}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise _CollectError(f"提交 {sha[:7]} 的详情响应不是 JSON 对象")
        return payload

    def _build_record(
        self, repo: str, item: dict[str, Any], detail: dict[str, Any]
    ) -> CommitRecord:
        """列表项 + 详情 → CommitRecord（§3.1 共 7 个字段）。

        author 取 GitHub 用户名；提交未关联 GitHub 账号（author 为 null）时
        退化为 commit.author.name，避免整条记录因缺用户名而丢失。
        """
        commit_meta = item.get("commit") or {}
        commit_author = commit_meta.get("author") or {}
        linked_author = item.get("author") or {}
        stats = detail.get("stats") or {}
        files = detail.get("files")
        return CommitRecord(
            author=str(linked_author.get("login") or commit_author.get("name") or "unknown"),
            message=str(commit_meta.get("message") or ""),
            timestamp=_parse_github_time(commit_author.get("date")).astimezone(
                self._timezone
            ),
            repo=repo,
            additions=int(stats.get("additions") or 0),
            deletions=int(stats.get("deletions") or 0),
            files_changed=len(files) if isinstance(files, list) else 0,
        )

    # ------------------------------------------------------------------
    # 请求与两类重试
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET + 超时重试 / 限流重试（§6.1），两个计数器独立。

        Raises:
            _CollectError: 某类重试预算耗尽。
            httpx.HTTPStatusError: 非限流类 4xx/5xx（如 404）。
            httpx.HTTPError: 其他网络错误。
        """
        timeout_retries = 0
        rate_limit_retries = 0
        while True:
            try:
                response = self._client.get(path, params=params)
            except httpx.TimeoutException as exc:
                timeout_retries = self._wait_after_timeout(path, timeout_retries, exc)
                continue

            if _is_rate_limited(response):
                rate_limit_retries = self._wait_after_rate_limit(
                    path, rate_limit_retries, response
                )
                continue

            response.raise_for_status()
            return response

    def _wait_after_timeout(self, path: str, used: int, exc: Exception) -> int:
        """超时重试预算检查 + 固定间隔等待；返回新的已用次数。"""
        if used >= MAX_TIMEOUT_RETRIES:
            raise _CollectError(f"请求 {path} 超时，重试 {used} 次后仍失败") from exc
        used += 1
        logger.error(
            "GitHub API 超时，稍后重试",
            extra={
                "fields": {"path": path, "attempt": used, "max": MAX_TIMEOUT_RETRIES}
            },
        )
        self._sleep(RETRY_INTERVAL_SECONDS)
        return used

    def _wait_after_rate_limit(
        self, path: str, used: int, response: httpx.Response
    ) -> int:
        """限流重试预算检查 + 等待至 X-RateLimit-Reset；返回新的已用次数。"""
        if used >= MAX_RATE_LIMIT_RETRIES:
            raise _CollectError(f"请求 {path} 触发 GitHub 限流，重试 {used} 次后仍失败")
        used += 1
        wait = _rate_limit_wait_seconds(response)
        logger.error(
            "GitHub API 限流，等待重置后重试",
            extra={
                "fields": {
                    "path": path,
                    "attempt": used,
                    "max": MAX_RATE_LIMIT_RETRIES,
                    "wait_seconds": round(wait, 1),
                }
            },
        )
        self._sleep(wait)
        return used

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GithubCollector:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# --------------------------------------------------------------------------
# 契约入口（design.md §4.1）
# --------------------------------------------------------------------------


def collect(
    repos: list[str],
    since: datetime,
    until: datetime,
    *,
    token: str | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    timezone: str = DEFAULT_TIMEZONE,
    sleep: Callable[[float], None] = time.sleep,
) -> CollectResult[CommitRecord]:
    """采集指定仓库在 [since, until] 窗口内的提交（design.md §4.1）。

    每个仓库独立采集：单个仓库失败不影响其他仓库（§3.2 语义表）；
    全部成功才 success=True，部分失败时 records 保留成功仓库已采到的记录、
    failed 记录失败仓库名。

    Args:
        repos: 仓库列表（owner/repo 形式）。
        since: 起始时间（tz-aware）。
        until: 结束时间（tz-aware）。
        token: GitHub 凭证；None 时取环境变量 GITHUB_TOKEN（§6.2 凭证只从环境变量读）。
        transport: 仅测试注入 httpx.MockTransport 用；生产代码不应传它。
        timeout: 单次请求超时（秒）。
        timezone: CommitRecord.timestamp 归一化到的采集时区（§6.5）。
        sleep: 等待函数，仅测试注入（避免测试真等重试间隔）。

    Returns:
        **永远返回 CollectResult，不抛异常**。预检失败（naive 时间、缺 token、
        时区无法解析）时 failed 为空列表 —— 没有任何仓库被真正尝试。
    """
    resolved_token = os.environ.get(TOKEN_ENV_VAR, "") if token is None else token
    if not resolved_token:
        return CollectResult.fail(
            SOURCE,
            f"环境变量 {TOKEN_ENV_VAR} 未设置，无法调用 GitHub API（design.md §6.2）",
        )

    try:
        client = GithubCollector(
            resolved_token,
            timeout=timeout,
            timezone=timezone,
            transport=transport,
            sleep=sleep,
        )
    except Exception as exc:  # noqa: BLE001 —— 初始化失败（如时区无法解析）也走返回值
        logger.error(
            "GitHub 采集客户端初始化失败", extra={"fields": {"error": str(exc)}}
        )
        return CollectResult.fail(SOURCE, f"初始化 GitHub 采集客户端失败：{exc}")

    try:
        return client.collect(repos, since, until)
    except Exception as exc:  # noqa: BLE001 —— 兜底：本函数对调用方的承诺是不抛异常
        logger.error(
            "GitHub 采集发生未预期异常",
            extra={"fields": {"error": str(exc)}},
            exc_info=True,
        )
        return CollectResult.fail(SOURCE, f"未预期异常：{exc}", failed=list(repos))
    finally:
        client.close()
