"""GitHub 采集模块测试（design.md §3.1/§3.2/§4.1/§6.1/§6.4/§6.5；tasks.md Task 3）。

全程使用 httpx.MockTransport，不打真实网络；重试间隔通过注入的 sleep
函数拦截，测试不会真的等待。
"""

from __future__ import annotations

import dataclasses
import inspect
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from collector import github
from collector.github import (
    BASE_URL,
    MAX_RATE_LIMIT_RETRIES,
    MAX_TIMEOUT_RETRIES,
    PER_PAGE,
    RETRY_INTERVAL_SECONDS,
    collect,
)
from shared.models import CommitRecord

# 采集窗口固定为北京时间的某天 00:00 - 18:00（design.md §6.5）
CST = timezone(timedelta(hours=8))
SINCE = datetime(2026, 10, 11, 0, 0, tzinfo=CST)
UNTIL = datetime(2026, 10, 11, 18, 0, tzinfo=CST)

GITHUB_TIME = "2026-10-11T02:33:00Z"  # GitHub 返回的 UTC ISO8601 示例
TS_SHANGHAI = datetime(2026, 10, 11, 10, 33, tzinfo=CST)  # 同一时刻的 +08:00 表述


# --------------------------------------------------------------------------
# 假数据构造
# --------------------------------------------------------------------------


def commit_item(
    sha: str,
    *,
    author: str | None = "zhangsan",
    name: str = "张三",
    date: str = GITHUB_TIME,
    message: str = "fix: bug",
) -> dict:
    """提交列表项（列表接口不返回增删行数）。author=None 模拟未关联 GitHub 账号。"""
    return {
        "sha": sha,
        "author": None if author is None else {"login": author},
        "commit": {"message": message, "author": {"name": name, "date": date}},
    }


def commit_detail(*, additions: int = 10, deletions: int = 3, file_count: int = 1) -> dict:
    """单条提交详情（stats 与 files 只在详情接口返回，design.md §6.4）。"""
    return {
        "stats": {"additions": additions, "deletions": deletions},
        "files": [{"filename": f"f{i}.py"} for i in range(file_count)],
    }


def split_path(path: str) -> tuple[str, str | None]:
    """把 /repos/owner/repo/commits[/sha] 拆成 (owner/repo, sha | None)。"""
    rest = path[len("/repos/") :]
    if "/commits/" in rest:
        repo, _, sha = rest.partition("/commits/")
        return repo, sha
    repo, _, _ = rest.partition("/commits")
    return repo, None


