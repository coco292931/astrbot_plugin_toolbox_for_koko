import aiohttp
import urllib.parse
import json

from astrbot.api.star import Context, Star, register
from astrbot.api.all import llm_tool
from astrbot.api import logger, AstrMessageEvent
import traceback

@register("astrbot_plugin_toolbox_for_koko", "coco", "多功能工具箱)", "1.0.0")
class ToolboxPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config

        # --- 配置加载 ---
        self.qweather_key = self.config.get("qweather_key", "")
        self.qweather_jwt_token = self.config.get("qweather_jwt_token", "")
        self.qweather_weather_host = self.config.get("qweather_weather_host", "devapi.qweather.com")
        self.qweather_geo_host = self.config.get("qweather_geo_host", "")
        self.zhipu_key = self.config.get("zhipu_key", "")
        
        # 功能开关
        self.enable_weather = self.config.get("enable_weather", True)
        self.enable_search = self.config.get("enable_search", True)
        self.enable_history = self.config.get("enable_history", True)
        
        # 7日天气压缩大模型设定的指令
        self.enable_weather_summary = self.config.get("enable_weather_summary", True)
        self.weather_summary_prompt = self.config.get(
            "weather_summary_prompt", 
            "请根据以下长篇天气预报数据，生成一份简短、友好的7天天气趋势总结："
        )
        self.weather_summary_llm_provider_id = self.config.get("weather_summary_llm_provider_id", "")

    def _build_qweather_auth(self):
        """优先使用 Bearer JWT（文档推荐），否则回退到 key 参数模式。"""
        headers = {}
        use_query_key = False
        if self.qweather_jwt_token:
            headers["Authorization"] = f"Bearer {self.qweather_jwt_token}"
        elif self.qweather_key:
            use_query_key = True
        return headers, use_query_key

    def _get_geo_host(self, use_query_key: bool) -> str:
        """Geo Host 选择规则：key 模式默认与 weather host 一致；JWT 模式默认 geoapi。"""
        if self.qweather_geo_host:
            return self.qweather_geo_host
        if use_query_key:
            return self.qweather_weather_host
        return "geoapi.qweather.com"

    # ---------------- 辅助方法 ----------------
    async def _fetch_qweather(self, api_type: str, location: str, extra_params: str = "") -> dict:
        """底层封装 QWeather 请求"""
        host = self.qweather_weather_host.replace("https://", "").replace("http://", "")
        headers, use_query_key = self._build_qweather_auth()
        location_safe = urllib.parse.quote(str(location), safe=",.")
        auth_part = f"&key={urllib.parse.quote(self.qweather_key)}" if use_query_key else ""
        url = f"https://{host}/v7/{api_type}?location={location_safe}{auth_part}{extra_params}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
                return {"code": str(resp.status)}

    async def _handle_location(self, args: dict) -> str:
        if not self.enable_weather:
            return "天气查询功能已被禁用。"
        if not self.qweather_jwt_token and not self.qweather_key:
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

        headers, use_query_key = self._build_qweather_auth()
        query_pairs = [
            ("location", location_kw),
            ("number", str(number)),
            ("lang", lang),
        ]
        if adm:
            query_pairs.append(("adm", adm))
        if range_:
            query_pairs.append(("range", range_))
        if use_query_key:
            query_pairs.append(("key", self.qweather_key))

        host = self._get_geo_host(use_query_key).replace("https://", "").replace("http://", "")
        url = f"https://{host}/v2/city/lookup?{urllib.parse.urlencode(query_pairs)}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    data = await resp.json()
                    if data.get("code") == "200" and data.get("location"):
                        results = []
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
                            + f"\n数据来源: {refer.get('sources')} 许可: {refer.get('license')}"
                        )
                    return f"未找到 '{location_kw}' 的位置信息，或参数不符合 GeoAPI 要求。"
        except Exception as e:
            return f"查询位置信息异常: {str(e)}"

    # ---------------- 工具暴露 ----------------
    @llm_tool("list_koko_tools")
    async def list_koko_tools(self, event: AstrMessageEvent) -> str:
        """
        获取koko工具箱工具列表。必须先使用此工具了解支持哪些内部命令，再使用 run_koko_tool!
        """
        tools = []
        if self.enable_weather:
            tools.append(
                "/tool_location [location] [number] [adm] [range] [lang]\n"
                "  查询城市编码（GeoAPI），返回详细候选列表。\n"
                "  location 支持城市名、经纬度、LocationID、Adcode；number 范围 1-20。\n"
                "\n"
                "/tool_weather [location] [query_type] [full_7d]\n"
                "  获取天气或生活指数。location 必须是 tool_location 输出中的 id。\n"
                "  query_type: 'now'(实时), '3d'(3日), '7d'(7日), 'indices_1d'(今日生活指数), 'indices_3d'(3日生活指数)。\n"
                "  full_7d: True(全量返回由你自行总结), False(默认,由接口直接截断成纯文本浓缩)。"
            )
        if self.enable_search:
            tools.append(
                "/tool_search [query] [engine] [content_size] [time_filter]\n"
                "  执行网页搜索。\n"
                "  query: 搜索关键词。\n"
                "  engine: 'search_std'(默认普搜) | 'search_pro_quark'(困难复杂问题的高级搜)。一般情况使用search_std\n"
                "  content_size: 'lite'(极简摘要) | 'medium'(常规云端摘要) | 'high'(全量查询并附带详情链接)。 一般情况使用lite即可\n"
                "  time_filter: 'noLimit'(不限) | 'oneDay'(一天内) | 'oneWeek' | 'oneMonth' | 'oneYear'。 默认为noLimit"
            )
        if self.enable_history:
            tools.append(
                "/tool_history [mode] [target_id] [message_seq] [count]\n"
                "  获取历史消息记录。\n"
                "  mode: 'group'(群聊) | 'friend'(好友私聊)。\n"
                "  target_id: 对应的群号或QQ号(如果在当前群聊查询则可留空)。\n"
                "  message_seq: 起始消息序号(不与page共用)。\n"
                "  page: 分页拉取(与message_seq互斥，1为最新)。\n"
                "  count: 数量(默认20条，最大100条)。"
            )
            
        if not tools:
            return "当前配置极简模式，没有启用任何工具。"
            
        return "当前可用命令：\n\n" + "\n\n".join(tools)

    @llm_tool("run_koko_tool")
    async def run_koko_tool(self, event: AstrMessageEvent, command: str, args: dict) -> str:
        """
        执行koko工具箱的工具。需先通过 list_koko_tools 拿到支持的命令说明。
        
        Args:
            command (string): 工具命令名，带或不带斜杠均可。例如 "tool_weather", "/tool_search"
            args (object): 参数字典。对应每个工具在 list_koko_tools 中的文档。
        """
        cmd = command.replace("/", "").strip()
        if cmd == "tool_location":
            return await self._handle_location(args)
        elif cmd == "tool_weather":
            return await self._handle_weather(args)
        elif cmd == "tool_search":
            return await self._handle_search(args)
        elif cmd == "tool_history":
            return await self._handle_history(event, args)
        else:
            return f"未知的命令: {command}。请使用 list_koko_tools 查看支持的工具。"

    async def _handle_weather(self, args: dict) -> str:
        if not self.enable_weather:
            return "天气查询功能已被禁用。"
        if not self.qweather_jwt_token and not self.qweather_key:
            return "缺失 QWeather 认证配置，请提供 qweather_jwt_token 或 qweather_key。"

        location_id = args.get("location", "") or args.get("location_id", "")
        if not location_id:
            return "缺少 location 参数，请先调用 tool_location 获取 Location ID。"

        query_type = args.get("query_type", "now")
        full_7d = args.get("full_7d", False)

        valid_types = {
            "now": "weather/now",
            "3d": "weather/3d",
            "7d": "weather/7d",
            "indices_1d": "indices/1d",
            "indices_3d": "indices/3d"
        }
        
        if query_type not in valid_types:
            return f"【API拒绝】无效的天气查询类型: {query_type}。"

        # 指数API调用（type=0表示获取所有类的生活指数数据）
        extra = "&type=0" if "indices" in query_type else ""

        try:
            data = await self._fetch_qweather(valid_types[query_type], location_id, extra)
            if data.get("code") != "200":
                return f"QWeather API 返回错误码: {data.get('code')}"
            
            # --- 7日天气处理 ---
            if query_type == "7d" and not full_7d and self.enable_weather_summary:
                summary_raw = "\n".join([f"{day['fxDate']}: 白天{day['textDay']} 夜间{day['textNight']}, {day['tempMin']}~{day['tempMax']}°C" for day in data.get("daily", [])])
                if self.weather_summary_llm_provider_id:
                    try:
                        prompt = f"{self.weather_summary_prompt}\n\n原始7日天气:\n{summary_raw}"
                        ai_resp = await self.context.llm_generate(
                            chat_provider_id=self.weather_summary_llm_provider_id,
                            prompt=prompt,
                        )
                        return ai_resp
                    except Exception:
                        logger.warning("7日天气LLM压缩失败，回退为原始精简文本。")
                return f"【系统提示: 已精简7日天气数据】\n{summary_raw}\n【系统行为指令】: {self.weather_summary_prompt}"
            
            # --- 生活指数处理 ---
            if "indices" in query_type:
                # 仅保留 daily 并在外层添加说明
                daily_indices = data.get("daily", [])
                return "生活指数数据:\n" + json.dumps(daily_indices, ensure_ascii=False)
                
            return json.dumps(data, ensure_ascii=False)
            
        except Exception as e:
            return f"天气查询内部异常: {str(e)}"

    async def _handle_search(self, args: dict) -> str:
        if not self.enable_search:
            return "网络搜索功能已被禁用。"
        if not self.zhipu_key:
            return "缺失智谱 API Key配置。"

        query = args.get("query", "")
        if not query:
            return "搜索关键词为空。"

        engine = args.get("engine", "search_std")
        content_size = args.get("content_size", "medium")
        time_filter = args.get("time_filter", "noLimit")

        url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.zhipu_key}",
            "Content-Type": "application/json"
        }
        
        # 'lite' 用本地拦截限制，API 发 high 或 medium
        api_content_size = "high" if content_size == "high" else "medium"
        # 兼容用户配置的模型名字，假设配置中如果不存在就使用默认的 GLM-4.7-flash
        model = self.config.get("zhipu_search_model", "glm-4.7-flash")

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": query}],
            "tools": [{
                "type": "web_search",
                "web_search": {
                    "search_result": True,
                    "search_engine": engine,
                    "search_intent": True,
                    "search_recency_filter": time_filter,
                    "content_size": api_content_size,
                    "count": 10
                }
            }]
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload, timeout=60) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        return f"搜索请求失败，状态码: {resp.status}。细节: {err}"
                        
                    data = await resp.json()
                    content = data['choices'][0]['message'].get('content', '')
                    
                    if content_size == "lite":
                        # 返回极简摘要给模型自己读
                        return f"【极简摘要】\n{content}"
                    elif content_size == "medium":
                        return f"【常规搜索】\n{content}"
                    else:
                        web_search = data.get('web_search', [])
                        sources = [{"title": w.get("title"), "link": w.get("link"), "media": w.get("media")} for w in web_search]
                        return f"【全量搜索汇总】\n摘要: {content}\n\n参考来源:\n{json.dumps(sources, ensure_ascii=False)}"
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"搜索内部异常: {str(e)}"

    async def _handle_history(self, event: AstrMessageEvent, args: dict) -> str:
        if not self.enable_history:
            return "历史查询功能已被禁用。"

        mode = args.get("mode", "group")
        target_id = args.get("target_id", "")
        message_seq = args.get("message_seq", 0)
        count = args.get("count", 20)

        # 获取底层 OneBot / go_cqhttp 的 client 实例
        client = None
        if hasattr(event, "bot") and getattr(event.bot, "api", None):
            client = getattr(event.bot, "api", None)
        elif hasattr(event, "bot"):
            client = event.bot

        if not client or not hasattr(client, "call_action"):
            # 有些 AstrBot 版本下，需要使用不同的方法拿去 adapter 或者 call_action，这里做一个基础兼容保障：
            # 如果我们找不到支持 call_action 的属性，则通知大模型获取失败
            return "无法获取客户端 adapter，该端点可能不支持原生 call_action()。"

        try:
            if mode == "group":
                # 群聊历史记录
                if not target_id:
                    # 尝试从消息对象提取 group_id
                    target_id = getattr(event.message_obj, "group_id", "")
                    if not target_id:
                        return "缺少目标群号。如果你不在群聊中使用该功能，必须提供 target_id！"

                result = await client.call_action('get_group_msg_history', group_id=target_id, message_seq=message_seq, count=min(count, 100))
            else:
                # 好友历史记录
                if not target_id:
                    return "私聊模式下请提供好友的 user_id 或 QQ 号！"

                result = await client.call_action('get_friend_msg_history', user_id=target_id, message_seq=message_seq, count=min(count, 100))

            # 统一解析返回结构
            messages = result.get('data', {}).get('messages', [])
            if not messages:
                return "暂无历史消息记录"

            # 拼装返回摘要文字
            title = f"📜 群 {target_id} 历史消息" if mode == "group" else f"📜 好友 {target_id} 历史消息"
            lines = [f"{title}（拉取到 {len(messages)} 条）："]
            
            for msg in messages:
                sender_info = msg.get('sender', {})
                sender = sender_info.get('nickname', sender_info.get('user_id', '未知'))
                # 优先获取原始纯文本消息
                raw_msg = msg.get('raw_message', '')
                if not raw_msg:
                    # 如果为空，尝试提取内容里的文本片段
                    for segment in msg.get('message', []):
                        if isinstance(segment, dict) and segment.get('type') == 'text':
                            raw_msg += segment.get('data', {}).get('text', '')
                # 简单格式化与截断过长单条消息
                content = raw_msg[:200].replace('\n', '  ')
                lines.append(f"• {sender}: {content}")
                
            return "\n".join(lines)
            
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"查询历史记录失败，可能缺少相关权限或 API 不受支持: {str(e)}"
