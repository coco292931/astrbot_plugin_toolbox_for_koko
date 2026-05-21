"""
图片转述后处理器 (ImageCaptionHandler)

在 on_llm_request 钩子中检测 AstrBot 图片转述失败，从原始消息重新
提取图片进行降级转述。

流程:
  1. 检测 extra_user_content_parts 中有 [Image Captioning Failed]
  2. 从 event.get_messages() 重新提取图片 URL + sub_type
  3. 先尝试 URL 直传转述
  4. 失败时降级：下载 → 压缩 → PIL GIF 取帧 → 本地路径重试
  5. 替换失败标记为 [表情包: 描述] 或 [图片: 描述]

与群聊上下文的关系:
  - 不影响 kc_context._transcribe_images（群聊上下文中的图片转述）
  - image_caption_hook_enabled 关闭时完全跳过

维护说明:
  - 图片 URL 从 event.get_messages() 提取，顺序与用户发送一致
  - 文本内容在 req.prompt 中，不会被影响
  - 多张图片 + 文本夹杂场景：按 event.get_messages() 顺序逐个处理
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image as MsgImage
from astrbot.core.utils.media_utils import compress_image


class ImageCaptionHandler:
    """图片转述后处理器。"""

    # 默认提示词模板
    DEFAULT_PROMPT_TEMPLATE = "Please describe the {image_type} using Chinese."

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def _get_compress_config(self) -> tuple[bool, int, int]:
        """从 AstrBot 配置读取图片压缩参数。"""
        try:
            cfg = self.plugin.context.get_config()
            p_settings = cfg.get("provider_settings", {})
            enabled = bool(p_settings.get("image_compress_enabled", True))
            options = p_settings.get("image_compress_options", {}) or {}
            max_size = int(options.get("max_size", 1280))
            quality = int(options.get("quality", 95))
            quality = max(1, min(quality, 100))
            return enabled, max_size, quality
        except Exception:
            return True, 1280, 95

    async def process(self, event: AstrMessageEvent, request: Any) -> None:
        """后处理：检测图片转述失败，从原始消息重新提取并降级。"""
        if not self.plugin.image_caption_hook_enabled:
            return

        # 检测 AstrBot 图片转述失败的标记
        has_failure = False
        if hasattr(request, "extra_user_content_parts"):
            for part in request.extra_user_content_parts:
                if hasattr(part, "text") and "[Image Captioning Failed]" in str(
                    part.text
                ):
                    has_failure = True
                    break

        if not has_failure:
            return

        # 从原始消息提取图片 URL 和类型
        image_infos: list[tuple[str, str]] = []
        for comp in event.get_messages():
            if isinstance(comp, MsgImage):
                url = comp.url if comp.url else comp.file
                if url:
                    # sub_type 保留供后续扩展，当前一律按 GIF 处理
                    # sub_type = getattr(comp, "sub_type", None)
                    image_infos.append((url, "GIF"))

        if not image_infos:
            return

        logger.info(
            f"[ImageCaption] 检测到图片转述失败，尝试降级处理 {len(image_infos)} 张图片"
        )

        provider = await self._resolve_caption_provider()
        if not provider:
            logger.warning("[ImageCaption] 未找到可用图片转述 Provider，跳过降级")
            return

        for img_url, img_type in image_infos:
            # 构建提示词
            prompt_template = self.plugin.image_caption_prompt_template
            if not prompt_template:
                prompt_template = self.DEFAULT_PROMPT_TEMPLATE
            try:
                caption_prompt = prompt_template.format(image_type=img_type)
            except KeyError:
                caption_prompt = self.DEFAULT_PROMPT_TEMPLATE.format(
                    image_type=img_type
                )

            caption_tag = await self._transcribe_one(
                provider, img_url, caption_prompt, img_type
            )

            # 替换 [Image Captioning Failed] 标记（仅替换第一个匹配项）
            if hasattr(request, "extra_user_content_parts"):
                for part in request.extra_user_content_parts:
                    if hasattr(part, "text") and "[Image Captioning Failed]" in str(
                        part.text
                    ):
                        part.text = caption_tag
                        break

    async def _resolve_caption_provider(self):
        """获取用于图片转述的 Provider。

        优先级:
          1. AstrBot 配置 provider_ltm_settings.image_caption_provider_id
          2. 当前使用的第一个可用 Provider
          3. None
        """
        ctx = self.plugin.context
        try:
            cfg = ctx.get_config()
            caption_pid = cfg.get("provider_ltm_settings", {}).get(
                "image_caption_provider_id", ""
            )
            if caption_pid:
                provider = ctx.get_provider_by_id(caption_pid)
                if provider:
                    return provider
        except Exception:
            pass
        try:
            provider = ctx.get_using_provider()
            if provider:
                return provider
        except Exception:
            pass
        return None

    async def _transcribe_one(
        self, provider: Any, img_url: str, prompt: str, img_type: str
    ) -> str:
        """转述单张图片，失败时降级处理。"""
        # 尝试直接 URL 转述
        try:
            caption = await provider.text_chat(
                prompt=prompt,
                image_urls=[img_url],
                persist=False,
            )
            if caption and caption.completion_text:
                text = caption.completion_text.strip()
                logger.info(f"[ImageCaption] 转述成功: {img_type}")
                return f"[{img_type}: {text}]"
        except Exception as e:
            logger.debug(f"[ImageCaption] URL 转述失败: {e}")

        # 降级：下载 → PIL 取帧 → 本地路径重试
        try:
            from astrbot.core.utils.io import download_image_by_url
            from PIL import Image as PILImage
            from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

            local_path = await download_image_by_url(img_url)
            # 下载后压缩（读取 AstrBot 配置）
            compress_enabled, max_size, quality = self._get_compress_config()
            if compress_enabled:
                local_path = await compress_image(
                    local_path, max_size=max_size, quality=quality
                )
            # GIF 多帧处理
            with PILImage.open(local_path) as pil_img:
                if getattr(pil_img, "is_animated", False):
                    pil_img.seek(0)
                    frame = pil_img.convert("RGB")
                    frame_path = os.path.join(
                        get_astrbot_temp_path(),
                        f"kc_img_{uuid.uuid4()}.jpg",
                    )
                    frame.save(frame_path, "JPEG", quality=quality)
                    local_path = frame_path

            caption2 = await provider.text_chat(
                prompt=prompt,
                image_urls=[local_path],
                persist=False,
            )
            if caption2 and caption2.completion_text:
                text = caption2.completion_text.strip()
                logger.info(f"[ImageCaption] 降级转述成功: {img_type}")
                return f"[{img_type}: {text}]"
        except Exception as e:
            logger.debug(f"[ImageCaption] 降级转述失败: {e}")

        # 完全失败
        logger.warning(f"[ImageCaption] 转述完全失败: {img_type}")
        return f"[{img_type}]"
