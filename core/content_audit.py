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
    "请你作为AI回复质量分析员审核AI的回复是否符合以下标准，并分点给出调整方向（如果需要）。\n\n"
    "[审核标准]\n"
    "{criteria}\n\n"
    "[前次调整方向]\n"
    "{last_correction}\n\n"
    "---\n"
    "[待审核的对话]\n\n"
    "{conversation}\n\n"
    "---\n"
    "请认真查看并分析以上对话中 AI 的回复是否符合审核标准，并相较于上一次是否有改正趋势\n\n"
    "输出格式：\n"
    "- 如果完全符合上述所有标准 → 回复：无需调整\n"
    "- 如果存在任何不符合项 → 回复：调整方向:<简洁、清晰、可执行的校正指示，并说明“建议”还是“要求”>（例如：“建议使用更活泼的语气”“建议适当增加语气词”“后续回复中括号内不允许‘像是’句式，只写动作”“后续回复中禁止使用markdown”）\n"
    "- 如果无法判断或审核失败 → 回复：无法进行审核\n"
    "- 如果对话中用户有要求，则以用户要求为优先，临时更改、减弱或忽略部分调整项。\n"
    "- 矫正规则可以适当放宽，忽略轻微偏移，但若触发重点或禁止性规则，则必须说明。\n"
    "- 校正指示应当简洁、清晰说明 AI 应当如何改进回复，不宜长篇大论。\n"
    "- 矫正指示应当引导AI调整后续回复内容，但避免指出具体案例。"
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

        # 审核关键词（针对 AI 回复内容匹配，命中后直接生成校正指示）
        self._audit_keywords: list[str] = getattr(plugin, "content_audit_keywords", [])
        if not isinstance(self._audit_keywords, list):
            self._audit_keywords = []

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
            logger.debug("[ContentAudit] 跳过: 无法生成 key")
            return
        if not provider:
            logger.debug("[ContentAudit] 跳过: provider 为空")
            return

        # ---- 第零步：记录用户消息到 _audit_chats ----
        user_message_text = (event.get_message_outline() or "").strip()
        if user_message_text:
            await self._audit_record_message(key, "user", user_message_text, event)

        logger.info(
            f"[ContentAudit] on_ai_reply 被调用，key={key}，"
            f"回复长度 {len(reply_text)}"
        )

        # ---- 第一步：将本次 AI 回复写入 _audit_chats ----
        await self._append_reply_to_buffer(key, reply_text)

        # ---- 第二步：计数器 +1 ----
        async with self._counter_lock:
            current_count = self._counters.get(key, 0) + 1
            self._counters[key] = current_count

        logger.info(
            f"[ContentAudit] key={key} 消息计数: "
            f"{current_count}/{self._audit_rounds}"
        )

        # ---- 第三步：检查保持位（每次都要检查） ----
        should_trigger = False
        trigger_reason = ""
        has_pending = False

        async with self._pending_keyword_lock:
            has_pending = self._pending_keyword.pop(key, False)

        if has_pending and current_count >= self._min_rounds:
            should_trigger = True
            trigger_reason = "保持位触发"
            logger.info(
                f"[ContentAudit] key={key} 保持位触发审核，"
                f"当前计数 {current_count} >= 最小轮数 {self._min_rounds}"
            )

        # ---- 第四步：判断是否为关键词触发 ----
        matched_keywords: list[str] = []
        if not should_trigger and self._audit_keywords and reply_text:
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
                should_trigger = True
                trigger_reason = "达到消息阈值，触发审核"
            else:
                logger.debug(f"[ContentAudit] key={key} 未达触发条件，跳过")
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
                key in self._running_tasks
                and not self._running_tasks[key].done()
            )

        if has_running:
            async with self._audit_queue_lock:
                if key not in self._audit_queue:
                    self._audit_queue[key] = [True]
                else:
                    logger.debug(f"[ContentAudit] key={key} 已有等待中的审核请求，合并")
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
            logger.debug(f"[ContentAudit] 后台任务: 会话 {session_id} 被取消")
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

        Args:
            event: 当前消息事件
            request: ProviderRequest 对象
        """
        key = await self._make_key(event)
        if not key:
            logger.debug("[ContentAudit] inject_to_request 跳过: 无法生成 key")
            return

        logger.debug(f"[ContentAudit] inject_to_request 被调用，key={key}")

        correction = None
        ready = False
        async with self._inject_ready_lock:
            ready = self._inject_ready.pop(key, False)

        if not ready:
            logger.debug(
                f"[ContentAudit] key={key} 后台审核未完成或无需注入，跳过"
            )
            return

        async with self._correction_lock:
            correction = self._corrections.pop(key, None)

        logger.debug(
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
            logger.debug(f"[ContentAudit] key={key} 无需调整，跳过注入")
            return

        reminder = f"<system_WARNING>上下文内容已触发对话审核规则，请按照指示调整后续回复：{correction}</system_WARNING>"

        if (
            hasattr(request, "extra_user_content_parts")
            and request.extra_user_content_parts
        ):
            try:
                request.extra_user_content_parts.append(
                    type(request.extra_user_content_parts[0])(text=reminder)
                )
                logger.info(f"[ContentAudit] 已注入校正指示到 key={key}")
            except Exception as e:
                logger.debug(f"[ContentAudit] 注入校正指示失败: {e}")
        elif hasattr(request, "system_prompt"):
            request.system_prompt += f"\n{reminder}\n"
            logger.info(
                f"[ContentAudit] 已注入校正指示(system_prompt)到 key={key}"
            )

        # ---- 内部方法 ----

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

        logger.debug(
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
            logger.debug(f"[ContentAudit] 获取 conversation_id 失败: {e}")
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