class FakeGithub:
    """按脚本作答的 GitHub API 替身：记录请求，永不打真实网络。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.pages: dict[str, list[list[dict]]] = {}  # repo -> 每页的提交列表
        self.details: dict[tuple[str, str], dict] = {}  # (repo, sha) -> 详情响应
        self.repo_status: dict[str, int] = {}  # repo -> 强制返回的状态码（如 404）
        self.emit_link_header = True  # False 时只靠「本页是否取满」判断翻页
        self.route_handler = None  # 完全接管应答（重试/异常类测试用）

    # -- 调用入口 ---------------------------------------------------------

    def run(
        self,
        repos: list[str],
        since: datetime = SINCE,
        until: datetime = UNTIL,
        **kwargs,
    ):
        kwargs.setdefault("token", "gh_test_token")
        return collect(
            repos,
            since,
            until,
            transport=httpx.MockTransport(self.handler),
            sleep=self.sleep,
            **kwargs,
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.route_handler is not None:
            return self.route_handler(request)
        return self._default_response(request)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def list_pages(self) -> list[str | None]:
        """只取提交列表请求的 page 参数序列。"""
        return [
            request.url.params.get("page")
            for request in self.requests
            if request.url.path.endswith("/commits")
        ]

    # -- 默认应答 ---------------------------------------------------------

    def _default_response(self, request: httpx.Request) -> httpx.Response:
        repo, sha = split_path(request.url.path)
        status = self.repo_status.get(repo)
        if status is not None:
            return httpx.Response(status, json={"message": "scripted failure"})
        if sha is None:
            return self._list_response(request, repo)
        detail = self.details.get((repo, sha))
        return httpx.Response(200, json=detail if detail is not None else commit_detail())

    def _list_response(self, request: httpx.Request, repo: str) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        pages = self.pages.get(repo, [])
        items = pages[page - 1] if 1 <= page <= len(pages) else []
        headers = {}
        if self.emit_link_header and page < len(pages):
            headers["Link"] = (
                f'<{BASE_URL}/repos/{repo}/commits?page={page + 1}>; rel="next"'
            )
        return httpx.Response(200, json=items, headers=headers)


@pytest.fixture
def fake() -> FakeGithub:
    return FakeGithub()


# --------------------------------------------------------------------------
# 契约：签名与常量
# --------------------------------------------------------------------------


def test_collect_signature_matches_design_4_1():
    """签名符合 design.md §4.1：前三个位置参数为 repos / since / until。"""
    params = list(inspect.signature(collect).parameters.values())

    positional = [
        p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    assert [p.name for p in positional] == ["repos", "since", "until"]
    assert positional[0].annotation == "list[str]"
    assert positional[1].annotation == "datetime"
    assert positional[2].annotation == "datetime"

    # 测试注入用的参数一律 keyword-only 且有默认值，不改变 §4.1 的调用形式
    for extra in params[len(positional):]:
        assert extra.kind is inspect.Parameter.KEYWORD_ONLY
        assert extra.default is not inspect.Parameter.empty

    assert "CollectResult[CommitRecord]" in str(inspect.signature(collect).return_annotation)


def test_base_url_is_github_api():
    assert BASE_URL == "https://api.github.com"


# --------------------------------------------------------------------------
# 请求构造：认证头、时间窗口、1 + N 请求（design.md §6.4）
# --------------------------------------------------------------------------


def test_requests_carry_auth_and_api_version_headers(fake):
    fake.pages["owner/repo"] = [[commit_item("s1")]]

    fake.run(["owner/repo"])

    request = fake.requests[0]
    assert request.headers["Authorization"] == "Bearer gh_test_token"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_window_is_sent_as_utc_iso8601(fake):
    """since/until 是 tz-aware 的 +08:00 时间，发给 GitHub 前换算成 UTC。"""
    fake.run(["owner/repo"])

    params = fake.requests[0].url.params
    assert params["since"] == "2026-10-10T16:00:00Z"
    assert params["until"] == "2026-10-11T10:00:00Z"
    assert params["per_page"] == str(PER_PAGE)


def test_detail_requested_per_commit(fake):
    """列表接口没有增删行数，每条提交必须追加一次详情请求（§6.4）。"""
    fake.pages["owner/repo"] = [[commit_item("s1"), commit_item("s2")]]

    fake.run(["owner/repo"])

    assert fake.paths() == [
        "/repos/owner/repo/commits",
        "/repos/owner/repo/commits/s1",
        "/repos/owner/repo/commits/s2",
    ]


def test_no_detail_request_when_no_commits(fake):
    fake.run(["owner/repo"])

    assert fake.paths() == ["/repos/owner/repo/commits"]


# --------------------------------------------------------------------------
# 七字段与类型（design.md §3.1）
# --------------------------------------------------------------------------


def test_record_has_all_seven_fields_with_correct_types(fake):
    fake.pages["owner/repo"] = [
        [
            commit_item(
                "abc123", author="zhangsan", date=GITHUB_TIME, message="feat: 新功能"
            )
        ]
    ]
    fake.details[("owner/repo", "abc123")] = commit_detail(
        additions=120, deletions=15, file_count=3
    )

    result = fake.run(["owner/repo"])
    record = result.records[0]

    assert isinstance(record, CommitRecord)
    assert [f.name for f in dataclasses.fields(record)] == [
        "author",
        "message",
        "timestamp",
        "repo",
        "additions",
        "deletions",
        "files_changed",
    ]
    assert record.author == "zhangsan"
    assert record.message == "feat: 新功能"
    assert record.timestamp == TS_SHANGHAI
    assert record.repo == "owner/repo"
    assert record.additions == 120
    assert record.deletions == 15
    assert record.files_changed == 3
    assert isinstance(record.additions, int)
    assert isinstance(record.deletions, int)
    assert isinstance(record.files_changed, int)
    assert isinstance(record.timestamp, datetime)


def test_stats_come_from_detail_not_list(fake):
    """列表项里没有 stats —— 数字只能来自详情接口。"""
    fake.pages["owner/repo"] = [
        [{"sha": "s1", "author": {"login": "zhangsan"}, "commit": {"message": "m", "author": {"name": "张三", "date": GITHUB_TIME}}}]
    ]
    fake.details[("owner/repo", "s1")] = commit_detail(
        additions=7, deletions=0, file_count=2
    )

    record = fake.run(["owner/repo"]).records[0]

    assert (record.additions, record.deletions, record.files_changed) == (7, 0, 2)


def test_author_falls_back_to_commit_author_name(fake):
    """提交未关联 GitHub 账号（author 为 null）时退化为署名，不丢记录。"""
    fake.pages["owner/repo"] = [[commit_item("s1", author=None, name="匿名开发者")]]

    record = fake.run(["owner/repo"]).records[0]

    assert record.author == "匿名开发者"


# --------------------------------------------------------------------------
# 分页
# --------------------------------------------------------------------------


def test_pagination_follows_link_header(fake):
    fake.pages["owner/repo"] = [
        [commit_item("s1")],  # 不足 per_page，但 Link 头指明有下一页
        [commit_item("s2"), commit_item("s3")],
    ]

    result = fake.run(["owner/repo"])

    assert result.success is True
    assert len(result.records) == 3
    assert fake.list_pages() == ["1", "2"]


def test_pagination_without_link_header_uses_short_page(fake):
    """没有 Link 头时，本页取满 per_page ⇒ 继续翻页；不足 ⇒ 终止。"""
    fake.emit_link_header = False
    fake.pages["owner/repo"] = [
        [commit_item(f"s{i}") for i in range(PER_PAGE)],
        [commit_item("s-last")],
    ]

    result = fake.run(["owner/repo"])

    assert len(result.records) == PER_PAGE + 1
    assert fake.list_pages() == ["1", "2"]


def test_link_header_without_next_stops_pagination(fake):
    """本页恰好取满，但 Link 头没有 rel="next" ⇒ 不再翻页。"""
    items = [commit_item(f"s{i}") for i in range(PER_PAGE)]

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            return httpx.Response(
                200,
                json=items,
                headers={"Link": '<https://api.github.com/x>; rel="prev"'},
            )
        return httpx.Response(200, json=commit_detail())

    fake.route_handler = route

    fake.run(["owner/repo"])

    assert fake.list_pages() == ["1"]


def test_single_page_makes_one_list_request(fake):
    fake.pages["owner/repo"] = [[commit_item("s1")]]

    fake.run(["owner/repo"])

    assert fake.list_pages() == ["1"]


# --------------------------------------------------------------------------
# 超时重试（§6.1：3 次上限，间隔 5s）
# --------------------------------------------------------------------------


def test_timeout_retries_three_times_then_fails(fake):
    def route(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    fake.route_handler = route

    result = fake.run(["owner/repo"])

    assert result.success is False
    assert result.records == []
    assert result.failed == ["owner/repo"]
    assert "owner/repo" in result.error
    # 首次 + 3 次重试 = 4 次请求，等待间隔 3 次、每次 5s
    assert len(fake.requests) == 1 + MAX_TIMEOUT_RETRIES
    assert fake.sleeps == [RETRY_INTERVAL_SECONDS] * MAX_TIMEOUT_RETRIES


def test_single_timeout_then_success(fake):
    calls = {"n": 0}

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadTimeout("timed out")
            return httpx.Response(200, json=[commit_item("s1")])
        return httpx.Response(200, json=commit_detail())

    fake.route_handler = route

    result = fake.run(["owner/repo"])

    assert result.success is True
    assert fake.sleeps == [RETRY_INTERVAL_SECONDS]
    assert len(fake.requests) == 3  # 超时 + 列表重试 + 详情


def test_timeout_budget_is_per_request(fake):
    """超时重试是每个请求各自的预算：列表超时重试成功不影响详情照常请求。"""
    calls = {"n": 0}

    def route(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("timed out")
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, json=[commit_item("s1")])
        return httpx.Response(200, json=commit_detail())

    fake.route_handler = route

    assert fake.run(["owner/repo"]).success is True


# --------------------------------------------------------------------------
# 限流重试（§6.1：读 X-RateLimit-Reset 等待至该时刻，上限 3 次）
# --------------------------------------------------------------------------


def _rate_limited_response(status: int = 403, reset_in: float = 0) -> httpx.Response:
    return httpx.Response(
        status,
        json={"message": "API rate limit exceeded"},
        headers={
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(int(time.time() + reset_in)),
        },
    )


def test_rate_limit_waits_until_reset_then_retries(fake):
    calls = {"n": 0}

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            calls["n"] += 1
            if calls["n"] <= 2:
                return _rate_limited_response(reset_in=10)
            return httpx.Response(200, json=[commit_item("s1")])
        return httpx.Response(200, json=commit_detail())

    fake.route_handler = route

    result = fake.run(["owner/repo"])

    assert result.success is True
    assert len(fake.sleeps) == 2
    # 等待到 X-RateLimit-Reset 时刻（约 10s），而不是固定间隔
    assert all(8.0 <= waited <= 10.0 for waited in fake.sleeps)


@pytest.mark.parametrize("status", [403, 429])
def test_rate_limit_statuses_are_retried(fake, status):
    calls = {"n": 0}

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            calls["n"] += 1
            if calls["n"] == 1:
                return _rate_limited_response(status)
            return httpx.Response(200, json=[commit_item("s1")])
        return httpx.Response(200, json=commit_detail())

    fake.route_handler = route

    assert fake.run(["owner/repo"]).success is True
    assert len(fake.sleeps) == 1


def test_rate_limit_exhausted_fails(fake):
    fake.route_handler = lambda request: _rate_limited_response(reset_in=0)

    result = fake.run(["owner/repo"])

    assert result.success is False
    assert result.failed == ["owner/repo"]
    assert "限流" in result.error
    assert len(fake.requests) == 1 + MAX_RATE_LIMIT_RETRIES
    assert fake.sleeps == [0.0] * MAX_RATE_LIMIT_RETRIES  # reset 已过 → 立即重试


def test_forbidden_without_exhausted_quota_is_not_retried(fake):
    """403 且配额未耗尽 = 权限不足，重试解决不了问题。"""

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"message": "Resource not accessible"},
            headers={"X-RateLimit-Remaining": "42", "X-RateLimit-Reset": str(int(time.time()) + 3600)},
        )

    fake.route_handler = route

    result = fake.run(["owner/repo"])

    assert result.success is False
    assert len(fake.requests) == 1
    assert fake.sleeps == []


# --------------------------------------------------------------------------
# 两类重试计数器独立（ADR-004）
# --------------------------------------------------------------------------


def test_timeout_retries_do_not_consume_rate_limit_budget(fake):
    """超时重试 3 次耗尽后，限流重试仍保有独立的 3 次预算。"""
    calls = {"n": 0}

    def route(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= MAX_TIMEOUT_RETRIES:
            raise httpx.ReadTimeout("timed out")
        if calls["n"] == MAX_TIMEOUT_RETRIES + 1:
            return _rate_limited_response(reset_in=0)
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, json=[commit_item("s1")])
        return httpx.Response(200, json=commit_detail())

    fake.route_handler = route

    result = fake.run(["owner/repo"])

    assert result.success is True
    # 列表请求：3 次超时 + 1 次限流 + 1 次成功 = 5；加上 1 次详情请求
    assert len(fake.requests) == MAX_TIMEOUT_RETRIES + 3
    assert fake.sleeps[:MAX_TIMEOUT_RETRIES] == [RETRY_INTERVAL_SECONDS] * MAX_TIMEOUT_RETRIES
    assert fake.sleeps[MAX_TIMEOUT_RETRIES] == 0.0  # 这一次是限流重试自己的等待


def test_rate_limit_retries_do_not_consume_timeout_budget(fake):
    """限流重试 3 次耗尽后，超时重试仍保有独立的 3 次预算。"""
    calls = {"n": 0}

    def route(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= MAX_RATE_LIMIT_RETRIES:
            return _rate_limited_response(reset_in=0)
        if calls["n"] == MAX_RATE_LIMIT_RETRIES + 1:
            raise httpx.ReadTimeout("timed out")
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, json=[commit_item("s1")])
        return httpx.Response(200, json=commit_detail())

    fake.route_handler = route

    result = fake.run(["owner/repo"])

    assert result.success is True
    # 列表请求：3 次限流 + 1 次超时 + 1 次成功 = 5；加上 1 次详情请求
    assert len(fake.requests) == MAX_RATE_LIMIT_RETRIES + 3
    assert fake.sleeps[MAX_RATE_LIMIT_RETRIES] == RETRY_INTERVAL_SECONDS


# --------------------------------------------------------------------------
# §3.2 语义表：四种情形
# --------------------------------------------------------------------------


def test_all_repos_succeed_with_data(fake):
    fake.pages["owner/repoA"] = [[commit_item("s1")]]
    fake.pages["owner/repoB"] = [[commit_item("s2", author="lisi")]]

    result = fake.run(["owner/repoA", "owner/repoB"])

    assert result.source == "github"
    assert result.success is True
    assert result.error is None
    assert result.failed == []
    assert len(result.records) == 2
    assert {r.repo for r in result.records} == {"owner/repoA", "owner/repoB"}


def test_all_repos_succeed_without_data(fake):
    """成功但无数据 ≠ 失败：success=True 且 records/failed 为空、error 为 None。"""
    result = fake.run(["owner/repo"])

    assert result.source == "github"
    assert result.success is True
    assert result.records == []
    assert result.failed == []
    assert result.error is None


def test_partial_failure_keeps_successful_records(fake):
    fake.pages["owner/repoA"] = [[commit_item("s1")]]
    fake.repo_status["owner/repoB"] = 404

    result = fake.run(["owner/repoA", "owner/repoB"])

    assert result.source == "github"
    assert result.success is False
    assert [r.repo for r in result.records] == ["owner/repoA"]
    assert result.failed == ["owner/repoB"]
    assert result.error is not None and "owner/repoB" in result.error
    assert "404" in result.error


def test_all_repos_failed(fake):
    fake.repo_status["owner/repoA"] = 404
    fake.repo_status["owner/repoB"] = 500

    result = fake.run(["owner/repoA", "owner/repoB"])

    assert result.source == "github"
    assert result.success is False
    assert result.records == []
    assert result.failed == ["owner/repoA", "owner/repoB"]
    assert result.error is not None


def test_failure_of_one_repo_does_not_block_later_repos(fake):
    """失败仓库排在前面时，后续仓库照常采集。"""
    fake.repo_status["owner/repoA"] = 404
    fake.pages["owner/repoB"] = [[commit_item("s1")]]

    result = fake.run(["owner/repoA", "owner/repoB"])

    assert result.success is False
    assert [r.repo for r in result.records] == ["owner/repoB"]
    assert result.failed == ["owner/repoA"]


def test_empty_repo_list_is_success_with_no_data(fake):
    result = fake.run([])

    assert result.success is True
    assert result.records == [] and result.failed == [] and result.error is None
    assert fake.requests == []


# --------------------------------------------------------------------------
# 不抛异常：一切失败以返回值表达
# --------------------------------------------------------------------------


def test_network_error_returns_failure_instead_of_raising(fake):
    def route(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    fake.route_handler = route

    result = fake.run(["owner/repo"])

    assert result.success is False
    assert result.failed == ["owner/repo"]


def test_invalid_json_returns_failure_instead_of_raising(fake):
    fake.route_handler = lambda request: httpx.Response(
        200, content=b"<html>not json</html>"
    )

    result = fake.run(["owner/repo"])

    assert result.success is False
    assert result.failed == ["owner/repo"]


def test_detail_failure_fails_the_repo(fake):
    """任一提交详情失败 → 该仓库整体标记失败，其他仓库不受影响。"""
    fake.pages["owner/repoA"] = [[commit_item("s1"), commit_item("s2")]]
    fake.pages["owner/repoB"] = [[commit_item("s3")]]
    fake.details[("owner/repoA", "s1")] = commit_detail()
    # repoA 的 s2 详情走默认 200，改为路由强制 404：

    original = fake._default_response

    def default(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/s2"):
            return httpx.Response(404, json={"message": "Not Found"})
        return original(request)

    fake._default_response = default

    result = fake.run(["owner/repoA", "owner/repoB"])

    assert result.success is False
    assert result.failed == ["owner/repoA"]
    assert [r.repo for r in result.records] == ["owner/repoB"]


def test_naive_datetime_returns_failure_instead_of_raising(fake):
    result = fake.run(
        ["owner/repo"], since=datetime(2026, 10, 11, 0, 0), until=UNTIL
    )

    assert result.success is False
    assert "since" in result.error and "tz-aware" in result.error
    assert fake.requests == []  # 预检失败，一个请求都不发


def test_missing_token_returns_failure(monkeypatch, fake):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    result = collect(
        ["owner/repo"],
        SINCE,
        UNTIL,
        transport=httpx.MockTransport(fake.handler),
        sleep=fake.sleep,
    )

    assert result.success is False
    assert "GITHUB_TOKEN" in result.error
    assert fake.requests == []


def test_unresolvable_timezone_returns_failure(fake):
    result = fake.run(["owner/repo"], timezone="Not/AZone")

    assert result.success is False
    assert "Not/AZone" in result.error
    assert fake.requests == []


# --------------------------------------------------------------------------
# 时区归一化（design.md §6.5）
# --------------------------------------------------------------------------


def test_utc_timestamp_normalized_to_asia_shanghai(fake):
    fake.pages["owner/repo"] = [[commit_item("s1", date="2026-10-11T02:33:00Z")]]

    record = fake.run(["owner/repo"]).records[0]

    assert record.timestamp.tzinfo is not None
    assert record.timestamp.utcoffset() == timedelta(hours=8)  # 确实归一到 +08:00
    assert record.timestamp == TS_SHANGHAI  # 同一时刻（aware 按瞬时比较）
    assert record.timestamp.date().isoformat() == "2026-10-11"


def test_window_boundary_is_compared_in_target_timezone(fake):
    """00:00（+08:00）的提交仍属于当日 —— 若按 UTC 判日会被算到前一天。"""
    fake.pages["owner/repo"] = [[commit_item("s1", date="2026-10-10T16:00:00Z")]]

    record = fake.run(["owner/repo"]).records[0]

    assert record.timestamp.date().isoformat() == "2026-10-11"


def test_timezone_param_controls_normalization(fake):
    fake.pages["owner/repo"] = [[commit_item("s1", date="2026-10-11T02:33:00Z")]]

    record = fake.run(["owner/repo"], timezone="UTC").records[0]

    assert record.timestamp == datetime(2026, 10, 11, 2, 33, tzinfo=timezone.utc)
    assert record.timestamp.utcoffset() == timedelta(0)


def test_naive_github_timestamp_treated_as_utc(fake):
    """对方省略 Z 后缀时按 UTC 解释，而不是按部署机器的本地时区。"""
    fake.pages["owner/repo"] = [[commit_item("s1", date="2026-10-11T02:33:00")]]

    record = fake.run(["owner/repo"]).records[0]

    assert record.timestamp == TS_SHANGHAI


# --------------------------------------------------------------------------
# 生命周期
# --------------------------------------------------------------------------


def test_collect_closes_client(fake, monkeypatch):
    closed = {"n": 0}

    def fake_close(self) -> None:
        closed["n"] += 1

    monkeypatch.setattr(github.GithubCollector, "close", fake_close)

    fake.run(["owner/repo"])

    assert closed["n"] == 1


def test_context_manager_closes(fake):
    with github.GithubCollector(
        "gh_test_token", transport=httpx.MockTransport(fake.handler)
    ) as client:
        client.collect(["owner/repo"], SINCE, UNTIL)

    assert client._client.is_closed
