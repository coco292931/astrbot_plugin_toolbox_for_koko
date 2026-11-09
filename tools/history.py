"""历史消息工具（OneBot get_group_msg_history / get_friend_msg_history）。"""

from __future__ import annotations

import traceback
from datetime import datetime
from typing import Any

from astrbot.api import logger

# 锚点翻页（message_seq + reverse_order）单次请求条数上下限、最多请求轮数
_HISTORY_MIN_FETCH_COUNT = 50
_HISTORY_MAX_FETCH_COUNT = 500
_HISTORY_MAX_FETCH_ROUNDS = 8
# 适配器不支持锚点翻页时退回的 count 递增方式
_HISTORY_LEGACY_STEP = 100
_HISTORY_LEGACY_MAX_COUNT = 1000


def _history_new_cache() -> dict:
    return {
        "messages": [],
        "seen": set(),
        "anchor": None,
        "anchor_real": None,
        "last_fetch_count": 0,
        "legacy": False,
        "exhausted": False,
        "updated_at": int(datetime.now().timestamp()),
    }


def _history_make_cache_key(mode: str, target_id: str) -> str:
    return f"{mode}|{target_id}"


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


def _history_seq_text(msg: dict) -> str:
    """可用于去重的 message_seq（缺失或 0 视为不可用）。"""
    value = msg.get("message_seq")
    if value is None:
        return ""
    text = str(value).strip()
    if text in ("", "0"):
        return ""
    return text


def _history_msg_unique_key(msg: dict) -> str:
    seq_text = _history_seq_text(msg)
    if seq_text:
        return f"seq={seq_text}"
    msg_id = msg.get("message_id")
    time_text = msg.get("time", "")
    sender_id = ""
    sender = msg.get("sender")
    if isinstance(sender, dict):
        sender_id = str(sender.get("user_id", "") or "")
    raw = str(msg.get("raw_message", "") or "")
    return f"id={msg_id}|t={time_text}|u={sender_id}|raw={raw[:32]}"


def _history_anchor_value(msg: dict) -> Any:
    """翻页锚点：优先 message_seq，回退 message_id。"""
    for key in ("message_seq", "message_id"):
        value = msg.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        try:
            return int(text)
        except Exception:
            return text
    return None


def _history_matches_anchor(msg: dict, anchor: Any) -> bool:
    anchor_text = str(anchor).strip()
    for key in ("message_seq", "message_id"):
        value = msg.get(key)
        if value is not None and str(value).strip() == anchor_text:
            return True
    return False


def _history_real_seq(msg: dict) -> int | None:
    try:
        value = int(str(msg.get("real_seq")))
    except Exception:
        return None
    return value if value >= 0 else None


def _history_merge(cache: dict, batch: list[dict]) -> int:
    """把一批消息去重后并入缓存，返回新增条数。"""
    seen = cache.get("seen")
    if not isinstance(seen, set):
        seen = set()
        cache["seen"] = seen
    messages = cache.get("messages")
    if not isinstance(messages, list):
        messages = []
        cache["messages"] = messages
    added = 0
    for msg in batch:
        unique_key = _history_msg_unique_key(msg)
        if unique_key in seen:
            continue
        seen.add(unique_key)
        messages.append(msg)
        added += 1
    return added


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


