"""用户消息交付时限守卫（tool deadline guard）。

背景
----
AstrBot 的 Agent 循环是串行的::

    API 请求 -> 工具批次 -> API 请求 -> 工具批次 -> ...

用户在 Agent 运行期间发来的消息，会被 follow-up 机制暂存，并且**只在
"下一次 API 请求"那一刻**随工具返回值交给 LLM。因此消息的实际等待时间是::

    在途 API 调用的剩余时长 + 在途工具批次的剩余时长

插件无法干预在途的 API 调用，但可以控制工具批次。

设计原则（务必守住）
--------------------
1. **没有消息插入时，LLM 的工具调用行为必须与关闭本功能完全一致**：
   ``yield_time_ms`` 原样透传，返回值逐字节相同，不额外消耗任何 LLM 轮次。
2. **只有在工具执行期间真的有用户消息插入时**，才在消息到达的那一刻把命令
   转入后台，让 Agent 立刻发出下一次 API 请求把消息交付给 LLM。

为什么不能用钩子直接改参数
--------------------------
``on_using_llm_tool`` 每次工具调用只触发一次、且在执行**之前**。工具开始
等待后插件完全失聪，没有任何钩子能在"等待中途"改值。所以只能在调用前后
各设一个极端：

* 提前把等待压小 -> 无消息时也改变了行为（额外轮询、无谓转后台）；
* 只压已有标记 -> 消息在工具启动之后才到，完全失效。

两者都不满足设计原则，因此本模块改为**拦截工具的 ``call``**，把一次长等待
拆成「启动 + 可中断的等待」：

.. code-block:: text

    task = create_task(原生 call(yield_time_ms=模型给的值))
    done, _ = await wait({task, 消息到达信号}, FIRST_COMPLETED)

    task 先完成  -> return task.result()            # 逐字节原生结果
    消息先到达    -> task.cancel(); return 句柄占位   # 立刻让位

取消只作用于"等待"，不会杀进程：``exec_managed`` 内部用
``asyncio.wait_for(asyncio.shield(wait_task), ...)``，shield 的 wait_task
继续运行，托管会话仍留在 booter 的会话表里、已读 cursor 停在原处，模型后续
``astrbot_shell_session`` 轮询可以接着取回输出。

兼容与兜底
----------
* 只拦截**托管会话类**工具（``LocalExecuteShellTool`` / ``ShellSessionTool``），
  沙箱 ``ExecuteShellTool``、MCP、业务工具完全不碰。
* 类在运行时从 ``ProviderRequest.func_tool`` 里发现，不硬编码模块路径。
* 拿不到会话句柄时按「不重复执行、用 action=list 自查」提示模型。
* 任何异常都退回原生实现；后台唤醒事件（platform 为 ``cron``）一律跳过。
* ``uninstall()`` 会还原原始方法，避免热重载叠加补丁。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Callable

try:  # pragma: no cover - 仅在 AstrBot 运行时可用
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

WAIT_PARAM = "yield_time_ms"
"""工具用于"最多等待多久再返回句柄"的参数名。"""

_CRON_PLATFORM = "cron"
"""AstrBot 后台唤醒事件的平台名，必须跳过。"""

MANAGED_TOOL_CLASS_NAMES = frozenset({"LocalExecuteShellTool", "ShellSessionTool"})
"""默认拦截的工具类名：只有它们返回的是可续查的托管会话句柄。"""

_MAX_SIGNALS = 512
"""信号表上限，超出时清理已置位的条目，避免长期运行后无限增长。"""

_PATCHED_ORIGINALS: dict[type, Callable[..., Any]] = {}
"""已打补丁的类 -> 原始 ``call``。"""

_ACTIVE_GUARD: "ToolDeadlineGuard | None" = None
"""当前生效的守卫实例；``dispatch`` 通过它取配置与信号。"""


def _normalize_names(raw: Any) -> frozenset[str]:
    """规范化工具名集合，忽略空值与类型错误。"""
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(item).strip() for item in raw if str(item).strip())


def _schema_declares_wait_param(tool: Any) -> bool:
    """工具 schema 是否声明了 ``yield_time_ms``。"""
    params = getattr(tool, "parameters", None)
    if not isinstance(params, dict):
        return False
    props = params.get("properties")
    if not isinstance(props, dict):
        return False
    return isinstance(props.get(WAIT_PARAM), dict)


async def _call_original(
    original: Callable[..., Any],
    tool_self: Any,
    context: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    """调用原始 ``call``，兼容同步/异步实现。"""
    result = original(tool_self, context, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _patched_call(self: Any, context: Any, *args: Any, **kwargs: Any) -> Any:
    """工具 ``call`` 的统一入口补丁。"""
    guard = _ACTIVE_GUARD
    original = None
    for base in type(self).__mro__:
        if base in _PATCHED_ORIGINALS:
            original = _PATCHED_ORIGINALS[base]
            break
    if original is None:
        raise RuntimeError(
            f"{type(self).__name__}.call 已被 tool_deadline 接管但找不到原始实现。"
        )
    if guard is None:
        return await _call_original(original, self, context, args, kwargs)
    return await guard.dispatch(original, self, context, args, kwargs)


def _build_backgrounded_result(tool_name: str) -> str:
    """构造"已转后台"占位结果。"""
    payload = {
        "status": "backgrounded",
        "session_id": None,
        "stdout": "",
        "stderr": "",
        "exit_code": None,
        "cursor": 0,
        "has_more": False,
        "session_closed": False,
        "note": (
            "对方在这条命令执行期间发来了新消息，命令已安全转入后台继续运行，请立即回复对方"
            "本轮调用不再触发等待。不要重复执行同一条命令；稍后用 "
            "`astrbot_shell_session` 的 action='list' 找到该会话，"
            "再用 action='poll' 读取后续输出。"
        ),
    }
    logger.debug("[tool_deadline] %s 已转后台（用户消息插入）", tool_name)
    return json.dumps(payload, ensure_ascii=False)


class ToolDeadlineGuard:
    """把"用户消息等待工具"的时间压到毫秒级。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        private_only: bool = True,
        grace_ms: int = 0,
        wait_tools: Any = None,
        skip_tools: Any = None,
        reset_follow_up_seq: bool = True,
    ) -> None:
        """初始化守卫。

        Args:
            enabled: 总开关；关闭后完全不介入（连补丁都不打）。
            private_only: 只对私聊生效；群聊保持原生行为。
            grace_ms: 消息到达后额外等待命令自然结束的宽限（毫秒）。
                默认 0，即消息一到立刻让位。设为正数可让"马上就跑完"的
                命令自然收尾，代价是交付延迟最多增加该值。
            wait_tools: 拦截工具白名单（按类名）。留空表示使用
                ``MANAGED_TOOL_CLASS_NAMES`` 自动识别。
            skip_tools: 拦截工具黑名单，优先级高于白名单。
            reset_follow_up_seq: 是否在安全时机释放 AstrBot 遗留的
                follow-up 序号状态（见 ``reset_stale_follow_up_seq``）。
        """
        self.enabled = bool(enabled)
        self.private_only = bool(private_only)
        self.grace_ms = max(0, int(grace_ms)) if _is_int_like(grace_ms) else 0
        self.wait_tools = _normalize_names(wait_tools)
        self.skip_tools = _normalize_names(skip_tools)
        self.reset_follow_up_seq = bool(reset_follow_up_seq)
        self._signals: dict[str, asyncio.Event] = {}
        self._stats = {
            "marked": 0,
            "cleared": 0,
            "preempted": 0,
            "interrupted": 0,
            "bypassed": 0,
            "seq_reset": 0,
            "patched": 0,
            "step_hooked": 0,
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def install(self) -> None:
        """把守卫设为当前生效实例。类补丁在发现工具时按需打上。"""
        global _ACTIVE_GUARD
        _ACTIVE_GUARD = self

    def uninstall(self) -> None:
        """还原所有已打补丁的类，并解除当前实例。"""
        global _ACTIVE_GUARD
        if _ACTIVE_GUARD is self:
            _ACTIVE_GUARD = None
        for cls, original in list(_PATCHED_ORIGINALS.items()):
            try:
                if getattr(cls, "call", None) is _patched_call:
                    cls.call = original  # type: ignore[method-assign]
            except Exception:
                logger.debug("[tool_deadline] 还原 %s 失败", cls, exc_info=True)
        _PATCHED_ORIGINALS.clear()

    # ------------------------------------------------------------------
    # 钩子入口
    # ------------------------------------------------------------------
    def mark_arrival(self, event: Any) -> bool:
        """记录"有用户消息正在等待交付"。

        Args:
            event: 用户消息事件。

        Returns:
            是否成功置位。
        """
        try:
            umo = self._guardable_umo(event)
            if not umo:
                return False
            if len(self._signals) > _MAX_SIGNALS:
                for key in [k for k, sig in self._signals.items() if sig.is_set()]:
                    self._signals.pop(key, None)
            self._signal(umo).set()
            self._stats["marked"] += 1
            logger.debug("[tool_deadline] 用户消息到达，标记待交付 umo=%s", umo)
            # 关键：确保"待交付"信号在下一次 API 请求发出时被清除。
            # OnLLMRequestEvent 每次 Agent 运行只触发一次，覆盖不到后续 step，
            # 必须挂到 runner 的 step 上，否则信号会一直置位。
            self._install_step_hook(umo)
            return True
        except Exception:
            logger.debug("[tool_deadline] mark_arrival 失败", exc_info=True)
            return False

    def on_llm_request(self, event: Any, request: Any = None) -> None:
        """API 请求即将发出：消息已被交付，清除信号并补打工具补丁。"""
        try:
            self.clear(event)
            self.patch_from_request(request)
            self.reset_stale_follow_up_seq(event)
        except Exception:
            logger.debug("[tool_deadline] on_llm_request 失败", exc_info=True)

    def clear(self, event: Any) -> bool:
        """清除该会话的"有待交付消息"信号。"""
        try:
            umo = self._umo(event)
            if not umo:
                return False
            return self._clear_umo(umo)
        except Exception:
            logger.debug("[tool_deadline] clear 失败", exc_info=True)
            return False

    def _clear_umo(self, umo: str) -> bool:
        """按会话清除信号。"""
        signal = self._signals.get(umo)
        if signal is None or not signal.is_set():
            return False
        signal.clear()
        self._stats["cleared"] += 1
        logger.debug("[tool_deadline] 已交付，清除待交付信号 umo=%s", umo)
        return True

    def _install_step_hook(self, umo: str) -> None:
        """在活跃 AgentRunner 的 ``step`` 上挂"清除待交付信号"的钩子。

        为什么需要它：``OnLLMRequestEvent`` 在 AstrBot 里每次 Agent 运行只触发
        一次（在 runner 开始逐步执行之前），runner 内部的后续 step 不会再触发。
        若只依赖该钩子，用户在工具执行中插入消息后信号会**一直保持置位**，导致
        同一轮里之后每一次工具调用都走"启动即让位"（``yield_time_ms=0``），
        模型拿不到任何真实等待、只能反复轮询。

        ``step()`` 与 API 请求一一对应，是"消息即将随报文交付"的精确边界，因此
        每次 step 开始时清除信号。

        该钩子挂在 runner **实例**上，随 runner 生命周期自然回收，无需卸载。
        """
        try:
            from astrbot.core.pipeline.process_stage import follow_up as follow_up_mod

            runner = follow_up_mod._ACTIVE_AGENT_RUNNERS.get(umo)
        except Exception:
            return
        if runner is None or getattr(runner, "_tool_deadline_step_hooked", False):
            return
        original_step = getattr(runner, "step", None)
        if original_step is None:
            return
        try:
            runner._tool_deadline_step_hooked = True
        except Exception:
            return

        guard = self

        async def _step_with_clear(*args: Any, **kwargs: Any):
            # step 开始 = 一次 API 请求即将发出 = 待交付信号可以清除
            guard._clear_umo(umo)
            async for item in original_step(*args, **kwargs):
                yield item

        try:
            runner.step = _step_with_clear  # type: ignore[method-assign]
            self._stats["step_hooked"] += 1
            logger.debug("[tool_deadline] 已挂 step 清除钩子 umo=%s", umo)
        except Exception:
            logger.debug("[tool_deadline] 挂 step 钩子失败", exc_info=True)

    def patch_from_request(self, request: Any) -> int:
        """从 ``ProviderRequest.func_tool`` 发现并补打工具类。

        Args:
            request: 当前 LLM 请求对象（含 ``func_tool``）。

        Returns:
            本次新打补丁的类数量。
        """
        if not self.enabled:
            return 0
        toolset = getattr(request, "func_tool", None)
        tools = getattr(toolset, "tools", None)
        if not isinstance(tools, (list, tuple)):
            return 0

        added = 0
        for tool in tools:
            cls = type(tool)
            if cls in _PATCHED_ORIGINALS:
                continue
            if not self._should_intercept_class(cls):
                continue
            if not _schema_declares_wait_param(tool):
                continue
            original = getattr(cls, "call", None)
            if original is None or original is _patched_call:
                continue
            try:
                _PATCHED_ORIGINALS[cls] = original
                cls.call = _patched_call  # type: ignore[method-assign]
            except Exception:
                _PATCHED_ORIGINALS.pop(cls, None)
                logger.debug("[tool_deadline] 打补丁失败 %s", cls, exc_info=True)
                continue
            added += 1
            self._stats["patched"] += 1
            logger.debug("[tool_deadline] 已接管 %s", getattr(cls, "__name__", cls))
        return added

    # ------------------------------------------------------------------
    # 执行路径
    # ------------------------------------------------------------------
    async def dispatch(
        self,
        original: Callable[..., Any],
        tool_self: Any,
        context: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """拦截一次工具调用：无消息则完全透传，有消息则立刻让位。"""
        tool_name = str(getattr(tool_self, "name", "") or type(tool_self).__name__)
        event = self._event_from(context)

        if not self._eligible(event, tool_name):
            return await _call_original(original, tool_self, context, args, kwargs)

        requested = kwargs.get(WAIT_PARAM)
        if isinstance(requested, bool):
            return await _call_original(original, tool_self, context, args, kwargs)
        if isinstance(requested, int) and requested <= 0:
            # 模型本来就不等，无需干预
            return await _call_original(original, tool_self, context, args, kwargs)
        if requested is not None and not isinstance(requested, int):
            # 非整数形态交回工具自行处理
            return await _call_original(original, tool_self, context, args, kwargs)

        umo = self._guardable_umo(event)
        if not umo:
            return await _call_original(original, tool_self, context, args, kwargs)

        signal = self._signal(umo)
        if signal.is_set():
            # 消息在工具启动前就在等：直接以 0 等待启动，拿到真实 session_id
            self._stats["preempted"] += 1
            logger.debug("[tool_deadline] %s 启动即让位（已有消息在等）", tool_name)
            immediate = dict(kwargs)
            immediate[WAIT_PARAM] = 0
            return await _call_original(original, tool_self, context, args, immediate)

        task = asyncio.ensure_future(
            _call_original(original, tool_self, context, args, kwargs)
        )
        waiter = asyncio.ensure_future(signal.wait())
        try:
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            # 外层被取消（例如用户 /stop）：一并收敛在途任务，避免孤儿任务
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            raise
        finally:
            if not waiter.done():
                waiter.cancel()
                try:
                    await waiter
                except (asyncio.CancelledError, Exception):
                    pass

        if task.done():
            # 无插话：原样返回原生结果（逐字节一致）
            return task.result()

        return await self._yield_to_user(task, tool_name)

    async def _yield_to_user(self, task: "asyncio.Future[Any]", tool_name: str) -> str:
        """用户消息先到：尽可能让命令自然收尾，否则转后台返回占位。"""
        if self.grace_ms > 0:
            await asyncio.wait({task}, timeout=self.grace_ms / 1000)
            if task.done():
                return task.result()

        self._stats["interrupted"] += 1
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        return _build_backgrounded_result(tool_name)

    # ------------------------------------------------------------------
    # follow-up 序号残留清理
    # ------------------------------------------------------------------
    def reset_stale_follow_up_seq(self, event: Any) -> bool:
        """补偿 AstrBot 核心的 follow-up 序号残留。

        ``astrbot.core.pipeline.process_stage.follow_up._FOLLOW_UP_ORDER_STATE``
        以 umo 为键保存 ``next_order`` 递增计数器，而释放条件为::

            if not statuses and _ACTIVE_AGENT_RUNNERS.get(umo) is None: pop

        但"消息被合并进工具结果"这条路径上 runner 必然仍活跃（正是它在消费
        这张 ticket），该条件永不成立，``next_order`` 只会单调增长。

        本方法在该会话已无 pending/active 槽位时主动释放；``statuses`` 非空
        时绝不动作，因此不会破坏仍在排队的 follow-up 顺序。

        Args:
            event: 当前事件，用于定位 unified_msg_origin。

        Returns:
            是否成功释放了一份残留状态。
        """
        if not self.reset_follow_up_seq:
            return False
        try:
            from astrbot.core.pipeline.process_stage import follow_up as follow_up_mod
        except Exception:
            return False

        try:
            umo = self._guardable_umo(event)
            if not umo:
                return False
            state = follow_up_mod._FOLLOW_UP_ORDER_STATE.get(umo)
            if not isinstance(state, dict):
                return False
            statuses = state.get("statuses")
            if not isinstance(statuses, dict) or statuses:
                return False
            follow_up_mod._FOLLOW_UP_ORDER_STATE.pop(umo, None)
        except Exception:
            logger.debug("[tool_deadline] 释放 follow-up 序号失败", exc_info=True)
            return False

        self._stats["seq_reset"] += 1
        logger.debug("[tool_deadline] 已释放残留 follow-up 序号 umo=%s", umo)
        return True

    # ------------------------------------------------------------------
    # 查询/诊断
    # ------------------------------------------------------------------
    def has_pending_message(self, event: Any) -> bool:
        """该会话当前是否有尚未交付给 LLM 的用户消息。"""
        try:
            umo = self._guardable_umo(event)
            if not umo:
                return False
            signal = self._signals.get(umo)
            return bool(signal and signal.is_set())
        except Exception:
            return False

    def stats(self) -> dict[str, int]:
        """返回累计计数，供调试与测试使用。"""
        return dict(self._stats)

    def reset(self) -> None:
        """清空信号与计数。"""
        for signal in self._signals.values():
            signal.clear()
        self._signals.clear()
        self._stats = {
            "marked": 0,
            "cleared": 0,
            "preempted": 0,
            "interrupted": 0,
            "bypassed": 0,
            "seq_reset": 0,
            "patched": 0,
            "step_hooked": 0,
        }

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _should_intercept_class(self, cls: type) -> bool:
        name = str(getattr(cls, "__name__", "") or "")
        if not name:
            return False
        if name in self.skip_tools:
            return False
        if self.wait_tools:
            return name in self.wait_tools
        return name in MANAGED_TOOL_CLASS_NAMES

    def _eligible(self, event: Any, tool_name: str) -> bool:
        if not self.enabled or event is None:
            return False
        if tool_name in self.skip_tools:
            return False
        if self.wait_tools and tool_name not in self.wait_tools:
            return False
        if self._is_synthetic(event):
            return False
        if self.private_only and not self._is_private(event):
            return False
        return True

    def _signal(self, umo: str) -> asyncio.Event:
        signal = self._signals.get(umo)
        if signal is None:
            signal = asyncio.Event()
            self._signals[umo] = signal
        return signal

    def _guardable_umo(self, event: Any) -> str | None:
        if not self.enabled or event is None:
            return None
        if self._is_synthetic(event):
            return None
        if self.private_only and not self._is_private(event):
            return None
        return self._umo(event)

    @staticmethod
    def _event_from(context: Any) -> Any:
        """从 ``ContextWrapper`` 里取出事件。"""
        if context is None:
            return None
        inner = getattr(context, "context", None)
        return getattr(inner, "event", None)

    @staticmethod
    def _is_synthetic(event: Any) -> bool:
        try:
            platform = str(event.get_platform_name() or "").strip().lower()
        except Exception:
            return True
        return platform == _CRON_PLATFORM

    @staticmethod
    def _is_private(event: Any) -> bool:
        try:
            return bool(event.is_private_chat())
        except Exception:
            return False

    @staticmethod
    def _umo(event: Any) -> str | None:
        try:
            umo = event.unified_msg_origin
        except Exception:
            return None
        return str(umo) if umo else None


def _is_int_like(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        int(value)
    except Exception:
        return False
    return True


__all__ = [
    "ToolDeadlineGuard",
    "WAIT_PARAM",
    "MANAGED_TOOL_CLASS_NAMES",
]
