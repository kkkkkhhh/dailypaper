"""JSON lines 日志（design.md §6.3）。

- 格式：每行一个 JSON 对象，便于日志分析工具解析
- 级别：INFO（正常流程）+ ERROR（异常）
- 每次执行记录：开始时间、结束时间、各数据源采集条数及推送结果

用法：main.py 启动时调用一次 configure_logging()，各模块用 get_logger()。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_LOG_FILE = "daily_report.log"

# 给自家 handler 打的标记。宿主应用（如 pytest）也会在根 logger 上挂 handler，
# 只看「有没有 handler」会把它们误当成已配置；移除时也只该移除自己的，
# 否则会破坏宿主的日志捕获。
_HANDLER_MARK = "_daily_report_handler"


def _own_handlers() -> list[logging.Handler]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, _HANDLER_MARK, False)
    ]


class JsonLinesFormatter(logging.Formatter):
    """把 LogRecord 渲染成单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .astimezone()
            .isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # 调用方通过 logger.info("...", extra={"fields": {...}}) 附加结构化字段
        extra_fields = getattr(record, "fields", None)
        if isinstance(extra_fields, dict):
            payload.update(extra_fields)

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


def configure_logging(log_dir: str | None = None, level: str = "INFO") -> None:
    """配置根 logger：JSON lines 输出到 stdout，可选同时落盘。

    Args:
        log_dir: 日志目录；None 或空表示不落盘，仅输出到 stdout。
        level: "INFO" 或 "ERROR"，大小写不敏感。
    """
    root = logging.getLogger()
    for handler in _own_handlers():
        root.removeHandler(handler)
        handler.close()

    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    formatter = JsonLinesFormatter()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    setattr(stream, _HANDLER_MARK, True)
    root.addHandler(stream)

    if log_dir:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(directory / _LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(formatter)
        setattr(file_handler, _HANDLER_MARK, True)
        root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """取得指定名称的 logger。

    若本模块尚未配置过，会以默认设置（INFO，仅 stdout）初始化一次，
    这样单独使用某个模块时也能正常输出。

    判断依据是「自家 handler 在不在」，而不是一个「是否已配置过」的标志位：
    标志位会在 handler 被外部移除后与实际状态脱节，届时日志会静默丢失。
    """
    if not _own_handlers():
        configure_logging()
    return logging.getLogger(name)
