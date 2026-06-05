from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image as MsgImage
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.media_utils import compress_image


class ImageGenerationResultHandler:
    """为图像生成任务完成后的唤醒请求补充识图结果。"""

    DEFAULT_PROMPT_TEMPLATE = (
        "Please describe the generated image in Chinese. "
        "Focus on the main subjects, style, composition, colors, and notable details."
    )
    _TASK_HISTORY_MARKER = "Output your last task result below."
    _WAKE_PROMPT_MARKERS = (
        "You are awakened because an image generation task you initiated earlier has completed.",
        "Use `send_message_to_user` to deliver the image generation result now.",
        "<image_generation_task_result>",
    )
    _TASK_RESULT_PATTERN = re.compile(
        r"<image_generation_task_result>\s*(\{.*?\})\s*</image_generation_task_result>",
        re.DOTALL,
    )
    _TASK_SUMMARY_PATTERN = re.compile(
        r"\[ImageGenerationTask\]\s*task_id=([a-zA-Z0-9_-]+),\s*status=([a-zA-Z0-9_-]+)",
        re.DOTALL,
    )
    _GENERATED_PATH_PATTERN = re.compile(
        r"\[Generated image path:\s*(.*?)\]",
        re.DOTALL,
    )

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._task_caption_cache: dict[str, str] = {}
        self._sent_image_cache: dict[str, float] = {}

    async def process(self, event: AstrMessageEvent, request: Any) -> None:
        """检测图像生成任务完成请求，并把识图结果注入上下文。"""
        if not self.plugin.image_generation_result_hook_enabled:
            return

        if not self._is_task_wakeup_request(request):
            return

        payload = self._extract_task_payload(request)
        result_paths = self._extract_result_paths(request, payload)
        if not payload and not result_paths:
            return

        task_id = str((payload or {}).get("task_id", "") or "").strip()
        status = str((payload or {}).get("status", "") or "").strip().lower()
        if not task_id:
            task_id, status = self._extract_task_summary(request)
        if status and status != "succeeded":
            logger.debug(
                f"[ImageGenResult] 检测到任务 {task_id or 'unknown'} 状态={status}，跳过识图"
            )
            return
        if not result_paths:
            return

        logger.info(
            f"[ImageGenResult] 捕获到生图完成请求: 任务={task_id or 'unknown'}，图片数={len(result_paths)}，会话={event.unified_msg_origin}"
        )

        caption_text = self._task_caption_cache.get(task_id)
        if not caption_text:
            provider = await self._resolve_caption_provider()
            if not provider:
                logger.warning(
                    f"[ImageGenResult] 未找到可用识图 Provider，跳过任务 {task_id or 'unknown'}"
                )
                return

            max_images = max(
                1,
                min(
                    int(self.plugin.image_generation_result_max_images or 1),
                    len(result_paths),
                ),
            )
            captions: list[str] = []
            for index, img_path in enumerate(result_paths[:max_images], start=1):
                prompt = self._build_prompt(
                    task_id=task_id,
                    image_index=index,
                    image_count=len(result_paths),
                )
                text = await self._caption_generated_image(
                    provider=provider,
                    image_path=img_path,
                    prompt=prompt,
                    task_id=task_id,
                    image_index=index,
                )
                if not text:
                    continue

                captions.append(f"- 图片{index}: {text}")

            if not captions:
                logger.warning(
                    f"[ImageGenResult] 任务 {task_id or 'unknown'} 未产出任何识图摘要"
                )
                return

            caption_text = "\n".join(captions)
            if task_id:
                self._task_caption_cache[task_id] = caption_text

        block = (
            "<toolbox_image_recognition>\n"
            "以下是 toolbox 对本次生图结果的识图摘要，请在发送最终图片时酌情参考：\n"
            f"任务ID: {task_id or 'unknown'}\n"
            f"{caption_text}\n"
            "</toolbox_image_recognition>"
        )
        if hasattr(request, "extra_user_content_parts"):
            request.extra_user_content_parts.append(TextPart(text=block))
        if hasattr(request, "prompt") and isinstance(request.prompt, str):
            if block not in request.prompt:
                request.prompt = f"{request.prompt}\n\n{block}".strip()
        logger.info(
            f"[ImageGenResult] 已为任务 {task_id or 'unknown'} 注入识图摘要，会话: {event.unified_msg_origin}"
        )

    async def process_sent_message(self, event: AstrMessageEvent) -> str | None:
        """在 Bot 发图后补发一条独立的识图结果。"""
        image_paths = self._extract_generation_images_from_event(event)
        if not image_paths:
            return None

        fingerprint = f"{event.unified_msg_origin}|{'|'.join(image_paths)}"
        now = time.time()
        expired = [
            key for key, ts in self._sent_image_cache.items() if (now - ts) > 600
        ]
        for key in expired:
            self._sent_image_cache.pop(key, None)
        if fingerprint in self._sent_image_cache:
            logger.debug(
                f"[ImageGenResult] 跳过重复的发图识别: 会话={event.unified_msg_origin}"
            )
            return None

        provider = await self._resolve_caption_provider()
        if not provider:
            logger.warning(
                f"[ImageGenResult] 发图后识图未找到可用 Provider，会话={event.unified_msg_origin}"
            )
            return None

        logger.info(
            f"[ImageGenResult] 捕获到发图后的生图结果，会话={event.unified_msg_origin}，图片数={len(image_paths)}"
        )
        max_images = max(
            1,
            min(
                int(self.plugin.image_generation_result_max_images or 1),
                len(image_paths),
            ),
        )
        lines: list[str] = []
        for index, img_path in enumerate(image_paths[:max_images], start=1):
            prompt = self._build_prompt(
                task_id="sent_message",
                image_index=index,
                image_count=len(image_paths),
            )
            text = await self._caption_generated_image(
                provider=provider,
                image_path=img_path,
                prompt=prompt,
                task_id="sent_message",
                image_index=index,
            )
            if text:
                lines.append(f"图片{index}：{text}")

        if not lines:
            logger.warning(
                f"[ImageGenResult] 发图后识图没有产出摘要，会话={event.unified_msg_origin}"
            )
            return None

        self._sent_image_cache[fingerprint] = now
        if len(lines) == 1:
            return f"识图结果：{lines[0]}"
        return "识图结果：\n" + "\n".join(lines)

    def _extract_task_payload(self, request: Any) -> dict[str, Any] | None:
        for text in self._iter_request_texts(request):
            if "<image_generation_task_result>" not in text:
                continue

            match = self._TASK_RESULT_PATTERN.search(text)
            if not match:
                continue

            try:
                payload = json.loads(match.group(1))
            except json.JSONDecodeError as exc:
                logger.debug(f"[ImageGenResult] 解析任务结果 JSON 失败: {exc}")
                continue
            if isinstance(payload, dict):
                return payload
        return None

    def _extract_task_summary(self, request: Any) -> tuple[str, str]:
        for text in self._iter_current_request_texts(request):
            match = self._TASK_SUMMARY_PATTERN.search(text)
            if match:
                return match.group(1).strip(), match.group(2).strip().lower()
        return "", ""

    def _extract_result_paths(
        self,
        request: Any,
        payload: dict[str, Any] | None,
    ) -> list[str]:
        paths: list[str] = []
        if isinstance(payload, dict):
            for path in payload.get("result_paths") or []:
                normalized = str(path).strip()
                if normalized:
                    paths.append(normalized)

        image_urls = getattr(request, "image_urls", None)
        if isinstance(image_urls, list):
            for path in image_urls:
                normalized = str(path).strip()
                if normalized:
                    paths.append(normalized)

        for text in self._iter_request_texts(request):
            for match in self._GENERATED_PATH_PATTERN.finditer(text):
                normalized = str(match.group(1) or "").strip()
                if normalized:
                    paths.append(normalized)

        deduped: list[str] = []
        seen: set[str] = set()
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            deduped.append(path)
        return deduped

    def _extract_generation_images_from_event(
        self, event: AstrMessageEvent
    ) -> list[str]:
        paths: list[str] = []
        try:
            messages = event.get_messages() or []
        except Exception:
            messages = []

        for comp in messages:
            if not isinstance(comp, MsgImage):
                continue
            candidate = str(
                getattr(comp, "file", "") or getattr(comp, "url", "") or ""
            ).strip()
            if not candidate:
                continue
            lowered = candidate.lower()
            filename = Path(candidate).name.lower()
            if "astrbot_plugin_image_generation" in lowered or filename.startswith(
                "gen_"
            ):
                paths.append(candidate)

        deduped: list[str] = []
        seen: set[str] = set()
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            deduped.append(path)
        return deduped

    def _iter_request_texts(self, request: Any):
        if hasattr(request, "prompt") and isinstance(request.prompt, str):
            yield request.prompt
        if hasattr(request, "system_prompt") and isinstance(request.system_prompt, str):
            yield request.system_prompt
        contexts = getattr(request, "contexts", None)
        if isinstance(contexts, list):
            for message in contexts:
                yield from self._iter_message_texts(message)
        if hasattr(request, "extra_user_content_parts"):
            for part in request.extra_user_content_parts:
                text = str(getattr(part, "text", "") or "")
                if text:
                    yield text

    def _iter_current_request_texts(self, request: Any):
        if hasattr(request, "prompt") and isinstance(request.prompt, str):
            yield request.prompt
        if hasattr(request, "system_prompt") and isinstance(request.system_prompt, str):
            yield request.system_prompt
        if hasattr(request, "extra_user_content_parts"):
            for part in request.extra_user_content_parts:
                text = str(getattr(part, "text", "") or "")
                if text:
                    yield text

    def _is_task_wakeup_request(self, request: Any) -> bool:
        for text in self._iter_current_request_texts(request):
            if any(marker in text for marker in self._WAKE_PROMPT_MARKERS):
                return True

        image_urls = getattr(request, "image_urls", None)
        if isinstance(image_urls, list) and image_urls:
            for path in image_urls:
                normalized = str(path or "").strip().lower()
                if "astrbot_plugin_image_generation" in normalized or "/gen_" in normalized:
                    return True

        contexts = getattr(request, "contexts", None)
        if isinstance(contexts, list):
            for message in contexts:
                for text in self._iter_message_texts(message):
                    if self._TASK_HISTORY_MARKER in text:
                        return False

        return False

    def _iter_message_texts(self, message: Any):
        if isinstance(message, str):
            text = message.strip()
            if text:
                yield text
            return

        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                text = content.strip()
                if text:
                    yield text
            elif isinstance(content, list):
                for part in content:
                    yield from self._iter_content_part_texts(part)
            return

        content = getattr(message, "content", None)
        if isinstance(content, str):
            text = content.strip()
            if text:
                yield text
        elif isinstance(content, list):
            for part in content:
                yield from self._iter_content_part_texts(part)

    def _iter_content_part_texts(self, part: Any):
        if isinstance(part, str):
            text = part.strip()
            if text:
                yield text
            return

        if isinstance(part, dict):
            for key in ("text", "content"):
                value = part.get(key)
                if isinstance(value, str):
                    text = value.strip()
                    if text:
                        yield text
            return

        for attr in ("text", "content"):
            value = getattr(part, attr, None)
            if isinstance(value, str):
                text = value.strip()
                if text:
                    yield text

    def _build_prompt(
        self,
        *,
        task_id: str,
        image_index: int,
        image_count: int,
    ) -> str:
        template = (
            self.plugin.image_generation_result_prompt_template
            or self.DEFAULT_PROMPT_TEMPLATE
        )
        try:
            return template.format(
                task_id=task_id,
                image_index=image_index,
                image_count=image_count,
            )
        except KeyError:
            return self.DEFAULT_PROMPT_TEMPLATE

    async def _resolve_caption_provider(self):
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

    def _get_compress_config(self) -> tuple[bool, int, int]:
        try:
            cfg = self.plugin.context.get_config()
            settings = cfg.get("provider_settings", {})
            enabled = bool(settings.get("image_compress_enabled", True))
            options = settings.get("image_compress_options", {}) or {}
            max_size = int(options.get("max_size", 1280))
            quality = int(options.get("quality", 95))
            quality = max(1, min(quality, 100))
            return enabled, max_size, quality
        except Exception:
            return True, 1280, 95

    async def _caption_generated_image(
        self,
        *,
        provider: Any,
        image_path: str,
        prompt: str,
        task_id: str,
        image_index: int,
    ) -> str:
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                image_urls=[image_path],
                persist=False,
            )
            text = str(getattr(resp, "completion_text", "") or "").strip()
            if not text and isinstance(resp, dict):
                text = str(resp.get("completion_text", "") or "").strip()
            if text:
                logger.info(
                    f"[ImageGenResult] 任务 {task_id or 'unknown'} 第 {image_index} 张识图成功"
                )
                return text
        except Exception as exc:
            logger.debug(
                f"[ImageGenResult] 任务 {task_id or 'unknown'} 第 {image_index} 张直接识图失败: {exc}"
            )

        local_path = Path(image_path)
        if not local_path.exists():
            logger.debug(
                f"[ImageGenResult] 任务 {task_id or 'unknown'} 第 {image_index} 张路径不存在: {image_path}"
            )
            return ""

        retry_path = str(local_path)
        try:
            compress_enabled, max_size, quality = self._get_compress_config()
            if compress_enabled:
                retry_path = await compress_image(
                    retry_path, max_size=max_size, quality=quality
                )

            if local_path.suffix.lower() == ".gif":
                from PIL import Image as PILImage
                from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

                with PILImage.open(retry_path) as pil_img:
                    if getattr(pil_img, "is_animated", False):
                        pil_img.seek(0)
                        frame = pil_img.convert("RGB")
                        frame_path = os.path.join(
                            get_astrbot_temp_path(),
                            f"img_gen_result_{uuid.uuid4()}.jpg",
                        )
                        frame.save(frame_path, "JPEG", quality=quality)
                        retry_path = frame_path

            resp = await provider.text_chat(
                prompt=prompt,
                image_urls=[retry_path],
                persist=False,
            )
            text = str(getattr(resp, "completion_text", "") or "").strip()
            if not text and isinstance(resp, dict):
                text = str(resp.get("completion_text", "") or "").strip()
            if text:
                logger.info(
                    f"[ImageGenResult] 任务 {task_id or 'unknown'} 第 {image_index} 张降级识图成功"
                )
                return text
        except Exception as exc:
            logger.debug(
                f"[ImageGenResult] 任务 {task_id or 'unknown'} 第 {image_index} 张降级识图失败: {exc}"
            )
        return ""
