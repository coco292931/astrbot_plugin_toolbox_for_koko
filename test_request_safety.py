#!/usr/bin/env python3
"""文本注入安全 + 图片转述补丁 自测。

覆盖两个与既有功能相关的真实缺陷：

1. **Goal / Recall / 内容审核在"只发图片"的消息里静默丢失**。
   旧实现写成 ``type(extra_user_content_parts[0])(text=...)``，照抄首个 part
   的类型。图片消息里首个 part 是 ``ImageURLPart``：``text`` 被 pydantic 忽略、
   ``image_url`` 必填缺失 → ``ValidationError`` → 被 ``except Exception`` 吞掉。

2. **``_ensure_img_caption`` 猴补丁签名过期**。AstrBot 4.26+ 增加了
   ``montage_refs`` 参数并返回"已转述引用集合"；旧补丁既没有该参数、
   也不返回值，一旦被调用就是 ``TypeError`` 或返回 ``None``。

运行::

    python test_request_safety.py
    python test_request_safety.py -v
"""

from __future__ import annotations

import asyncio
import enum
import importlib
import inspect
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLUGIN_PKG = "koko_request_safety"


# ---------------------------------------------------------------------------
# AstrBot 桩模块
# ---------------------------------------------------------------------------


class _Recorder:
    def getattr_ok(self):  # pragma: no cover - 占位，避免 __getattr__ 递归
        return None

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        def factory(*args, **kwargs):
            def decorator(func):
                return func

            return decorator

        return factory


class _EventMessageType(enum.Flag):
    GROUP_MESSAGE = enum.auto()
    PRIVATE_MESSAGE = enum.auto()
    OTHER_MESSAGE = enum.auto()
    ALL = GROUP_MESSAGE | PRIVATE_MESSAGE | OTHER_MESSAGE


class _Filter(_Recorder):
    EventMessageType = _EventMessageType

    class PermissionType:
        ADMIN = "admin"
        MEMBER = "member"


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, msg, *a, **k):
        try:
            self.warnings.append(str(msg) % a if a else str(msg))
        except Exception:
            self.warnings.append(str(msg))

    def error(self, *a, **k):
        pass


LOGGER = _Logger()


class _Component:
    def __init__(self, *a, **k):
        pass


class _MessageChain:
    def __init__(self, chain=None, type=None):
        self.chain = list(chain or [])
        self.type = type

    def message(self, text):
        self.chain.append(text)
        return self


class _FakeContext:
    def get_config(self, umo=None):
        return {}


# --- 对齐 astrbot.core.agent.message 的替身 ---


class ContentPart:
    type = "text"

    def __init__(self, **kwargs):
        self._no_save = False
        for key, value in kwargs.items():
            setattr(self, key, value)

    def mark_as_temp(self):
        self._no_save = True
        return self


class TextPart(ContentPart):
    type = "text"

    def __init__(self, text: str):
        super().__init__(text=text)

    def __repr__(self):
        return f"TextPart({self.text!r})"


class ThinkPart(ContentPart):
    type = "think"

    def __init__(self, think: str, encrypted=None):
        super().__init__(think=think, encrypted=encrypted)


class ImageURLPart(ContentPart):
    """image_url 必填——照抄类型会因缺少字段而失败。"""

    type = "image_url"

    class ImageURL:
        def __init__(self, url: str, id: str | None = None):
            self.url = url
            self.id = id

    def __init__(self, image_url=None, **kwargs):
        if image_url is None:
            raise TypeError("ImageURLPart 需要 image_url 字段")
        if "text" in kwargs:
            raise TypeError("ImageURLPart 不接受 text 参数")
        super().__init__(image_url=image_url)

    def __repr__(self):
        return f"ImageURLPart({self.image_url.url!r})"


