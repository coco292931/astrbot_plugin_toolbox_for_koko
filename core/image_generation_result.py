from __future__ import annotations

import json
import re
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.message import TextPart


class ImageGenerationResultHandler:
    """为图像生成任务完成后的唤醒请求补充识图结果。"""

    DEFAULT_PROMPT_TEMPLATE = (
        "Please describe the generated image in Chinese. "
        "Focus on the main subjects, style, composition, colors, and notable details."
    )
    _TASK_RESULT_PATTERN = re.compile(
        r"<image_generation_task_result>\s*(\{.*?\})\s*</image_generation_task_result>",
        re.DOTALL,
    )

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._task_caption_cache: dict[str, str] = {}

    async def process(self, event: AstrMessageEvent, request: Any) -> None:
        """检测图像生成任务完成请求，并把识图结果注入上下文。"""
        if not self.plugin.image_generation_result_hook_enabled:
            return

        payload = self._extract_task_payload(request)
        if not payload:
            return

        task_id = str(payload.get("task_id", "") or "").strip()
        status = str(payload.get("status", "") or "").strip().lower()
        result_paths = [
            str(path).strip()
            for path in (payload.get("result_paths") or [])
            if str(path).strip()
        ]
        if status != "succeeded" or not result_paths:
            return

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
                try:
                    resp = await provider.text_chat(
                        prompt=prompt,
                        image_urls=[img_path],
                        persist=False,
                    )
                except Exception as exc:
                    logger.debug(
                        f"[ImageGenResult] 任务 {task_id or 'unknown'} 第 {index} 张识图失败: {exc}"
                    )
                    continue

                text = str(getattr(resp, "completion_text", "") or "").strip()
                if not text and isinstance(resp, dict):
                    text = str(resp.get("completion_text", "") or "").strip()
                if not text:
                    continue

                captions.append(f"- 图片{index}: {text}")

            if not captions:
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

    def _extract_task_payload(self, request: Any) -> dict[str, Any] | None:
        if not hasattr(request, "extra_user_content_parts"):
            return None

        for part in request.extra_user_content_parts:
            text = str(getattr(part, "text", "") or "")
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
