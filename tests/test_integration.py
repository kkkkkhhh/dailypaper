"""端到端集成测试（tasks.md Task 10）。

与其它测试的分工：单元测试验证「某个模块自己做得对不对」，这里验证
**「把它们接起来还对不对」**。所以只替换两样东西：

- HTTP 层：用 MockTransport 顶替 GitHub 与飞书，数据形状按官方文档构造
- SMTP：不能真发信

其余全部走真实实现 —— 真实 config.yaml、真实 SQLite、真实聚合与模板渲染。
数据从采集一路流到存档与推送，中间任何一环的接口对不上，这里都会红。
"""

from __future__ import annotations

import json
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import yaml

import main
from collector import github as github_module
from collector import lark_msg as lark_msg_module
from collector import lark_task as lark_task_module
from collector.lark_client import LarkClient
from notifier import email as email_module
from notifier import lark_bot as lark_bot_module
from shared import storage

TZ = timezone(timedelta(hours=8))
CHAT_ID = "oc_test_chat"
REPO = "kkkkkhhh/dailypaper"

ENV_VARS = {
    "GITHUB_TOKEN": "gh_test_token",
    "FEISHU_APP_ID": "cli_test",
    "FEISHU_APP_SECRET": "app_secret",
    "FEISHU_BOT_OPEN_ID": "ou_the_bot",
    "SMTP_PASSWORD": "smtp_auth_code",
}

# 采集窗口覆盖的是「今天」，所以假数据的时间戳必须相对当下生成，
# 否则测试会因为写死日期而在别的日子失效。
NOW = datetime.now(TZ)


def within_window(hours_ago: float) -> datetime:
    return NOW - timedelta(hours=hours_ago)


def as_epoch_ms(moment: datetime) -> str:
    """飞书响应体用的是**毫秒**级时间戳字符串。"""
    return str(int(moment.timestamp() * 1000))


def as_epoch_seconds(moment: datetime) -> str:
    """飞书请求参数用的是**秒**级时间戳 —— 与响应体差 1000 倍。"""
    return str(int(moment.timestamp()))


# ---------------------------------------------------------------------------
# 假 GitHub
# ---------------------------------------------------------------------------


class FakeGithub:
    def __init__(self) -> None:
        self.repos: dict[str, list[dict]] = {}
        self.stats: dict[str, dict] = {}
        self.fail: set[str] = set()
        self.calls: list[str] = []

    def add_commit(self, repo, sha, author, message, when, additions, deletions, files):
        self.repos.setdefault(repo, []).append(
            {
                "sha": sha,
                "commit": {
                    "message": message,
                    "author": {"name": author, "date": when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")},
                },
            }
        )
        self.stats[sha] = {
            "stats": {"additions": additions, "deletions": deletions},
            "files": [{"filename": f"f{i}.py"} for i in range(files)],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)

        if not request.headers.get("Authorization"):
            return httpx.Response(401, json={"message": "Requires authentication"})

        for repo in self.fail:
            if path.startswith(f"/repos/{repo}"):
                return httpx.Response(404, json={"message": "Not Found"})

        for repo, commits in self.repos.items():
            if path == f"/repos/{repo}/commits":
                return httpx.Response(200, json=commits)

        for sha, detail in self.stats.items():
            if path.endswith(f"/commits/{sha}"):
                return httpx.Response(200, json=detail)

        return httpx.Response(404, json={"message": "Not Found"})


# ---------------------------------------------------------------------------
# 假飞书
# ---------------------------------------------------------------------------


class FakeFeishu:
    def __init__(self) -> None:
        self.token_status = 200
        self.tasklists: list[dict] = [{"guid": "tl_1"}]
        self.tasks: dict[str, list[dict]] = {}
        self.messages: list[dict] = []
        self.chat_name = "智能日报"
        self.fail_messages_api = False
        self.fail_send = False
        self.sent: list[dict] = []
        self.message_query_params: dict[str, str] = {}

    def add_task(self, guid, summary, assignee_open_id, completed_at):
        self.tasks.setdefault(guid, []).append(
            {
                "guid": f"task_{len(self.tasks.get(guid, []))}",
                "summary": summary,
                "completed_at": as_epoch_ms(completed_at),
                "members": [
                    {"id": assignee_open_id, "type": "user", "role": "assignee", "name": "x"}
                ],
            }
        )

    def add_message(self, sender_open_id, content, when):
        self.messages.append(
            {
                "msg_type": "text",
                "create_time": as_epoch_ms(when),
                "deleted": False,
                "sender": {
                    "id": sender_open_id,
                    "id_type": "open_id",
                    "sender_type": "user",
                },
                "body": {"content": json.dumps({"text": content}, ensure_ascii=False)},
            }
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if path == "/open-apis/auth/v3/tenant_access_token/internal":
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"msg": "denied"})
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "t-test",
                    "expire": 7200,
                },
            )

        if not request.headers.get("Authorization"):
            return httpx.Response(401, json={"code": 99991661, "msg": "no token"})

        if path == "/open-apis/task/v2/tasklists":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"items": self.tasklists, "has_more": False}},
            )

        for guid, items in self.tasks.items():
            if path == f"/open-apis/task/v2/tasklists/{guid}/tasks":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"items": items, "has_more": False}},
                )

        if path == f"/open-apis/im/v1/chats/{CHAT_ID}":
            return httpx.Response(
                200, json={"code": 0, "data": {"name": self.chat_name}}
            )

        if path == "/open-apis/im/v1/messages" and method == "GET":
            if self.fail_messages_api:
                return httpx.Response(
                    200, json={"code": 99991672, "msg": "permission denied"}
                )
            self.message_query_params = dict(request.url.params)
            return httpx.Response(
                200,
                json={"code": 0, "data": {"items": self.messages, "has_more": False}},
            )

        if path == "/open-apis/im/v1/messages" and method == "POST":
            if self.fail_send:
                return httpx.Response(
                    200, json={"code": 230002, "msg": "The bot is not in the group."}
                )
            self.sent.append(json.loads(request.content))
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_1"}})

        if path == "/open-apis/bot/v3/info":
            return httpx.Response(
                200, json={"code": 0, "bot": {"app_name": "智能日报"}}
            )

        return httpx.Response(404, json={"code": 99999, "msg": f"unhandled {path}"})


