"""日报生成（design.md §4.2）。

把已聚合的成员数据整理成「代码提交 → 任务进展 → 协作沟通」三段式日报，
输出 Markdown 与 HTML。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from shared.logger import get_logger
from shared.models import DailyReport, MemberReport
from generator.template import render_html, render_markdown

logger = get_logger("generator.formatter")

# (板块标题, 数据源标识, MemberReport 上的字段名, 展示用名称)
_SECTIONS: tuple[tuple[str, str, str, str], ...] = (
    ("代码提交", "github", "commits", "GitHub"),
    ("任务进展", "lark_task", "tasks", "飞书任务"),
    ("协作沟通", "lark_msg", "messages", "飞书消息"),
)

_SOURCE_LABELS = {source: label for _, source, _, label in _SECTIONS}


def generate(
    members: list[MemberReport],
    failed_sources: list[str],
    date: date,
    team_name: str,
    now: datetime | None = None,
    failed_details: dict[str, list[str]] | None = None,
) -> DailyReport:
    """生成日报。

    Args:
        members: 各成员数据（已聚合）。
        failed_sources: 采集失败的数据源标识，决定**哪些板块**显示失败标注。
            **没有它就无法区分「今日无记录」与「数据获取失败」** —— 这两者
            会产出完全相同的成员数据，只看 members 是分辨不出来的。
        date: 日报日期。
        team_name: 团队名称。
        now: 生成时刻，仅测试用于固定时间。生产代码不应传。
        failed_details: 数据源 → 失败子项明细（如 ``{"github": ["owner/repoB"]}``），
            渲染成标注括号里的内容。整源失败时值为该源的错误摘要。

    Returns:
        DailyReport，``markdown`` 与 ``html`` 由同一上下文分别渲染。
    """
    context = build_context(
        members, failed_sources, date, team_name, now, failed_details
    )

    markdown = render_markdown(context)
    html = render_html(context)

    logger.info(
        "日报已生成",
        extra={
            "fields": {
                "date": date.isoformat(),
                "member_count": len(members),
                "failed_sources": list(failed_sources),
                "markdown_chars": len(markdown),
            }
        },
    )

    return DailyReport(
        date=date,
        team_name=team_name,
        members=members,
        failed_sources=list(failed_sources),
        failed_details={k: list(v) for k, v in (failed_details or {}).items()},
        generated_at=context["generated_at_dt"],
        markdown=markdown,
        html=html,
    )


def build_context(
    members: list[MemberReport],
    failed_sources: list[str],
    report_date: date,
    team_name: str,
    now: datetime | None = None,
    failed_details: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """构造模板上下文。与模板的接口契约见 template.py。"""
    generated_at = now or datetime.now().astimezone()
    failed = set(failed_sources)
    details = failed_details or {}

    sections: list[dict[str, Any]] = []
    for title, source, attribute, label in _SECTIONS:
        sections.append(
            {
                "title": title,
                "kind": attribute,
                "source_label": label,
                "failed": source in failed,
                "failed_detail": "、".join(details.get(source, [])),
                "members": [
                    {"name": member.name, "records": list(getattr(member, attribute))}
                    for member in members
                ],
            }
        )

    unknown = failed - set(_SOURCE_LABELS)
    if unknown:
        # 数据源标识拼错时不该静默 —— 否则日报会无声地少一个「获取失败」标注
        names = "、".join(sorted(unknown))
        logger.error(
            f"failed_sources 含未知数据源标识（{names}），该失败将不会体现在任何板块",
            extra={"fields": {"unknown_sources": sorted(unknown)}},
        )

    return {
        "team_name": team_name,
        "date": report_date.isoformat(),
        "generated_at": generated_at.strftime("%Y-%m-%d %H:%M %z"),
        "generated_at_dt": generated_at,
        "failed_sources": sorted(failed),
        "sections": sections,
    }
