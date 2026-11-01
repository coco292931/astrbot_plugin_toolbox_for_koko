#!/usr/bin/env python3
"""astrbot_plugin_toolbox_for_koko：用户消息交付时限（工具让位）自测脚本。

被测语义
--------
AstrBot 的 Agent 循环是串行的，用户在工具执行中发来的消息只会在"下一次
LLM API 请求"时随工具返回值交付。本插件通过拦截托管会话类工具的 ``call``，
把一次长等待拆成「启动 + 可中断的等待」：

* **没有消息插入** -> 原样透传 ``yield_time_ms``，返回原生结果（逐字节一致）。
* **消息在工具执行中到达** -> 立刻取消"等待"（不杀进程），返回转后台占位。
* **消息在工具启动前就在等** -> 以 ``yield_time_ms=0`` 启动，返回真实句柄。

运行方式
--------
    python test_tool_deadline.py
    python test_tool_deadline.py -v

脚本自带 AstrBot 桩模块，**不需要安装 AstrBot**，不读真实配置，
不发起任何网络请求或子进程。
"""

from __future__ import annotations

import asyncio
import enum
import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLUGIN_PKG = "koko_toolbox_under_test"

# ---------------------------------------------------------------------------
# AstrBot 桩模块：让插件可以在无 AstrBot 环境下导入
# ---------------------------------------------------------------------------


class _EventMessageType(enum.Flag):
    GROUP_MESSAGE = enum.auto()
    PRIVATE_MESSAGE = enum.auto()
    OTHER_MESSAGE = enum.auto()
    ALL = GROUP_MESSAGE | PRIVATE_MESSAGE | OTHER_MESSAGE


class _MessageType:
    FRIEND_MESSAGE = "friend_message"
    GROUP_MESSAGE = "group_message"
    OTHER_MESSAGE = "other_message"


class _FilterStub:
    """记录所有被装饰的 handler，便于断言注册结果。"""

    EventMessageType = _EventMessageType

    class PermissionType:
        ADMIN = "admin"
        MEMBER = "member"

    def __init__(self) -> None:
        self.registry: list[tuple[str, str]] = []

    def __getattr__(self, name: str):
        def factory(*args, **kwargs):
            def decorator(func):
                self.registry.append((name, getattr(func, "__name__", "?")))
                return func

            return decorator

        return factory

    def kinds(self) -> dict[str, str]:
        return {fname: kind for kind, fname in self.registry}


class _Component:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class _MessageChain:
    def __init__(self, chain=None, type=None):
        self.chain = list(chain or [])
        self.type = type

    def message(self, text):
        self.chain.append(text)
        return self


class _StarTools:
    _base = Path(tempfile.gettempdir()) / "koko_toolbox_test_data"

    @classmethod
    def get_data_dir(cls, name: str | None = None):
        target = cls._base if not name else cls._base / str(name)
        target.mkdir(parents=True, exist_ok=True)
        return str(target)


class _Logger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _FakeContext:
    """插件 __init__ 只需要一个可用的 context 占位。"""

    def __init__(self) -> None:
        self.config = {}
        self.conversation_manager = None

    def get_config(self, umo=None):
        return self.config


FILTER_STUB: _FilterStub


async def _async_noop(*args, **kwargs):
    return None


def _install_astrbot_stubs() -> None:
    """把最小 AstrBot 模块树塞进 sys.modules。"""
    global FILTER_STUB
    if getattr(sys.modules.get("astrbot"), "_koko_stub", False):
        return

    def make(name: str, **attrs):
        mod = types.ModuleType(name)
        mod.__path__ = []
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules[name] = mod
        return mod

    make("astrbot", _koko_stub=True)
    make("astrbot.api", logger=_Logger())

    make(
        "astrbot.api.star",
        Context=_FakeContext,
        Star=type(
            "Star",
            (),
            {"__init__": lambda self, ctx: setattr(self, "context", ctx)},
        ),
        register=lambda *a, **k: (lambda cls: cls),
    )
    make("astrbot.api.all", llm_tool=lambda *a, **k: (lambda f: f))

    FILTER_STUB = _FilterStub()
    make(
        "astrbot.api.event",
        AstrMessageEvent=type("AstrMessageEvent", (), {}),
        filter=FILTER_STUB,
    )
    make(
        "astrbot.api.message_components",
        At=_Component,
        Image=_Component,
        Plain=_Component,
    )
    make("astrbot.api.platform", MessageType=_MessageType)

    make("astrbot.core")
    make("astrbot.core.message")
    make("astrbot.core.message.message_event_result", MessageChain=_MessageChain)
    make("astrbot.core.star")
    make("astrbot.core.star.star_tools", StarTools=_StarTools)
    make("astrbot.core.utils")
    make(
        "astrbot.core.utils.astrbot_path",
        get_astrbot_data_path=lambda: str(_StarTools._base),
    )
    make("astrbot.core.utils.io", download_image_by_url=_async_noop)
    make("astrbot.core.utils.media_utils", compress_image=_async_noop)
    make("astrbot.core.agent")
    make("astrbot.core.agent.message", TextPart=_Component)


