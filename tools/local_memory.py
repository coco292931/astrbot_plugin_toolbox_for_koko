"""本地记忆工具（JSON 文件存储）。"""
from __future__ import annotations

import json


async def handle_add_memory(plugin, event, args: dict) -> str:
    content = str(args.get("content", "") or "").strip()
    if not content:
        return "❌ 参数缺失：请提供记忆内容。"

    user_id = str(args.get("user_id", "") or "").strip() or str(event.get_sender_id())
    tags = str(args.get("tags", "") or "")
    tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    try:
        importance = int(args.get("importance", 5))
    except Exception:
        importance = 5
    importance = max(1, min(10, importance))

    memory_id = await plugin.memory_manager.add_memory(user_id, content, tags_list, importance)
    preview = f"{content[:50]}{'...' if len(content) > 50 else ''}"
    return f"✅ 记忆已保存\nID: {memory_id}\n内容: {preview}"


async def handle_search_memories(plugin, event, args: dict) -> str:
    keywords = str(args.get("keyword", "") or "").strip()
    user_specific = plugin._safe_bool(args.get("user_specific", True), True)

    raw_limit = args.get("limit", 10)
    try:
        limit = int(raw_limit)
    except Exception:
        limit = 10
    limit = max(1, min(limit, 20))

    forced_user_id = str(args.get("user_id", "") or "").strip()
    user_id = forced_user_id or (str(event.get_sender_id()) if user_specific else None)

    seen_keywords = set()
    keyword_list: list[str] = []
    for kw in keywords.split():
        k = kw.strip()
        if not k or k in seen_keywords:
            continue
        seen_keywords.add(k)
        keyword_list.append(k)

    if not keyword_list:
        memories = await plugin.memory_manager.get_memories(
            user_id=user_id,
            keyword=None,
            limit=limit,
        )
    else:
        memories_by_id: dict[str, dict] = {}
        for keyword in keyword_list:
            results = await plugin.memory_manager.get_memories(
                user_id=user_id,
                keyword=keyword,
                limit=limit,
            )
            for memory in results:
                memory_id = str(memory.get("id", "") or "")
                if memory_id and memory_id not in memories_by_id:
                    memories_by_id[memory_id] = memory

        memories = list(memories_by_id.values())
        memories.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        memories = memories[:limit]

    if not memories:
        if keywords:
            return f"📭 未找到包含「{keywords}」的记忆"
        return "📭 暂无记忆"

    lines = [f"📚 找到 {len(memories)} 条记忆："]
    for index, memory in enumerate(memories[:limit], 1):
        tags = memory.get("tags", []) or []
        tags_text = f"[{', '.join(tags)}]" if tags else ""
        content = str(memory.get("content", "") or "")
        preview = content[:40] + ("..." if len(content) > 40 else "")
        lines.append(
            f"{index}. [{memory.get('id')}] {preview} "
            f"(重要度:{memory.get('importance', 5)}) {tags_text} - {str(memory.get('updated_at', ''))[:10]}"
        )
    return "\n".join(lines)


async def handle_update_memory(plugin, args: dict) -> str:
    memory_id = str(args.get("memory_id", "") or "").strip()
    if not memory_id:
        return "❌ 参数缺失：请提供要更新的记忆ID。"

    existing = await plugin.memory_manager.get_memory_by_id(memory_id)
    if not existing:
        return f"❌ 未找到记忆ID: {memory_id}"

    content = args.get("content")
    if content is not None:
        content = str(content)

    tags = args.get("tags")
    tags_list = None
    if tags is not None:
        tags_list = [t.strip() for t in str(tags).split(",") if t.strip()]

    importance = args.get("importance")
    if importance is not None:
        try:
            importance = int(importance)
        except Exception:
            return "❌ 参数错误：importance 必须是数字。"

    success = await plugin.memory_manager.update_memory(memory_id, content, tags_list, importance)
    return f"✅ 记忆已更新\nID: {memory_id}" if success else "❌ 更新失败"


async def handle_delete_memory(plugin, args: dict) -> str:
    memory_id = str(args.get("memory_id", "") or "").strip()
    if not memory_id:
        return "❌ 参数缺失：请提供要删除的记忆ID。"

    existing = await plugin.memory_manager.get_memory_by_id(memory_id)
    if not existing:
        return f"❌ 未找到记忆ID: {memory_id}"

    success = await plugin.memory_manager.delete_memory(memory_id)
    return f"🗑️ 记忆已删除\nID: {memory_id}" if success else "❌ 删除失败"


async def handle_get_memory_detail(plugin, args: dict) -> str:
    memory_id = str(args.get("memory_id", "") or "").strip()
    if not memory_id:
        return "❌ 参数缺失：请提供记忆ID。"

    memory = await plugin.memory_manager.get_memory_by_id(memory_id)
    if not memory:
        return f"❌ 未找到记忆ID: {memory_id}"

    lines = [
        "📋 记忆详情",
        f"ID: {memory.get('id')}",
        f"用户: {memory.get('user_id')}",
        f"内容: {memory.get('content')}",
        f"标签: {', '.join(memory.get('tags', [])) or '无'}",
        f"重要度: {memory.get('importance', 5)}/10",
        f"创建: {memory.get('created_at')}",
        f"更新: {memory.get('updated_at')}",
    ]
    return "\n".join(lines)


