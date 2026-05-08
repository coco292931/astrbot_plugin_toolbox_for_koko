"""发送消息工具（OneBot）"""

from __future__ import annotations


async def handle_send_message(plugin, event, args: dict) -> str:
    target_id = str(args.get("target_id", "") or "").strip()
    message = str(args.get("message", "") or "").strip()
    if not target_id or not message:
        return "❌ 参数缺失：请提供目标ID和消息内容。"

    chat_type = str(args.get("chat_type", "auto") or "auto").strip().lower()
    if chat_type not in {"auto", "group", "private"}:
        return "❌ 参数错误：chat_type 仅支持 group/private/auto。"

    is_valid, normalized_target = plugin._validate_target_id(target_id)
    if not is_valid:
        return f"参数错误: {normalized_target}"

    client = await plugin._get_client(event)
    if not client or not hasattr(client, "call_action"):
        return "错误：无法获取客户端"

    final_chat_type = chat_type
    if final_chat_type == "auto":
        await plugin._update_contacts_cache(client)
        is_group = any(
            str(g.get("group_id")) == normalized_target for g in plugin._groups_cache
        )
        final_chat_type = "group" if is_group else "private"

    try:
        if final_chat_type == "group":
            await client.call_action(
                "send_group_msg", group_id=int(normalized_target), message=message
            )
        else:
            await client.call_action(
                "send_private_msg", user_id=int(normalized_target), message=message
            )
        return f"✅ 已发送消息到 {normalized_target}"
    except Exception as e:
        return f"发送失败: {str(e)}"


async def run_send_message(plugin, event, args: dict) -> str:
    """兼容旧命名。"""
    return await handle_send_message(plugin, event, args)