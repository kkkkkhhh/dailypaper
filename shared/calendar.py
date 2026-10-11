"""工作日判定（design.md §6.6）。

proposal.md §3.3 要求「识别非工作日（周末及法定节假日），并自动跳过
日报生成」。判定采用三级顺序，两个清单缺一不可：

1. workdays 命中 → 工作日（调休补班日，即使是周末）
2. holidays 命中 → 非工作日（法定假日，即使是周一）
3. 否则按周末判定

为什么需要 workdays：中国的法定节假日安排包含**调休补班**，某些周六
周日是法定工作日。只判周末会把补班日误判为休息而漏发日报。
"""

from __future__ import annotations

from datetime import date


def is_workday(d: date, holidays: list[date], workdays: list[date]) -> bool:
    """判定 d 是否为工作日。

    Args:
        d: 待判定的日期。
        holidays: 法定假日清单。
        workdays: 调休补班日清单（优先级高于 holidays）。

    Returns:
        True 表示应当生成日报。
    """
    if d in workdays:
        return True
    if d in holidays:
        return False
    return d.weekday() < 5


def skip_reason(d: date, holidays: list[date], workdays: list[date]) -> str | None:
    """工作日返回 None；非工作日返回可直接写入日志的判定依据。

    日志需要记录「跳过原因与判定依据」（tasks.md Task 9），
    仅一个布尔值不足以说明是哪种规则命中的。
    """
    if is_workday(d, holidays, workdays):
        return None
    if d in holidays:
        return f"{d.isoformat()} 命中法定假日清单"
    return f"{d.isoformat()} 为周末（周六/周日）且不在调休补班日清单中"