def _history_sort_key_desc(msg: dict) -> tuple[int, int, int]:
    ts_num = 0
    try:
        ts_num = int(str(msg.get("time", 0) or 0))
    except Exception:
        ts_num = 0
    real_seq = _history_real_seq(msg)
    return ts_num, 0 if real_seq is None else real_seq, _history_pick_seq(msg)


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

    cache_key = _history_make_cache_key(mode, target_id)
    cache_dict = getattr(plugin, "_history_pagination_cache", None)
    if not isinstance(cache_dict, dict):
        cache_dict = {}
        plugin._history_pagination_cache = cache_dict
    cache = cache_dict.get(cache_key)

    if page == 1 or refresh or not isinstance(cache, dict):
        cache = _history_new_cache()
        cache_dict[cache_key] = cache
    else:
        # 兼容旧版本缓存字典（缺少 anchor/legacy 等字段）
        for key, default in _history_new_cache().items():
            cache.setdefault(key, default)

    async def _call_history(params: dict) -> Any:
        if mode == "group":
            return await client.call_action(
                "get_group_msg_history", group_id=target_id, **params
            )
        return await client.call_action(
            "get_friend_msg_history", user_id=target_id, **params
        )

    try:
        needed_end = page * page_size
        fetch_rounds = 0

        while len(cache["messages"]) < needed_end and not cache["exhausted"]:
            if fetch_rounds >= _HISTORY_MAX_FETCH_ROUNDS:
                break
            fetch_rounds += 1

            if cache["legacy"]:
                # 适配器不支持 message_seq/reverse_order：按 count 递增重拉最新
                fetch_count = min(
                    int(cache["last_fetch_count"]) + _HISTORY_LEGACY_STEP,
                    _HISTORY_LEGACY_MAX_COUNT,
                )
                batch_messages = _history_extract_messages(
                    await _call_history({"count": fetch_count})
                )
                if not batch_messages:
                    cache["exhausted"] = True
                    break
                cache["last_fetch_count"] = fetch_count
                added = _history_merge(cache, batch_messages)
                if added == 0 or len(batch_messages) < fetch_count:
                    cache["exhausted"] = True
                cache["updated_at"] = int(datetime.now().timestamp())
                continue

            # 首轮取最新一批，之后用 message_seq 作锚点 + reverse_order 向前翻页
            remaining = needed_end - len(cache["messages"])
            fetch_count = min(
                max(remaining, page_size, _HISTORY_MIN_FETCH_COUNT),
                _HISTORY_MAX_FETCH_COUNT,
            )
            anchor = cache["anchor"]
            params: dict = {"count": fetch_count}
            if anchor is not None:
                params["message_seq"] = anchor
                params["reverse_order"] = True

            try:
                result = await _call_history(params)
            except Exception:
                logger.debug(
                    "锚点翻页请求失败，改用 count 递增方式\n" + traceback.format_exc()
                )
                cache["legacy"] = True
                continue

            batch_messages = _history_extract_messages(result)
            logger.debug(
                f"历史消息接口返回 {len(batch_messages)} 条"
                f"（anchor={anchor}, count={fetch_count}）"
            )
            if not batch_messages:
                cache["exhausted"] = True
                break

            if anchor is not None and not any(
                _history_matches_anchor(msg, anchor) for msg in batch_messages
            ):
                logger.debug(
                    "接口未按锚点返回（可能不支持 message_seq/reverse_order），"
                    "改用 count 递增方式"
                )
                cache["legacy"] = True
                continue

            added = _history_merge(cache, batch_messages)
            oldest = batch_messages[0]
            oldest_anchor = _history_anchor_value(oldest)
            oldest_real = _history_real_seq(oldest)

            if oldest_anchor is None:
                logger.debug("消息缺少 message_seq，无法锚点翻页，改用 count 递增方式")
                cache["legacy"] = True
                continue

            progressed = str(oldest_anchor) != str(anchor)
            if oldest_real is not None:
                prev_real = cache["anchor_real"]
                if prev_real is not None and oldest_real >= int(prev_real):
                    progressed = False
                if oldest_real <= 1:
                    cache["exhausted"] = True

            if added == 0 or not progressed:
                # 首轮就翻不动说明没数据了；已有锚点还翻不动说明锚点翻页不可用
                if anchor is None:
                    cache["exhausted"] = True
                else:
                    cache["legacy"] = True
                continue

            cache["anchor"] = oldest_anchor
            if oldest_real is not None:
                cache["anchor_real"] = oldest_real
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
            f"群 {target_id} 历史消息"
            if mode == "group"
            else f"好友 {target_id} 历史消息"
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