_install_astrbot_stubs()


def _load_modules():
    """按包方式加载插件，保证 main.py 里的相对导入可用。"""
    pkg = types.ModuleType(PLUGIN_PKG)
    pkg.__path__ = [str(ROOT)]
    sys.modules[PLUGIN_PKG] = pkg
    guard_mod = importlib.import_module(f"{PLUGIN_PKG}.core.tool_deadline")
    main_mod = importlib.import_module(f"{PLUGIN_PKG}.main")
    return guard_mod, main_mod


guard_module, main_module = _load_modules()
ToolDeadlineGuard = guard_module.ToolDeadlineGuard
ToolboxPlugin = main_module.ToolboxPlugin


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class FakeEvent:
    def __init__(
        self,
        umo: str = "aiocqhttp:FriendMessage:10001",
        *,
        private: bool = True,
        platform: str = "aiocqhttp",
    ) -> None:
        self.unified_msg_origin = umo
        self._private = private
        self._platform = platform

    def is_private_chat(self) -> bool:
        return self._private

    def get_platform_name(self) -> str:
        return self._platform


def make_context(event: FakeEvent) -> object:
    """构造一个形似 ContextWrapper[AstrAgentContext] 的对象。"""
    inner = types.SimpleNamespace(event=event)
    return types.SimpleNamespace(context=inner)


SHELL_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string"},
        "yield_time_ms": {
            "type": "integer",
            "default": 10_000,
            "minimum": 0,
            "maximum": 30_000,
        },
    },
    "required": ["command"],
}
SESSION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string"},
        "yield_time_ms": {
            "type": "integer",
            "default": 5_000,
            "minimum": 0,
            "maximum": 300_000,
        },
    },
}


def make_tool_class(call_name: str, schema: dict, tool_name: str):
    """动态构造一个形似 FunctionTool 的类。"""

    async def call(self, context, **kwargs):
        return "native-result"

    return type(
        call_name,
        (),
        {
            "name": tool_name,
            "parameters": schema,
            "call": call,
            "__init__": lambda self: None,
        },
    )


LocalShell = make_tool_class(
    "LocalExecuteShellTool", SHELL_SCHEMA, "astrbot_execute_shell"
)
ShellSession = make_tool_class(
    "ShellSessionTool", SESSION_SCHEMA, "astrbot_shell_session"
)
SandboxShell = make_tool_class(
    "ExecuteShellTool", SHELL_SCHEMA, "astrbot_execute_shell"
)
NoWaitTool = make_tool_class(
    "FileReadTool",
    {"type": "object", "properties": {"path": {"type": "string"}}},
    "astrbot_file_read_tool",
)


class FakeToolSet:
    def __init__(self, tools) -> None:
        self.tools = list(tools)


class FakeRequest:
    def __init__(self, tools) -> None:
        self.func_tool = FakeToolSet(tools)


_PRISTINE_CALLS: dict[type, object] = {
    cls: cls.__dict__["call"]
    for cls in (LocalShell, ShellSession, SandboxShell, NoWaitTool)
}


def reset_patch_state() -> None:
    """把工具类的 ``call`` 与守卫全局状态恢复干净，供各测试类隔离使用。"""
    for cls, original in _PRISTINE_CALLS.items():
        cls.call = original  # type: ignore[method-assign]
    guard_module._PATCHED_ORIGINALS.clear()
    guard_module._ACTIVE_GUARD = None


def make_guard(**overrides) -> ToolDeadlineGuard:
    kwargs = {
        "enabled": True,
        "private_only": True,
        "grace_ms": 0,
    }
    kwargs.update(overrides)
    guard = ToolDeadlineGuard(**kwargs)
    guard.install()
    return guard


class SlowTool:
    """记录实际收到的 yield_time_ms，并按托管会话语义返回。

    模仿 AstrBot ``exec_managed``：最多等 ``yield_time_ms``，命令先结束就返回
    完整结果；否则返回 ``running`` + ``session_id``。
    """

    name = "astrbot_execute_shell"
    parameters = SHELL_SCHEMA

    def __init__(self, delay_ms: int = 0, result: object = "native-result") -> None:
        self.delay_ms = delay_ms
        self.result = result
        self.seen_kwargs: dict | None = None
        self.cancelled = False
        self.finished = False

    async def original(self, tool_self, context, *args, **kwargs):
        self.seen_kwargs = dict(kwargs)
        wait_ms = kwargs.get("yield_time_ms")
        if not isinstance(wait_ms, int):
            wait_ms = self.delay_ms
        effective = min(self.delay_ms, max(wait_ms, 0))
        try:
            if effective > 0:
                await asyncio.sleep(effective / 1000)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.delay_ms <= wait_ms:
            self.finished = True
            return self.result
        return json.dumps(
            {"session_id": "sh_fake", "status": "running", "cursor": 0}
        )


