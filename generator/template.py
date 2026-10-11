"""日报模板管理（design.md §2）。

Markdown 与 HTML **由同一份上下文分别渲染**，而不是先出 Markdown 再转
HTML。这样两个格式的内容天然一致，也省掉一个 Markdown 解析依赖
（requirements 里只有 httpx / pyyaml / jinja2）。

**为什么 Markdown 不用 Jinja 模板**：Markdown 的空白行是有语义的 ——
标题、引用块、列表之间少了空行，CommonMark 会把相邻内容并进同一个块
（例如引用块会把下一行当成它的延续）。模板的 `trim_blocks` / `lstrip_blocks`
恰恰会吃掉这些换行，调起来靠试。用同一份上下文直接拼装，空白完全可控，
也省掉一类只在渲染后才看得出来的 bug。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from jinja2 import Environment

#: HTML 的空白无语义，适合模板；autoescape 必须开 —— 提交信息里出现
#: `<script>` 就会被当标签执行。
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
  <li>{{ r.title }}：{{ r.status_from | status_text }} → {{ r.status_to | status_text }}（{{ r.updated_at | hhmm }}）</li>
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
  <li>{{ r.title }}：{{ r.status_from | status_text }} → {{ r.status_to | status_text }}（{{ r.updated_at | hhmm }}）</li>
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


#: 飞书任务接口的状态码 → 日报里的中文表述。
#:
#: 接口给的是 todo / done 这类机器标识，直接印在日报上（「todo → done」）
#: 对读日报的人不友好。翻译是**表述层**的职责，采集层照原样返回，
#: 这样状态码本身的语义不会被两处改动同时影响。
#: 未收录的状态码原样透出 —— 与其猜一个中文名，不如让人看见原始值。
_STATUS_LABELS = {
    "todo": "待办",
    "doing": "进行中",
    "in_progress": "进行中",
    "done": "已完成",
    "completed": "已完成",
}


def _status_text(code: str) -> str:
    return _STATUS_LABELS.get(str(code).strip().lower(), str(code))


def _hhmm(value: datetime) -> str:
    """时间戳渲染为 HH:MM —— 日报读的是当天，日期部分纯属冗余。"""
    return value.strftime("%H:%M") if isinstance(value, datetime) else str(value)


def _commit_line(record: Any) -> str:
    return (
        f"- `{record.repo}` {record.message}"
        f"（+{record.additions} 行 / -{record.deletions} 行，"
        f"{record.files_changed} 个文件）"
    )


def _task_line(record: Any) -> str:
    return (
        f"- {record.title}：{_status_text(record.status_from)} → "
        f"{_status_text(record.status_to)}（{_hhmm(record.updated_at)}）"
    )


def _message_line(record: Any) -> str:
    return f"- [{_hhmm(record.timestamp)}] {record.content}"


#: 板块类型 → 单条记录的 Markdown 行渲染器
_LINE_BUILDERS = {
    "commits": _commit_line,
    "tasks": _task_line,
    "messages": _message_line,
}


def _markdown_member(kind: str, member: dict[str, Any]) -> str:
    blocks = [f"### {member['name']}"]
    if not member["records"]:
        blocks.append("今日无记录")
    else:
        build = _LINE_BUILDERS[kind]
        blocks.append("\n".join(build(record) for record in member["records"]))
    return "\n\n".join(blocks)


def _markdown_section(section: dict[str, Any]) -> str:
    blocks = [f"## {section['title']}"]

    if section["failed"]:
        notice = f"> ⚠️ {section['source_label']} 数据获取失败"
        if section["failed_detail"]:
            notice += f"（{section['failed_detail']}）"
        blocks.append(notice)

        # 失败板块绝不写「今日无记录」—— 我们并不知道成员当天有没有记录，
        # 那句话等于用「没干活」掩盖「采集失败」。有数据的按部分失败展示。
        if section["has_records"]:
            blocks.append("以下为**已成功采集到的部分**（数据不完整）：")
            for member in section["members"]:
                if member["records"]:
                    blocks.append(_markdown_member(section["kind"], member))
        return "\n\n".join(blocks)

    for member in section["members"]:
        blocks.append(_markdown_member(section["kind"], member))
    return "\n\n".join(blocks)


def render_markdown(context: dict[str, Any]) -> str:
    """渲染 Markdown 日报。上下文结构见 formatter.build_context()。

    各块之间统一用空行分隔 —— Markdown 的块级语法靠空行划界，
    少一个空行就可能让引用块把下一行吞进去。
    """
    blocks = [
        f"# {context['team_name']} 日报 · {context['date']}",
        f"> 生成时间：{context['generated_at']}",
    ]
    blocks.extend(_markdown_section(section) for section in context["sections"])
    return "\n\n".join(blocks) + "\n"


def _make_env(autoescape: bool) -> Environment:
    env = Environment(autoescape=autoescape, trim_blocks=True, lstrip_blocks=True)
    env.filters["hhmm"] = _hhmm
    env.filters["status_text"] = _status_text
    return env


_html_env = _make_env(autoescape=True)
_html_template = _html_env.from_string(HTML_TEMPLATE)


def render_html(context: dict[str, Any]) -> str:
    """渲染 HTML 日报。用户数据由 autoescape 转义。"""
    return _html_template.render(**context)
