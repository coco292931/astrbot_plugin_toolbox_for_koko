"""
群聊上下文管理器 (KCContextManager)

职责:
  1. 记录群聊消息到内存缓冲区 (_session_chats)
  2. 在触发回复时从缓冲区取出上下文并构建 prompt
  3. 图片转述（触发时延迟调用，避免每条消息都转述）
  4. AI 回复也记录回缓冲区，形成完整对话闭环

与 AstrBot LTM 的关系:
  - 完全独立于 AstrBot 内置的 LongTermMemory
  - 通过 keyword_capture_manage_context 开关控制启用
  - 在 on_llm_request 中撤销 LTM 的群聊上下文追加，避免重复

维护说明:
  - _session_chats 是纯内存结构，重启即丢失
  - 每条消息的格式: {"role":"user|assistant","nickname":"...","time":"HH:MM:SS","content":"...","images":[...]}
  - images 字段存储图片 URL，触发回复时统一转述
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.platform import MessageType

# 默认中文 prompt 模板
DEFAULT_CONTEXT_PROMPT_ZH = (
    "你现在在一个群聊中。以下是最近的聊天记录：\n"
    "{context}\n\n"
    "现在有一条新消息：`{prompt}`\n"
    "请根据聊天记录中的语境回复。只输出你的回复内容，不要输出其他任何信息。"
    "请使用与群聊相同的语言回复。"
)

# 回退英文 prompt 模板（与 AstrBot LTM 一致）
FALLBACK_CONTEXT_PROMPT_EN = (
    "You are now in a chatroom. The chat history is as follows:\n"
    "{context}\n\n"
    "Now, a new message is coming: `{prompt}`. "
    "Please react to it. Only output your response and do not output any other information. "
    "You MUST use the SAME language as the chatroom is using."
)


class KCContextManager:
    """群聊上下文管理器。"""

    def __init__(self, plugin: Any) -> None:
        """
        Args:
            plugin: ToolboxPlugin 实例，用于获取 Provider、配置等
        """
        self.plugin = plugin
        # unified_msg_origin -> list[dict]
        self._session_chats: dict[str, list[dict]] = defaultdict(list)
        self._lock = asyncio.Lock()

    # ---- 公开方法 ----

    async def record_message(self, event: AstrMessageEvent) -> None:
        """记录一条群聊消息到上下文缓冲区（不转述图片，仅存 URL）。"""
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return
        if event.get_sender_id() == event.get_self_id():
            return

        umo = event.unified_msg_origin
        now = datetime.now()
        nickname = getattr(
            getattr(event.message_obj, "sender", None), "nickname", "User"
        )

        parts: list[str] = []
        image_urls: list[str] = []

        for comp in event.get_messages():
            if isinstance(comp, Plain):
                parts.append(comp.text)
            elif isinstance(comp, Image):
                url = comp.url if comp.url else comp.file
                if url:
                    image_urls.append(url)
                parts.append("[Image]")
            elif isinstance(comp, At):
                parts.append(f"@{getattr(comp, 'name', '') or getattr(comp, 'qq', '')}")

        content = "".join(parts)
        if not content and not image_urls:
            return

        entry: dict[str, Any] = {
            "role": "user",
            "nickname": nickname,
            "time": now.strftime("%H:%M:%S"),
            "content": content.strip(),
            "images": image_urls,
        }

        async with self._lock:
            self._session_chats[umo].append(entry)
            max_cnt = self.plugin.keyword_capture_context_max_cnt
            while len(self._session_chats[umo]) > max_cnt:
                self._session_chats[umo].pop(0)

    async def record_reply(self, event: AstrMessageEvent, reply_text: str) -> None:
        """记录 AI 回复到上下文缓冲区。"""
        umo = event.unified_msg_origin
        if umo not in self._session_chats:
            return

        now = datetime.now()
        entry: dict[str, Any] = {
            "role": "assistant",
            "nickname": "Bot",
            "time": now.strftime("%H:%M:%S"),
            "content": reply_text.strip(),
            "images": [],
        }

        async with self._lock:
            self._session_chats[umo].append(entry)
            max_cnt = self.plugin.keyword_capture_context_max_cnt
            while len(self._session_chats[umo]) > max_cnt:
                self._session_chats[umo].pop(0)

    async def build_prompt(
        self,
        event: AstrMessageEvent,
        message_text: str,
        image_limit: int = 3,
    ) -> str:
        """构建带有群聊上下文的 prompt。

        Args:
            event: 当前消息事件
            message_text: 用户原始消息
            image_limit: 最多转述的图片数

        Returns:
            拼接后的 prompt 字符串。如果无上下文或未启用管理，返回原消息。
        """
        if not self.plugin.keyword_capture_manage_context:
            return message_text

        umo = event.unified_msg_origin
        if umo not in self._session_chats or not self._session_chats[umo]:
            return message_text

        history_limit = self.plugin.keyword_capture_context_history_limit

        async with self._lock:
            entries = list(self._session_chats[umo])

        # 取最近 N 条
        recent = entries[-history_limit:] if history_limit > 0 else entries

        # 图片转述（延迟执行，仅对最近的消息中的图片）
        recent = await self._transcribe_images(recent, image_limit)

        # 格式化为文本
        context_lines: list[str] = []
        for entry in recent:
            line = f"[{entry['nickname']}/{entry['time']}]: {entry['content']}"
            context_lines.append(line)

        chats_str = "\n---\n".join(context_lines)

        # 选择模板
        prompt_template = self.plugin.keyword_capture_context_prompt
        if not prompt_template:
            prompt_template = DEFAULT_CONTEXT_PROMPT_ZH

        try:
            return prompt_template.format(context=chats_str, prompt=message_text)
        except KeyError:
            # 模板格式不对，回退到英文硬编码
            return FALLBACK_CONTEXT_PROMPT_EN.format(
                context=chats_str, prompt=message_text
            )

    # ---- 内部方法 ----

    async def _transcribe_images(
        self, entries: list[dict], image_limit: int
    ) -> list[dict]:
        """对消息中的图片进行转述，替换 [Image] 占位符为描述文本。

        Args:
            entries: 消息列表
            image_limit: 最多转述的图片数

        Returns:
            转述后的消息列表（返回新列表，不修改原数据）
        """
        if image_limit <= 0:
            return entries

        provider = await self._resolve_image_caption_provider()
        if provider is None:
            return entries

        result = list(entries)
        transcribed = 0

        # 从最新的消息开始转述
        for entry in reversed(result):
            if transcribed >= image_limit:
                break
            images = entry.get("images", [])
            if not images:
                continue

            captions: list[str] = []
            for url in images:
                if transcribed >= image_limit:
                    break
                try:
                    # 使用 AstrBot 配置的图片转述提示词
                    caption_prompt = "Please describe the image using Chinese."
                    try:
                        cfg = self.plugin.context.get_config()
                        cfg_caption_prompt = cfg.get("provider_settings", {}).get(
                            "image_caption_prompt", ""
                        )
                        if cfg_caption_prompt:
                            caption_prompt = cfg_caption_prompt
                    except Exception:
                        pass
                    caption = await provider.text_chat(
                        prompt=caption_prompt,
                        image_urls=[url],
                        persist=False,
                    )
                    if caption and caption.completion_text:
                        captions.append(caption.completion_text.strip())
                    transcribed += 1
                except Exception as e:
                    logger.debug(f"[kc] 图片转述失败: {e}")
                    captions.append("[图片]")

            if captions:
                # 替换 content 中的 [Image] 占位符
                content = entry["content"]
                for cap in captions:
                    content = content.replace("[Image]", f"[Image: {cap}]", 1)
                entry = dict(entry)
                entry["content"] = content

        return result

    async def _resolve_image_caption_provider(self):
        """自动探测图片转述使用的 Provider。

        优先级:
          1. AstrBot 配置 provider_ltm_settings.image_caption_provider_id
          2. 当前使用的第一个可用 Provider
          3. None

        Returns:
            Provider 实例或 None
        """
        ctx = self.plugin.context

        # 尝试从 AstrBot 配置读取
        try:
            cfg = ctx.get_config()
            ltm_settings = cfg.get("provider_ltm_settings", {})
            caption_pid = ltm_settings.get("image_caption_provider_id", "")
            if caption_pid:
                provider = ctx.get_provider_by_id(caption_pid)
                if provider:
                    return provider
        except Exception:
            pass

        # 回退到当前使用的 Provider
        try:
            provider = ctx.get_using_provider()
            if provider:
                return provider
        except Exception:
            pass

        logger.warning(
            "[kc] 未找到可用的图片转述 Provider。"
            "请配置 AstrBot 群聊图片转述模型或确保有可用的 LLM Provider。"
        )
        return None

    # ---- 清理 ----

    async def clear_session(self, unified_msg_origin: str) -> None:
        """清理指定会话的上下文。"""
        async with self._lock:
            self._session_chats.pop(unified_msg_origin, None)
