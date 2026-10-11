"""工作日判定测试（design.md §6.6）。

核心是三级判定顺序：workdays → holidays → 周末。
两个清单缺一不可 —— 只判周末会漏掉调休补班日，只判 holidays 会把补班日
误判成休息。
"""

from __future__ import annotations

from datetime import date

from shared.calendar import is_workday, skip_reason

# 2026-10-01 是周四（国庆）；2026-10-11 是周日（调休补班）；2026-10-12 是周一
HOLIDAY = date(2026, 10, 1)  # 周四
MAKEUP = date(2026, 10, 11)  # 周日
PLAIN_WEEKDAY = date(2026, 10, 12)  # 周一
PLAIN_WEEKEND = date(2026, 10, 17)  # 周六

NO_HOLIDAYS: list[date] = []
NO_WORKDAYS: list[date] = []


def test_plain_weekday_is_workday():
    assert is_workday(PLAIN_WEEKDAY, NO_HOLIDAYS, NO_WORKDAYS) is True


def test_plain_weekend_is_not_workday():
    assert is_workday(PLAIN_WEEKEND, NO_HOLIDAYS, NO_WORKDAYS) is False


def test_holiday_on_a_weekday_is_not_workday():
    """法定假日在周一至周五 —— 规则 2 覆盖规则 3。"""
    assert is_workday(HOLIDAY, [HOLIDAY], NO_WORKDAYS) is False


def test_makeup_day_on_a_weekend_is_workday():
    """调休补班日在周六/周日 —— 规则 1 覆盖规则 3。

    这是只判周末的实现会漏掉日报的那一天。
    """
    assert is_workday(MAKEUP, NO_HOLIDAYS, [MAKEUP]) is True


def test_workday_list_beats_holiday_list():
    """同一天同时出现在两个清单里时，workdays 优先。"""
    assert is_workday(MAKEUP, [MAKEUP], [MAKEUP]) is True


def test_holiday_list_does_not_affect_other_days():
    assert is_workday(PLAIN_WEEKDAY, [HOLIDAY], NO_WORKDAYS) is True


# --------------------------------------------------------------------------
# skip_reason：日志要记录「跳过原因与判定依据」（tasks.md Task 9）
# --------------------------------------------------------------------------


def test_skip_reason_is_none_on_workday():
    assert skip_reason(PLAIN_WEEKDAY, NO_HOLIDAYS, NO_WORKDAYS) is None


def test_skip_reason_names_the_holiday_rule():
    reason = skip_reason(HOLIDAY, [HOLIDAY], NO_WORKDAYS)

    assert reason is not None
    assert "法定假日" in reason
    assert HOLIDAY.isoformat() in reason


def test_skip_reason_names_the_weekend_rule():
    reason = skip_reason(PLAIN_WEEKEND, NO_HOLIDAYS, NO_WORKDAYS)

    assert reason is not None
    assert "周末" in reason
    assert PLAIN_WEEKEND.isoformat() in reason


def test_skip_reason_distinguishes_holiday_from_weekend():
    """两种跳过原因的文案必须可区分 —— 否则日志无法说明是哪条规则命中的。"""
    holiday_reason = skip_reason(HOLIDAY, [HOLIDAY], NO_WORKDAYS)
    weekend_reason = skip_reason(PLAIN_WEEKEND, NO_HOLIDAYS, NO_WORKDAYS)

    assert holiday_reason != weekend_reason
