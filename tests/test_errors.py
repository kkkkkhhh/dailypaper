"""自定义异常测试（design.md §4.4）。

验收标准：errors.py 定义 CollectorError、GeneratorError、NotifierError、
ConfigError 四个自定义异常。
"""

from __future__ import annotations

import pytest

from shared import errors


def test_four_exceptions_defined():
    assert hasattr(errors, "CollectorError")
    assert hasattr(errors, "GeneratorError")
    assert hasattr(errors, "NotifierError")
    assert hasattr(errors, "ConfigError")


@pytest.mark.parametrize(
    "name",
    ["CollectorError", "GeneratorError", "NotifierError", "ConfigError"],
)
def test_exception_is_raisable_and_catchable(name):
    exc_type = getattr(errors, name)
    assert issubclass(exc_type, Exception)

    with pytest.raises(exc_type):
        raise exc_type("boom")


def test_exceptions_are_distinct():
    """四个异常互不为子类 —— 否则 except 语句会误捕获相邻层的失败。"""
    types = [
        errors.CollectorError,
        errors.GeneratorError,
        errors.NotifierError,
        errors.ConfigError,
    ]

    for outer in types:
        for inner in types:
            if outer is inner:
                continue
            assert not issubclass(inner, outer)


def test_each_has_docstring():
    for name in ["CollectorError", "GeneratorError", "NotifierError", "ConfigError"]:
        assert getattr(errors, name).__doc__, f"{name} 缺少 docstring"