class AudioURLPart(ContentPart):
    type = "audio_url"

    class AudioURL:
        def __init__(self, url: str):
            self.url = url

    def __init__(self, audio_url=None, **kwargs):
        if audio_url is None:
            raise TypeError("AudioURLPart 需要 audio_url 字段")
        super().__init__(audio_url=audio_url)


FAKE_ASTR_MAIN_AGENT = types.SimpleNamespace()
CAPTION_CALLS: list[dict] = []


async def _request_img_caption(
    provider_id, cfg, image_urls, plugin_context, montage_refs=None
):
    CAPTION_CALLS.append(
        {"image_urls": list(image_urls), "montage_refs": montage_refs}
    )
    return "一只猫"


async def _compress_image_for_provider(url, cfg):
    return url


def _is_generated_compressed_image_path(src, out):
    return False


async def _unused(*a, **k):
    return None


def _install_stubs() -> None:
    if getattr(sys.modules.get("astrbot"), "_koko_stub", False):
        return

    def make(name, **attrs):
        mod = types.ModuleType(name)
        mod.__path__ = []
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
        return mod

    make("astrbot", _koko_stub=True)
    make("astrbot.api", logger=LOGGER)
    make(
        "astrbot.api.star",
        Context=_FakeContext,
        Star=type("Star", (), {"__init__": lambda self, ctx: None}),
        register=lambda *a, **k: (lambda cls: cls),
    )
    make("astrbot.api.all", llm_tool=lambda *a, **k: (lambda f: f))
    make(
        "astrbot.api.event",
        AstrMessageEvent=type("AstrMessageEvent", (), {}),
        filter=_Filter(),
    )
    make(
        "astrbot.api.message_components",
        At=_Component,
        Image=_Component,
        Plain=_Component,
    )
    make(
        "astrbot.api.platform",
        MessageType=type("MessageType", (), {"FRIEND_MESSAGE": "f"}),
    )
    make("astrbot.core")
    make("astrbot.core.message")
    make("astrbot.core.message.message_event_result", MessageChain=_MessageChain)
    make("astrbot.core.star")
    make(
        "astrbot.core.star.star_tools",
        StarTools=type(
            "StarTools",
            (),
            {"get_data_dir": staticmethod(lambda name=None: str(ROOT / "_data"))},
        ),
    )
    make("astrbot.core.utils")
    make(
        "astrbot.core.utils.astrbot_path",
        get_astrbot_data_path=lambda: str(ROOT / "_data"),
    )
    make("astrbot.core.utils.io", download_image_by_url=_unused)
    make("astrbot.core.utils.media_utils", compress_image=_unused)
    make("astrbot.core.agent")
    make(
        "astrbot.core.agent.message",
        TextPart=TextPart,
        ThinkPart=ThinkPart,
        ImageURLPart=ImageURLPart,
        AudioURLPart=AudioURLPart,
        ContentPart=ContentPart,
    )

    FAKE_ASTR_MAIN_AGENT._request_img_caption = _request_img_caption
    FAKE_ASTR_MAIN_AGENT._compress_image_for_provider = _compress_image_for_provider
    FAKE_ASTR_MAIN_AGENT._is_generated_compressed_image_path = (
        _is_generated_compressed_image_path
    )

    async def _ensure_img_caption(
        event, req, cfg, plugin_context, image_caption_provider, montage_refs=None
    ) -> set[str]:
        """模拟 AstrBot 4.26+ 的真实实现。"""
        return set()

    FAKE_ASTR_MAIN_AGENT._ensure_img_caption = _ensure_img_caption
    sys.modules["astrbot.core.astr_main_agent"] = FAKE_ASTR_MAIN_AGENT


_install_stubs()


def _load():
    pkg = types.ModuleType(PLUGIN_PKG)
    pkg.__path__ = [str(ROOT)]
    sys.modules[PLUGIN_PKG] = pkg
    utils = importlib.import_module(f"{PLUGIN_PKG}.core.request_utils")
    caption = importlib.import_module(f"{PLUGIN_PKG}.core.image_caption")
    return utils, caption