async def build_memory_injection_block(plugin, user_id: str) -> str | None:
    """供 on_llm_request 使用：将最近记忆整理成一段 system_prompt 文本。"""
    user_id = (user_id or "").strip()
    if not user_id:
        return None

    memories = await plugin.memory_manager.get_memories(
        user_id=user_id,
        limit=getattr(plugin, "memory_inject_count", 5),
        sort_by="updated_at",
    )
    if not memories:
        return None

    memory_lines: list[str] = []
    for idx, memory in enumerate(memories, 1):
        content = str(memory.get("content", "") or "").strip()
        if not content:
            continue
        importance = memory.get("importance", 5)
        tags = memory.get("tags", []) or []
        tags_text = f" [{', '.join(tags)}]" if tags else ""
        memory_lines.append(f"{idx}. {content}{tags_text} (重要度:{importance})")

    if not memory_lines:
        return None

    return f"[用户历史记忆] 该用户({user_id})的重要信息：" + "\n".join(memory_lines)


# ---- 向量记忆：内部实现（从 main.py 迁移过来） ----


async def handle_search_memory_vector(plugin, event, args: dict) -> str:
    query = str(args.get("query", "") or "").strip()
    if not query:
        return "❌ 参数缺失：请提供 query。"

    collection_name = str(args.get("collection_name", "") or "").strip() or None

    raw_top_k = args.get("top_k", 5)
    try:
        top_k = int(raw_top_k)
    except Exception:
        top_k = 5
    top_k = max(1, min(top_k, 50))

    try:
        from core.config import run_mnemosyne_vector_search  # fmt: skip
        results = await run_mnemosyne_vector_search(
            plugin, query=query, top_k=top_k, collection_name=collection_name,
        )
    except Exception as e:
        return f"❌ 向量检索失败: {e}"

    payload = {
        "query": query,
        "top_k": top_k,
        "results": results,
    }
    try:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception:
        return str(payload)


async def _collect_forwarded_output_text(plugin, event, fn_name: str, **kwargs) -> str:
    parts: list[str] = []
    try:
        async for item in plugin._forward_to_mnemosyne(event, fn_name, **(kwargs or {})):
            text = plugin._extract_llm_text(item)
            if not text:
                try:
                    text = json.dumps(item, ensure_ascii=False)
                except Exception:
                    text = str(item)
            if text:
                parts.append(text)
    except Exception:
        pass
    return "\n".join(parts).strip()


async def handle_list_memory_vector(plugin, event) -> str:
    text = await _collect_forwarded_output_text(plugin, event, "list_collections_cmd")
    return text or "(empty)"


async def handle_list_records_memory_vector(plugin, event, args: dict) -> str:
    collection_name = str(args.get("collection_name", "") or "").strip() or None
    raw_limit = args.get("limit", 5)
    try:
        limit = int(raw_limit)
    except Exception:
        limit = 5
    limit = max(1, min(limit, 50))
    text = await _collect_forwarded_output_text(
        plugin, event, "list_records_cmd",
        collection_name=collection_name,
        limit=limit,
    )
    return text or "(empty)"


async def handle_remember_memory_vector(plugin, event, args: dict) -> str:
    content = str(args.get("content", "") or "").strip()
    if not content:
        return "❌ 参数缺失：请提供 content。"
    text = await _collect_forwarded_output_text(plugin, event, "remember_cmd", content=content)
    return text or "(ok)"


async def handle_delete_record_memory_vector(plugin, event, args: dict) -> str:
    memory_id = str(args.get("memory_id", "") or "").strip()
    if not memory_id:
        return "❌ 参数缺失：请提供 memory_id。"
    session_id = str(args.get("session_id", "") or "").strip() or None
    confirm = str(args.get("confirm", "") or "").strip() or None
    text = await _collect_forwarded_output_text(
        plugin, event, "delete_record_cmd",
        memory_id=memory_id,
        session_id=session_id,
        confirm=confirm,
    )
    return text or "(ok)"


# ---- 兼容旧命名（保留给 handlers/command_handlers.py 或其它外部引用） ----


async def run_add_memory(plugin, event, args: dict) -> str:
    return await handle_add_memory(plugin, event, args)


async def run_search_memories(plugin, event, args: dict) -> str:
    return await handle_search_memories(plugin, event, args)


async def run_update_memory(plugin, event, args: dict) -> str:
    return await handle_update_memory(plugin, args)


async def run_delete_memory(plugin, event, args: dict) -> str:
    return await handle_delete_memory(plugin, args)


async def run_get_memory_detail(plugin, event, args: dict) -> str:
    return await handle_get_memory_detail(plugin, args)


async def run_search_memory_vector(plugin, event, args: dict) -> str:
    return await handle_search_memory_vector(plugin, event, args)


async def run_list_memory_vector(plugin, event, args: dict) -> str:
    return await handle_list_memory_vector(plugin, event)


async def run_list_records_memory_vector(plugin, event, args: dict) -> str:
    return await handle_list_records_memory_vector(plugin, event, args)


async def run_remember_memory_vector(plugin, event, args: dict) -> str:
    return await handle_remember_memory_vector(plugin, event, args)


async def run_delete_record_memory_vector(plugin, event, args: dict) -> str:
    return await handle_delete_record_memory_vector(plugin, event, args)