"""主编排入口（design.md §2、tasks.md Task 9）。

职责严格限定为三件事：**按顺序调用各层、兜底全局异常、记录执行状态**。
**不含任何业务逻辑** —— 聚合在 `generator/aggregator.py`，格式化在
`generator/formatter.py`，失败信息怎么组进日报在 `generator/formatter.py`。
本模块只负责把它们串起来，以及决定失败时要不要告警。

三种运行模式：
- 默认：采集 → 聚合 → 生成 → 存档 → 推送
- `--dry-run`：采集 → 聚合 → 生成 → 存档，**不推送**；不受非工作日限制
- `--check`：逐项验证配置、凭证与各接口连通性，**不采集也不推送**
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from collector import github, lark_msg, lark_task
from collector.lark_client import LarkClient
from generator.aggregator import aggregate
from generator.formatter import generate
from notifier import alert, lark_bot
from notifier import email as email_notifier
from shared import storage
from shared.calendar import skip_reason
from shared.config import Config, load as load_config
from shared.errors import ConfigError
from shared.logger import configure_logging, get_logger
from shared.models import CollectResult, MemberConfig

EXIT_OK = 0
EXIT_FAILED = 1

#: 配置错误单独一个退出码，便于 cron 告警区分「没配好」与「跑失败」
EXIT_CONFIG_ERROR = 2

#: --check 探测采集接口时用的窗口：够短，不会拉到有意义的量，
#: 但足以让请求真的打到接口上，从而验证令牌与权限。
_PROBE_WINDOW = timedelta(minutes=1)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        # 配置还没读出来，只能用默认日志设置（仅 stdout）。
        # 缺失项写进消息本身而非只放结构化字段 —— 运维是直接 grep 日志的。
        configure_logging()
        get_logger("main").error(
            f"配置错误，无法启动：{exc}", extra={"fields": {"error": str(exc)}}
        )
        return EXIT_CONFIG_ERROR

    configure_logging(config.schedule.log_dir)

    if args.check:
        return _run_check(config)

    return _run_pipeline(config, push=not args.dry_run)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="智能日报生成器 —— 采集 GitHub 与飞书数据，生成日报并推送",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="逐项验证配置、凭证与各接口连通性，不采集也不推送",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="执行采集与生成，但不推送（不受非工作日限制）",
    )
    return parser.parse_args(argv)


# --------------------------------------------------------------------------
# 正常流程
# --------------------------------------------------------------------------


def _run_pipeline(config: Config, *, push: bool, now: datetime | None = None) -> int:
    log = get_logger("main")
    tz = ZoneInfo(config.schedule.timezone)
    started = now or datetime.now(tz)
    today = started.date()

    log.info(
        "执行开始",
        extra={"fields": {"date": today.isoformat(), "dry_run": not push}},
    )

    # 非工作日跳过只作用于正式执行 —— --dry-run 的用途正是在任意日期验证链路
    if push:
        reason = skip_reason(
            today, config.schedule.holidays, config.schedule.workdays
        )
        if reason is not None:
            log.info(
                "非工作日，跳过日报生成",
                extra={"fields": {"date": today.isoformat(), "reason": reason}},
            )
            return EXIT_OK

    storage.init_db(config.schedule.db_path)

    window_start = datetime.combine(today, dtime.min, tzinfo=tz)

    results = _collect_all(config, window_start, started)

    for source, result in results.items():
        log.info(
            "数据源采集完成",
            extra={
                "fields": {
                    "source": source,
                    "success": result.success,
                    "count": len(result.records),
                    "failed": list(result.failed),
                }
            },
        )

    failed_sources = [name for name, result in results.items() if not result.success]

    if len(failed_sources) == len(results):
        detail = "；".join(
            f"{name}: {result.error or '未知错误'}" for name, result in results.items()
        )
        log.error(
            "所有数据源均不可用，不生成空日报",
            extra={"fields": {"failed_sources": failed_sources, "detail": detail}},
        )
        alert.report_alert(
            "智能日报生成失败：所有数据源均不可用",
            f"日期：{today.isoformat()}\n\n{detail}",
            config=config,
        )
        return EXIT_FAILED

    members = _resolve_members(config)
    reports = aggregate(
        results["github"], results["lark_task"], results["lark_msg"], members
    )

    report = generate(
        reports,
        failed_sources,
        today,
        config.team_name,
        now=started,
        failed_details=_failure_details(results, failed_sources),
    )

    storage.save_report(report)
    log.info("日报已存档", extra={"fields": {"db_path": config.schedule.db_path}})

    if not push:
        log.info("dry-run 模式，跳过推送", extra={"fields": {"date": today.isoformat()}})
        return EXIT_OK

    return _push_and_finish(config, report)


def _collect_all(
    config: Config, since: datetime, until: datetime
) -> dict[str, CollectResult]:
    """调用三个采集模块。任一模块失败都以 CollectResult 表达，不会抛异常。

    注意此处**没有 try/except**：三个采集模块的契约就是「绝不抛异常」，
    若真抛了，那是契约被破坏 —— 应该让它炸出来，而不是在这里兜住掩盖掉。
    """
    return {
        "github": github.collect(
            config.github.repos,
            since,
            until,
            token=config.github.token,
            timezone=config.schedule.timezone,
        ),
        "lark_task": lark_task.collect(config.lark.project_id, since, until),
        "lark_msg": lark_msg.collect(
            config.lark.chat_id, config.filters.keywords, since, until
        ),
    }


def _failure_details(
    results: dict[str, CollectResult], failed_sources: list[str]
) -> dict[str, list[str]]:
    """把各源的失败信息整理成 `failed_details`（design.md §3.2）。

    部分失败用 `failed` 的子项明细；整源失败（`failed` 为空）则退回 `error` 摘要 ——
    否则日报上只会写「数据获取失败」而说不出失败在哪。
    """
    details: dict[str, list[str]] = {}
    for source in failed_sources:
        result = results[source]
        details[source] = list(result.failed) or ([result.error] if result.error else [])
    return details


def _resolve_members(config: Config) -> list[MemberConfig]:
    """把配置里用邮箱写的飞书标识解析成 open_id（design.md §3.4）。

    采集层返回的飞书身份是 open_id，而配置允许写邮箱 —— 中间差的一层解析
    在这里补上。**解析不到的成员必须显式报错**：否则他们的飞书记录会被
    聚合层按「未映射账号」静默丢弃，日报上只显示「今日无记录」，
    看起来像当天没干活。
    """
    log = get_logger("main")

    emails = [m.lark for m in config.members if not m.lark_open_id and m.lark]
    if not emails:
        return list(config.members)

    try:
        with LarkClient(config.lark.app_id, config.lark.app_secret) as client:
            resolved = client.resolve_open_ids(emails)
    except Exception as exc:  # noqa: BLE001 —— 解析失败不该让整个流程停摆
        log.error(
            "飞书邮箱反查失败，这些成员的飞书记录将被丢弃",
            extra={"fields": {"emails": emails, "error": f"{type(exc).__name__}: {exc}"}},
        )
        return list(config.members)

    missing = [email for email in emails if email not in resolved]
    if missing:
        log.error(
            "部分成员的飞书邮箱无法解析为 open_id，其飞书记录将被丢弃。"
            "可改在 config.yaml 用 lark_open_id 直接填写",
            extra={"fields": {"emails": missing}},
        )

    return [
        replace(member, lark_open_id=resolved[member.lark])
        if (not member.lark_open_id and member.lark in resolved)
        else member
        for member in config.members
    ]


def _push_and_finish(config: Config, report) -> int:
    """推送日报。失败走告警通道，且**用没失败的那个通道**。"""
    log = get_logger("main")

    email_ok = email_notifier.send(report, config.smtp.recipients)
    if not email_ok:
        alert.report_alert(
            "日报邮件推送失败",
            f"日期：{report.date.isoformat()}\n团队：{config.team_name}\n"
            f"收件人：{', '.join(config.smtp.recipients)}",
            channels=[alert.LARK],
            config=config,
        )

    lark_ok = lark_bot.send(report, config.lark.chat_id)
    if not lark_ok:
        alert.report_alert(
            "日报飞书推送失败",
            f"日期：{report.date.isoformat()}\n团队：{config.team_name}\n"
            f"目标群：{config.lark.chat_id}",
            channels=[alert.EMAIL],
            config=config,
        )

    log.info(
        "执行结束",
        extra={
            "fields": {
                "date": report.date.isoformat(),
                "email_pushed": email_ok,
                "lark_pushed": lark_ok,
                "failed_sources": list(report.failed_sources),
            }
        },
    )

    # 两个通道全挂说明日报根本没送出去，对 cron 而言算失败
    return EXIT_OK if (email_ok or lark_ok) else EXIT_FAILED


# --------------------------------------------------------------------------
# --check
# --------------------------------------------------------------------------


def _run_check(config: Config) -> int:
    """逐项验证配置、凭证与接口连通性。**不做任何采集与推送。**

    刻意不发送邮件与飞书消息 —— 健康检查不该在别人的收件箱和团队群里
    留下痕迹。推送通道改用「连接 + 登录」与「查询机器人自身信息」验证。
    """
    log = get_logger("main")
    tz = ZoneInfo(config.schedule.timezone)
    now = datetime.now(tz)

    checks: list[tuple[str, bool, str]] = [
        (
            "配置与环境变量",
            True,
            f"团队「{config.team_name}」，成员 {len(config.members)} 人，"
            f"仓库 {len(config.github.repos)} 个",
        ),
        ("GitHub API", *_probe_github(config, now)),
        ("飞书任务接口", *_probe_lark_task(config, now)),
        ("飞书消息接口", *_probe_lark_msg(config, now)),
        ("SMTP", *email_notifier.check(config.smtp)),
        ("飞书机器人", *lark_bot.check(config)),
    ]

    failed = 0
    for name, ok, detail in checks:
        extra = {"fields": {"check": name, "passed": ok, "detail": detail}}
        if ok:
            log.info(f"[通过] {name}：{detail}", extra=extra)
        else:
            failed += 1
            log.error(f"[失败] {name}：{detail}", extra=extra)

    if failed:
        log.error(
            "健康检查未通过",
            extra={"fields": {"failed_count": failed, "total": len(checks)}},
        )
        return EXIT_FAILED

    log.info("健康检查全部通过", extra={"fields": {"total": len(checks)}})
    return EXIT_OK


def _probe_github(config: Config, now: datetime) -> tuple[bool, str]:
    result = github.collect(
        config.github.repos,
        now - _PROBE_WINDOW,
        now,
        token=config.github.token,
        timezone=config.schedule.timezone,
    )
    if result.success:
        return True, f"令牌有效，{len(config.github.repos)} 个仓库均可访问"
    return False, result.error or "未知错误"


def _probe_lark_task(config: Config, now: datetime) -> tuple[bool, str]:
    result = lark_task.collect(config.lark.project_id, now - _PROBE_WINDOW, now)
    if result.success:
        scope = config.lark.project_id or "全部可读清单"
        return True, f"任务接口可用，采集范围：{scope}"
    return False, result.error or "未知错误"


def _probe_lark_msg(config: Config, now: datetime) -> tuple[bool, str]:
    result = lark_msg.collect(
        config.lark.chat_id, [], now - _PROBE_WINDOW, now
    )
    if result.success:
        return True, f"消息接口可用，目标群 {config.lark.chat_id}"
    return False, result.error or "未知错误"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
