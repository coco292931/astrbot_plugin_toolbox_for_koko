"""
自动内容审核校正器 (ContentAuditLoop)

职责:
  1. 消息计数器追踪每个 session 的消息条数
  2. 达到阈值时，从 KCContextManager._session_chats 抓取最近 N 轮对话
  3. 合并审核标准 + 上次校正方向 → 调审核 LLM
  4. 存储审核结果（校正方向或"无需调整"）
  5. 下一条用户消息时，以 <system_reminder> 注入到请求

数据全是纯内存，重启即重置。
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger


# 默认审核 LLM prompt
DEFAULT_AUDIT_PROMPT = (
    "请你作为AI回复质量分析员审核AI的回复是否符合以下标准，并给出调整方向（如果需要）。\n\n"
    "[审核标准]\n"
    "{criteria}\n\n"
    "[前次调整方向]\n"
    "{last_correction}\n\n"
    "---\n"
    "[待审核的对话]\n\n"
    "{conversation}\n\n"
    "---\n"
    "请分析以上对话中 AI 的回复是否符合审核标准\n\n"
    "输出格式：\n"
    "- 如果完全符合上述所有标准 → 回复：无需调整\n"
    "- 如果存在任何不符合项 → 回复：调整方向:<简洁、清晰、可执行的校正指示，并说明“建议”还是“要求”>（例如：“建议使用更活泼的语气”“建议将‘我看看’改为‘让我看看汪~’”“删除括号内的‘像是’句式，只写动作‘（停了一下）’”）\n"
    "- 如果对话中用户有要求，则以用户要求为优先，临时更改、减弱或忽略部分调整项。\n"
    "- 矫正规则可以适当放宽，忽略轻微偏移，但若触发重点或禁止性规则，则必须说明。\n"
    "- 校正指示应当简洁、清晰说明 AI 应当如何改进回复，不宜长篇大论。"
)


class ContentAuditLoop:
    """自动内容审核校正器。"""

    def __init__(self, plugin: Any) -> None:
        """
        Args:
            plugin: ToolboxPlugin 实例，用于获取配置、Provider、kc_context 等
        """
        self.plugin = plugin

        # 配置
        self._audit_rounds: int = max(1, getattr(plugin, "content_audit_rounds", 5))
        self._fetch_rounds: int = max(
            1, getattr(plugin, "content_audit_fetch_rounds", 5)
        )
        self._criteria: str = (
            getattr(plugin, "content_audit_criteria", "")
            or "回复应当友好、有用，不包含敏感内容。"
        )

        # session_id -> int（消息计数器）
        self._counters: dict[str, int] = {}
        self._counter_lock = asyncio.Lock()

        # session_id -> str | None
        #   None  = 尚未审核过
        #   ""    = 已审核，无需调整
        #   其他   = 校正文本
        self._corrections: dict[str, str | None] = {}
        self._correction_lock = asyncio.Lock()

    # ---- 公开方法 ----

    async def on_ai_reply(
        self,
        session_id: str,
        reply_text: str,
        provider: Any,
    ) -> None:
        """在 AI 回复后调用：计数 + 检查阈值 + 触发审核。

        Args:
            session_id: 会话 ID（event.unified_msg_origin）
            reply_text: AI 回复文本（仅用于计数，审核时从 kc_context 取完整上下文）
            provider: LLM Provider 实例
        """
        if not session_id:
            return
        if not provider:
            return

        # 1. 递增计数器
        async with self._counter_lock:
            count = self._counters.get(session_id, 0) + 1
            self._counters[session_id] = count

        logger.debug(
            f"[ContentAudit] 会话 {session_id} 消息计数: {count}/{self._audit_rounds * 2}"
        )

        # 2. 检查是否达到阈值
        if count < self._audit_rounds * 2:
            return

        logger.info(
            f"[ContentAudit] 会话 {session_id} 达到 {self._audit_rounds} 轮，开始审核..."
        )

        # 3. 取最近的消息
        conversation_text = self._fetch_recent_conversation(session_id)
        if not conversation_text:
            logger.warning("[ContentAudit] 无可审核的对话内容，跳过")
            async with self._counter_lock:
                self._counters[session_id] = 0
            return

        # 4. 取上次校正方向
        last_correction = ""
        async with self._correction_lock:
            prev = self._corrections.get(session_id)
            if prev and prev.strip():
                last_correction = prev.strip()

        # 5. 调审核 LLM
        try:
            correction = await self._call_audit_llm(
                provider, conversation_text, last_correction
            )
        except Exception as e:
            logger.error(f"[ContentAudit] 审核 LLM 调用失败: {e}")
            correction = None

        # 6. 存储结果
        async with self._correction_lock:
            self._corrections[session_id] = correction

        # 7. 重置计数器
        async with self._counter_lock:
            self._counters[session_id] = 0

        if correction:
            logger.info(f"[ContentAudit] 审核完成，校正指示: {correction[:80]}...")
        else:
            logger.info("[ContentAudit] 审核完成，无需调整")

    async def inject_to_request(self, session_id: str, request: Any) -> None:
        """在下一条用户消息的 LLM 请求中注入校正指示（一次性）。

        Args:
            session_id: 会话 ID
            request: ProviderRequest 对象
        """
        if not session_id:
            return

        # 取出并清除（一次性）
        async with self._correction_lock:
            correction = self._corrections.pop(session_id, None)

        if correction is None:
            return  # 还没有审核结果
        if not correction.strip():
            return  # "无需调整"

        # 以 <system_WARNING> 格式注入
        reminder = f"<system_WARNING>长下文已触发对话审核，请按照以下指示调整回复：{correction}</system_WARNING>"

        if (
            hasattr(request, "extra_user_content_parts")
            and request.extra_user_content_parts
        ):
            # 追加到 extra_user_content_parts，紧跟用户消息
            try:
                request.extra_user_content_parts.append(
                    type(request.extra_user_content_parts[0])(text=reminder)
                )
                logger.info(f"[ContentAudit] 已注入校正指示到会话 {session_id}")
            except Exception as e:
                logger.debug(f"[ContentAudit] 注入校正指示失败: {e}")
        elif hasattr(request, "system_prompt"):
            # 回退：追加到 system_prompt
            request.system_prompt += f"\n{reminder}\n"
            logger.info(
                f"[ContentAudit] 已注入校正指示(system_prompt)到会话 {session_id}"
            )

    # ---- 内部方法 ----

    def _fetch_recent_conversation(self, session_id: str) -> str:
        """从 KCContextManager._session_chats 抓取最近 N 轮对话。

        复用 kc_context 的 _session_chats 数据格式。
        """
        kc_context = getattr(self.plugin, "kc_context", None)
        if not kc_context:
            return ""

        session_chats = getattr(kc_context, "_session_chats", None)
        if not session_chats:
            return ""

        entries = session_chats.get(session_id, [])
        if not entries:
            return ""

        # 取最近 fetch_rounds * 2 条（用户+AI 各一条算一轮）
        max_entries = self._fetch_rounds * 2
        recent = entries[-max_entries:] if max_entries > 0 else entries

        lines: list[str] = []
        for entry in recent:
            role = entry.get("role", "user")
            nickname = entry.get("nickname", "User")
            content = entry.get("content", "")
            time_str = entry.get("time", "")
            # 如果有图片转述结果，从 content 中已包含 [Image: ...]
            lines.append(f"[{nickname}/{time_str}] ({role}): {content}")

        return "\n".join(lines)

    async def _call_audit_llm(
        self,
        provider: Any,
        conversation_text: str,
        last_correction: str,
    ) -> str | None:
        """调用审核 LLM，返回校正文本或 None（表示无需调整）。

        Returns:
            str | None:
                None — 调用失败/异常
                ""   — LLM 返回"无需调整"
                其他  — 校正指示文本
        """
        prompt_text = DEFAULT_AUDIT_PROMPT.format(
            criteria=self._criteria,
            last_correction=last_correction or "无",
            conversation=conversation_text,
        )

        try:
            resp = await provider.text_chat(
                prompt=prompt_text,
                persist=False,
            )
        except Exception as e:
            logger.error(f"[ContentAudit] 审核 LLM 调用异常: {e}")
            return None

        if resp is None:
            return None

        result_text = ""
        if hasattr(resp, "completion_text") and resp.completion_text:
            result_text = resp.completion_text.strip()
        elif isinstance(resp, dict):
            result_text = str(resp.get("completion_text", "") or "").strip()

        if not result_text:
            return None

        # 解析结果
        if "无需调整" in result_text:
            return ""

        # 提取调整方向
        for prefix in ("调整方向:", "调整方向："):
            if prefix in result_text:
                idx = result_text.find(prefix)
                return result_text[idx + len(prefix) :].strip()

        # 未匹配任何已知格式，当作有校正内容返回
        return result_text.strip()
