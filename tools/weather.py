"""天气工具（QWeather）：
- tool_weather_location：位置检索，返回 LocationID 候选
- tool_weather：实时/预报天气
- tool_weather_history：历史天气/空气

以 main_refactored.py 的实现为基准做模块化抽离。
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timedelta

import aiohttp
from astrbot.api import logger


async def handle_weather_location(plugin, args: dict) -> str:
    if not getattr(plugin, "enable_weather", True):
        return "天气查询功能已被禁用。"
    if not getattr(plugin, "qweather_jwt_token", "") and not getattr(
        plugin, "qweather_key", ""
    ):
        return "缺失 QWeather 认证配置，请提供 qweather_jwt_token 或 qweather_key。"

    location_kw = args.get("location", "") or args.get("city_name", "")
    if not location_kw:
        return "请输入 location（支持城市名、经纬度、LocationID 或 Adcode）。"

    number_raw = args.get("number", 10)
    try:
        number = max(1, min(int(number_raw), 20))
    except Exception:
        number = 10

    adm = args.get("adm", "")
    range_ = args.get("range", "")
    lang = args.get("lang", "zh")

    headers, use_query_key = plugin._build_qweather_auth()
    query_pairs: list[tuple[str, str]] = [
        ("location", str(location_kw)),
        ("number", str(number)),
        ("lang", str(lang)),
    ]
    if adm:
        query_pairs.append(("adm", str(adm)))
    if range_:
        query_pairs.append(("range", str(range_)))
    if use_query_key:
        query_pairs.append(("key", plugin.qweather_key))

    host = plugin._get_geo_host(use_query_key).replace("https://", "").replace(
        "http://", ""
    )
    url = f"https://{host}/geo/v2/city/lookup?{urllib.parse.urlencode(query_pairs)}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if not isinstance(data, dict):
                    return f"GeoAPI 返回了非预期数据格式: {data}"

                if data.get("code") == "200" and data.get("location"):
                    results: list[str] = []
                    for i, loc in enumerate(data["location"], start=1):
                        detail = (
                            f"[{i}] id={loc.get('id')} name={loc.get('name')} "
                            f"adm2={loc.get('adm2')} adm1={loc.get('adm1')} country={loc.get('country')} "
                            f"lat={loc.get('lat')} lon={loc.get('lon')} tz={loc.get('tz')} "
                            f"utcOffset={loc.get('utcOffset')} isDst={loc.get('isDst')} "
                            f"type={loc.get('type')} rank={loc.get('rank')} fxLink={loc.get('fxLink')}"
                        )
                        results.append(detail)

                    refer = data.get("refer", {})
                    return (
                        "GeoAPI 查询成功。请从下列候选中选择 id 作为 tool_weather 的 location 参数。\n"
                        f"查询词: {location_kw}，返回条数: {len(results)}\n"
                        + "\n".join(results)
                        + f"\n数据来源: {refer.get('sources')}"
                    )

                return f"未找到 '{location_kw}' 的位置信息，或参数不符合 GeoAPI 要求。"
    except Exception as e:
        return f"查询位置信息异常: {str(e)}"


def _resolve_weather_query_type(query_type_raw: str) -> tuple[str, int]:
    """将 query_type 参数映射为 (api_path_suffix, days_count)。

    支持的值:
        now    -> ("now", 0)
        3d     -> ("weather/3d", 3)
        7d     -> ("weather/7d", 7)
        indices_1d -> ("indices/1d", 0)  生活指数
        indices_3d -> ("indices/3d", 0)  生活指数
        默认    -> ("weather/3d", 3)
    """
    qt = (query_type_raw or "").strip().lower()
    mapping = {
        "now": ("weather/now", 0),
        "3d": ("weather/3d", 3),
        "7d": ("weather/7d", 7),
        "indices_1d": ("indices/1d", 0),
        "indices_3d": ("indices/3d", 0),
    }
    return mapping.get(qt, ("weather/3d", 3))


async def handle_weather(plugin, args: dict) -> str:
    if not getattr(plugin, "enable_weather", True):
        return "天气查询功能已被禁用。"

    location_id = args.get("location")
    if not location_id:
        return "缺少 location 参数。请先用 tool_weather_location 查询 LocationID。"

    query_type_raw = args.get("query_type", "now")
    api_suffix, days = _resolve_weather_query_type(query_type_raw)

    lang = args.get("lang", "zh")
    unit = args.get("unit", "m")

    headers, use_query_key = plugin._build_qweather_auth()
    host = plugin._get_weather_host(use_query_key).replace("https://", "").replace(
        "http://", ""
    )

    is_indices = api_suffix.startswith("indices/")

    results: dict = {"code": "200", "location": str(location_id)}

    async with aiohttp.ClientSession() as session:

        # --- 实时天气（query_type 为 now/3d/7d 时获取）---
        if not is_indices:
            now_pairs: list[tuple[str, str]] = [
                ("location", str(location_id)),
                ("lang", str(lang)),
                ("unit", str(unit)),
            ]
            if use_query_key:
                now_pairs.append(("key", plugin.qweather_key))

            now_url = f"https://{host}/v7/weather/now?{urllib.parse.urlencode(now_pairs)}"
            async with session.get(now_url, headers=headers) as resp_now:
                now_data = await resp_now.json(content_type=None)
            if not isinstance(now_data, dict) or now_data.get("code") != "200":
                return f"实时天气查询失败，错误码: {now_data.get('code', 'unknown')}"
            results["now"] = now_data.get("now", {})
            results["refer"] = now_data.get("refer", {})

        # --- 预报天气 / 生活指数 ---
        query_pairs: list[tuple[str, str]] = [
            ("location", str(location_id)),
            ("lang", str(lang)),
        ]
        if not is_indices:
            query_pairs.append(("unit", str(unit)))
        if use_query_key:
            query_pairs.append(("key", plugin.qweather_key))
        # 生活指数 API 默认传 type=0 获取所有类别
        if is_indices:
            query_pairs.append(("type", "0"))

        # indices API 路径为 /v7/indices/... 而非 /v7/weather/...
        api_version_path = f"v7/{api_suffix}"
        query_url = f"https://{host}/{api_version_path}?{urllib.parse.urlencode(query_pairs)}"
        async with session.get(query_url, headers=headers) as resp:
            query_data = await resp.json(content_type=None)
        if not isinstance(query_data, dict) or query_data.get("code") != "200":
            return f"{query_type_raw}查询失败，错误码: {query_data.get('code', 'unknown')}"

        if is_indices:
            results["daily"] = query_data.get("daily", [])
        elif days > 0:
            results["daily"] = query_data.get("daily", [])
            results["refer"] = results.get("refer", {})
            ref2 = query_data.get("refer", {})
            if ref2:
                results["refer"]["forecast"] = ref2
        else:
            # now 模式，不取 daily
            results["daily"] = []

    return json.dumps(results, ensure_ascii=False)


async def handle_weather_history(plugin, args: dict) -> str:
    if not getattr(plugin, "enable_weather", True):
        return "天气查询功能已被禁用。"

    location_id = args.get("location")
    if not location_id:
        return "缺少 location 参数。"

    history_type = args.get("history_type", "weather")
    days_raw = args.get("days", 7)
    try:
        days = max(1, min(int(days_raw), 30))
    except Exception:
        days = 7

    lang = args.get("lang", "zh")
    unit = args.get("unit", "m")

    headers, use_query_key = plugin._build_qweather_auth()

    def _build_url(date_str: str) -> str:
        if history_type == "air":
            host = plugin._get_air_host(use_query_key).replace("https://", "").replace(
                "http://", ""
            )
            pairs: list[tuple[str, str]] = [
                ("location", str(location_id)),
                ("date", date_str),
                ("lang", str(lang)),
            ]
            if use_query_key:
                pairs.append(("key", plugin.qweather_key))
            return f"https://{host}/v7/historical/air?{urllib.parse.urlencode(pairs)}"

        host = plugin._get_weather_host(use_query_key).replace("https://", "").replace(
            "http://", ""
        )
        pairs = [
            ("location", str(location_id)),
            ("date", date_str),
            ("lang", str(lang)),
            ("unit", str(unit)),
        ]
        if use_query_key:
            pairs.append(("key", plugin.qweather_key))
        return f"https://{host}/v7/historical/weather?{urllib.parse.urlencode(pairs)}"

    try:
        today = datetime.utcnow().date()
        date_list: list[str] = []
        for i in range(days):
            d = today - timedelta(days=i + 1)
            date_list.append(d.strftime("%Y%m%d"))

        historical_list: list[dict] = []
        async with aiohttp.ClientSession() as session:
            for date_str in date_list:
                url = _build_url(date_str)
                async with session.get(url, headers=headers) as resp:
                    data = await resp.json(content_type=None)
                    if not isinstance(data, dict) or data.get("code") != "200":
                        historical_list.append(
                            {"date": date_str, "code": getattr(data, "get", lambda _k: None)("code")}
                        )
                        continue

                    if history_type == "air":
                        historical_list.append(
                            {"date": date_str, "air": data.get("air", {}), "refer": data.get("refer", {})}
                        )
                    else:
                        historical_list.append(
                            {
                                "date": date_str,
                                "weatherDaily": data.get("weatherDaily", {}),
                                "refer": data.get("refer", {}),
                            }
                        )

        return json.dumps(
            {
                "code": "200",
                "location": str(location_id),
                "history_type": str(history_type),
                "days": days,
                "historical": historical_list,
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return f"历史数据查询内部异常: {str(e)}"


# ---- 兼容 wrappers（供 handlers/command_handlers.py 与 toolbox_plugin.py 调用） ----


async def run_weather_location(plugin, args: dict) -> str:
    return await handle_weather_location(plugin, args)


async def run_weather(plugin, args: dict) -> str:
    return await handle_weather(plugin, args)


async def run_weather_history(plugin, args: dict) -> str:
    return await handle_weather_history(plugin, args)