# ---------------------------------------------------------------------------
# 一、核心语义：无消息 = 零改动
# ---------------------------------------------------------------------------


class TestNoMessageIsTransparent(unittest.IsolatedAsyncioTestCase):
    async def test_forwards_yield_time_ms_untouched(self):
        guard = make_guard()
        tool = SlowTool(delay_ms=10)
        await guard.dispatch(
            tool.original,
            SlowTool(),
            make_context(FakeEvent()),
            (),
            {"command": "sleep 30", "yield_time_ms": 30_000},
        )

        self.assertEqual(tool.seen_kwargs["yield_time_ms"], 30_000)
        self.assertTrue(tool.finished)

    async def test_returns_native_result_verbatim(self):
        guard = make_guard()
        tool = SlowTool(delay_ms=5, result='{"status": "completed", "stdout": "ok"}')
        result = await guard.dispatch(
            tool.original,
            SlowTool(),
            make_context(FakeEvent()),
            (),
            {"command": "ls", "yield_time_ms": 30_000},
        )

        self.assertEqual(result, '{"status": "completed", "stdout": "ok"}')
        self.assertFalse(tool.cancelled)

    async def test_missing_param_stays_missing(self):
        """模型没传 yield_time_ms 时，不能凭空插入。"""
        guard = make_guard()
        tool = SlowTool(delay_ms=5)
        await guard.dispatch(
            tool.original, SlowTool(), make_context(FakeEvent()), (), {"command": "ls"}
        )

        self.assertNotIn("yield_time_ms", tool.seen_kwargs)

    async def test_no_kwargs_are_mutated_in_place(self):
        guard = make_guard()
        tool = SlowTool(delay_ms=5)
        kwargs = {"command": "ls", "yield_time_ms": 30_000}
        snapshot = dict(kwargs)

        await guard.dispatch(
            tool.original, SlowTool(), make_context(FakeEvent()), (), kwargs
        )

        self.assertEqual(kwargs, snapshot, "不得就地修改调用方传入的参数")

    async def test_exception_propagates_like_native(self):
        guard = make_guard()

        async def boom(tool_self, context, *args, **kwargs):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            await guard.dispatch(
                boom,
                SlowTool(),
                make_context(FakeEvent()),
                (),
                {"yield_time_ms": 30_000},
            )

    async def test_explicit_zero_yield_bypasses(self):
        guard = make_guard()
        tool = SlowTool(delay_ms=5)
        await guard.dispatch(
            tool.original,
            SlowTool(),
            make_context(FakeEvent()),
            (),
            {"command": "ls", "yield_time_ms": 0},
        )

        self.assertEqual(tool.seen_kwargs["yield_time_ms"], 0)


# ---------------------------------------------------------------------------
# 二、核心语义：消息插入 = 立刻让位
# ---------------------------------------------------------------------------


class TestMessageArrivesMidTool(unittest.IsolatedAsyncioTestCase):
    async def test_returns_immediately_when_message_arrives(self):
        guard = make_guard()
        event = FakeEvent()
        tool = SlowTool(delay_ms=5_000)  # 原生会等 5 秒

        async def interrupt() -> None:
            await asyncio.sleep(0.02)
            guard.mark_arrival(event)

        payload = await asyncio.gather(
            guard.dispatch(
                tool.original,
                SlowTool(),
                make_context(event),
                (),
                {"command": "sleep 35", "yield_time_ms": 30_000},
            ),
            interrupt(),
        )

        raw = payload[0]
        data = json.loads(raw)
        self.assertEqual(data["status"], "backgrounded")
        self.assertIsNone(data["session_id"])
        self.assertEqual(data["cursor"], 0)
        self.assertIn("astrbot_shell_session", data["note"])
        self.assertTrue(tool.cancelled)
        self.assertFalse(tool.finished)

    async def test_latency_is_far_below_yield_time(self):
        guard = make_guard()
        event = FakeEvent()
        tool = SlowTool(delay_ms=30_000)

        start = asyncio.get_running_loop().time()

        async def interrupt() -> None:
            await asyncio.sleep(0.02)
            guard.mark_arrival(event)

        await asyncio.gather(
            guard.dispatch(
                tool.original,
                SlowTool(),
                make_context(event),
                (),
                {"command": "sleep 35", "yield_time_ms": 30_000},
            ),
            interrupt(),
        )

        elapsed = asyncio.get_running_loop().time() - start
        self.assertLess(elapsed, 0.5, "消息到达后必须立刻让位，不能等满 yield_time_ms")

    async def test_yield_time_is_not_preemptively_shortened(self):
        """关键回归：不能提前把 yield_time_ms 改小。"""
        guard = make_guard()
        event = FakeEvent()
        tool = SlowTool(delay_ms=5_000)

        async def interrupt() -> None:
            await asyncio.sleep(0.02)
            guard.mark_arrival(event)

        await asyncio.gather(
            guard.dispatch(
                tool.original,
                SlowTool(),
                make_context(event),
                (),
                {"command": "sleep 35", "yield_time_ms": 30_000},
            ),
            interrupt(),
        )

        self.assertEqual(
            tool.seen_kwargs["yield_time_ms"],
            30_000,
            "模型给的 yield_time_ms 必须原样透传",
        )

    async def test_stats_record_interruption(self):
        guard = make_guard()
        event = FakeEvent()
        tool = SlowTool(delay_ms=5_000)

        async def interrupt() -> None:
            await asyncio.sleep(0.02)
            guard.mark_arrival(event)

        await asyncio.gather(
            guard.dispatch(
                tool.original,
                SlowTool(),
                make_context(event),
                (),
                {"yield_time_ms": 30_000},
            ),
            interrupt(),
        )

        self.assertEqual(guard.stats()["interrupted"], 1)


