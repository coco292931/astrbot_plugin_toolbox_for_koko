"""
自动内容审核校正器 (ContentAuditLoop)

职责:
  1. 消息计数器追踪每个对话的消息条数（key = f"{umo}:{cid}"，精确区分不同对话）
  2. 达到阈值时，从私有 _audit_chats 缓冲区抓取最近 N 轮对话
  3. 合并审核标准 + 上次校正方向 → 调审核 LLM
  4. 存储审核结果（校正方向或"无需调整"）
  5. 下一条用户消息时，以 <system_WARNING> 注入到请求

与 KCContextManager 的关系:
  - 完全独立，不使用 KCContextManager._session_chats
  - 维护自己的 _audit_chats 缓冲区（纯内存，重启即重置）
  - key 使用 umo:cid 复合键，精确区分同一群聊内的不同对话
  - 通过 plugin.context.conversation_manager.get_curr_conversation_id() 获取 cid

所有状态全是纯内存，重启即重置。
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


# 默认审核 LLM prompt
DEFAULT_AUDIT_PROMPT = (
    "请你作为AI回复质量分析员审核AI的回复是否符合以下标准，相较于上一次是否有改正趋势，并分点给出调整方向（如果需要）。\n\n"
    "输出格式：\n"
    "- 如果完全符合上述所有标准 → 回复：无需调整\n"
    "- 如果存在任何不符合项 → 回复：调整方向:<简洁、清晰、可执行的校正指示，并说明“建议”还是“要求”>（例如：“建议使用更活泼的语气”“建议适当增加语气词”“后续回复中括号内不允许‘像是’句式，只写动作”“后续回复中禁止使用markdown”）\n"
    "- 如果无法判断或审核失败 → 回复：无法进行审核\n"
    "- 如果对话中用户有要求，则以用户要求为优先，临时更改、减弱或忽略部分调整项。\n"
    "- 如果对话中用户明确提出或质疑ai的回复风格不对，优先指引ai重新回顾自身提示词，基于自身设定重新组织语言。\n"
    "- 矫正规则可以适当放宽，忽略轻微偏移，但若触发重点或禁止性规则，则必须说明。\n"
    "- 前次调整方向仅供参考，若非必要或与审核标准相悖，可以忽略。\n"
    "- 校正指示应当简洁、清晰说明 AI 应当如何改进回复，不宜长篇大论。\n"
    "- 矫正指示应当引导AI调整后续回复内容，但避免指出具体错误案例。\n\n"
    "[审核标准]\n"
    "{criteria}\n\n"
    "[前次调整方向]\n"
    "{last_correction}\n\n"
    "---\n"
    "[待审核的对话]\n\n"
    "{conversation}\n\n"
    "---\n"
    "请认真查看并逐条分析以上对话中 AI 的回复是否符合审核标准，并向ai给出改进建议\n\n"
)

# 默认人格遵循审核 LLM prompt
DEFAULT_PERSONA_AUDIT_PROMPT = (
    "请你作为AI角色扮演审核员，审核AI的回复是否符合其设定的人格特征。\n\n"
    "输出格式：\n"
    "- 如果AI的回复完全符合所设定人格 → 回复：无需调整\n"
    "- 如果任何方面不符合人格设定 → 回复：调整方向:<简洁、清晰的校正指示>（例如：“建议使用更温柔的语气”“建议增加口语化表达”“建议减少正式用语”）\n"
    "- 如果无法判断 → 回复：无法进行审核\n"
    "- 如果对话中用户明确要求AI改变风格，则以用户要求为优先。\n\n"
    "[AI人格设定]\n"
    "{persona_prompt}\n\n"
    "[前次调整方向]\n"
    "{last_correction}\n\n"
    "---\n"
    "[待审核的对话]\n\n"
    "{conversation}\n\n"
    "---\n"
    "请认真审核以上对话中AI的回复是否符合人格设定，逐条分析，并给出改进建议。"
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
        self._min_rounds: int = max(0, getattr(plugin, "content_audit_min_rounds", 2))
        self._fetch_rounds: int = max(
            1, getattr(plugin, "content_audit_fetch_rounds", 5)
        )
        self._criteria: str = (
            getattr(plugin, "content_audit_criteria", "")
            or "回复应当友好、有用，不包含敏感内容。"
        )

        # 关键词审核开关
        self._keyword_audit_enabled: bool = bool(
            getattr(plugin, "content_audit_keyword_enabled", True)
        )

        # 审核关键词（针对 AI 回复内容匹配，命中后直接生成校正指示）
        self._audit_keywords: list[str] = getattr(plugin, "content_audit_keywords", [])
        if not isinstance(self._audit_keywords, list):
            self._audit_keywords = []

        # ---- 人格遵循审核配置 ----
        self._persona_audit_enabled: bool = bool(
            getattr(plugin, "persona_audit_enabled", False)
        )
        self._persona_audit_rounds: int = max(
            1, getattr(plugin, "persona_audit_rounds", 5)
        )
        self._persona_prompt: str = getattr(plugin, "persona_audit_prompt", "") or ""

        self._inject_mode_pa: str = (
            getattr(plugin, "persona_audit_inject_mode", "conversation")
            or "conversation"
        )
        if self._inject_mode_pa not in ("prompt", "conversation"):
            self._inject_mode_pa = "conversation"

        # 调试日志开关
        self._debug_enabled: bool = bool(getattr(plugin, "content_audit_debug", False))

        # 注入模式: "prompt" → system_prompt, "conversation" → extra_user_content_parts
        self._inject_mode_ca: str = (
            getattr(plugin, "content_audit_inject_mode", "conversation")
            or "conversation"
        )
        if self._inject_mode_ca not in ("prompt", "conversation"):
            self._inject_mode_ca = "conversation"

        # ---- 内容审核状态（原有） ----
        # session_id -> int（消息计数器）
        self._counters: dict[str, int] = {}
        self._counter_lock = asyncio.Lock()

        # session_id -> str | None
        #   None  = 尚未审核过
        #   ""    = 已审核，无需调整
        #   其他   = 校正文本
        self._corrections: dict[str, str | None] = {}
        self._correction_lock = asyncio.Lock()

        # session_id -> str | None（上次校正方向，供下次审核 LLM 参考，不被 inject 消费）
        self._last_corrections: dict[str, str | None] = {}
        self._last_correction_lock = asyncio.Lock()

        # session_id -> bool（注入就绪标记：后台审核已完成，可注入）
        self._inject_ready: dict[str, bool] = {}
        self._inject_ready_lock = asyncio.Lock()

        # session_id -> asyncio.Task（当前正在运行的后台审核任务）
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._running_tasks_lock = asyncio.Lock()

        # session_id -> int（水印位置：记录上次审核触发时缓冲区消息数，用于连续触发时累积）
        self._watermarks: dict[str, int] = {}
        self._watermark_lock = asyncio.Lock()

        # session_id -> list[bool]（审核请求队列占位：前一个完成后自动处理下一个）
        self._audit_queue: dict[str, list[bool]] = {}
        self._audit_queue_lock = asyncio.Lock()

        # session_id -> bool（保持位：关键词触发但未达最小轮数，等待下一轮）
        self._pending_keyword: dict[str, bool] = {}
        self._pending_keyword_lock = asyncio.Lock()

        # ---- 人格遵循审核状态（独立于内容审核） ----
        self._pa_counters: dict[str, int] = {}
        self._pa_counter_lock = asyncio.Lock()
        self._pa_corrections: dict[str, str | None] = {}
        self._pa_correction_lock = asyncio.Lock()
        self._pa_last_corrections: dict[str, str | None] = {}
        self._pa_last_correction_lock = asyncio.Lock()
        self._pa_inject_ready: dict[str, bool] = {}
        self._pa_inject_ready_lock = asyncio.Lock()
        self._pa_running_tasks: dict[str, asyncio.Task] = {}
        self._pa_running_tasks_lock = asyncio.Lock()
        self._pa_watermarks: dict[str, int] = {}
        self._pa_watermark_lock = asyncio.Lock()
        self._pa_audit_queue: dict[str, list[bool]] = {}
        self._pa_audit_queue_lock = asyncio.Lock()

        # ---- 审计专用的对话上下文缓冲区（不依赖 KCContextManager._session_chats） ----
        # key = f"{umo}:{cid}"，同一对话的 user/assistant 消息混在一起
        self._audit_chats: dict[str, list[dict]] = {}
        self._chat_lock = asyncio.Lock()

    # ---- 公开方法 ----

    async def on_ai_reply(
        self,
        event: AstrMessageEvent,
        reply_text: str,
        provider: Any,
    ) -> None:
        """在 AI 回复后调用：记录回复到缓冲区 + 计数 + 检查阈值 + 触发审核。

        Args:
            event: 当前消息事件（从中获取 umo、cid）
            reply_text: AI 回复文本
            provider: LLM Provider 实例
        """
        key = await self._make_key(event)
        if not key:
            self._log_debug("[ContentAudit] 跳过: 无法生成 key")
            return
        if not provider:
            self._log_debug("[ContentAudit] 跳过: provider 为空")
            return

        # ---- 第零步：记录用户消息到 _audit_chats ----
        user_message_text = (event.get_message_outline() or "").strip()
        if user_message_text:
            await self._audit_record_message(key, "user", user_message_text, event)

        logger.info(
            f"[ContentAudit] on_ai_reply 被调用，key={key}，回复长度 {len(reply_text)}"
        )

        # ---- 第一步：将本次 AI 回复写入 _audit_chats ----
        await self._append_reply_to_buffer(key, reply_text)

        # ---- 第二步：计数器 +1 ----
        async with self._counter_lock:
            current_count = self._counters.get(key, 0) + 1
            self._counters[key] = current_count

        logger.info(
            f"[ContentAudit] key={key} 消息计数: {current_count}/{self._audit_rounds}"
        )

        # ---- 第三步：检查保持位（每次都要检查） ----
        should_trigger = False
        trigger_reason = ""
        has_pending = False

        async with self._pending_keyword_lock:
            has_pending = self._pending_keyword.pop(key, False)

        if has_pending:
            if current_count >= self._min_rounds:
                should_trigger = True
                trigger_reason = "保持位触发"
                logger.info(
                    f"[ContentAudit] key={key} 保持位触发审核，"
                    f"当前计数 {current_count} >= 最小轮数 {self._min_rounds}"
                )
            else:
                # 保持位已消费但未达到最小轮数，重新设回等待下一轮
                self._pending_keyword[key] = True
                self._log_debug(
                    f"[ContentAudit] key={key} 保持位已消费但当前计数 "
                    f"{current_count} < 最小轮数 {self._min_rounds}，重新设置保持位"
                )

        # ---- 第四步：判断是否为关键词触发 ----
        matched_keywords: list[str] = []
        if (
            not should_trigger
            and self._keyword_audit_enabled
            and self._audit_keywords
            and reply_text
        ):
            matched_keywords = [kw for kw in self._audit_keywords if kw in reply_text]
        is_keyword_trigger = bool(matched_keywords)

        if is_keyword_trigger:
            logger.info(
                f"[ContentAudit] 关键词触发审核，key={key}，"
                f"匹配关键词: {matched_keywords}，"
                f"回复: {reply_text[:60]}..."
            )

        # ---- 第五步：决定是否触发审核 ----
        if not should_trigger:
            if is_keyword_trigger:
                if current_count >= self._min_rounds:
                    should_trigger = True
                    trigger_reason = "关键词触发"
                else:
                    logger.info(
                        f"[ContentAudit] key={key} 关键词触发但当前计数 "
                        f"{current_count} < 最小轮数 {self._min_rounds}，"
                        f"设保持位等待下一轮"
                    )
                    async with self._pending_keyword_lock:
                        self._pending_keyword[key] = True
                    return
            elif current_count >= self._audit_rounds:
                if self._min_rounds > 0 and current_count < self._min_rounds:
                    self._log_debug(
                        f"[ContentAudit] key={key} 达到消息阈值({current_count}>={self._audit_rounds})"
                        f"但未达到最小间隔({self._min_rounds})，跳过"
                    )
                    return
                should_trigger = True
                trigger_reason = "达到消息阈值，触发审核"
            else:
                self._log_debug(f"[ContentAudit] key={key} 未达触发条件，跳过")
                return

        logger.info(f"[ContentAudit] key={key} {trigger_reason}，开始审核...")

        # 取最近的消息
        fetch_count = current_count * 2
        conversation_text = await self._fetch_recent_conversation(key, fetch_count)
        if not conversation_text:
            logger.warning("[ContentAudit] 无可审核的对话内容，跳过")
            async with self._counter_lock:
                self._counters[key] = 0
            return

        # 取上次校正方向
        last_correction = ""
        async with self._last_correction_lock:
            prev = self._last_corrections.get(key)
            if prev and prev.strip():
                last_correction = prev.strip()

        # 检查是否已有运行中的后台任务
        async with self._running_tasks_lock:
            has_running = (
                key in self._running_tasks and not self._running_tasks[key].done()
            )

        if has_running:
            async with self._audit_queue_lock:
                if key not in self._audit_queue:
                    self._audit_queue[key] = [True]
                else:
                    self._log_debug(
                        f"[ContentAudit] key={key} 已有等待中的审核请求，合并"
                    )
            async with self._counter_lock:
                self._counters[key] = 0
            async with self._watermark_lock:
                self._watermarks[key] = await self._get_buffer_length(key)
            return

        async with self._counter_lock:
            self._counters[key] = 0
        async with self._watermark_lock:
            self._watermarks[key] = await self._get_buffer_length(key)

        task = asyncio.create_task(
            self._run_audit_task(
                session_id=key,
                provider=provider,
                conversation_text=conversation_text,
                last_correction=last_correction,
            )
        )
        async with self._running_tasks_lock:
            self._running_tasks[key] = task

    async def _run_audit_task(
        self,
        session_id: str,
        provider: Any,
        conversation_text: str,
        last_correction: str,
    ) -> None:
        """后台审核任务：调用审核 LLM 并存储结果。"""
        try:
            logger.info(f"[ContentAudit] 后台任务: 会话 {session_id} 调用审核 LLM...")
            correction = await self._call_audit_llm(
                provider, conversation_text, last_correction
            )
            logger.info(
                f"[ContentAudit] 后台任务: 会话 {session_id} 审核 LLM 返回: "
                f"{'None' if correction is None else ('空' if correction == '' else correction[:200])}..."
            )

            async with self._correction_lock:
                self._corrections[session_id] = correction
            async with self._inject_ready_lock:
                self._inject_ready[session_id] = True
            async with self._last_correction_lock:
                self._last_corrections[session_id] = correction

            if correction:
                logger.info(
                    f"[ContentAudit] 后台任务: 会话 {session_id} "
                    f"审核完成，校正指示: {correction[:20]}..."
                )
            else:
                logger.info(
                    f"[ContentAudit] 后台任务: 会话 {session_id} 审核完成，无需调整"
                )
        except asyncio.CancelledError:
            self._log_debug(f"[ContentAudit] 后台任务: 会话 {session_id} 被取消")
        except Exception as e:
            logger.error(f"[ContentAudit] 后台任务: 会话 {session_id} 审核异常: {e}")
        finally:
            # 检查队列中是否有等待的审核请求（先读水印再清理）
            has_next = False
            async with self._audit_queue_lock:
                if self._audit_queue.pop(session_id, None) is not None:
                    has_next = True

            if has_next:
                # 有等待的审核请求：先读水印再清理，基于水印取新消息启动下一个任务
                async with self._watermark_lock:
                    watermark = self._watermarks.get(session_id, 0)
                    self._watermarks.pop(session_id, None)
                logger.info(
                    f"[ContentAudit] 后台任务: 会话 {session_id} "
                    f"队列中有等待的审核请求，继续处理（水印={watermark}）..."
                )
                # 从缓冲区取水印到末尾的消息
                buffer_len = await self._get_buffer_length(session_id)
                fetch_count = buffer_len - watermark
                if fetch_count < 1:
                    fetch_count = self._fetch_rounds * 2

                new_conversation = await self._fetch_recent_conversation(
                    session_id, fetch_count
                )
                if new_conversation:
                    # 取上次校正方向（从 _last_corrections 读，不受 inject_to_request 影响）
                    last_correction = ""
                    async with self._last_correction_lock:
                        prev = self._last_corrections.get(session_id)
                        if prev and prev.strip():
                            last_correction = prev.strip()

                    # 重置计数器并更新水印
                    async with self._counter_lock:
                        self._counters[session_id] = 0
                    async with self._watermark_lock:
                        self._watermarks[session_id] = await self._get_buffer_length(
                            session_id
                        )

                    task = asyncio.create_task(
                        self._run_audit_task(
                            session_id=session_id,
                            provider=provider,
                            conversation_text=new_conversation,
                            last_correction=last_correction,
                        )
                    )
                    async with self._running_tasks_lock:
                        self._running_tasks[session_id] = task
                else:
                    logger.warning(
                        f"[ContentAudit] 后台任务: 会话 {session_id} "
                        f"队列请求无可审核内容，跳过"
                    )
            else:
                # 无队列请求，清理水印
                async with self._watermark_lock:
                    self._watermarks.pop(session_id, None)

            # 清理运行中任务引用
            async with self._running_tasks_lock:
                if self._running_tasks.get(session_id) is asyncio.current_task():
                    del self._running_tasks[session_id]

    async def inject_to_request(self, event: AstrMessageEvent, request: Any) -> None:
        """在下一条用户消息的 LLM 请求中注入校正指示（一次性）。

        注入位置由 _inject_mode_ca / _inject_mode_pa 控制：
          - "prompt"       → 追加到 system_prompt
          - "conversation" → 追加到 extra_user_content_parts（会话侧）
        默认 conversation。

        Args:
            event: 当前消息事件
            request: ProviderRequest 对象
        """
        key = await self._make_key(event)
        if not key:
            self._log_debug("[ContentAudit] inject_to_request 跳过: 无法生成 key")
            return

        self._log_debug(f"[ContentAudit] inject_to_request 被调用，key={key}")

        correction = None
        ready = False
        async with self._inject_ready_lock:
            ready = self._inject_ready.pop(key, False)

        if not ready:
            self._log_debug(f"[ContentAudit] key={key} 后台审核未完成或无需注入，跳过")
            return

        async with self._correction_lock:
            correction = self._corrections.pop(key, None)

        self._log_debug(
            f"[ContentAudit] key={key} 取出校正: "
            f"{'None' if correction is None else ('空' if correction == '' else correction[:60])}..."
        )

        if correction is None:
            logger.warning(
                f"[ContentAudit] key={key} _inject_ready 为 True 但 _corrections 为空，"
                f"恢复标记等待下次注入"
            )
            async with self._inject_ready_lock:
                self._inject_ready[key] = True
            return
        if not correction.strip():
            self._log_debug(f"[ContentAudit] key={key} 无需调整，跳过注入")
            return

        reminder = f"<system_WARNING>上下文内容已触发对话审核规则，请按照指示调整后续回复：{correction}</system_WARNING>"

        # 根据 _inject_mode_ca 选择注入位置
        self._do_inject(request, reminder, self._inject_mode_ca, "ContentAudit", key)

        # === 第二部分：人格遵循审核注入 ===
        pa_correction = None
        pa_ready = False
        async with self._pa_inject_ready_lock:
            pa_ready = self._pa_inject_ready.pop(key, False)

        if pa_ready:
            async with self._pa_correction_lock:
                pa_correction = self._pa_corrections.pop(key, None)

            if pa_correction is None:
                logger.warning(
                    f"[PersonaAudit] key={key} _pa_inject_ready 为 True 但 _pa_corrections 为空，"
                    f"恢复标记等待下次注入"
                )
                async with self._pa_inject_ready_lock:
                    self._pa_inject_ready[key] = True
            elif pa_correction.strip():
                pa_reminder = f"<system_WARNING>上下文内容已触发人格遵循审核，请按照指示调整后续回复：{pa_correction}</system_WARNING>"
                self._do_inject(
                    request, pa_reminder, self._inject_mode_pa, "PersonaAudit", key
                )
            else:
                self._log_debug(f"[PersonaAudit] key={key} 无需调整，跳过注入")

    def _do_inject(
        self,
        request: Any,
        text: str,
        mode: str,
        tag: str,
        key: str,
    ) -> None:
        """将校正文本注入到 request 的指定位置。

        Args:
            request: ProviderRequest 对象
            text: 要注入的校正文本（已含 <system_WARNING> 标签）
            mode: "prompt" 或 "conversation"
            tag: 日志标签（如 "ContentAudit" / "PersonaAudit"）
            key: 会话 key（仅用于日志）
        """
        if mode == "conversation":
            # 注入到会话侧：extra_user_content_parts
            if (
                hasattr(request, "extra_user_content_parts")
                and request.extra_user_content_parts
            ):
                try:
                    request.extra_user_content_parts.append(
                        type(request.extra_user_content_parts[0])(text=text)
                    )
                    logger.info(f"[{tag}] 已注入校正指示(conversation)到 key={key}")
                    return
                except Exception as e:
                    self._log_debug(f"[{tag}] 注入校正指示(conversation)失败: {e}")
            # 回退：尝试 prompt
            if hasattr(request, "prompt"):
                current = request.prompt if isinstance(request.prompt, str) else ""
                request.prompt = current + f"\n\n{text}"
                logger.info(f"[{tag}] 已注入校正指示(prompt-回退)到 key={key}")
                return
            # 最终回退：system_prompt
            if hasattr(request, "system_prompt") and request.system_prompt:
                request.system_prompt += f"\n\n{text}"
                logger.info(
                    f"[{tag}] 已注入校正指示(system_prompt-最终回退)到 key={key}"
                )
                return
            logger.warning(f"[{tag}] 无可用的注入点位，key={key}，丢弃校正指示")
        else:
            # mode == "prompt"：注入到 system_prompt
            if hasattr(request, "system_prompt") and request.system_prompt:
                request.system_prompt += f"\n\n{text}"
                logger.info(f"[{tag}] 已注入校正指示(system_prompt)到 key={key}")
            elif (
                hasattr(request, "extra_user_content_parts")
                and request.extra_user_content_parts
            ):
                try:
                    request.extra_user_content_parts.append(
                        type(request.extra_user_content_parts[0])(text=text)
                    )
                    logger.info(f"[{tag}] 已注入校正指示(extra-回退)到 key={key}")
                except Exception as e:
                    self._log_debug(f"[{tag}] 注入校正指示(extra-回退)失败: {e}")
            elif hasattr(request, "prompt"):
                current = request.prompt if isinstance(request.prompt, str) else ""
                request.prompt = current + f"\n\n{text}"
                logger.info(f"[{tag}] 已注入校正指示(prompt-最终回退)到 key={key}")
            else:
                logger.warning(f"[{tag}] 无可用的注入点位，key={key}，丢弃校正指示")

    # ---- 人格遵循审核入口 ----

    async def on_ai_reply_persona(
        self,
        event: AstrMessageEvent,
        reply_text: str,
        provider: Any,
    ) -> None:
        """人格遵循审核入口，与 on_ai_reply 并行调用。

        与内容审核共享 _audit_chats 缓冲区，但使用独立的 _pa_ 状态变量。
        无人格关键词触发逻辑，仅有轮数触发。
        """
        if not self._persona_audit_enabled:
            return
        if not self._persona_prompt:
            return

        key = await self._make_key(event)
        if not key:
            return
        if not provider:
            return

        # 用户消息已由 on_ai_reply 写入 _audit_chats，无需重复写入
        # AI 回复也已由 on_ai_reply 写入 _audit_chats，无需重复写入

        # 计数器 +1
        async with self._pa_counter_lock:
            current_count = self._pa_counters.get(key, 0) + 1
            self._pa_counters[key] = current_count

        # 检查是否达到轮数阈值（仅轮数触发，无关键词）
        if current_count < self._persona_audit_rounds:
            self._log_debug(
                f"[PersonaAudit] key={key} 未达触发条件 ({current_count}/{self._persona_audit_rounds})，跳过"
            )
            return

        logger.info(f"[PersonaAudit] key={key} 达到轮数阈值，开始审核...")

        fetch_count = current_count * 2
        conversation_text = await self._fetch_recent_conversation(key, fetch_count)
        if not conversation_text:
            logger.warning("[PersonaAudit] 无可审核的对话内容，跳过")
            async with self._pa_counter_lock:
                self._pa_counters[key] = 0
            return

        # 取上次校正方向
        last_correction = ""
        async with self._pa_last_correction_lock:
            prev = self._pa_last_corrections.get(key)
            if prev and prev.strip():
                last_correction = prev.strip()

        # 检查是否有运行中的后台任务
        async with self._pa_running_tasks_lock:
            has_running = (
                key in self._pa_running_tasks and not self._pa_running_tasks[key].done()
            )

        if has_running:
            async with self._pa_audit_queue_lock:
                if key not in self._pa_audit_queue:
                    self._pa_audit_queue[key] = [True]
                else:
                    self._log_debug(
                        f"[PersonaAudit] key={key} 已有等待中的审核请求，合并"
                    )
            async with self._pa_counter_lock:
                self._pa_counters[key] = 0
            async with self._pa_watermark_lock:
                self._pa_watermarks[key] = await self._get_buffer_length(key)
            return

        async with self._pa_counter_lock:
            self._pa_counters[key] = 0
        async with self._pa_watermark_lock:
            self._pa_watermarks[key] = await self._get_buffer_length(key)

        task = asyncio.create_task(
            self._run_pa_audit_task(
                session_id=key,
                provider=provider,
                conversation_text=conversation_text,
                last_correction=last_correction,
            )
        )
        async with self._pa_running_tasks_lock:
            self._pa_running_tasks[key] = task

    async def _run_pa_audit_task(
        self,
        session_id: str,
        provider: Any,
        conversation_text: str,
        last_correction: str,
    ) -> None:
        """人格审核后台任务：调用审核 LLM 并存储结果。"""
        try:
            logger.info(f"[PersonaAudit] 后台任务: 会话 {session_id} 调用审核 LLM...")
            correction = await self._call_persona_audit_llm(
                provider, conversation_text, last_correction
            )
            logger.info(
                f"[PersonaAudit] 后台任务: 会话 {session_id} 审核 LLM 返回: "
                f"{'None' if correction is None else ('空' if correction == '' else correction[:200])}..."
            )

            async with self._pa_correction_lock:
                self._pa_corrections[session_id] = correction
            async with self._pa_inject_ready_lock:
                self._pa_inject_ready[session_id] = True
            async with self._pa_last_correction_lock:
                self._pa_last_corrections[session_id] = correction

            if correction:
                logger.info(
                    f"[PersonaAudit] 后台任务: 会话 {session_id} "
                    f"审核完成，校正指示: {correction[:20]}..."
                )
            else:
                logger.info(
                    f"[PersonaAudit] 后台任务: 会话 {session_id} 审核完成，无需调整"
                )
        except asyncio.CancelledError:
            self._log_debug(f"[PersonaAudit] 后台任务: 会话 {session_id} 被取消")
        except Exception as e:
            logger.error(f"[PersonaAudit] 后台任务: 会话 {session_id} 审核异常: {e}")
        finally:
            has_next = False
            async with self._pa_audit_queue_lock:
                if self._pa_audit_queue.pop(session_id, None) is not None:
                    has_next = True

            if has_next:
                async with self._pa_watermark_lock:
                    watermark = self._pa_watermarks.get(session_id, 0)
                    self._pa_watermarks.pop(session_id, None)
                logger.info(
                    f"[PersonaAudit] 后台任务: 会话 {session_id} "
                    f"队列中有等待的审核请求，继续处理（水印={watermark}）..."
                )
                buffer_len = await self._get_buffer_length(session_id)
                fetch_count = buffer_len - watermark
                if fetch_count < 1:
                    fetch_count = self._fetch_rounds * 2

                new_conversation = await self._fetch_recent_conversation(
                    session_id, fetch_count
                )
                if new_conversation:
                    last_correction = ""
                    async with self._pa_last_correction_lock:
                        prev = self._pa_last_corrections.get(session_id)
                        if prev and prev.strip():
                            last_correction = prev.strip()

                    async with self._pa_counter_lock:
                        self._pa_counters[session_id] = 0
                    async with self._pa_watermark_lock:
                        self._pa_watermarks[session_id] = await self._get_buffer_length(
                            session_id
                        )

                    task = asyncio.create_task(
                        self._run_pa_audit_task(
                            session_id=session_id,
                            provider=provider,
                            conversation_text=new_conversation,
                            last_correction=last_correction,
                        )
                    )
                    async with self._pa_running_tasks_lock:
                        self._pa_running_tasks[session_id] = task
                else:
                    logger.warning(
                        f"[PersonaAudit] 后台任务: 会话 {session_id} "
                        f"队列请求无可审核内容，跳过"
                    )
            else:
                async with self._pa_watermark_lock:
                    self._pa_watermarks.pop(session_id, None)

            async with self._pa_running_tasks_lock:
                if self._pa_running_tasks.get(session_id) is asyncio.current_task():
                    del self._pa_running_tasks[session_id]

    async def _call_persona_audit_llm(
        self,
        provider: Any,
        conversation_text: str,
        last_correction: str,
    ) -> str | None:
        """调用人格审核 LLM，使用独立的人格 prompt 模板。"""
        prompt_text = DEFAULT_PERSONA_AUDIT_PROMPT.format(
            persona_prompt=self._persona_prompt,
            last_correction=last_correction or "无",
            conversation=conversation_text,
        )

        try:
            resp = await provider.text_chat(
                prompt=prompt_text,
                persist=False,
            )
        except Exception as e:
            logger.error(f"[PersonaAudit] 审核 LLM 调用异常: {e}")
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

        if "无需调整" in result_text or "无法进行审核" in result_text:
            return ""

        for prefix in ("调整方向:", "调整方向："):
            if prefix in result_text:
                idx = result_text.find(prefix)
                return result_text[idx + len(prefix) :].strip()

        return result_text.strip()

    # ---- 内部方法 ----

    def _log_debug(self, msg: str) -> None:
        """仅在 content_audit_debug 开启时输出 debug 日志。"""
        if self._debug_enabled:
            logger.debug(msg)

    async def _append_reply_to_buffer(self, key: str, reply_text: str) -> None:
        """将本次 AI 回复写入 _audit_chats。"""
        from datetime import datetime

        now = datetime.now()
        entry = {
            "role": "assistant",
            "nickname": "Bot",
            "time": now.strftime("%H:%M:%S"),
            "content": reply_text.strip(),
            "images": [],
        }
        async with self._chat_lock:
            if key not in self._audit_chats:
                self._audit_chats[key] = []
            self._audit_chats[key].append(entry)

    async def _audit_record_message(
        self, key: str, role: str, content: str, event: AstrMessageEvent
    ) -> None:
        """记录用户/AI消息到 _audit_chats。"""
        from datetime import datetime

        nickname = "User"
        try:
            sender = getattr(event.message_obj, "sender", None)
            if sender:
                nickname = getattr(sender, "nickname", "User") or "User"
        except Exception:
            pass

        now = datetime.now()
        entry = {
            "role": role,
            "nickname": nickname,
            "time": now.strftime("%H:%M:%S"),
            "content": content.strip(),
            "images": [],
        }
        async with self._chat_lock:
            if key not in self._audit_chats:
                self._audit_chats[key] = []
            self._audit_chats[key].append(entry)

    async def _get_buffer_length(self, key: str) -> int:
        """获取 _audit_chats 中指定 key 的当前消息数。"""
        async with self._chat_lock:
            return len(self._audit_chats.get(key, []))

    async def _fetch_recent_conversation(
        self, key: str, max_entries: int | None = None
    ) -> str:
        """从 _audit_chats 抓取最近的消息。

        Args:
            key: 复合 key（umo:cid）
            max_entries: 最多抓取的消息条数。None 则使用配置的 fetch_rounds * 2。
        """
        async with self._chat_lock:
            entries = list(self._audit_chats.get(key, []))

        if not entries:
            return ""

        if max_entries is None:
            max_entries = self._fetch_rounds * 2
        recent = entries[-max_entries:] if max_entries > 0 else entries

        lines: list[str] = []
        for entry in recent:
            role = entry.get("role", "user")
            nickname = entry.get("nickname", "User")
            content = entry.get("content", "")
            time_str = entry.get("time", "")
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

        self._log_debug(
            "传入审核 LLM 信息：\n"
            f"{DEFAULT_AUDIT_PROMPT.format(criteria=self._criteria[:100], last_correction=last_correction[:50] or '无', conversation=conversation_text)}\n"
            "\n---"
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
        if "无需调整" in result_text or "无法进行审核" in result_text:
            return ""

        # 提取调整方向
        for prefix in ("调整方向:", "调整方向："):
            if prefix in result_text:
                idx = result_text.find(prefix)
                return result_text[idx + len(prefix) :].strip()

        # 未匹配任何已知格式，当作有校正内容返回
        return result_text.strip()

    async def _resolve_cid(self, umo: str) -> str | None:
        """获取当前对话 ID（conversation_id）。

        通过 AstrBot ConversationManager 获取。
        如果失败或为空，返回 None 表示回退到只用 umo。
        """
        try:
            conv_mgr = getattr(self.plugin.context, "conversation_manager", None)
            if conv_mgr:
                cid = await conv_mgr.get_curr_conversation_id(umo)
                if cid and isinstance(cid, str) and cid.strip():
                    return cid.strip()
        except Exception as e:
            self._log_debug(f"[ContentAudit] 获取 conversation_id 失败: {e}")
        return None

    async def _make_key(self, event: AstrMessageEvent) -> str | None:
        """生成复合 key：umo:cid。

        优先使用 cid 区分对话，回退到纯 umo。
        如果 umo 也为空则返回 None。
        """
        umo = getattr(event, "unified_msg_origin", None)
        if not umo or not isinstance(umo, str) or not umo.strip():
            return None
        umo = umo.strip()

        cid = await self._resolve_cid(umo)
        if cid:
            return f"{umo}:{cid}"
        return umo
