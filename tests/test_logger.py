"""日志模块测试（design.md §6.3）。

验收标准：logger.py 输出 JSON lines 格式日志 —— 即每一行都是一个
可独立 json.loads 的对象。
"""

from __future__ import annotations

import json
import logging

import pytest

from shared.logger import configure_logging, get_logger


@pytest.fixture(autouse=True)
def reset_logging():
    """每个测试前后只移除**本模块**的 handler。

    不能清空根 logger 的全部 handler —— pytest 自己也在上面挂了日志捕获
    handler，一并删掉会破坏测试框架本身。
    """
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_daily_report_handler", False):
            root.removeHandler(handler)
            handler.close()


def read_lines(capsys) -> list[dict]:
    """把 stdout 的每一行解析成 JSON。"""
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_each_line_is_valid_json(capsys):
    configure_logging()
    log = get_logger("test")

    log.info("第一条")
    log.info("第二条")

    lines = read_lines(capsys)
    assert len(lines) == 2
    assert [line["msg"] for line in lines] == ["第一条", "第二条"]


def test_required_keys_present(capsys):
    configure_logging()
    get_logger("test.meta").info("hello")

    payload = read_lines(capsys)[0]

    assert set(payload) >= {"ts", "level", "logger", "msg"}
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.meta"


def test_chinese_not_escaped(capsys):
    """ensure_ascii=False —— 日志里的中文应当可读，而不是 \\uXXXX。"""
    configure_logging()
    get_logger("test").info("日报生成完成")

    raw = capsys.readouterr().out
    assert "日报生成完成" in raw


def test_structured_fields_are_merged(capsys):
    """各数据源采集条数等结构化信息通过 extra 附加（design.md §6.3）。"""
    configure_logging()
    get_logger("test").info(
        "采集完成",
        extra={"fields": {"source": "github", "count": 7}},
    )

    payload = read_lines(capsys)[0]
    assert payload["source"] == "github"
    assert payload["count"] == 7


def test_error_level(capsys):
    configure_logging()
    get_logger("test").error("出错了")

    payload = read_lines(capsys)[0]
    assert payload["level"] == "ERROR"
    assert payload["msg"] == "出错了"


def test_exception_is_serialized(capsys):
    configure_logging()
    log = get_logger("test")

    try:
        raise ValueError("boom")
    except ValueError:
        log.error("捕获异常", exc_info=True)

    payload = read_lines(capsys)[0]
    assert "boom" in payload["exc"]


def test_file_output(tmp_path, capsys):
    log_dir = tmp_path / "logs"
    configure_logging(log_dir=str(log_dir))
    get_logger("test").info("落盘测试")

    written = (log_dir / "daily_report.log").read_text(encoding="utf-8").strip()
    payload = json.loads(written)

    assert payload["msg"] == "落盘测试"

    # stdout 与文件内容一致
    assert read_lines(capsys)[0]["msg"] == "落盘测试"


def test_get_logger_without_configure_is_usable(capsys):
    """单独使用某模块时也应能正常输出，不必强制先 configure。"""
    get_logger("standalone").info("即取即用")

    assert read_lines(capsys)[0]["msg"] == "即取即用"


def test_level_filtering(capsys):
    configure_logging(level="ERROR")
    log = get_logger("test")

    log.info("不该出现")
    log.error("该出现")

    lines = read_lines(capsys)
    assert [line["msg"] for line in lines] == ["该出现"]