# ---------------------------------------------------------------------------
# 世界
# ---------------------------------------------------------------------------


class FakeSmtp:
    """顶替 smtplib.SMTP_SSL / SMTP。"""

    sent: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, message):
        FakeSmtp.sent.append(message)

    def quit(self):
        pass

    def close(self):
        pass


class World:
    def __init__(self, tmp_path: Path, monkeypatch, config_data: dict):
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.github = FakeGithub()
        self.feishu = FakeFeishu()
        self.config_data = config_data

        FakeSmtp.sent = []
        self.emails = FakeSmtp.sent

        self._write_config()
        self._patch_http()
        self._patch_smtp()

    # -- 装配 ----------------------------------------------------------

    def _write_config(self):
        self.path = self.tmp_path / "config.yaml"
        self.path.write_text(
            yaml.safe_dump(self.config_data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def reconfigure(self, data: dict) -> "World":
        """换一份配置重跑，假服务端与补丁保持复用。"""
        self.config_data = data
        self._write_config()
        return self

    def _patch_http(self):
        gh_transport = httpx.MockTransport(self.github.handler)
        lark_transport = httpx.MockTransport(self.feishu.handler)

        real_github_collect = github_module.collect

        def github_collect(*args, **kwargs):
            kwargs["transport"] = gh_transport
            return real_github_collect(*args, **kwargs)

        self.monkeypatch.setattr(main.github, "collect", github_collect)
        self.monkeypatch.setattr(
            lark_task_module, "LarkClient", lambda *a, **k: LarkClient(*a, transport=lark_transport, **k)
        )
        self.monkeypatch.setattr(
            lark_msg_module, "LarkClient", lambda *a, **k: LarkClient(*a, transport=lark_transport, **k)
        )
        self.monkeypatch.setattr(
            lark_bot_module, "LarkClient", lambda *a, **k: LarkClient(*a, transport=lark_transport, **k)
        )
        self.monkeypatch.setattr(
            main, "LarkClient", lambda *a, **k: LarkClient(*a, transport=lark_transport, **k)
        )
        lark_task_module._client = None
        lark_bot_module._client = None

    def _patch_smtp(self):
        import smtplib

        self.monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtp)
        self.monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)

    # -- 运行 ----------------------------------------------------------

    def run(self, *, push=True, now=None):
        return main._run_pipeline(self.load_config(), push=push, now=now)

    def load_config(self):
        import os

        from shared.config import load

        return load(self.path)

    def db(self):
        storage.init_db(self.tmp_path / self.config_data["schedule"]["db_path"])