request_utils, image_caption = _load()
make_text_part = request_utils.make_text_part
ImageCaptionHandler = image_caption.ImageCaptionHandler


class FakeRequest:
    def __init__(self, parts=None, image_urls=None, audio_urls=None, prompt="hi"):
        self.extra_user_content_parts = list(parts or [])
        self.image_urls = list(image_urls or [])
        self.audio_urls = list(audio_urls or [])
        self.system_prompt = ""
        self.prompt = prompt


class FakeEvent:
    def __init__(self):
        self.tracked: list[str] = []

    def track_temporary_local_file(self, path):
        self.tracked.append(path)


def image_part(url: str) -> ImageURLPart:
    return ImageURLPart(ImageURLPart.ImageURL(url=url))


# ---------------------------------------------------------------------------
# 一、文本注入：各类消息形态
# ---------------------------------------------------------------------------


class TestMakeTextPart(unittest.TestCase):
    def test_returns_real_text_part_without_template(self):
        part = make_text_part("hello")
        self.assertIsInstance(part, TextPart)
        self.assertEqual(part.text, "hello")

    def test_never_copies_image_type(self):
        """核心回归：模板是 ImageURLPart 时也必须产出 TextPart。"""
        part = make_text_part("goal", template=image_part("https://x/a.jpg"))
        self.assertIsInstance(part, TextPart)
        self.assertFalse(hasattr(part, "image_url"))

    def test_mixed_message_text_first(self):
        parts = [TextPart("看图"), image_part("https://x/a.jpg")]
        self.assertIsInstance(make_text_part("x", template=parts[0]), TextPart)

    def test_mixed_message_image_first(self):
        parts = [image_part("https://x/a.jpg"), TextPart("看图")]
        self.assertIsInstance(make_text_part("x", template=parts[0]), TextPart)

    def test_think_template(self):
        self.assertIsInstance(
            make_text_part("x", template=ThinkPart("思考")), TextPart
        )

    def test_audio_template(self):
        part = AudioURLPart(AudioURLPart.AudioURL("/tmp/a.wav"))
        self.assertIsInstance(make_text_part("x", template=part), TextPart)

    def test_dict_template(self):
        for template in (
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "https://x/a.jpg"}},
        ):
            with self.subTest(template=template):
                self.assertIsInstance(
                    make_text_part("x", template=template), TextPart
                )

    def test_goal_injection_on_image_only_message(self):
        """回归：纯图片消息下注入 goal，必须产出合法文本 part。"""
        req = FakeRequest([image_part("https://x/a.jpg")])

        part = make_text_part("\n[Goal] 测试", template=req.extra_user_content_parts[0])
        req.extra_user_content_parts.append(part)

        self.assertIsInstance(req.extra_user_content_parts[-1], TextPart)
        self.assertEqual(len(req.extra_user_content_parts), 2)

    def test_old_pattern_really_breaks(self):
        """对照实验：旧写法在图片消息下确实会抛异常。"""
        first = image_part("https://x/a.jpg")
        with self.assertRaises(Exception):
            type(first)(text="\n[Goal] x")


# ---------------------------------------------------------------------------
# 二、_ensure_img_caption 补丁
# ---------------------------------------------------------------------------


