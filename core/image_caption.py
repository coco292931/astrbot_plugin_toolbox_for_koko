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

import json
import os
import re
import uuid
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image as MsgImage
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.media_utils import compress_image


class ImageCaptionHandler:
    """图片转述后处理器。"""

    # 默认提示词模板
    DEFAULT_PROMPT_TEMPLATE = "Please describe the {image_type} using Chinese."
    DEFAULT_SENSITIVE_SYSTEM_PROMPT = """## Workflow (思维链) - 关键步骤
在生成最终输出之前，模型必须执行以下内部流程：
1. **全景场景映射:** 将整个场景视为一张地图进行可视化。识别所有层次：背景 -> 中景物体 -> 前景主体。
2. **主体与对象清单:** 列出图像中所有独立实体。对每个实体记录关键属性。
3. **互动与环境分析:** 分析实体之间的关系。识别哪些吸引人、引人注目的物体。
4. **氛围与构图检查:** 注意光线、色调、拍摄角度和整体情绪。
注意：请贴合图片实际描述。禁止模糊词语。"""
    DEFAULT_SENSITIVE_USER_PROMPT = "先判断图片类型并尽可能详细描述图片内容。"
    FORMAT_PARSE_ERROR_KEYWORDS = (
        "图片输入格式/解析错误",
        "图片解析错误",
        "image input format",
        "image parse error",
        "invalid image",
        "unsupported image",
        "failed to parse image",
        "unable to parse image",
        "code': '1210'",
        'code": "1210"',
    )
    SENSITIVE_ERROR_KEYWORDS = (
        "不安全",
        "敏感内容",
        "unsafe",
        "sensitive",
        "content filter",
        "contentfilter",
        "content policy",
        "safety",
        "code': '1301'",
        'code": "1301"',
    )
    ERROR_BLOCK_PATTERN = re.compile(
        r"<toolbox_image_caption_error>\s*(\{.*?\})\s*</toolbox_image_caption_error>",
        re.DOTALL,
    )
    _PATCH_FLAG = "_toolbox_image_caption_error_patch_installed"

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._install_astrbot_caption_error_patch()

    def _install_astrbot_caption_error_patch(self) -> None:
        """运行时 patch AstrBot，把原始图片转述异常透传到 request。"""
        try:
            from astrbot.core import astr_main_agent as astr_main_agent
            from astrbot.core.agent.message import TextPart as CoreTextPart
        except Exception as e:
            logger.debug(f"[ImageCaption] 安装 AstrBot 转述错误补丁失败: {e}")
            return

        current = getattr(astr_main_agent, "_ensure_img_caption", None)
        if current is None:
            logger.debug("[ImageCaption] 未找到 AstrBot _ensure_img_caption，跳过补丁")
            return
        if getattr(current, self._PATCH_FLAG, False):
            return

        async def patched_ensure_img_caption(
            event: Any,
            req: Any,
            cfg: dict,
            plugin_context: Any,
            image_caption_provider: str,
        ) -> None:
            try:
                compressed_urls = []
                for url in req.image_urls:
                    compressed_url = await astr_main_agent._compress_image_for_provider(
                        url, cfg
                    )
                    compressed_urls.append(compressed_url)
                    if astr_main_agent._is_generated_compressed_image_path(
                        url, compressed_url
                    ):
                        event.track_temporary_local_file(compressed_url)
                caption = await astr_main_agent._request_img_caption(
                    image_caption_provider,
                    cfg,
                    compressed_urls,
                    plugin_context,
                )
                if caption:
                    req.extra_user_content_parts.append(
                        CoreTextPart(text=f"<image_caption>{caption}</image_caption>")
                    )
                    req.image_urls = []
            except Exception as exc:  # noqa: BLE001
                logger.debug("[ImageCaptionPatch] 处理图片描述失败: %s", exc)
                error_payload = {
                    "provider_id": str(image_caption_provider or "").strip(),
                    "error": str(exc).strip(),
                }
                req.extra_user_content_parts.append(
                    CoreTextPart(
                        text=(
                            "<toolbox_image_caption_error>"
                            f"{json.dumps(error_payload, ensure_ascii=False)}"
                            "</toolbox_image_caption_error>"
                        )
                    )
                )
                req.extra_user_content_parts.append(
                    CoreTextPart(text="[Image Captioning Failed]")
                )
            finally:
                req.image_urls = []

        setattr(patched_ensure_img_caption, self._PATCH_FLAG, True)
        setattr(patched_ensure_img_caption, "__wrapped__", current)
        astr_main_agent._ensure_img_caption = patched_ensure_img_caption
        logger.info("[ImageCaption] 已安装 AstrBot 图片转述错误透传补丁")

    def _extract_failure_payload(self, request: Any) -> dict[str, str] | None:
        if not hasattr(request, "extra_user_content_parts"):
            return None

        for part in request.extra_user_content_parts:
            text = str(getattr(part, "text", "") or "")
            if not text or "<toolbox_image_caption_error>" not in text:
                continue
            match = self.ERROR_BLOCK_PATTERN.search(text)
            if not match:
                continue
            try:
                payload = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return {
                    "provider_id": str(payload.get("provider_id", "") or "").strip(),
                    "error": str(payload.get("error", "") or "").strip(),
                }
        return None

    def _build_failure_context(self, payload: dict[str, str] | None) -> str | None:
        if not payload:
            return None

        provider_id = payload.get("provider_id", "")
        error_text = payload.get("error", "")
        if not error_text:
            return None

        provider_line = f"Provider: {provider_id}\n" if provider_id else ""
        return (
            "<toolbox_image_caption_failure_context>\n"
            "以下是 AstrBot 原始图片转述失败信息，请结合该失败原因理解后续图片内容：\n"
            f"{provider_line}"
            f"Error: {error_text}\n"
            "</toolbox_image_caption_failure_context>"
        )

    def _matches_error_keywords(
        self, error_text: str, keywords: tuple[str, ...]
    ) -> bool:
        normalized = str(error_text or "").casefold()
        if not normalized:
            return False
        return any(keyword.casefold() in normalized for keyword in keywords)

    def _should_skip_direct_url_retry(self, error_text: str) -> bool:
        keywords = tuple(
            getattr(self.plugin, "image_caption_parse_error_keywords", []) or []
        ) or self.FORMAT_PARSE_ERROR_KEYWORDS
        return self._matches_error_keywords(
            error_text,
            keywords,
        )

    def _should_try_sensitive_fallback(self, error_text: str) -> bool:
        keywords = tuple(
            getattr(self.plugin, "image_caption_sensitive_error_keywords", []) or []
        ) or self.SENSITIVE_ERROR_KEYWORDS
        return self._matches_error_keywords(
            error_text,
            keywords,
        )

    def _load_sensitive_fallback_provider_ids(self) -> list[str]:
        provider_ids = getattr(
            self.plugin,
            "image_caption_sensitive_fallback_provider_ids",
            [],
        )
        if not isinstance(provider_ids, list):
            return []
        return [str(v).strip() for v in provider_ids if str(v).strip()]

    async def _prepare_local_image_path(self, img_url: str) -> str:
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
        from astrbot.core.utils.io import download_image_by_url
        from PIL import Image as PILImage

        local_path = await download_image_by_url(img_url)
        compress_enabled, max_size, quality = self._get_compress_config()
        if compress_enabled:
            local_path = await compress_image(
                local_path,
                max_size=max_size,
                quality=quality,
            )

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
        return local_path

    async def _caption_local_path(
        self,
        provider: Any,
        local_path: str,
        prompt: str,
        img_type: str,
    ) -> str | None:
        caption = await provider.text_chat(
            prompt=prompt,
            image_urls=[local_path],
            persist=False,
        )
        if caption and caption.completion_text:
            text = caption.completion_text.strip()
            logger.info(f"[ImageCaption] 降级转述成功: {img_type}")
            return f"[{img_type}: {text}]"
        return None

    async def _try_sensitive_fallback_models(
        self,
        img_url: str,
        img_type: str,
    ) -> str | None:
        if not getattr(self.plugin, "image_caption_sensitive_fallback_enabled", False):
            return None

        provider_ids = self._load_sensitive_fallback_provider_ids()
        if not provider_ids:
            logger.debug("[ImageCaption] 未配置敏感内容兜底模型，跳过")
            return None

        try:
            local_path = await self._prepare_local_image_path(img_url)
        except Exception as e:
            logger.debug(f"[ImageCaption] 准备敏感兜底图片失败: {e}")
            return None

        system_prompt = (
            getattr(
                self.plugin,
                "image_caption_sensitive_fallback_system_prompt",
                "",
            )
            or self.DEFAULT_SENSITIVE_SYSTEM_PROMPT
        )
        max_tokens = int(
            getattr(self.plugin, "image_caption_sensitive_fallback_max_tokens", 300)
            or 300
        )

        for provider_id in provider_ids:
            provider = self.plugin.context.get_provider_by_id(provider_id)
            if not provider:
                logger.debug(
                    f"[ImageCaption] 敏感兜底 Provider 不存在，跳过: {provider_id}"
                )
                continue
            try:
                caption = await provider.text_chat(
                    prompt=self.DEFAULT_SENSITIVE_USER_PROMPT,
                    system_prompt=system_prompt,
                    image_urls=[local_path],
                    persist=False,
                    max_tokens=max_tokens,
                )
                if not caption or not getattr(caption, "completion_text", ""):
                    logger.debug(
                        f"[ImageCaption] 敏感兜底 Provider {provider_id} 返回空内容"
                    )
                    continue
                text = caption.completion_text.strip()
                logger.info(
                    f"[ImageCaption] 敏感兜底 Provider {provider_id} 转述成功: {img_type}"
                )
                return f"[{img_type}: {text}]"
            except Exception as e:
                logger.debug(
                    f"[ImageCaption] 敏感兜底 Provider {provider_id} 调用异常: {e}"
                )
        return None

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

        failure_payload = self._extract_failure_payload(request)
        failure_context = self._build_failure_context(failure_payload)
        failure_error = failure_payload.get("error", "") if failure_payload else ""
        skip_direct_url_retry = self._should_skip_direct_url_retry(failure_error)
        try_sensitive_fallback = self._should_try_sensitive_fallback(failure_error)
        if failure_payload and failure_payload.get("error"):
            logger.info(
                "[ImageCaption] 捕获到 AstrBot 原始转述错误: "
                f"{failure_payload.get('error', '')}"
            )
        if skip_direct_url_retry:
            logger.info("[ImageCaption] 命中图片格式/解析错误，跳过 URL 转述直传")
        if try_sensitive_fallback:
            logger.info("[ImageCaption] 命中敏感内容错误，尝试自定义兜底模型")

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

        # 先清理所有 [Image Attachment: path ...] 和 [Image Captioning Failed] 标记
        if hasattr(request, "extra_user_content_parts"):
            cleaned_parts = []
            for part in request.extra_user_content_parts:
                text = str(getattr(part, "text", ""))
                if (
                    "[Image Attachment:" in text
                    or "[Image Captioning Failed]" in text
                    or "<toolbox_image_caption_error>" in text
                ):
                    continue  # 丢弃，后续用转述结果替换
                cleaned_parts.append(part)
            if failure_context:
                cleaned_parts.append(TextPart(text=failure_context))
            request.extra_user_content_parts = cleaned_parts

        provider = await self._resolve_caption_provider()
        if not provider and not try_sensitive_fallback:
            logger.warning("[ImageCaption] 未找到可用图片转述 Provider，跳过降级")
            return

        for img_url, img_type in image_infos:
            # 构建提示词
            prompt_template = self.plugin.image_caption_prompt_template

            # 捕获 AstrBot 配置中的图片转述提示词（如果有的话），优先级低于插件配置
            ctx = self.plugin.context
            try:
                cfg = ctx.get_config()
                astrbot_image_caption_prompt = cfg.get("provider_settings", {}).get(
                    "image_caption_prompt", ""
                )
            except Exception:
                astrbot_image_caption_prompt = ""

            if not prompt_template:
                if not astrbot_image_caption_prompt:
                    prompt_template = self.DEFAULT_PROMPT_TEMPLATE
                else:
                    prompt_template = astrbot_image_caption_prompt

            try:
                caption_prompt = prompt_template.format(image_type=img_type)
            except KeyError:
                caption_prompt = self.DEFAULT_PROMPT_TEMPLATE.format(
                    image_type=img_type
                )

            caption_tag = None
            if try_sensitive_fallback:
                caption_tag = await self._try_sensitive_fallback_models(
                    img_url,
                    img_type,
                )

            if not caption_tag and provider:
                caption_tag = await self._transcribe_one(
                    provider,
                    img_url,
                    caption_prompt,
                    img_type,
                    skip_direct_url_retry=skip_direct_url_retry,
                )
            if not caption_tag:
                caption_tag = f"[{img_type}]"

            # 追加转述结果
            if hasattr(request, "extra_user_content_parts"):
                request.extra_user_content_parts.append(TextPart(text=caption_tag))

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
        self,
        provider: Any,
        img_url: str,
        prompt: str,
        img_type: str,
        skip_direct_url_retry: bool = False,
    ) -> str:
        """转述单张图片，失败时降级处理。"""
        if not skip_direct_url_retry:
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

        try:
            local_path = await self._prepare_local_image_path(img_url)
            caption2 = await self._caption_local_path(
                provider,
                local_path,
                prompt,
                img_type,
            )
            if caption2:
                return caption2
        except Exception as e:
            logger.debug(f"[ImageCaption] 降级转述失败: {e}")

        # 完全失败
        logger.warning(f"[ImageCaption] 转述完全失败: {img_type}")
        return f"[{img_type}]"
