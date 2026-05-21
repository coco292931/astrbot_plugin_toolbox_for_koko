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
from astrbot.core.utils.io import download_image_by_url

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

    # 图片转述结果缓存 TTL（秒），同一 URL 在 TTL 内不会重复转述
    IMAGE_CAPTION_TTL = 3600 * 24 * 7  # 7天

    def __init__(self, plugin: Any) -> None:
        """
        Args:
            plugin: ToolboxPlugin 实例，用于获取 Provider、配置等
        """
        self.plugin = plugin
        # unified_msg_origin -> list[dict]
        self._session_chats: dict[str, list[dict]] = defaultdict(list)
        self._lock = asyncio.Lock()
        # image_url -> (timestamp, caption)
        self._caption_cache: dict[str, tuple[float, str]] = {}

    # ---- 公开方法 ----

    async def record_message(self, event: AstrMessageEvent) -> None:
        """记录一条消息到上下文缓冲区（不转述图片，仅存 URL）。"""
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

        logger.debug(
            f"[kc] 记录消息 - 会话: {umo}，内容: {content[:50]}...，图片数: {len(image_urls)}"
        )

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

        logger.debug(f"[kc] 记录回复 - 会话: {umo}，内容: {reply_text[:50]}...")

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

        # 排除当前消息自身（它在 kc_context_recorder 中已被记录，
        # 注入到 prompt 中会造成“当前消息在上下文和 prompt 各出现一次”的冗余）
        current_msg_text = (event.get_message_outline() or "").strip()
        if current_msg_text:
            recent = [e for e in recent if e.get("content") != current_msg_text]

        # 图片转述（延迟执行，仅对最近的消息中的图片）
        recent = await self._transcribe_images(recent, image_limit)

        # 格式化为文本
        context_lines: list[str] = []
        for entry in recent:
            line = f"[{entry['nickname']}/{entry['time']}]: {entry['content']}"
            context_lines.append(line)

        chats_str = "\n---\n".join(context_lines)

        # 如果没有上下文可注入，至少带上发送者信息
        if not chats_str:
            nickname = getattr(
                getattr(event.message_obj, "sender", None), "nickname", "User"
            )
            now_str = datetime.now().strftime("%H:%M:%S")
            formatted = f"[{nickname}/{now_str}]: {message_text}"
            logger.debug(f"[kc] 无历史上下文，返回单条格式化消息: {formatted[:60]}")
            return formatted

        # 选择模板
        prompt_template = self.plugin.keyword_capture_context_prompt
        if not prompt_template:
            prompt_template = DEFAULT_CONTEXT_PROMPT_ZH

        logger.debug(
            f"[kc] 构建 prompt - 会话: {umo}，消息: {message_text[:30]}，上下文条数: {len(recent)}"
        )

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

                # 查缓存
                now_ts = datetime.now().timestamp()
                cached = self._caption_cache.get(url)
                if cached and (now_ts - cached[0]) < self.IMAGE_CAPTION_TTL:
                    captions.append(cached[1])
                    logger.debug(f"[kc] 图片转述命中缓存 - URL: {url[:40]}...")
                    continue

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
                        caption_text = caption.completion_text.strip()
                        captions.append(caption_text)
                        # 写入缓存
                        self._caption_cache[url] = (now_ts, caption_text)
                    transcribed += 1
                except Exception as e:
                    logger.debug(f"[kc] 图片转述失败（URL方式），尝试下载后重试: {e}")
                    # 降级：下载 → PIL 处理（GIF 取第一帧）→ 本地路径重试
                    try:
                        local_path = await download_image_by_url(url)
                        from PIL import Image as PILImage

                        with PILImage.open(local_path) as pil_img:
                            if getattr(pil_img, "is_animated", False):
                                pil_img.seek(0)
                                frame = pil_img.convert("RGB")
                                import uuid
                                import os
                                from astrbot.core.utils.astrbot_path import (
                                    get_astrbot_temp_path,
                                )

                                frame_path = os.path.join(
                                    get_astrbot_temp_path(),
                                    f"kc_frame_{uuid.uuid4()}.jpg",
                                )
                                frame.save(frame_path, "JPEG", quality=85)
                                local_path = frame_path
                        caption2 = await provider.text_chat(
                            prompt=caption_prompt,
                            image_urls=[local_path],
                            persist=False,
                        )
                        if caption2 and caption2.completion_text:
                            caption_text = caption2.completion_text.strip()
                            captions.append(caption_text)
                            self._caption_cache[url] = (now_ts, caption_text)
                            transcribed += 1
                            logger.info(f"[kc] 图片转述降级成功 - URL: {url[:40]}...")
                        else:
                            raise Exception("降级转述返回空")
                    except Exception as e2:
                        logger.debug(f"[kc] 图片转述完全失败: {e2}")
                        captions.append("[图片]")
                        logger.debug(f"[kc] 图片转述失败 - URL: {url}，错误: {str(e2)}")
                logger.debug(
                    f"[kc] 转述图片 - URL: {url}，描述: {captions[-1] if captions else None}"
                )

            if captions:
                # 替换 content 中的 [Image] 占位符
                content = entry["content"]
                for cap in captions:
                    content = content.replace("[Image]", f"[Image: {cap}]", 1)
                logger.debug(f"[kc] 替换图片占位符 - 内容: {content[:50]}...")
                new_entry = dict(entry)
                new_entry["content"] = content
                # 写回 result 列表中对应的位置
                for i, e in enumerate(result):
                    if e is entry:
                        result[i] = new_entry
                        break

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
