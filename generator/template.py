"""日报模板管理（design.md §2）。

Markdown 与 HTML **由同一份上下文分别渲染**，而不是先出 Markdown 再转
HTML。这样两个格式的内容天然一致，也省掉一个 Markdown 解析依赖
（requirements 里只有 httpx / pyyaml / jinja2）。

两个环境的 autoescape 不同，这是关键：
- HTML 必须转义 —— 提交信息里出现 `<script>` 就会被当标签执行
- Markdown 不能转义 —— 转义会把用户输入的 `&` 变成 `&amp;`，污染正文
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from jinja2 import Environment

MARKDOWN_TEMPLATE = """\
# {{ team_name }} 日报 · {{ date }}

> 生成时间：{{ generated_at }}
{% for section in sections %}
## {{ section.title }}

{% if section.failed %}
> ⚠️ {{ section.source_label }} 数据获取失败{% if section.failed_detail %}（{{ section.failed_detail }}）{% endif %}
{% if section.has_records %}

以下为**已成功采集到的部分**（数据不完整）：
{% for member in section.members if member.records %}
### {{ member.name }}

{% for r in member.records %}
{% if section.kind == 'commits' %}- `{{ r.repo }}` {{ r.message }}（+{{ r.additions }} 行 / -{{ r.deletions }} 行，{{ r.files_changed }} 个文件）
{% elif section.kind == 'tasks' %}- {{ r.title }}：{{ r.status_from }} → {{ r.status_to }}（{{ r.updated_at | hhmm }}）
{% elif section.kind == 'messages' %}- [{{ r.timestamp | hhmm }}] {{ r.content }}
{% endif %}
{% endfor %}
{% endfor %}
{% endif %}
{% else %}
{% for member in section.members %}
### {{ member.name }}

{% if not member.records %}
今日无记录
{% elif section.kind == 'commits' %}
{% for r in member.records %}
- `{{ r.repo }}` {{ r.message }}（+{{ r.additions }} 行 / -{{ r.deletions }} 行，{{ r.files_changed }} 个文件）
{% endfor %}
{% elif section.kind == 'tasks' %}
{% for r in member.records %}
- {{ r.title }}：{{ r.status_from }} → {{ r.status_to }}（{{ r.updated_at | hhmm }}）
{% endfor %}
{% elif section.kind == 'messages' %}
{% for r in member.records %}
- [{{ r.timestamp | hhmm }}] {{ r.content }}
{% endfor %}
{% endif %}
{% endfor %}
{% endif %}
{% endfor %}"""

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{{ team_name }} 日报 · {{ date }}</title>
<style>
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         line-height: 1.7; color: #1f2328; max-width: 720px; margin: 0 auto; padding: 24px; }
  h1 { font-size: 22px; border-bottom: 2px solid #1f2328; padding-bottom: 8px; }
  h2 { font-size: 18px; margin-top: 32px; color: #0969da; }
  h3 { font-size: 15px; margin-top: 20px; color: #57606a; }
  ul { padding-left: 22px; }
  li { margin: 4px 0; }
  .meta { color: #57606a; font-size: 13px; }
  .empty { color: #8c959f; font-style: italic; }
  .failed { background: #fff8c5; border-left: 4px solid #d4a72c; padding: 8px 12px; }
  code { background: #f6f8fa; padding: 1px 5px; border-radius: 4px; font-size: 13px; }
</style>
</head>
<body>
<h1>{{ team_name }} 日报 · {{ date }}</h1>
<p class="meta">生成时间：{{ generated_at }}</p>
{% for section in sections %}
<h2>{{ section.title }}</h2>
{% if section.failed %}
<p class="failed">⚠️ {{ section.source_label }} 数据获取失败{% if section.failed_detail %}（{{ section.failed_detail }}）{% endif %}</p>
{% if section.has_records %}
<p class="meta">以下为<strong>已成功采集到的部分</strong>（数据不完整）：</p>
{% for member in section.members if member.records %}
<h3>{{ member.name }}</h3>
<ul>
{% for r in member.records %}
{% if section.kind == 'commits' %}
  <li><code>{{ r.repo }}</code> {{ r.message }}（+{{ r.additions }} 行 / -{{ r.deletions }} 行，{{ r.files_changed }} 个文件）</li>
{% elif section.kind == 'tasks' %}
  <li>{{ r.title }}：{{ r.status_from }} → {{ r.status_to }}（{{ r.updated_at | hhmm }}）</li>
{% elif section.kind == 'messages' %}
  <li>[{{ r.timestamp | hhmm }}] {{ r.content }}</li>
{% endif %}
{% endfor %}
</ul>
{% endfor %}
{% endif %}
{% else %}
{% for member in section.members %}
<h3>{{ member.name }}</h3>
{% if not member.records %}
<p class="empty">今日无记录</p>
{% else %}
<ul>
{% for r in member.records %}
{% if section.kind == 'commits' %}
  <li><code>{{ r.repo }}</code> {{ r.message }}（+{{ r.additions }} 行 / -{{ r.deletions }} 行，{{ r.files_changed }} 个文件）</li>
{% elif section.kind == 'tasks' %}
  <li>{{ r.title }}：{{ r.status_from }} → {{ r.status_to }}（{{ r.updated_at | hhmm }}）</li>
{% elif section.kind == 'messages' %}
  <li>[{{ r.timestamp | hhmm }}] {{ r.content }}</li>
{% endif %}
{% endfor %}
</ul>
{% endif %}
{% endfor %}
{% endif %}
{% endfor %}
</body>
</html>"""


def _hhmm(value: datetime) -> str:
    """时间戳渲染为 HH:MM —— 日报读的是当天，日期部分纯属冗余。"""
    return value.strftime("%H:%M") if isinstance(value, datetime) else str(value)


def _make_env(autoescape: bool) -> Environment:
    env = Environment(autoescape=autoescape, trim_blocks=True, lstrip_blocks=True)
    env.filters["hhmm"] = _hhmm
    return env


_markdown_env = _make_env(autoescape=False)
_html_env = _make_env(autoescape=True)

_markdown_template = _markdown_env.from_string(MARKDOWN_TEMPLATE)
_html_template = _html_env.from_string(HTML_TEMPLATE)


def render_markdown(context: dict[str, Any]) -> str:
    """渲染 Markdown 日报。

    上下文结构见 formatter.build_context()。
    """
    return _markdown_template.render(**context)


def render_html(context: dict[str, Any]) -> str:
    """渲染 HTML 日报。用户数据由 autoescape 转义。"""
    return _html_template.render(**context)