def config_data(*, members=None, holidays=(), workdays=(), repos=(REPO,)):
    return {
        "team": {"name": "研发一组"},
        "members": members
        or [
            {"name": "张三", "github": "zhangsan", "lark_open_id": "ou_zhang"},
            {"name": "李四", "github": "lisi-dev", "lark_open_id": "ou_li"},
            {"name": "王五", "github": "wangwu", "lark_open_id": "ou_wang"},
        ],
        "github": {"repos": list(repos)},
        "lark": {"chat_id": CHAT_ID, "project_id": ""},
        "smtp": {
            "host": "smtp.qq.com",
            "port": 465,
            "sender": "sender@qq.com",
            "recipients": ["leader@company.com"],
        },
        "filters": {"keywords": ["项目", "模板"], "sensitive_keywords": ["薪资"]},
        "schedule": {
            "timezone": "Asia/Shanghai",
            "db_path": "data/daily.db",
            "log_dir": "logs",
            "holidays": list(holidays),
            "workdays": list(workdays),
        },
    }


WORKDAY = datetime(2026, 10, 12, 18, 0, tzinfo=TZ)  # 周一


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ENV_VARS:
        monkeypatch.setenv(name, ENV_VARS[name])

    w = World(tmp_path, monkeypatch, config_data())
    w.github.add_commit(
        REPO, "sha1", "zhangsan", "feat: 日报模板支持三段式编排",
        WORKDAY.replace(hour=10, minute=20), 214, 37, 6,
    )
    w.github.add_commit(
        REPO, "sha2", "lisi-dev", "refactor: 提取 CollectResult 到共享层",
        WORKDAY.replace(hour=11, minute=45), 96, 141, 9,
    )
    w.feishu.add_task("tl_1", "日报模板设计", "ou_zhang", WORKDAY.replace(hour=16, minute=30))
    w.feishu.add_message("ou_zhang", "项目模板已提交，麻烦帮忙看下", WORKDAY.replace(hour=14, minute=32))
    w.feishu.add_message("ou_wang", "项目这边今天下午请假", WORKDAY.replace(hour=15, minute=10))
    return w


def read_report(world, day=None):
    world.db()
    return storage.get_report(day or WORKDAY.date())


# ---------------------------------------------------------------------------
# 正常场景
# ---------------------------------------------------------------------------


def test_happy_path_produces_a_complete_report(world):
    assert world.run(now=WORKDAY) == main.EXIT_OK

    report = read_report(world)
    assert report is not None
    assert report.team_name == "研发一组"
    assert report.failed_sources == []
    assert [m.name for m in report.members] == ["张三", "李四", "王五"]

    assert "## 代码提交" in report.markdown
    assert "## 任务进展" in report.markdown
    assert "## 协作沟通" in report.markdown

    assert "feat: 日报模板支持三段式编排" in report.markdown
    assert "+214 行 / -37 行" in report.markdown
    # 采集层返回的是接口原始状态码（todo/done），表述层翻成中文
    assert "日报模板设计：待办 → 已完成（16:30）" in report.markdown
    assert "项目模板已提交，麻烦帮忙看下" in report.markdown


def test_happy_path_pushes_to_both_channels(world):
    assert world.run(now=WORKDAY) == main.EXIT_OK

    assert len(world.emails) == 1
    message = world.emails[0]
    assert "研发一组" in message["Subject"]
    assert WORKDAY.date().isoformat() in message["Subject"]
    assert "leader@company.com" in message["To"]

    assert len(world.feishu.sent) == 1
    body = world.feishu.sent[0]
    assert body["receive_id"] == CHAT_ID
    assert body["msg_type"] == "post"


def test_records_are_attributed_to_the_right_people(world):
    world.run(now=WORKDAY)

    report = read_report(world)
    by_name = {m.name: m for m in report.members}

    assert [c.message for c in by_name["张三"].commits] == ["feat: 日报模板支持三段式编排"]
    assert [c.message for c in by_name["李四"].commits] == ["refactor: 提取 CollectResult 到共享层"]
    assert by_name["王五"].commits == []

    assert [t.title for t in by_name["张三"].tasks] == ["日报模板设计"]
    assert [m.content for m in by_name["王五"].messages] == ["项目这边今天下午请假"]


def test_unmapped_accounts_are_dropped(world):
    world.github.add_commit(
        REPO, "sha9", "stranger", "feat: 陌生人提交", WORKDAY.replace(hour=12), 1, 1, 1
    )

    world.run(now=WORKDAY)

    report = read_report(world)
    assert "陌生人提交" not in report.markdown


