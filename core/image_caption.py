"""
图片转述前处理器 (ImageCaptionHandler)

在 on_llm_request 钩子中主动接管图片转述，不让 AstrBot 处理图片，
从而规避其概率性吞图片的 bug。

流程:
  1. 检测 req.image_urls 有内容
  2. 立即清空 req.image_urls，阻止 AstrBot 处理
  3. 从 event.get_messages() 获取图片 URL + sub_type（0=普通图片，1=表情包）
  4. 自行调用 Provider 转述（支持自定义提示词模板，可用 {image_type} 占位符）
  5. 失败时降级：下载 → PIL GIF 取帧 → 本地路径重试
  6. 以 [表情包: 描述] 或 [图片: 描述] 追加到 extra_user_content_parts

可配置项:
  - image_caption_hook_enabled: 总开关
  - image_caption_prompt_template: 自定义提示词模板

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
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.media_utils import compress_image


class ImageCaptionHandler:
    """图片转述前处理器。"""

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
        """处理图片转述。检测 req.image_urls 有内容时接管。"""
        if not hasattr(request, "image_urls") or not request.image_urls:
            return

        if not self.plugin.image_caption_hook_enabled:
            return

        image_urls = list(request.image_urls)
        request.image_urls = []  # 清空，防止 AstrBot 处理

        # 从原始消息获取图片类型（保持顺序）
        image_types: list[str] = []
        for comp in event.get_messages():
            if isinstance(comp, MsgImage):
                sub_type = getattr(comp, "sub_type", None)
                # OneBot 中 sub_type: 0=普通图片, 1=表情包/动画表情
                image_types.append("表情包" if sub_type == 1 else "图片")
            else:
                # 非 Image 组件插入 None 占位，保持索引对齐
                image_types.append(None)  # type: ignore[arg-type]

        # 只保留 Image 对应的类型
        img_only_types = [t for t in image_types if t is not None]

        provider = self.plugin.context.get_using_provider()
        if not provider:
            logger.warning("[ImageCaption] 未找到可用 Provider，跳过图片转述")
            return

        for idx, img_url in enumerate(image_urls):
            img_type = img_only_types[idx] if idx < len(img_only_types) else "图片"

            # 构建提示词（支持自定义模板）
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

            if hasattr(request, "extra_user_content_parts"):
                request.extra_user_content_parts.append(TextPart(text=caption_tag))

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
