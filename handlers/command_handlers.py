"""
命令处理器 - 各个 tool 命令的处理入口
"""


async def handle_tool_weather_location(plugin, event, args: dict) -> str:
    from tools.weather import run_weather_location

    return await run_weather_location(plugin, args)


async def handle_tool_weather(plugin, event, args: dict) -> str:
    from tools.weather import run_weather

    return await run_weather(plugin, args)


async def handle_tool_weather_history(plugin, event, args: dict) -> str:
    from tools.weather import run_weather_history

    return await run_weather_history(plugin, args)


async def handle_tool_search(plugin, event, args: dict) -> str:
    from tools.search import run_search

    return await run_search(plugin, args)


async def handle_tool_fetch_url(plugin, event, args: dict) -> str:
    from tools.fetch_url import run_fetch_url

    return await run_fetch_url(plugin, event, args)


async def handle_tool_batch_download(plugin, event, args: dict) -> str:
    from tools.fetch_url import run_batch_download

    return await run_batch_download(plugin, event, args)


async def handle_tool_send_message(plugin, event, args: dict) -> str:
    from tools.send_msg import run_send_message

    return await run_send_message(plugin, event, args)


async def handle_tool_history(plugin, event, args: dict) -> str:
    from tools.history import run_history

    return await run_history(plugin, event, args)


async def handle_tool_add_memory(plugin, event, args: dict) -> str:
    from tools.local_memory import run_add_memory

    return await run_add_memory(plugin, event, args)


async def handle_tool_search_memories(plugin, event, args: dict) -> str:
    from tools.local_memory import run_search_memories

    return await run_search_memories(plugin, event, args)


async def handle_tool_search_memory_vector(plugin, event, args: dict) -> str:
    from tools.local_memory import run_search_memory_vector

    return await run_search_memory_vector(plugin, event, args)


async def handle_tool_list_memory_vector(plugin, event, args: dict) -> str:
    from tools.local_memory import run_list_memory_vector

    return await run_list_memory_vector(plugin, event, args)


async def handle_tool_list_records_memory_vector(plugin, event, args: dict) -> str:
    from tools.local_memory import run_list_records_memory_vector

    return await run_list_records_memory_vector(plugin, event, args)


async def handle_tool_remember_memory_vector(plugin, event, args: dict) -> str:
    from tools.local_memory import run_remember_memory_vector

    return await run_remember_memory_vector(plugin, event, args)


async def handle_tool_delete_record_memory_vector(plugin, event, args: dict) -> str:
    from tools.local_memory import run_delete_record_memory_vector

    return await run_delete_record_memory_vector(plugin, event, args)


async def handle_tool_update_memory(plugin, event, args: dict) -> str:
    from tools.local_memory import run_update_memory

    return await run_update_memory(plugin, event, args)


async def handle_tool_delete_memory(plugin, event, args: dict) -> str:
    from tools.local_memory import run_delete_memory

    return await run_delete_memory(plugin, event, args)


async def handle_tool_get_memory_detail(plugin, event, args: dict) -> str:
    from tools.local_memory import run_get_memory_detail

    return await run_get_memory_detail(plugin, event, args)


async def handle_tool_mnemosyne_bridge(plugin, event, args: dict) -> str:
    from tools.bridge import run_mnemosyne_bridge

    return await run_mnemosyne_bridge(plugin, event, args)


async def handle_tool_qzone_bridge(plugin, event, args: dict) -> str:
    from tools.bridge import run_qzone_bridge

    return await run_qzone_bridge(plugin, event, args)


# 工具名称到处理函数的映射
TOOL_HANDLER_MAP = {
    "tool_weather_location": handle_tool_weather_location,
    "tool_weather": handle_tool_weather,
    "tool_weather_history": handle_tool_weather_history,
    "tool_search": handle_tool_search,
    "tool_fetch_url": handle_tool_fetch_url,
    "tool_batch_download": handle_tool_batch_download,
    "tool_send_message": handle_tool_send_message,
    "tool_history": handle_tool_history,
    "tool_add_memory": handle_tool_add_memory,
    "tool_search_memories": handle_tool_search_memories,
    "tool_search_memory_vector": handle_tool_search_memory_vector,
    "tool_list_memory_vector": handle_tool_list_memory_vector,
    "tool_list_records_memory_vector": handle_tool_list_records_memory_vector,
    "tool_remember_memory_vector": handle_tool_remember_memory_vector,
    "tool_delete_record_memory_vector": handle_tool_delete_record_memory_vector,
    "tool_update_memory": handle_tool_update_memory,
    "tool_delete_memory": handle_tool_delete_memory,
    "tool_get_memory_detail": handle_tool_get_memory_detail,
    "tool_mnemosyne_bridge": handle_tool_mnemosyne_bridge,
    "tool_qzone_bridge": handle_tool_qzone_bridge,
}