def test_feishu_message_query_uses_seconds_not_milliseconds(world):
    """请求参数是**秒级**时间戳，响应体才是毫秒 —— 搞反会让窗口偏差 1000 倍。"""
    world.run(now=WORKDAY)

    params = world.feishu.message_query_params
    start = int(params["start_time"])
    end = int(params["end_time"])

    window_start = int(datetime.combine(WORKDAY.date(), dtime.min, tzinfo=TZ).timestamp())
    assert abs(start - window_start) <= 1
    assert abs(end - int(WORKDAY.timestamp())) <= 1


def test_sensitive_messages_never_reach_the_report(world):
    world.feishu.add_message("ou_zhang", "项目这边薪资要调整一下", WORKDAY.replace(hour=13))

    world.run(now=WORKDAY)

    report = read_report(world)
    assert "薪资" not in report.markdown
    assert "薪资" not in report.html
    assert "薪资" not in world.emails[0].get_content()


def test_bot_own_messages_are_not_recollected(world):
    """否则前一天推送的日报会被当成今天的讨论记录采回来，形成回环。"""
    world.feishu.add_message("ou_the_bot", "项目 昨日日报已推送", WORKDAY.replace(hour=9))

    world.run(now=WORKDAY)

    report = read_report(world)
    assert "昨日日报已推送" not in report.markdown


# ---------------------------------------------------------------------------
# 降级：部分失败
# ---------------------------------------------------------------------------


def test_partial_github_failure_keeps_other_repos_data(world):
    world.github.add_commit(
        "kkkkkhhh/legacy-tool", "sha3", "zhangsan", "fix: 旧仓库的提交",
        WORKDAY.replace(hour=9), 1, 1, 1,
    )
    world.github.fail.add("kkkkkhhh/legacy-tool")
    world.reconfigure(config_data(repos=(REPO, "kkkkkhhh/legacy-tool")))

    assert world.run(now=WORKDAY) == main.EXIT_OK

    report = read_report(world)
    assert report.failed_sources == ["github"]
    assert report.failed_details == {"github": ["kkkkkhhh/legacy-tool"]}
    assert "kkkkkhhh/legacy-tool" in report.markdown
    # 好仓库的数据照样在
    assert "feat: 日报模板支持三段式编排" in report.markdown


def test_one_source_down_does_not_block_the_others(world):
    world.feishu.fail_messages_api = True

    assert world.run(now=WORKDAY) == main.EXIT_OK

    report = read_report(world)
    assert report.failed_sources == ["lark_msg"]
    assert "数据获取失败" in report.markdown

    task_section = report.markdown.split("## 任务进展")[1].split("## 协作沟通")[0]
    assert "日报模板设计" in task_section


# ---------------------------------------------------------------------------
# 空数据
# ---------------------------------------------------------------------------


def test_empty_day_shows_no_records_today(world):
    """清空的是各接口返回的**列表内容**，而不是字典本身 ——
    把字典清掉会让假服务端返回 404，那测的就变成「故障」而非「空数据」了。"""
    world.github.repos[REPO] = []
    world.feishu.tasks["tl_1"] = []
    world.feishu.messages = []

    assert world.run(now=WORKDAY) == main.EXIT_OK

    report = read_report(world)
    assert report.failed_sources == []  # 全成功，只是没数据
    assert report.markdown.count("今日无记录") == 9  # 3 个成员 × 3 个板块
    assert "数据获取失败" not in report.markdown


def test_member_with_no_activity_is_still_listed(world):
    """某人当天什么都没干，应该在日报上显示「今日无记录」，而不是整个消失。"""
    world.run(now=WORKDAY)

    report = read_report(world)
    commit_section = report.markdown.split("## 代码提交")[1].split("## 任务进展")[0]
    assert "### 王五" in commit_section
    assert "今日无记录" in commit_section


# ---------------------------------------------------------------------------
# 全部失败
# ---------------------------------------------------------------------------


def test_all_sources_down_aborts_without_empty_report(world):
    world.github.fail.add(REPO)
    world.feishu.fail_messages_api = True

    original = world.feishu.handler

    def failing_handler(request):
        if request.url.path == "/open-apis/task/v2/tasklists":
            return httpx.Response(200, json={"code": 99991672, "msg": "no permission"})
        return original(request)

    world.monkeypatch.setattr(world.feishu, "handler", failing_handler)
    world._patch_http()

    assert world.run(now=WORKDAY) == main.EXIT_FAILED
    assert read_report(world) is None  # 不生成空日报

    # 全源失败**要发告警**（design.md §6.1），但绝不推日报。
    # 用 msg_type 区分：日报走 post 富文本，告警走 text。
    subjects = [m["Subject"] for m in world.emails]
    assert len(subjects) == 1
    assert "所有数据源均不可用" in subjects[0]
    assert world.feishu.sent, "告警应当同时走飞书通道"
    assert all(body["msg_type"] == "text" for body in world.feishu.sent)


