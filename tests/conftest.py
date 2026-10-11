"""共享测试夹具。"""

from __future__ import annotations

import textwrap

import pytest

ENV_VARS = {
    "GITHUB_TOKEN": "gh_test_token",
    "FEISHU_APP_ID": "cli_test_app",
    "FEISHU_APP_SECRET": "test_app_secret",
    "SMTP_PASSWORD": "test_smtp_password",
}

VALID_CONFIG = textwrap.dedent(
    """
    team:
      name: "研发一组"

    members:
      - name: "张三"
        github: "zhangsan"
        lark: "zhangsan@company.com"
      - name: "李四"
        github: "lisi-dev"
        lark_open_id: "ou_11111111111111111111111111111111"

    github:
      repos:
        - "owner/repo"

    lark:
      chat_id: "oc_11111111111111111111111111111111"
      project_id: ""

    smtp:
      host: "smtp.qq.com"
      port: 465
      sender: "sender@qq.com"
      recipients:
        - "leader@company.com"

    filters:
      keywords:
        - "项目"
      sensitive_keywords:
        - "薪资"

    schedule:
      timezone: "Asia/Shanghai"
      db_path: "data/daily_report.db"
      log_dir: "logs"
      holidays:
        - "2026-10-01"
      workdays:
        - "2026-10-11"
    """
).lstrip()


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """切到临时目录（避开仓库里的真实 .env），并清空相关环境变量。

    这一步是必须的：仓库根目录存在真实的 .env，若不切走，
    load() 内部的 load_dotenv() 会把真实凭证读进来，测试就不再是
    密封的，「环境变量缺失应报错」这类断言也会失效。
    """
    monkeypatch.chdir(tmp_path)
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def env_set(isolated_env):
    """在隔离环境的基础上，设置齐全的凭证环境变量。"""
    for name, value in ENV_VARS.items():
        isolated_env.setenv(name, value)
    return isolated_env
