"""历史消息工具（OneBot get_group_msg_history / get_friend_msg_history）。"""

from __future__ import annotations

import traceback
from datetime import datetime
from typing import Any

from astrbot.api import logger


def _history_make_cache_key(mode: str, target_id: str, page_size: int) -> str:
    return f"{mode}|{target_id}|{page_size}"


def _history_prune_cache(plugin) -> None:
    now_ts = int(datetime.now().timestamp())
    expire_before = now_ts - int(getattr(plugin, "_history_cache_ttl_seconds", 1200))
    cache_dict = getattr(plugin, "_history_pagination_cache", {})
    if not isinstance(cache_dict, dict):
        return
    to_delete = []
    for key, item in cache_dict.items():
        updated_at = int(item.get("updated_at", 0)) if isinstance(item, dict) else 0
        if updated_at <= expire_before:
            to_delete.append(key)
    for key in to_delete:
        cache_dict.pop(key, None)


def _history_extract_messages(result: Any) -> list[dict]:
    messages: list[Any] = []
    if isinstance(result, list):
        messages = result
    elif isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, dict):
            for key in ("messages", "message", "list", "records"):
                value = data.get(key)
                if isinstance(value, list):
                    messages = value
                    break
            if not messages and isinstance(data.get("data"), list):
                messages = data.get("data", [])
        elif isinstance(data, list):
            messages = data

        if not messages:
            for key in ("messages", "message", "list", "records"):
                value = result.get(key)
                if isinstance(value, list):
                    messages = value
                    break

    if not isinstance(messages, list):
        return []
    return [m for m in messages if isinstance(m, dict)]


def _history_msg_unique_key(msg: dict) -> str:
    msg_id = msg.get("message_id")
    msg_seq = msg.get("message_seq")
    time_text = msg.get("time", "")
    sender_id = ""
    sender = msg.get("sender")
    if isinstance(sender, dict):
        sender_id = str(sender.get("user_id", "") or "")
    raw = str(msg.get("raw_message", "") or "")
    return f"id={msg_id}|seq={msg_seq}|t={time_text}|u={sender_id}|raw={raw[:32]}"


def _history_pick_seq(msg: dict) -> int:
    for key in ("message_seq", "message_id"):
        value = msg.get(key)
        try:
            seq_num = int(str(value))
            if seq_num >= 0:
                return seq_num
        except Exception:
            continue
    return -1


def _history_format_time(msg: dict) -> str:
    ts = msg.get("time")
    try:
        ts_num = int(str(ts))
        if ts_num <= 0:
            return "--:--"
        return datetime.fromtimestamp(ts_num).strftime("%H:%M")
    except Exception:
        return "--:--"


def _history_sort_key_desc(msg: dict) -> tuple[int, int]:
    ts_num = 0
    try:
        ts_num = int(str(msg.get("time", 0) or 0))
    except Exception:
        ts_num = 0
    seq_num = _history_pick_seq(msg)
    return ts_num, seq_num