# ---------------------------------------------------------------------------
# 三、核心语义：消息先到 = 启动即让位
# ---------------------------------------------------------------------------


class TestMessageAlreadyWaiting(unittest.IsolatedAsyncioTestCase):
    async def test_starts_with_zero_wait(self):
        guard = make_guard()
        event = FakeEvent()
        guard.mark_arrival(event)

        tool = SlowTool(delay_ms=50)
        result = await guard.dispatch(
            tool.original,
            SlowTool(),
            make_context(event),
            (),
            {"command": "sleep 35", "yield_time_ms": 30_000},
        )

        self.assertEqual(tool.seen_kwargs["yield_time_ms"], 0)
        payload = json.loads(result)
        self.assertEqual(payload["session_id"], "sh_fake")
        self.assertEqual(payload["status"], "running")
        self.assertFalse(tool.cancelled)
        self.assertFalse(tool.finished)
        self.assertEqual(guard.stats()["preempted"], 1)

    async def test_returns_real_session_handle(self):
        """以 yield=0 启动时，原生会直接返回带 session_id 的 JSON。"""
        guard = make_guard()
        event = FakeEvent()
        guard.mark_arrival(event)

        async def managed(tool_self, context, *args, **kwargs):
            assert kwargs["yield_time_ms"] == 0
            return json.dumps(
                {"session_id": "sh_abc", "status": "running", "cursor": 0}
            )

        result = await guard.dispatch(
            managed, SlowTool(), make_context(event), (), {"yield_time_ms": 30_000}
        )

        self.assertEqual(json.loads(result)["session_id"], "sh_abc")

    async def test_signal_cleared_on_next_llm_request(self):
        guard = make_guard()
        event = FakeEvent()
        guard.mark_arrival(event)
        self.assertTrue(guard.has_pending_message(event))

        guard.on_llm_request(event, FakeRequest([LocalShell()]))
        self.assertFalse(guard.has_pending_message(event))

        tool = SlowTool(delay_ms=5)
        await guard.dispatch(
            tool.original,
            SlowTool(),
            make_context(event),
            (),
            {"yield_time_ms": 30_000},
        )
        self.assertEqual(tool.seen_kwargs["yield_time_ms"], 30_000)


# ---------------------------------------------------------------------------
# 四、宽限
# ---------------------------------------------------------------------------


class TestGrace(unittest.IsolatedAsyncioTestCase):
    async def test_zero_grace_backgrounds_immediately(self):
        guard = make_guard(grace_ms=0)
        event = FakeEvent()
        tool = SlowTool(delay_ms=200)

        async def interrupt() -> None:
            await asyncio.sleep(0.01)
            guard.mark_arrival(event)

        results = await asyncio.gather(
            guard.dispatch(
                tool.original,
                SlowTool(),
                make_context(event),
                (),
                {"yield_time_ms": 30_000},
            ),
            interrupt(),
        )
        self.assertEqual(json.loads(results[0])["status"], "backgrounded")

    async def test_positive_grace_lets_short_command_finish(self):
        guard = make_guard(grace_ms=300)
        event = FakeEvent()
        tool = SlowTool(delay_ms=50, result="finished-naturally")

        async def interrupt() -> None:
            await asyncio.sleep(0.01)
            guard.mark_arrival(event)

        results = await asyncio.gather(
            guard.dispatch(
                tool.original,
                SlowTool(),
                make_context(event),
                (),
                {"yield_time_ms": 30_000},
            ),
            interrupt(),
        )
        self.assertEqual(results[0], "finished-naturally")
        self.assertFalse(tool.cancelled)


# ---------------------------------------------------------------------------
# 五、不介入的场景
# ---------------------------------------------------------------------------