class TestCaptionPatch(unittest.TestCase):
    def setUp(self):
        CAPTION_CALLS.clear()

        async def _ensure(event, req, cfg, ctx, pid, montage_refs=None) -> set[str]:
            return set()

        FAKE_ASTR_MAIN_AGENT._ensure_img_caption = _ensure
        FAKE_ASTR_MAIN_AGENT._request_img_caption = _request_img_caption
        FAKE_ASTR_MAIN_AGENT._compress_image_for_provider = (
            _compress_image_for_provider
        )
        self.plugin = types.SimpleNamespace(
            image_caption_hook_enabled=True,
            image_caption_concurrency=2,
            image_caption_timeout=60,
        )

    def _handler(self) -> ImageCaptionHandler:
        return ImageCaptionHandler(self.plugin)

    def test_patch_accepts_montage_refs(self):
        handler = self._handler()
        sig = inspect.signature(FAKE_ASTR_MAIN_AGENT._ensure_img_caption)
        self.assertIn("montage_refs", sig.parameters)
        handler.uninstall_astrbot_caption_error_patch()

    def test_patch_returns_set(self):
        handler = self._handler()
        req = FakeRequest(image_urls=["a.jpg"])

        result = asyncio.run(
            FAKE_ASTR_MAIN_AGENT._ensure_img_caption(
                FakeEvent(), req, {}, None, "prov"
            )
        )

        self.assertEqual(result, {"a.jpg"})
        self.assertEqual(req.image_urls, [])
        handler.uninstall_astrbot_caption_error_patch()

    def test_patch_forwards_montage_refs(self):
        handler = self._handler()
        req = FakeRequest(image_urls=["a.jpg"])

        asyncio.run(
            FAKE_ASTR_MAIN_AGENT._ensure_img_caption(
                FakeEvent(), req, {}, None, "prov", montage_refs={"m.jpg"}
            )
        )

        self.assertEqual(CAPTION_CALLS[-1]["montage_refs"], {"m.jpg"})
        handler.uninstall_astrbot_caption_error_patch()

    def test_patch_records_failure_payload(self):
        async def boom(*a, **k):
            raise RuntimeError("sensitive")

        original = FAKE_ASTR_MAIN_AGENT._request_img_caption
        FAKE_ASTR_MAIN_AGENT._request_img_caption = boom
        try:
            handler = self._handler()
            req = FakeRequest(image_urls=["a.jpg"])

            asyncio.run(
                FAKE_ASTR_MAIN_AGENT._ensure_img_caption(
                    FakeEvent(), req, {}, None, "prov"
                )
            )

            texts = [p.text for p in req.extra_user_content_parts]
            self.assertTrue(any("[Image Captioning Failed]" in t for t in texts))
            payload = next(t for t in texts if "<toolbox_image_caption_error>" in t)
            body = payload.split(">", 1)[1].split("</", 1)[0]
            self.assertEqual(json.loads(body)["error"], "sensitive")
            handler.uninstall_astrbot_caption_error_patch()
        finally:
            FAKE_ASTR_MAIN_AGENT._request_img_caption = original

    def test_skipped_when_hook_disabled(self):
        self.plugin.image_caption_hook_enabled = False
        before = FAKE_ASTR_MAIN_AGENT._ensure_img_caption

        ImageCaptionHandler(self.plugin)

        self.assertIs(FAKE_ASTR_MAIN_AGENT._ensure_img_caption, before)

    def test_uninstall_restores_original(self):
        original = FAKE_ASTR_MAIN_AGENT._ensure_img_caption
        handler = self._handler()
        self.assertIsNot(FAKE_ASTR_MAIN_AGENT._ensure_img_caption, original)

        handler.uninstall_astrbot_caption_error_patch()
        self.assertIs(FAKE_ASTR_MAIN_AGENT._ensure_img_caption, original)

    def test_missing_compressor_is_tolerated(self):
        saved = FAKE_ASTR_MAIN_AGENT.__dict__.pop("_compress_image_for_provider")
        try:
            handler = self._handler()
            req = FakeRequest(image_urls=["a.jpg"])
            result = asyncio.run(
                FAKE_ASTR_MAIN_AGENT._ensure_img_caption(
                    FakeEvent(), req, {}, None, "prov"
                )
            )
            self.assertEqual(result, {"a.jpg"})
            handler.uninstall_astrbot_caption_error_patch()
        finally:
            FAKE_ASTR_MAIN_AGENT._compress_image_for_provider = saved


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2 if "-v" in sys.argv else 1)
    return 0 if runner.run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