async def handle_history(plugin, event, args: dict) -> str:
    if not getattr(plugin, "enable_history", True):
        return "历史查询功能已被禁用。"

    msg_obj = getattr(event, "message_obj", None)

    raw_mode = str(args.get("mode", "") or "").strip().lower()
    if raw_mode and raw_mode not in {"group", "friend"}:
        return "mode 参数无效：仅支持 group 或 friend。"
    mode = raw_mode

    target_id = str(args.get("target_id", "") or "").strip()

    try:
        page = int(args.get("page", 1))
    except Exception:
        page = 1
    page = max(1, page)
    refresh = plugin._safe_bool(args.get("refresh", False), False)

    try:
        count = int(args.get("count", 20))
    except Exception:
        count = 20
    page_size = max(1, min(count, 100))

    context_group_id = str(getattr(msg_obj, "group_id", "") or "").strip()

    sender_user_id = ""
    sender = getattr(msg_obj, "sender", None)
    if sender is not None:
        sender_user_id = str(getattr(sender, "user_id", "") or "").strip()
    if not sender_user_id:
        try:
            sender_user_id = str(event.get_sender_id() or "").strip()
        except Exception:
            sender_user_id = ""

    if not mode:
        mode = "group" if context_group_id else "friend"

    if not target_id:
        if mode == "group":
            target_id = context_group_id
        else:
            target_id = sender_user_id

    if not target_id:
        if mode == "group":
            return "缺少 target_id：group 模式请提供群号，或在群聊上下文中调用。"
        return "缺少 target_id：friend 模式请提供用户QQ号，或在私聊上下文中调用。"

    _history_prune_cache(plugin)

    client = None
    if hasattr(event, "bot") and getattr(event.bot, "api", None):
        client = getattr(event.bot, "api", None)
    elif hasattr(event, "bot"):
        client = event.bot

    if not client or not hasattr(client, "call_action"):
        return "无法获取客户端 adapter，该端点可能不支持原生 call_action()。"

    cache_key = _history_make_cache_key(mode, target_id, page_size)
    cache_dict = getattr(plugin, "_history_pagination_cache", None)
    if not isinstance(cache_dict, dict):
        cache_dict = {}
        plugin._history_pagination_cache = cache_dict
    cache = cache_dict.get(cache_key)

    if page == 1 or refresh or not isinstance(cache, dict):
        cache = {
            "messages": [],
            "seen": set(),
            "last_fetch_count": 0,
            "exhausted": False,
            "updated_at": int(datetime.now().timestamp()),
        }
        cache_dict[cache_key] = cache

    async def _call_history(fetch_count: int) -> Any:
        if mode == "group":
            return await client.call_action(
                "get_group_msg_history",
                group_id=target_id,
                count=fetch_count,
            )
        return await client.call_action(
            "get_friend_msg_history",
            user_id=target_id,
            count=fetch_count,
        )

    try:
        needed_end = page * page_size
        fetch_rounds = 0

        while len(cache.get("messages", [])) < needed_end and not cache.get(
            "exhausted", False
        ):
            if fetch_rounds >= 8:
                break
            fetch_rounds += 1

            fetch_count = max(int(cache.get("last_fetch_count", 0)), 0) + 100
            fetch_count = min(fetch_count, 1000)
            result = await _call_history(fetch_count)
            logger.debug(f"历史消息接口返回: {result}")

            batch_messages = _history_extract_messages(result)
            if not batch_messages:
                cache["exhausted"] = True
                break

            before_count = len(cache["messages"])
            seen = cache.get("seen", set())
            if not isinstance(seen, set):
                seen = set()

            for msg in batch_messages:
                unique_key = _history_msg_unique_key(msg)
                if unique_key in seen:
                    continue
                seen.add(unique_key)
                cache["messages"].append(msg)

            cache["seen"] = seen
            after_count = len(cache["messages"])
            if after_count == before_count:
                cache["exhausted"] = True
                break

            cache["last_fetch_count"] = fetch_count
            if len(batch_messages) < fetch_count:
                cache["exhausted"] = True

            cache["updated_at"] = int(datetime.now().timestamp())

        cache["updated_at"] = int(datetime.now().timestamp())

        all_messages = sorted(
            cache.get("messages", []),
            key=_history_sort_key_desc,
            reverse=True,
        )
        start_index = (page - 1) * page_size
        end_index = start_index + page_size
        page_messages = all_messages[start_index:end_index]

        if not page_messages:
            if all_messages:
                return f"暂无更多历史消息（当前共缓存 {len(all_messages)} 条）。"
            return "暂无历史消息记录"

        title = (
            f"群 {target_id} 历史消息" if mode == "group" else f"好友 {target_id} 历史消息"
        )
        if refresh:
            title += "（已刷新缓存）"
        lines = [
            f"{title}（第 {page} 页，每页 {page_size} 条，本地缓存共 {len(all_messages)} 条）："
        ]

        for msg in page_messages:
            sender_info = msg.get("sender", {})
            if not isinstance(sender_info, dict):
                sender_info = {}
            sender_name = sender_info.get(
                "nickname", sender_info.get("user_id", "未知")
            )
            time_text = _history_format_time(msg)

            raw_msg = msg.get("raw_message", "")
            if not raw_msg:
                for segment in msg.get("message", []):
                    if isinstance(segment, dict) and segment.get("type") == "text":
                        raw_msg += segment.get("data", {}).get("text", "")

            content = raw_msg[:200].replace("\n", "  ")
            lines.append(f"• [{time_text}] {sender_name}: {content}")

        has_more_local = len(all_messages) > end_index
        may_have_more_remote = not cache.get("exhausted", False)
        lines.append("")
        if has_more_local or may_have_more_remote:
            lines.append(
                f"分页提示：下一页可传 page={page + 1}, count={page_size}"
                f"（mode={mode}, target_id={target_id}）。"
            )
        else:
            lines.append("分页提示：已到达末页。")

        return "\n".join(lines)
    except Exception as e:
        logger.error(traceback.format_exc())
        return f"查询历史记录失败，可能缺少相关权限或 API 不受支持: {str(e)}"


async def run_history(plugin, event, args: dict) -> str:
    """兼容旧命名。"""
    return await handle_history(plugin, event, args)