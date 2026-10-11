"""跨数据源的成员聚合（design.md §4.5）。

职责：按 design.md §3.4 的规则，把三个独立数据源的记录按「人」聚合为
MemberReport；不在映射表中的账号，其记录一律丢弃（§6.2 访问控制）。

为什么单独成为一个接口：聚合规则原先只有描述、没有接口、没有属主，
实现者要么违反 §2 对 main.py 的边界约束（「不含业务逻辑」），要么自创
契约之外的接口。接口化后，「谁执行聚合」与「聚合产出什么」都有定义。
"""

from __future__ import annotations

from shared.logger import get_logger
from shared.models import (
    CollectResult,
    CommitRecord,
    MemberConfig,
    MemberReport,
    MessageRecord,
    TaskRecord,
)

logger = get_logger("generator.aggregator")


def aggregate(
    commits: CollectResult[CommitRecord],
    tasks: CollectResult[TaskRecord],
    messages: CollectResult[MessageRecord],
    members: list[MemberConfig],
) -> list[MemberReport]:
    """把三个数据源的记录按成员聚合。

    Args:
        commits: GitHub 采集结果。
        tasks: 飞书任务采集结果。
        messages: 飞书消息采集结果。
        members: 身份映射表。

    Returns:
        与 ``members`` **同序**的 MemberReport 列表 —— 顺序即配置顺序。
        要生成日报的人由配置决定，而不是由「谁恰好有数据」决定；
        否则某人当天没提交就会从日报里消失，而不是显示「今日无记录」。

    Note:
        **本函数只读不改**采集结果。失败信息不被消费，由编排层另行透传给
        ``generate(failed_sources=...)``（design.md §4.5）。

        飞书记录以 ``open_id`` 为准；若配置里写的是邮箱，编排层须先用
        ``LarkClient.resolve_open_ids()`` 反查后再传入（design.md §3.4）。
    """
    reports = {
        member.name: MemberReport(name=member.name, github_username=member.github)
        for member in members
    }

    by_github = {member.github: member for member in members}
    by_lark = {
        member.lark_identifier: member
        for member in members
        if member.lark_identifier
    }

    dropped = {"commits": 0, "tasks": 0, "messages": 0}

    for record in commits.records:
        owner = by_github.get(record.author)
        if owner is None:
            dropped["commits"] += 1
            continue
        reports[owner.name].commits.append(record)

    for task in tasks.records:
        owner = by_lark.get(task.assignee)
        if owner is None:
            dropped["tasks"] += 1
            continue
        reports[owner.name].tasks.append(task)

    for message in messages.records:
        owner = by_lark.get(message.sender)
        if owner is None:
            dropped["messages"] += 1
            continue
        reports[owner.name].messages.append(message)

    if any(dropped.values()):
        logger.info(
            "存在未映射账号的记录，已按访问控制丢弃",
            extra={"fields": {"dropped": dropped, "member_count": len(members)}},
        )

    return [reports[member.name] for member in members]
