"""配置读取与校验（design.md §4.4）。

职责边界（design.md §6.2「1. 密钥管理」）：
- `config.yaml` 只承载**非敏感**配置（仓库名、群 ID、收件人、成员映射…）
- 敏感凭证（Token / App Secret / 授权码）**仅从环境变量读取**

因此无论 config.yaml 怎么写，都不可能把密钥硬编码进去 —— 这不是靠
约定，是靠接口本身没有承载密钥的位置。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from shared.errors import ConfigError
from shared.models import MemberConfig

# --------------------------------------------------------------------------
# 配置对象
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GithubConfig:
    repos: list[str]
    token: str  # 环境变量 GITHUB_TOKEN


@dataclass(frozen=True)
class LarkConfig:
    chat_id: str
    project_id: str
    app_id: str  # 环境变量 FEISHU_APP_ID
    app_secret: str  # 环境变量 FEISHU_APP_SECRET
    bot_open_id: str | None = None  # 环境变量 FEISHU_BOT_OPEN_ID（可选）


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    sender: str
    recipients: list[str]
    password: str  # 环境变量 SMTP_PASSWORD


@dataclass(frozen=True)
class FiltersConfig:
    keywords: list[str] = field(default_factory=list)
    sensitive_keywords: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScheduleConfig:
    timezone: str
    db_path: str
    log_dir: str
    holidays: list[date] = field(default_factory=list)
    workdays: list[date] = field(default_factory=list)


@dataclass(frozen=True)
class Config:
    team_name: str
    members: list[MemberConfig]
    github: GithubConfig
    lark: LarkConfig
    smtp: SmtpConfig
    filters: FiltersConfig
    schedule: ScheduleConfig


# --------------------------------------------------------------------------
# 必填项声明
# --------------------------------------------------------------------------

_REQUIRED_FIELDS = (
    "team.name",
    "github.repos",
    "lark.chat_id",
    "smtp.host",
    "smtp.port",
    "smtp.sender",
    "smtp.recipients",
    "schedule.timezone",
    "schedule.db_path",
    "schedule.log_dir",
)

_REQUIRED_ENV = (
    "GITHUB_TOKEN",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "SMTP_PASSWORD",
)


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------


class _Missing:
    """区分「取到 None」与「键不存在」。"""

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<MISSING>"


_MISSING = _Missing()


def _dig(data: Any, dotted: str) -> Any:
    """按 "a.b.c" 取值；任一层缺失则返回 _MISSING。"""
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _as_list(value: Any, field_name: str) -> list[Any]:
    if value is _MISSING or value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"配置字段 {field_name} 应为列表，实际是 {type(value).__name__}")
    return value


def _parse_dates(values: list[Any], field_name: str) -> list[date]:
    """把 ["2026-10-01", ...] 解析为 date 列表。"""
    parsed: list[date] = []
    for item in values:
        if isinstance(item, date):
            parsed.append(item)
            continue
        try:
            parsed.append(date.fromisoformat(str(item)))
        except ValueError as exc:
            raise ConfigError(
                f"配置字段 {field_name} 含非法日期 {item!r}，应为 YYYY-MM-DD 格式"
            ) from exc
    return parsed


def load_dotenv(path: str | Path = ".env") -> None:
    """把 .env 读进 os.environ，**不覆盖**已存在的变量。

    真实环境变量优先于 .env —— 这样 CI 或容器里注入的凭证不会被本地
    文件意外盖掉。
    """
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _read_yaml(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise ConfigError(
            f"配置文件不存在：{config_path}（可从 config.yaml.example 复制）"
        )
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 {config_path} 不是合法 YAML：{exc}") from exc

    if raw is None:
        raise ConfigError(f"配置文件 {config_path} 为空")
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件 {config_path} 的顶层应为映射结构")
    return raw


def _parse_members(raw: Any) -> tuple[list[MemberConfig], list[str]]:
    """解析身份映射表，返回 (成员列表, 缺失项描述)。"""
    problems: list[str] = []

    if raw is _MISSING or raw is None:
        return [], ["members（身份映射表为空）"]

    if not isinstance(raw, list) or not raw:
        return [], ["members（应为非空的成员列表）"]

    members: list[MemberConfig] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            problems.append(f"members[{index}]（应为映射结构）")
            continue

        label = f"members[{index}].{{}}".format
        name = item.get("name")
        github = item.get("github")
        lark = item.get("lark")
        lark_open_id = item.get("lark_open_id")

        if not name:
            problems.append(label("name"))
        if not github:
            problems.append(label("github"))
        if not lark and not lark_open_id:
            problems.append(label("lark 或 lark_open_id（至少填一个）"))
        if problems and (not name or not github):
            continue

        if name and github:
            members.append(
                MemberConfig(
                    name=str(name),
                    github=str(github),
                    lark=str(lark) if lark else None,
                    lark_open_id=str(lark_open_id) if lark_open_id else None,
                )
            )

    return members, problems


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def load(path: str | Path = "config.yaml") -> Config:
    """读取 config.yaml 与环境变量，返回校验过的 Config。

    Raises:
        ConfigError: 缺少必填字段或环境变量。错误信息会**列出全部**缺失项，
            而不是遇到第一个就中断 —— 一次报全比逐个试错省时间。
    """
    load_dotenv()
    raw = _read_yaml(Path(path))

    missing_fields = [key for key in _REQUIRED_FIELDS if _dig(raw, key) is _MISSING]

    members, member_problems = _parse_members(_dig(raw, "members"))

    missing_env = [name for name in _REQUIRED_ENV if not os.environ.get(name)]

    if missing_fields or member_problems or missing_env:
        parts: list[str] = []
        if missing_fields:
            parts.append("config.yaml 缺少必填字段：" + "、".join(missing_fields))
        if member_problems:
            parts.append("身份映射表存在问题：" + "、".join(member_problems))
        if missing_env:
            parts.append("环境变量未设置：" + "、".join(missing_env))
        raise ConfigError("配置不完整 —— " + "；".join(parts))

    return Config(
        team_name=str(_dig(raw, "team.name")),
        members=members,
        github=GithubConfig(
            repos=[str(r) for r in _as_list(_dig(raw, "github.repos"), "github.repos")],
            token=os.environ["GITHUB_TOKEN"],
        ),
        lark=LarkConfig(
            chat_id=str(_dig(raw, "lark.chat_id")),
            project_id=str(_dig(raw, "lark.project_id") or ""),
            app_id=os.environ["FEISHU_APP_ID"],
            app_secret=os.environ["FEISHU_APP_SECRET"],
            bot_open_id=os.environ.get("FEISHU_BOT_OPEN_ID") or None,
        ),
        smtp=SmtpConfig(
            host=str(_dig(raw, "smtp.host")),
            port=int(_dig(raw, "smtp.port")),
            sender=str(_dig(raw, "smtp.sender")),
            recipients=[
                str(r) for r in _as_list(_dig(raw, "smtp.recipients"), "smtp.recipients")
            ],
            password=os.environ["SMTP_PASSWORD"],
        ),
        filters=FiltersConfig(
            keywords=[
                str(k)
                for k in _as_list(_dig(raw, "filters.keywords"), "filters.keywords")
            ],
            sensitive_keywords=[
                str(k)
                for k in _as_list(
                    _dig(raw, "filters.sensitive_keywords"),
                    "filters.sensitive_keywords",
                )
            ],
        ),
        schedule=ScheduleConfig(
            timezone=str(_dig(raw, "schedule.timezone")),
            db_path=str(_dig(raw, "schedule.db_path")),
            log_dir=str(_dig(raw, "schedule.log_dir")),
            holidays=_parse_dates(
                _as_list(_dig(raw, "schedule.holidays"), "schedule.holidays"),
                "schedule.holidays",
            ),
            workdays=_parse_dates(
                _as_list(_dig(raw, "schedule.workdays"), "schedule.workdays"),
                "schedule.workdays",
            ),
        ),
    )
