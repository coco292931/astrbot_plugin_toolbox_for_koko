"""向 ``ProviderRequest.extra_user_content_parts`` 注入文本的安全工具。

背景
----
``extra_user_content_parts`` 的元素是 ``ContentPart`` 的子类（``TextPart`` /
``ImageURLPart`` / ``AudioURLPart`` / ``ThinkPart`` …），由 pydantic 模型约束，
字段是必填的。

早期实现为了拿到 "带 model_dump_for_context 的 part"，写成::

    type(request.extra_user_content_parts[0])(text=...)

即照抄首个 part 的**类型**。这在纯文本消息下没问题，但纯图片消息里首个 part
是 ``ImageURLPart``：

* ``text`` 不是它的字段，会被 pydantic 静默忽略；
* ``image_url`` 是必填字段，缺失 → ``ValidationError``；
* 调用方用 ``except Exception`` 包着，异常被吞掉。

结果就是 **Goal / Recall / 内容审核在"只发图片"的消息里静默丢失**，
而且日志里只有一行 debug，很难发现。

本模块只做一件事：始终构造 ``TextPart``，不看参考 part 的类型。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

__all__ = ["make_text_part"]


def _text_part_class() -> type | None:
    """返回 AstrBot 的 ``TextPart``；不可用时返回 ``None``。"""
    try:
        from astrbot.core.agent.message import TextPart

        return TextPart
    except Exception:
        return None


def _is_text_part(part: Any) -> bool:
    """是否为文本类 part（``type`` 标记为 text 且有 ``text`` 字段）。"""
    if isinstance(part, dict):
        return part.get("type") == "text" and isinstance(part.get("text"), str)
    if not hasattr(part, "text"):
        return False
    return getattr(part, "type", None) in (None, "text")


def make_text_part(text: str, template: Any = None) -> Any | None:
    """构造一个纯文本 part。

    Args:
        text: 要注入的文本。
        template: 调用方原本想照抄类型的参考 part。仅当 AstrBot 的 ``TextPart``
            无法导入、且该模板本身就是文本 part 时，才会被用作兜底。

    Returns:
        可追加到 ``extra_user_content_parts`` 的 part；无法安全构造时返回
        ``None``，调用方应跳过注入而不是写入半成品。
    """
    cls = _text_part_class()
    if cls is not None:
        try:
            return cls(text=text)
        except Exception:
            logger.debug("[request_utils] 构造 TextPart 失败", exc_info=True)

    if template is not None and _is_text_part(template):
        try:
            return type(template)(text=text)
        except Exception:
            logger.debug("[request_utils] 兜底构造文本 part 失败", exc_info=True)
    return None
