"""自定义异常（design.md §4.4）。

四个异常分别对应一层职责的失败。注意：采集层的**预期内失败**
（超时、限流、单个仓库 404）不走异常，而以 CollectResult 返回值表达
（design.md §3.2）；这里的 CollectorError 只用于调用方式错误等
本不该发生的状况。
"""

from __future__ import annotations


class ConfigError(Exception):
    """配置缺失或非法。错误信息须包含具体的字段名或环境变量名。"""


class CollectorError(Exception):
    """数据采集过程中的非预期错误。"""


class GeneratorError(Exception):
    """日报生成失败。"""


class NotifierError(Exception):
    """推送失败。"""