def test_all_sources_down_sends_an_alert(world):
    world.github.fail.add(REPO)
    world.feishu.fail_messages_api = True

    original = world.feishu.handler

    def failing_handler(request):
        if request.url.path == "/open-apis/task/v2/tasklists":
            return httpx.Response(200, json={"code": 99991672, "msg": "no permission"})
        return original(request)

    world.monkeypatch.setattr(world.feishu, "handler", failing_handler)
    world._patch_http()

    alerts = []
    world.monkeypatch.setattr(
        main.alert,
        "report_alert",
        lambda subject, detail, channels=None, **kw: alerts.append((subject, detail)),
    )

    world.run(now=WORKDAY)

    assert len(alerts) == 1
    assert "所有数据源均不可用" in alerts[0][0]


# ---------------------------------------------------------------------------
# 推送失败 → 另一通道告警
# ---------------------------------------------------------------------------


def test_feishu_push_failure_alerts_via_email(world):
    world.feishu.fail_send = True

    assert world.run(now=WORKDAY) == main.EXIT_OK  # 邮件成功即算送达

    subjects = [m["Subject"] for m in world.emails]
    assert any("研发一组" in s and "日报" in s for s in subjects)
    assert any("推送失败" in s for s in subjects)


# ---------------------------------------------------------------------------
# 非工作日
# ---------------------------------------------------------------------------


def test_weekend_is_skipped_end_to_end(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name, value in ENV_VARS.items():
        monkeypatch.setenv(name, value)

    saturday = datetime(2026, 10, 17, 18, 0, tzinfo=TZ)
    w = World(tmp_path, monkeypatch, config_data())

    assert w.run(now=saturday) == main.EXIT_OK
    assert w.github.calls == []
    assert read_report(w, saturday.date()) is None


def test_makeup_workday_runs_on_a_weekend(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name, value in ENV_VARS.items():
        monkeypatch.setenv(name, value)

    saturday = datetime(2026, 10, 17, 18, 0, tzinfo=TZ)
    w = World(tmp_path, monkeypatch, config_data(workdays=[saturday.date().isoformat()]))
    w.github.add_commit(REPO, "s", "zhangsan", "feat: 补班日的提交", saturday.replace(hour=10), 1, 1, 1)

    assert w.run(now=saturday) == main.EXIT_OK
    assert read_report(w, saturday.date()) is not None


# ---------------------------------------------------------------------------
# 性能（proposal.md §3.2）
# ---------------------------------------------------------------------------


def test_pipeline_completes_within_sixty_seconds(world):
    """单次日报生成 <60s（5 人团队）。Mock 环境下这条余地很大，
    真正要防的是将来引入同步等待（比如把重试间隔写死成不可注入）。"""
    import time

    members = [
        {"name": f"成员{i}", "github": f"user{i}", "lark_open_id": f"ou_{i}"}
        for i in range(5)
    ]
    world.reconfigure(config_data(members=members))

    started = time.monotonic()
    assert world.run(now=WORKDAY) == main.EXIT_OK
    elapsed = time.monotonic() - started

    assert elapsed < 60


# ---------------------------------------------------------------------------
# CLI 端到端
# ---------------------------------------------------------------------------


def test_cli_full_run(tmp_path, monkeypatch):
    """真正走 main() 入口一次，覆盖参数解析与配置加载。

    配置里把「今天」写成补班日，否则今天恰好是周末时会被跳过。
    """
    monkeypatch.chdir(tmp_path)
    for name, value in ENV_VARS.items():
        monkeypatch.setenv(name, value)

    today = datetime.now(TZ)
    w = World(tmp_path, monkeypatch, config_data(workdays=[today.date().isoformat()]))
    w.github.add_commit(REPO, "sha_cli", "zhangsan", "feat: CLI 路径的提交", today - timedelta(hours=1), 3, 1, 1)

    assert main.main([]) == main.EXIT_OK

    report = read_report(w, today.date())
    assert report is not None
    assert "feat: CLI 路径的提交" in report.markdown
