#!/usr/bin/env python3
"""history 自测：message_seq 锚点翻页 + reverse_order。

覆盖三件事：

1. **首轮取最新一批**：不带 ``message_seq``，按 ``count`` 拿最新 N 条。
2. **锚点向前翻页**：后续请求带 ``message_seq=<上一批最旧消息>`` 与
   ``reverse_order=true``，不再用"count 递增重拉最新"的方式。
3. **适配器不支持新参数时回退**：锚点没生效（返回里找不到锚点 / 消息推送
   不前进）就退回 count 递增方式，结果仍然正确。

全程离线（用假的 call_action）。运行::

    python test_history.py
    python test_history.py -v
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


class _Logger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def _install_astrbot_stubs() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    api_mod.logger = _Logger()
    astrbot_mod.api = api_mod
    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod


_install_astrbot_stubs()

from tools import history as history_tool  # noqa: E402


class _FakeHistoryApi:
    """模拟 NapCat 的 get_friend_msg_history。

    total 条消息，seq 从 1（最旧）到 total（最新），real_seq 与 seq 相同。

    ignore_anchor=True    : 完全忽略 message_seq / reverse_order，总是返回最新一批
    forward_anchor=True   : 把 message_seq 当成"向后取"，返回比锚点更新的消息
    """

    def __init__(self, total: int = 100, *, ignore_anchor=False, forward_anchor=False):
        self.total = total
        self.ignore_anchor = ignore_anchor
        self.forward_anchor = forward_anchor
        self.calls: list[tuple[str, dict]] = []

    def _msg(self, seq: int) -> dict:
        return {
            "message_id": seq,
            "message_seq": seq,
            "real_seq": seq,
            "time": 1700000000 + seq,
            "sender": {"user_id": 1000 + seq % 3, "nickname": f"u{seq % 3}"},
            "raw_message": f"msg-{seq}",
        }

    def _range(self, start: int, end: int) -> list[dict]:
        start = max(1, start)
        end = min(self.total, end)
        if start > end:
            return []
        return [self._msg(seq) for seq in range(start, end + 1)]

    async def call_action(self, action: str, **params):
        self.calls.append((action, params))
        count = int(params.get("count", 0) or 0)
        anchor = params.get("message_seq")

        if anchor and not self.ignore_anchor and not self.forward_anchor:
            end = int(anchor)
            return self._ok(self._range(end - count + 1, end))
        if anchor and self.forward_anchor:
            return self._ok(self._range(int(anchor) + 1, int(anchor) + count))
        return self._ok(self._range(self.total - count + 1, self.total))

    @staticmethod
    def _ok(messages: list[dict]) -> dict:
        return {"status": "ok", "retcode": 0, "data": {"messages": messages}}


class _Plugin:
    enable_history = True
    _history_cache_ttl_seconds = 1200

    def __init__(self) -> None:
        self._history_pagination_cache: dict = {}

    def _safe_bool(self, value, default=False):
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return bool(value)


class _Event:
    def __init__(self, api, *, group_id="", sender_id="2001") -> None:
        self.bot = types.SimpleNamespace(api=api)
        self.message_obj = types.SimpleNamespace(
            group_id=group_id,
            sender=types.SimpleNamespace(user_id=sender_id),
        )

    def get_sender_id(self):
        return self.message_obj.sender.user_id


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("• ")]


def _seqs(text: str) -> list[str]:
    return [line.rsplit("msg-", 1)[-1] for line in _lines(text)]


class HistoryAnchorPagingTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, api, plugin, **args) -> str:
        event = _Event(api)
        return await history_tool.handle_history(plugin, event, args)

    async def test_first_page_fetches_newest_without_anchor(self):
        api = _FakeHistoryApi(total=100)
        plugin = _Plugin()

        text = await self._run(api, plugin, mode="friend", target_id="42", count=20)

        self.assertEqual(len(api.calls), 1)
        action, params = api.calls[0]
        self.assertEqual(action, "get_friend_msg_history")
        self.assertNotIn("message_seq", params)
        self.assertNotIn("reverse_order", params)
        self.assertGreaterEqual(params["count"], 20)
        self.assertEqual(_seqs(text)[:3], ["100", "99", "98"])
        self.assertEqual(len(_seqs(text)), 20)

    async def test_second_page_uses_cached_slice_without_refetch(self):
        api = _FakeHistoryApi(total=100)
        plugin = _Plugin()

        await self._run(api, plugin, mode="friend", target_id="42", count=20)
        calls_after_first = len(api.calls)
        text = await self._run(api, plugin, mode="friend", target_id="42", page=2, count=20)

        self.assertEqual(len(api.calls), calls_after_first, "第二页应复用本地缓存")
        self.assertEqual(_seqs(text)[0], "80")
        self.assertEqual(_seqs(text)[-1], "61")

    async def test_deep_page_pages_backwards_with_message_seq(self):
        api = _FakeHistoryApi(total=100)
        plugin = _Plugin()

        await self._run(api, plugin, mode="friend", target_id="42", count=20)
        first_anchor = api.calls[0][1]["count"]
        text = await self._run(api, plugin, mode="friend", target_id="42", page=3, count=20)

        self.assertEqual(len(api.calls), 2, "深页只需一次锚点请求，不再重拉最新")
        action, params = api.calls[-1]
        self.assertEqual(action, "get_friend_msg_history")
        self.assertEqual(params["reverse_order"], True)
        self.assertIsInstance(params["message_seq"], int)
        # 首轮取到的是最新 N 条，最旧一条就是第二轮锚点
        self.assertEqual(params["message_seq"], 100 - first_anchor + 1)
        self.assertEqual(_seqs(text)[0], "60")
        self.assertEqual(_seqs(text)[-1], "41")

    async def test_count_change_reuses_cache_and_moves_anchor(self):
        api = _FakeHistoryApi(total=100)
        plugin = _Plugin()

        await self._run(api, plugin, mode="friend", target_id="42", count=10)
        self.assertEqual(len(api.calls), 1)
        text = await self._run(api, plugin, mode="friend", target_id="42", page=2, count=40)

        self.assertEqual(len(api.calls), 2)
        self.assertIn("message_seq", api.calls[-1][1])
        self.assertEqual(_seqs(text)[0], "60")
        self.assertEqual(len(_seqs(text)), 40)

    async def test_ignored_anchor_falls_back_to_count_escalation(self):
        api = _FakeHistoryApi(total=100, ignore_anchor=True)
        plugin = _Plugin()

        await self._run(api, plugin, mode="friend", target_id="42", count=20)
        text = await self._run(api, plugin, mode="friend", target_id="42", page=3, count=20)

        self.assertEqual(_seqs(text)[0], "60")
        self.assertEqual(_seqs(text)[-1], "41")
        # 回退后按 count 递增重拉最新，且未带锚点参数
        last = api.calls[-1][1]
        self.assertNotIn("message_seq", last)
        self.assertGreater(last["count"], api.calls[0][1]["count"])

    async def test_forward_anchor_response_falls_back(self):
        api = _FakeHistoryApi(total=100, forward_anchor=True)
        plugin = _Plugin()

        await self._run(api, plugin, mode="friend", target_id="42", count=20)
        text = await self._run(api, plugin, mode="friend", target_id="42", page=3, count=20)

        self.assertEqual(_seqs(text)[0], "60")
        self.assertEqual(_seqs(text)[-1], "41")
        self.assertNotIn("message_seq", api.calls[-1][1])

    async def test_exhausted_history_reports_no_more(self):
        api = _FakeHistoryApi(total=20)
        plugin = _Plugin()

        first = await self._run(api, plugin, mode="friend", target_id="42", count=20)
        self.assertIn("已到达末页", first)
        text = await self._run(api, plugin, mode="friend", target_id="42", page=3, count=20)
        self.assertIn("暂无更多历史消息", text)
        self.assertEqual(len(api.calls), 1, "已到底后不应继续请求")

    async def test_group_mode_passes_group_id(self):
        api = _FakeHistoryApi(total=10)
        plugin = _Plugin()
        event = _Event(api, group_id="777")

        text = await history_tool.handle_history(plugin, event, {"count": 5})

        action, params = api.calls[0]
        self.assertEqual(action, "get_group_msg_history")
        self.assertEqual(params["group_id"], "777")
        self.assertIn("群 777 历史消息", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
