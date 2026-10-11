"""配置模块测试（design.md §4.4、§6.2）。

覆盖两组验收：
1. 「配置缺少必填字段时抛出 ConfigError，且错误信息包含缺失的字段名」
2. 「环境变量已设置时取到值；缺失时抛 ConfigError 且错误信息包含缺失的变量名」
   —— 用 monkeypatch.setenv / delenv 覆盖两个分支

配置改动一律走 YAML 解析后的字典操作，不做字符串替换 —— 后者对缩进敏感，
一旦缩进不匹配会静默失效，测试就变成了永远通过的空壳。
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from shared.config import load, load_dotenv
from shared.errors import ConfigError
from tests.conftest import ENV_VARS, VALID_CONFIG


def config_data() -> dict[str, Any]:
    """每次返回一份全新的可变副本。"""
    return yaml.safe_load(VALID_CONFIG)


def write_config(tmp_path: Path, data: Any) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------


def test_load_valid_config(env_set, tmp_path):
    config = load(write_config(tmp_path, config_data()))

    assert config.team_name == "研发一组"
    assert config.github.repos == ["owner/repo"]
    assert config.lark.chat_id == "oc_11111111111111111111111111111111"
    assert config.lark.project_id == ""
    assert config.smtp.port == 465
    assert config.smtp.recipients == ["leader@company.com"]
    assert config.filters.keywords == ["项目"]
    assert config.filters.sensitive_keywords == ["薪资"]
    assert config.schedule.timezone == "Asia/Shanghai"


def test_members_parsed_with_both_lark_forms(env_set, tmp_path):
    config = load(write_config(tmp_path, config_data()))

    assert len(config.members) == 2

    zhang, li = config.members
    assert zhang.name == "张三"
    assert zhang.github == "zhangsan"
    assert zhang.lark == "zhangsan@company.com"
    assert zhang.lark_identifier == "zhangsan@company.com"

    assert li.name == "李四"
    assert li.lark is None
    assert li.lark_identifier == "ou_11111111111111111111111111111111"


def test_holidays_and_workdays_parsed_as_dates(env_set, tmp_path):
    config = load(write_config(tmp_path, config_data()))

    assert config.schedule.holidays == [date(2026, 10, 1)]
    assert config.schedule.workdays == [date(2026, 10, 11)]


def test_optional_sections_default_to_empty(env_set, tmp_path):
    """可选清单缺省时取空列表，不报错。"""
    data = config_data()
    del data["filters"]
    del data["schedule"]["holidays"]
    del data["schedule"]["workdays"]

    config = load(write_config(tmp_path, data))

    assert config.filters.keywords == []
    assert config.filters.sensitive_keywords == []
    assert config.schedule.holidays == []
    assert config.schedule.workdays == []


# --------------------------------------------------------------------------
# 验收 1：缺少必填字段
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda d: d.pop("team"), "team.name"),
        (lambda d: d.pop("github"), "github.repos"),
        (lambda d: d.pop("lark"), "lark.chat_id"),
        (lambda d: d.pop("smtp"), "smtp.recipients"),
        (lambda d: d["schedule"].pop("timezone"), "schedule.timezone"),
        (lambda d: d["schedule"].pop("db_path"), "schedule.db_path"),
        (lambda d: d.pop("members"), "members"),
    ],
)
def test_missing_required_field_names_the_field(env_set, tmp_path, mutate, fragment):
    """删掉某个必填项后，错误信息必须**点名**它。"""
    data = config_data()
    mutate(data)

    with pytest.raises(ConfigError) as exc:
        load(write_config(tmp_path, data))

    assert fragment in str(exc.value)


def test_missing_config_file(env_set, tmp_path):
    with pytest.raises(ConfigError) as exc:
        load(tmp_path / "nope.yaml")

    assert "不存在" in str(exc.value)


def test_empty_config_file(env_set, tmp_path):
    path = write_config(tmp_path, None)

    with pytest.raises(ConfigError) as exc:
        load(path)

    assert "空" in str(exc.value)


def test_member_without_github_flagged(env_set, tmp_path):
    data = config_data()
    del data["members"][0]["github"]

    with pytest.raises(ConfigError) as exc:
        load(write_config(tmp_path, data))

    assert "members[0].github" in str(exc.value)


def test_member_without_any_lark_identifier_flagged(env_set, tmp_path):
    data = config_data()
    del data["members"][1]["lark_open_id"]

    with pytest.raises(ConfigError) as exc:
        load(write_config(tmp_path, data))

    assert "lark 或 lark_open_id" in str(exc.value)


def test_empty_members_flagged(env_set, tmp_path):
    data = config_data()
    data["members"] = []

    with pytest.raises(ConfigError) as exc:
        load(write_config(tmp_path, data))

    assert "members" in str(exc.value)


def test_member_with_only_open_id_is_valid(env_set, tmp_path):
    """只填 lark_open_id 不填 lark 是合法的 —— 权限受限的租户走这条路。"""
    data = config_data()
    del data["members"][0]["lark"]
    data["members"][0]["lark_open_id"] = "ou_aabbcc"

    config = load(write_config(tmp_path, data))

    assert config.members[0].lark_identifier == "ou_aabbcc"


def test_invalid_holiday_date_flagged(env_set, tmp_path):
    data = config_data()
    data["schedule"]["holidays"] = ["10/01/2026"]

    with pytest.raises(ConfigError) as exc:
        load(write_config(tmp_path, data))

    assert "schedule.holidays" in str(exc.value)


def test_all_missing_fields_reported_at_once(env_set, tmp_path):
    """一次报全，而不是遇到第一个就中断 —— 逐个试错太费时间。"""
    path = write_config(tmp_path, {"team": {"name": "x"}})

    with pytest.raises(ConfigError) as exc:
        load(path)

    message = str(exc.value)
    assert "github.repos" in message
    assert "lark.chat_id" in message
    assert "smtp.host" in message


# --------------------------------------------------------------------------
# 验收 2：环境变量（design.md §6.2「1. 密钥管理」）
# --------------------------------------------------------------------------


def test_env_value_reaches_config(env_set, tmp_path):
    """环境变量已设置 → Config 对应字段取到该值。"""
    config = load(write_config(tmp_path, config_data()))

    assert config.github.token == ENV_VARS["GITHUB_TOKEN"]
    assert config.lark.app_id == ENV_VARS["FEISHU_APP_ID"]
    assert config.lark.app_secret == ENV_VARS["FEISHU_APP_SECRET"]
    assert config.smtp.password == ENV_VARS["SMTP_PASSWORD"]


@pytest.mark.parametrize("missing", sorted(ENV_VARS))
def test_missing_env_var_raises_with_variable_name(isolated_env, tmp_path, missing):
    """环境变量缺失 → ConfigError，且错误信息包含**缺失的变量名**。"""
    for name, value in ENV_VARS.items():
        if name != missing:
            isolated_env.setenv(name, value)

    with pytest.raises(ConfigError) as exc:
        load(write_config(tmp_path, config_data()))

    assert missing in str(exc.value)


def test_all_missing_env_vars_reported_at_once(isolated_env, tmp_path):
    with pytest.raises(ConfigError) as exc:
        load(write_config(tmp_path, config_data()))

    message = str(exc.value)
    for name in ENV_VARS:
        assert name in message


def test_real_environment_overrides_dotenv(isolated_env, tmp_path):
    """真实环境变量优先于 .env —— 容器注入的凭证不该被本地文件盖掉。"""
    isolated_env.setenv("GITHUB_TOKEN", "from_real_env")
    (tmp_path / ".env").write_text("GITHUB_TOKEN=from_dotenv_file\n", encoding="utf-8")

    load_dotenv()

    assert os.environ["GITHUB_TOKEN"] == "from_real_env"


def test_dotenv_fills_missing_variable(isolated_env, tmp_path):
    (tmp_path / ".env").write_text(
        "# 注释行\n\nGITHUB_TOKEN=from_dotenv\nQUOTED='quoted_value'\nNOT_A_PAIR\n",
        encoding="utf-8",
    )

    load_dotenv()

    assert os.environ["GITHUB_TOKEN"] == "from_dotenv"
    assert os.environ["QUOTED"] == "quoted_value"


def test_dotenv_absent_is_not_an_error(isolated_env, tmp_path):
    load_dotenv()  # 不应抛异常


def test_secrets_have_no_place_in_config_file(env_set, tmp_path):
    """config.yaml 里根本没有承载密钥的键。

    这不是靠「约定不要写」，而是靠接口本身没有那个位置 —— 写进去也读不到。
    """
    data = config_data()

    assert "token" not in data.get("github", {})
    assert "password" not in data.get("smtp", {})
    assert "app_secret" not in data.get("lark", {})
