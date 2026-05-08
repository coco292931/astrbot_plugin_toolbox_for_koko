"""智谱联网搜索工具（Zhipu web_search）。

与插件配置字段保持一致：
- zhipu_key / zhipu_search_model / zhipu_search_intent
"""

from __future__ import annotations

import asyncio
import json

import aiohttp

from astrbot.api import logger


async def handle_search(plugin, args: dict) -> str:
    if not getattr(plugin, "enable_search", True):
        return "网络搜索功能已被禁用。"
    if not getattr(plugin, "zhipu_key", ""):
        return "缺失智谱 API Key配置。"

    query = args.get("query", "")
    if not query:
        return "搜索关键词为空。"

    engine = args.get("engine", "search_std")
    content_size = str(args.get("content_size", "lite")).lower()
    time_filter = args.get("time_filter", "noLimit")
    count_raw = args.get("count", 10)
    try:
        count = max(1, min(int(count_raw), 20))
    except Exception:
        count = 10

    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    headers = {
        "Authorization": f"Bearer {plugin.zhipu_key}",
        "Content-Type": "application/json",
    }

    api_content_size = "high" if content_size == "high" else "medium"
    model = getattr(plugin, "config", {}).get("zhipu_search_model", "glm-4.7-flash")

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": query}],
        "tools": [
            {
                "type": "web_search",
                "web_search": {
                    "search_engine": engine,
                    "search_intent": getattr(plugin, "config", {}).get(
                        "zhipu_search_intent", True
                    ),
                    "search_recency_filter": time_filter,
                    "content_size": api_content_size,
                    "count": count,
                },
            }
        ],
    }

    try:
        timeout = aiohttp.ClientTimeout(total=90)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    body_text = await resp.text()
                    try:
                        err_obj = json.loads(body_text)
                        err_code = err_obj.get("error", {}).get("code", "unknown")
                        err_msg = err_obj.get("error", {}).get("message", body_text)
                        return (
                            f"搜索请求失败，状态码: {resp.status}。code: {err_code}，message: {err_msg}"
                        )
                    except Exception:
                        return f"搜索请求失败，状态码: {resp.status}。细节: {body_text}"

                data = await resp.json(content_type=None)
                message = ((data.get("choices") or [{}])[0].get("message") or {})
                content = message.get("content", "")
                web_search = data.get("web_search", []) if isinstance(data, dict) else []

                if content_size == "lite":
                    return f"【极简摘要】\n{content}"
                if content_size == "medium":
                    sources = [
                        {
                            "title": w.get("title"),
                            "publish_date": w.get("publish_date"),
                            "media": w.get("media"),
                            "link": w.get("link"),
                        }
                        for w in web_search
                    ]
                    return (
                        "【常规搜索】\n"
                        f"摘要: {content}\n\n参考来源:\n{json.dumps(sources, ensure_ascii=False)}"
                    )

                sources = [
                    {
                        "title": w.get("title"),
                        "publish_date": w.get("publish_date"),
                        "media": w.get("media"),
                        "link": w.get("link"),
                        "content": w.get("content"),
                    }
                    for w in web_search
                ]
                return (
                    "【全量搜索汇总】\n"
                    f"摘要: {content}\n\n参考来源:\n{json.dumps(sources, ensure_ascii=False)}"
                )
    except asyncio.TimeoutError as e:
        logger.error("tools.search: timeout", exc_info=True)
        detail = str(e).strip() or repr(e)
        return f"搜索请求超时(90s): {detail}"
    except aiohttp.ClientError as e:
        logger.error("tools.search: client error", exc_info=True)
        detail = str(e).strip() or repr(e)
        return f"搜索网络异常({type(e).__name__}): {detail}"
    except Exception as e:
        logger.error("tools.search: internal error", exc_info=True)
        detail = str(e).strip() or repr(e)
        return f"搜索内部异常({type(e).__name__}): {detail}"


async def run_search(plugin, args: dict) -> str:
    """兼容旧命名。"""
    return await handle_search(plugin, args)