class TestNotIntervened(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_guard(self):
        guard = make_guard(enabled=False)
        event = FakeEvent()
        self.assertFalse(guard.mark_arrival(event))

        tool = SlowTool(delay_ms=5)
        await guard.dispatch(
            tool.original,
            SlowTool(),
            make_context(event),
            (),
            {"yield_time_ms": 30_000},
        )
        self.assertEqual(tool.seen_kwargs["yield_time_ms"], 30_000)

    async def test_group_chat_when_private_only(self):
        guard = make_guard(private_only=True)
        event = FakeEvent(private=False)
        self.assertFalse(guard.mark_arrival(event))

    async def test_group_chat_when_not_private_only(self):
        guard = make_guard(private_only=False)
        event = FakeEvent(private=False)
        self.assertTrue(guard.mark_arrival(event))
        self.assertTrue(guard.has_pending_message(event))

    async def test_cron_event_never_marked(self):
        guard = make_guard(private_only=False)
        cron = FakeEvent(platform="cron")
        self.assertFalse(guard.mark_arrival(cron))

    async def test_skip_tools(self):
        guard = make_guard(skip_tools=["astrbot_execute_shell"])
        event = FakeEvent()
        guard.mark_arrival(event)

        tool = SlowTool(delay_ms=5)
        await guard.dispatch(
            tool.original,
            SlowTool(),
            make_context(event),
            (),
            {"yield_time_ms": 30_000},
        )
        self.assertEqual(tool.seen_kwargs["yield_time_ms"], 30_000)

    async def test_context_without_event(self):
        guard = make_guard()
        tool = SlowTool(delay_ms=5)
        await guard.dispatch(
            tool.original, SlowTool(), None, (), {"yield_time_ms": 30_000}
        )
        self.assertEqual(tool.seen_kwargs["yield_time_ms"], 30_000)

    async def test_non_int_yield_is_left_alone(self):
        guard = make_guard()
        event = FakeEvent()
        guard.mark_arrival(event)

        tool = SlowTool(delay_ms=5)
        await guard.dispatch(
            tool.original,
            SlowTool(),
            make_context(event),
            (),
            {"yield_time_ms": "30000"},
        )
        self.assertEqual(tool.seen_kwargs["yield_time_ms"], "30000")

    async def test_bool_yield_is_left_alone(self):
        guard = make_guard()
        event = FakeEvent()
        guard.mark_arrival(event)

        tool = SlowTool(delay_ms=5)
        await guard.dispatch(
            tool.original,
            SlowTool(),
            make_context(event),
            (),
            {"yield_time_ms": True},
        )
        self.assertIs(tool.seen_kwargs["yield_time_ms"], True)


# ---------------------------------------------------------------------------
# 六、补丁安装/还原
# ---------------------------------------------------------------------------


class TestPatching(unittest.TestCase):
    def setUp(self) -> None:
        reset_patch_state()

    def tearDown(self) -> None:
        reset_patch_state()

    def test_patches_managed_session_tools(self):
        guard = make_guard()
        guard.patch_from_request(FakeRequest([LocalShell(), ShellSession()]))

        self.assertIs(LocalShell.call, guard_module._patched_call)
        self.assertIs(ShellSession.call, guard_module._patched_call)

    def test_does_not_patch_sandbox_shell(self):
        guard = make_guard()
        guard.patch_from_request(FakeRequest([SandboxShell()]))

        self.assertIs(SandboxShell.call, _PRISTINE_CALLS[SandboxShell])

    def test_does_not_patch_tools_without_wait_param(self):
        guard = make_guard()
        guard.patch_from_request(FakeRequest([NoWaitTool()]))

        self.assertIs(NoWaitTool.call, _PRISTINE_CALLS[NoWaitTool])

    def test_idempotent(self):
        guard = make_guard()
        guard.patch_from_request(FakeRequest([LocalShell()]))
        patched = LocalShell.call
        guard.patch_from_request(FakeRequest([LocalShell()]))

        self.assertIs(LocalShell.call, patched)
        self.assertEqual(guard.stats()["patched"], 1)

    def test_uninstall_restores_original(self):
        guard = make_guard()
        guard.patch_from_request(FakeRequest([LocalShell()]))
        self.assertIsNot(LocalShell.call, _PRISTINE_CALLS[LocalShell])

        guard.uninstall()
        self.assertIs(LocalShell.call, _PRISTINE_CALLS[LocalShell])

    def test_skip_tools_respected_at_patch_time(self):
        guard = make_guard(skip_tools=["LocalExecuteShellTool"])
        guard.patch_from_request(FakeRequest([LocalShell()]))

        self.assertIs(LocalShell.call, _PRISTINE_CALLS[LocalShell])

    def test_wait_tools_overrides_allowlist(self):
        guard = make_guard(wait_tools=["ExecuteShellTool"])
        guard.patch_from_request(FakeRequest([LocalShell(), SandboxShell()]))

        self.assertIs(LocalShell.call, _PRISTINE_CALLS[LocalShell])
        self.assertIs(SandboxShell.call, guard_module._patched_call)

    def test_disabled_guard_patches_nothing(self):
        guard = make_guard(enabled=False)
        guard.patch_from_request(FakeRequest([LocalShell()]))

        self.assertIs(LocalShell.call, _PRISTINE_CALLS[LocalShell])


# ---------------------------------------------------------------------------
# 七、follow-up 序号残留清理（补偿 AstrBot 核心状态）
# ---------------------------------------------------------------------------


def install_fake_follow_up(state: dict, runners: dict | None = None):
    for name in ("astrbot.core.pipeline", "astrbot.core.pipeline.process_stage"):
        if name not in sys.modules:
            pkg = types.ModuleType(name)
            pkg.__path__ = []
            sys.modules[name] = pkg
    module = types.ModuleType("astrbot.core.pipeline.process_stage.follow_up")
    module._FOLLOW_UP_ORDER_STATE = state
    module._ACTIVE_AGENT_RUNNERS = {} if runners is None else runners
    sys.modules[module.__name__] = module
    setattr(sys.modules["astrbot.core.pipeline.process_stage"], "follow_up", module)
    return module


class FakeRunner:
    """最小可用的 AgentRunner 替身：step() 是异步生成器。"""

    def __init__(self) -> None:
        self.step_calls = 0

    async def step(self, *args, **kwargs):
        self.step_calls += 1
        yield "step-result"


class TestFollowUpSeqReset(unittest.TestCase):
    UMO = "aiocqhttp:FriendMessage:10001"

    def setUp(self) -> None:
        self.state: dict = {}
        install_fake_follow_up(self.state)
        reset_patch_state()

    def tearDown(self) -> None:
        for name in (
            "astrbot.core.pipeline.process_stage.follow_up",
            "astrbot.core.pipeline.process_stage",
            "astrbot.core.pipeline",
        ):
            sys.modules.pop(name, None)
        guard_module._ACTIVE_GUARD = None

    def _event(self) -> FakeEvent:
        return FakeEvent(self.UMO)

    def test_releases_state_when_no_pending_slots(self):
        self.state[self.UMO] = {"statuses": {}, "next_order": 7, "next_turn": 7}
        guard = make_guard()

        self.assertTrue(guard.reset_stale_follow_up_seq(self._event()))
        self.assertNotIn(self.UMO, self.state)
        self.assertEqual(guard.stats()["seq_reset"], 1)

    def test_keeps_state_while_slots_pending(self):
        self.state[self.UMO] = {"statuses": {7: "pending"}, "next_order": 8}
        guard = make_guard()

        self.assertFalse(guard.reset_stale_follow_up_seq(self._event()))
        self.assertIn(self.UMO, self.state)

    def test_keeps_state_while_slot_active(self):
        self.state[self.UMO] = {"statuses": {3: "active"}, "next_order": 4}
        guard = make_guard()

        self.assertFalse(guard.reset_stale_follow_up_seq(self._event()))
        self.assertIn(self.UMO, self.state)

    def test_noop_when_state_absent(self):
        guard = make_guard()
        self.assertFalse(guard.reset_stale_follow_up_seq(self._event()))

    def test_noop_when_disabled(self):
        self.state[self.UMO] = {"statuses": {}, "next_order": 9}
        guard = make_guard(reset_follow_up_seq=False)

        self.assertFalse(guard.reset_stale_follow_up_seq(self._event()))
        self.assertIn(self.UMO, self.state)

    def test_noop_for_cron_event(self):
        self.state[self.UMO] = {"statuses": {}, "next_order": 9}
        guard = make_guard(private_only=False)
        cron = FakeEvent(self.UMO, platform="cron")

        self.assertFalse(guard.reset_stale_follow_up_seq(cron))
        self.assertIn(self.UMO, self.state)

    def test_malformed_state_is_ignored(self):
        self.state[self.UMO] = {"statuses": "not-a-dict", "next_order": 5}
        guard = make_guard()

        self.assertFalse(guard.reset_stale_follow_up_seq(self._event()))
        self.assertIn(self.UMO, self.state)

    def test_missing_core_module_is_tolerated(self):
        for name in (
            "astrbot.core.pipeline.process_stage.follow_up",
            "astrbot.core.pipeline.process_stage",
            "astrbot.core.pipeline",
        ):
            sys.modules.pop(name, None)
        guard = make_guard()

        self.assertFalse(guard.reset_stale_follow_up_seq(self._event()))


# ---------------------------------------------------------------------------
# 八、待交付信号的清除时机（关键回归：不能一直置位）
# ---------------------------------------------------------------------------


class TestSignalClearedAtStepBoundary(unittest.IsolatedAsyncioTestCase):
    """``OnLLMRequestEvent`` 每次运行只触发一次，必须靠 step 边界清除。"""

    UMO = "aiocqhttp:FriendMessage:10001"

    def setUp(self) -> None:
        self.state: dict = {}
        self.runners: dict = {}
        install_fake_follow_up(self.state, self.runners)
        reset_patch_state()

    def tearDown(self) -> None:
        for name in (
            "astrbot.core.pipeline.process_stage.follow_up",
            "astrbot.core.pipeline.process_stage",
            "astrbot.core.pipeline",
        ):
            sys.modules.pop(name, None)
        reset_patch_state()

    def _event(self) -> FakeEvent:
        return FakeEvent(self.UMO)

    async def test_mark_arrival_installs_step_hook(self):
        runner = FakeRunner()
        self.runners[self.UMO] = runner
        guard = make_guard()

        self.assertTrue(guard.mark_arrival(self._event()))
        self.assertTrue(getattr(runner, "_tool_deadline_step_hooked", False))
        self.assertEqual(guard.stats()["step_hooked"], 1)

    async def test_step_start_clears_signal(self):
        runner = FakeRunner()
        self.runners[self.UMO] = runner
        guard = make_guard()
        event = self._event()

        guard.mark_arrival(event)
        self.assertTrue(guard.has_pending_message(event))

        collected = [item async for item in runner.step()]

        self.assertEqual(collected, ["step-result"])
        self.assertEqual(runner.step_calls, 1)
        self.assertFalse(
            guard.has_pending_message(event),
            "step 开始等于 API 请求发出，信号必须在此清除",
        )

    async def test_hook_installed_only_once(self):
        runner = FakeRunner()
        self.runners[self.UMO] = runner
        guard = make_guard()
        event = self._event()

        guard.mark_arrival(event)
        guard.mark_arrival(event)
        guard.mark_arrival(event)

        self.assertEqual(guard.stats()["step_hooked"], 1)

    async def test_no_runner_is_graceful(self):
        guard = make_guard()
        event = self._event()

        self.assertTrue(guard.mark_arrival(event))
        self.assertTrue(guard.has_pending_message(event))
        self.assertEqual(guard.stats()["step_hooked"], 0)

    async def test_missing_core_module_is_graceful(self):
        for name in (
            "astrbot.core.pipeline.process_stage.follow_up",
            "astrbot.core.pipeline.process_stage",
            "astrbot.core.pipeline",
        ):
            sys.modules.pop(name, None)
        guard = make_guard()

        self.assertTrue(guard.mark_arrival(self._event()))

    async def test_second_tool_after_step_boundary_is_native(self):
        """回归：第一次让位后，下一个 step 的工具不能被再次强制 yield=0。"""
        runner = FakeRunner()
        self.runners[self.UMO] = runner
        guard = make_guard()
        event = self._event()

        # step #1 开始（信号清除，此处无消息）
        _ = [item async for item in runner.step()]

        # 工具 A 正在等待，用户消息到达
        tool_a = SlowTool(delay_ms=5_000)

        async def interrupt() -> None:
            await asyncio.sleep(0.02)
            guard.mark_arrival(event)

        results = await asyncio.gather(
            guard.dispatch(
                tool_a.original,
                SlowTool(),
                make_context(event),
                (),
                {"command": "sleep 35", "yield_time_ms": 30_000},
            ),
            interrupt(),
        )
        self.assertEqual(json.loads(results[0])["status"], "backgrounded")

        # step #2 开始：报文即将发出，信号清除
        _ = [item async for item in runner.step()]
        self.assertFalse(guard.has_pending_message(event))

        # 工具 B：必须恢复原生等待，而不是又被 yield=0
        tool_b = SlowTool(delay_ms=20, result="native-b")
        result_b = await guard.dispatch(
            tool_b.original,
            SlowTool(),
            make_context(event),
            (),
            {"command": "ls", "yield_time_ms": 30_000},
        )

        self.assertEqual(result_b, "native-b")
        self.assertEqual(tool_b.seen_kwargs["yield_time_ms"], 30_000)
        self.assertFalse(tool_b.cancelled)

    async def test_tools_in_same_batch_all_preempt(self):
        """同一批次内多个工具：信号未到 step 边界前应持续生效。"""
        runner = FakeRunner()
        self.runners[self.UMO] = runner
        guard = make_guard()
        event = self._event()

        guard.mark_arrival(event)

        first = SlowTool(delay_ms=5_000)
        second = SlowTool(delay_ms=5_000)
        await guard.dispatch(
            first.original,
            SlowTool(),
            make_context(event),
            (),
            {"command": "a", "yield_time_ms": 30_000},
        )
        await guard.dispatch(
            second.original,
            SlowTool(),
            make_context(event),
            (),
            {"command": "b", "yield_time_ms": 30_000},
        )

        self.assertEqual(first.seen_kwargs["yield_time_ms"], 0)
        self.assertEqual(second.seen_kwargs["yield_time_ms"], 0)
        self.assertEqual(guard.stats()["preempted"], 2)


# ---------------------------------------------------------------------------
# 九、插件接线（配置解析 + handler 注册 + 就地改写）
# ---------------------------------------------------------------------------


class TestPluginWiring(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_patch_state()

    def tearDown(self) -> None:
        reset_patch_state()

    def _plugin(self, config: dict | None = None) -> ToolboxPlugin:
        return ToolboxPlugin(_FakeContext(), config or {})

    def test_defaults(self):
        plugin = self._plugin()
        self.assertTrue(plugin.tool_deadline.enabled)
        self.assertTrue(plugin.tool_deadline.private_only)
        self.assertEqual(plugin.tool_deadline.grace_ms, 0)
        self.assertEqual(plugin.tool_deadline.wait_tools, frozenset())
        self.assertEqual(plugin.tool_deadline.skip_tools, frozenset())
        self.assertTrue(plugin.tool_deadline.reset_follow_up_seq)

    def test_install_sets_active_guard(self):
        plugin = self._plugin()
        self.assertIs(guard_module._ACTIVE_GUARD, plugin.tool_deadline)

    def test_grouped_config_is_flattened(self):
        plugin = self._plugin(
            {
                "tool_deadline": {
                    "tool_deadline_enabled": False,
                    "tool_deadline_private_only": False,
                    "tool_deadline_grace_ms": 250,
                    "tool_deadline_wait_tools": ["LocalExecuteShellTool"],
                    "tool_deadline_skip_tools": ["ShellSessionTool"],
                    "tool_deadline_reset_follow_up_seq": False,
                }
            }
        )
        self.assertFalse(plugin.tool_deadline.enabled)
        self.assertFalse(plugin.tool_deadline.private_only)
        self.assertEqual(plugin.tool_deadline.grace_ms, 250)
        self.assertEqual(
            plugin.tool_deadline.wait_tools, frozenset({"LocalExecuteShellTool"})
        )
        self.assertEqual(
            plugin.tool_deadline.skip_tools, frozenset({"ShellSessionTool"})
        )
        self.assertFalse(plugin.tool_deadline.reset_follow_up_seq)

    def test_flat_config_is_also_accepted(self):
        plugin = self._plugin({"tool_deadline_grace_ms": 500})
        self.assertEqual(plugin.tool_deadline.grace_ms, 500)

    def test_invalid_values_fall_back(self):
        plugin = self._plugin(
            {"tool_deadline": {"tool_deadline_grace_ms": "oops"}}
        )
        self.assertEqual(plugin.tool_deadline.grace_ms, 0)

    def test_handlers_are_registered(self):
        kinds = FILTER_STUB.kinds()
        self.assertEqual(
            kinds.get("tool_deadline_mark_arrival"), "event_message_type"
        )
        self.assertEqual(
            kinds.get("tool_deadline_on_llm_request"), "on_llm_request"
        )
        self.assertEqual(
            kinds.get("tool_deadline_after_message_sent"), "after_message_sent"
        )

    async def test_mark_arrival_does_not_touch_event(self):
        plugin = self._plugin()
        calls: list[str] = []
        event = FakeEvent()
        event.set_result = lambda *a, **k: calls.append("set_result")
        event.stop_event = lambda *a, **k: calls.append("stop_event")

        await plugin.tool_deadline_mark_arrival(event)

        self.assertEqual(calls, [])
        self.assertTrue(plugin.tool_deadline.has_pending_message(event))

    async def test_llm_request_clears_and_patches(self):
        plugin = self._plugin()
        event = FakeEvent()
        await plugin.tool_deadline_mark_arrival(event)
        self.assertTrue(plugin.tool_deadline.has_pending_message(event))

        request = FakeRequest([LocalShell(), SandboxShell()])
        await plugin.tool_deadline_on_llm_request(event, request)

        self.assertFalse(plugin.tool_deadline.has_pending_message(event))
        self.assertIs(LocalShell.call, guard_module._patched_call)
        self.assertIsNot(SandboxShell.call, guard_module._patched_call)

    async def test_after_message_sent_clears(self):
        plugin = self._plugin()
        event = FakeEvent()
        await plugin.tool_deadline_mark_arrival(event)

        await plugin.tool_deadline_after_message_sent(event)
        self.assertFalse(plugin.tool_deadline.has_pending_message(event))

    async def test_terminate_restores_patch(self):
        plugin = self._plugin()
        plugin.tool_deadline.patch_from_request(FakeRequest([LocalShell()]))
        self.assertIs(LocalShell.call, guard_module._patched_call)

        await plugin.terminate()
        self.assertIs(LocalShell.call, _PRISTINE_CALLS[LocalShell])

    async def test_handlers_never_raise_on_odd_input(self):
        plugin = self._plugin()
        await plugin.tool_deadline_mark_arrival(None)
        await plugin.tool_deadline_on_llm_request(None, None)
        await plugin.tool_deadline_after_message_sent(None)


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2 if "-v" in sys.argv else 1)
    return 0 if runner.run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
