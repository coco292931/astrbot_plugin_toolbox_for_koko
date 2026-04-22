import aiohttp
import asyncio
import urllib.parse
import json
import random
import socket
import ipaddress
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Optional, List, Dict

from bs4 import BeautifulSoup
from readability import Document

from astrbot.api.star import Context, Star, register
from astrbot.api.all import llm_tool
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.message_event_result import MessageChain
import traceback


class MemoryManager:
    def __init__(self, data_dir: Path, max_memories_per_user: int = 100):
        self.data_dir = data_dir
        self.max_memories_per_user = max_memories_per_user
        self._lock = asyncio.Lock()
        self._file_path = self.data_dir / "memories.json"
        self._ensure_file()

    def _ensure_file(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self._file_path.exists():
            self._save_data({"memories": []})

    def _load_data(self) -> dict:
        try:
            return json.loads(self._file_path.read_text(encoding="utf-8"))
        except Exception:
            return {"memories": []}

    def _save_data(self, data: dict) -> None:
        self._file_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _get_timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def _cleanup_if_needed(self, user_id: str) -> None:
        if self.max_memories_per_user <= 0:
            return
        data = self._load_data()
        memories = data.get("memories", [])
        user_memories = [m for m in memories if m.get("user_id") == str(user_id)]
        if len(user_memories) <= self.max_memories_per_user:
            return

        user_memories.sort(key=lambda x: x.get("updated_at", ""))
        remove_count = len(user_memories) - self.max_memories_per_user
        remove_ids = {m["id"] for m in user_memories[:remove_count]}
        memories = [m for m in memories if m.get("id") not in remove_ids]
        self._save_data({"memories": memories})

    async def add_memory(self, user_id: str, content: str, tags: list = None, importance: int = 5) -> str:
        async with self._lock:
            data = self._load_data()
            memories = data.get("memories", [])
            memory_id = str(uuid.uuid4())[:8]
            now = self._get_timestamp()
            memories.append(
                {
                    "id": memory_id,
                    "user_id": str(user_id),
                    "content": content,
                    "tags": tags or [],
                    "importance": max(1, min(10, importance)),
                    "created_at": now,
                    "updated_at": now,
                }
            )
            self._save_data({"memories": memories})
            await self._cleanup_if_needed(user_id)
            return memory_id

    async def update_memory(
        self,
        memory_id: str,
        content: str = None,
        tags: list = None,
        importance: int = None,
    ) -> bool:
        async with self._lock:
            data = self._load_data()
            memories = data.get("memories", [])
            for memory in memories:
                if memory.get("id") != memory_id:
                    continue
                if content is not None:
                    memory["content"] = content
                if tags is not None:
                    memory["tags"] = tags
                if importance is not None:
                    memory["importance"] = max(1, min(10, importance))
                memory["updated_at"] = self._get_timestamp()
                self._save_data({"memories": memories})
                return True
            return False

    async def delete_memory(self, memory_id: str) -> bool:
        async with self._lock:
            data = self._load_data()
            memories = data.get("memories", [])
            old_len = len(memories)
            memories = [m for m in memories if m.get("id") != memory_id]
            if len(memories) == old_len:
                return False
            self._save_data({"memories": memories})
            return True

    async def get_memories(
        self,
        user_id: str = None,
        keyword: str = None,
        limit: int = 10,
        sort_by: str = "updated_at",
    ) -> List[dict]:
        data = self._load_data()
        memories = data.get("memories", [])
        if user_id:
            memories = [m for m in memories if m.get("user_id") == str(user_id)]
        if keyword:
            key = keyword.lower()
            memories = [
                m
                for m in memories
                if key in m.get("content", "").lower()
                or any(key in str(tag).lower() for tag in m.get("tags", []))
            ]
        if sort_by == "importance":
            memories.sort(key=lambda x: x.get("importance", 0), reverse=True)
        elif sort_by in {"updated_at", "created_at"}:
            memories.sort(key=lambda x: x.get(sort_by, ""), reverse=True)
        return memories[:limit]

    async def get_memory_by_id(self, memory_id: str) -> Optional[dict]:
        memories = await self.get_memories(limit=10000)
        for memory in memories:
            if memory.get("id") == memory_id:
                return memory
        return None


def _load_schema_defaults() -> dict:
    """Load default values from local _conf_schema_config.json when available."""
    cfg_path = Path(__file__).with_name("_conf_schema_config.json")
    if not cfg_path.exists():
        return {}

    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}

        defaults = {}

        def _collect_defaults(node: dict) -> None:
            for key, meta in node.items():
                if not isinstance(meta, dict):
                    continue
                if "default" in meta:
                    defaults[key] = meta["default"]
                items = meta.get("items")
                if isinstance(items, dict):
                    _collect_defaults(items)

        _collect_defaults(raw)
        return defaults
    except Exception as e:
        logger.warning(f"读取 _conf_schema_config.json 失败，忽略默认配置: {e}")
        return {}


def _extract_grouped_runtime_config(raw: dict) -> dict:
    """只读取新的分组配置结构，并拍平成运行时键值；本次更新补充支持 interaction 配置组。"""
    if not isinstance(raw, dict):
        return {}

    incoming = {}

    for key in (
        "enable_weather",
        "enable_search",
        "enable_history",
        "enable_fetch_url",
    ):
        if key in raw:
            incoming[key] = raw.get(key)

    weather_cfg = raw.get("weather", {})
    if isinstance(weather_cfg, dict):
        for key in (
            "qweather_key",
            "qweather_jwt_token",
            "qweather_weather_host",
            "qweather_geo_host",
            "enable_weather_summary",
            "weather_summary_prompt",
            "weather_summary_llm_provider_id",
        ):
            if key in weather_cfg:
                incoming[key] = weather_cfg.get(key)

    search_cfg = raw.get("search", {})
    if isinstance(search_cfg, dict):
        for key in ("zhipu_key", "zhipu_search_model", "zhipu_search_intent"):
            if key in search_cfg:
                incoming[key] = search_cfg.get(key)

    web_fetch_cfg = raw.get("web_fetch", {})
    if isinstance(web_fetch_cfg, dict):
        for key in (
            "enable_fetch_url",
            "fetch_url_max_chars",
            "fetch_url_blocked_targets",
            "fetch_url_max_redirects",
            "fetch_url_over_limit_mode",
            "fetch_url_summary_prompt",
            "fetch_url_summary_llm_provider_id",
            "fetch_url_max_download_bytes",
        ):
            if key in web_fetch_cfg:
                incoming[key] = web_fetch_cfg.get(key)

    interaction_cfg = raw.get("interaction", {})
    if isinstance(interaction_cfg, dict):
        for key in (
            "enable_keyword_capture_reply",
            "keyword_capture_words",
            "keyword_capture_reply_probability",
        ):
            if key in interaction_cfg:
                incoming[key] = interaction_cfg.get(key)

    memory_cfg = raw.get("memory", {})
    if isinstance(memory_cfg, dict):
        for key in (
            "max_memories_per_user",
            "enable_admin_tool_memory_command",
            "memory_inject_enabled",
            "memory_inject_count",
        ):
            if key in memory_cfg:
                incoming[key] = memory_cfg.get(key)

    if "summary_prompt" in raw:
        incoming["summary_prompt"] = raw.get("summary_prompt")

    return incoming

@register("astrbot_plugin_toolbox_for_koko", "coco", "多功能工具箱", "0.3.0", "https://github.com/coco292931/astrbot_plugin_toolbox_for_koko")
class ToolboxPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        schema_defaults = _load_schema_defaults()
        incoming = _extract_grouped_runtime_config(config if isinstance(config, dict) else {})
        merged = dict(schema_defaults)
        for key, value in incoming.items():
            # None / 空字符串按“未提供”处理，避免覆盖配置文件默认值
            if value is None:
                continue
            if isinstance(value, str) and value == "":
                continue
            merged[key] = value
        self.config = merged

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
        self.enable_fetch_url = self.config.get("enable_fetch_url", True)
        self.enable_keyword_capture_reply = self._safe_bool(
            self.config.get("enable_keyword_capture_reply", False), False
        )
        self.keyword_capture_reply_probability = self._safe_float(
            self.config.get("keyword_capture_reply_probability", 0.7),
            0.7,
            0.0,
            1.0,
        )
        self.keyword_capture_words = self._parse_keywords(
            self.config.get("keyword_capture_words", [])
        )

        # 网页抓取配置
        self.fetch_url_max_chars = self._safe_int(self.config.get("fetch_url_max_chars"), 6000, 200, 200000)
        self.fetch_url_over_limit_mode = str(self.config.get("fetch_url_over_limit_mode", "truncate") or "truncate").strip().lower()
        if self.fetch_url_over_limit_mode not in {"truncate", "ai_summary", "full"}:
            self.fetch_url_over_limit_mode = "truncate"
        summary_prompt_default = "请你作为一名资深气象分析师，根据系统提供的多日天气数据，生成一份简短、口语化、亲切友好的天气趋势总结。"
        fetch_url_summary_prompt_default = "请根据以下网页正文提炼关键信息，给出准确、简洁的中文总结。"
        self.summary_prompt = self.config.get(
            "summary_prompt",
            self.config.get("weather_summary_prompt", summary_prompt_default),
        )
        self.fetch_url_summary_prompt = self.config.get(
            "fetch_url_summary_prompt",
            fetch_url_summary_prompt_default,
        )
        self.fetch_url_summary_llm_provider_id = self.config.get("fetch_url_summary_llm_provider_id", "")
        self.fetch_url_blocked_targets = self._parse_blocked_targets(
            self.config.get("fetch_url_blocked_targets", [])
        )
        # 默认放宽到 6MB，并允许按配置上调（上限 30MB），提升长文抓取成功率。
        self.fetch_url_max_download_bytes = self._safe_int(self.config.get("fetch_url_max_download_bytes", 6 * 1024 * 1024), 6 * 1024 * 1024, 500_000, 30 * 1024 * 1024)
        self.fetch_url_max_redirects = self._safe_int(self.config.get("fetch_url_max_redirects", 4), 4, 0, 10)

        # 构建工具注册表（用于 call-search-run 三段式调用）
        self._tool_registry = self._build_tool_registry()
        
        # 7日天气压缩大模型设定的指令
        self.enable_weather_summary = self.config.get("enable_weather_summary", True)
        self.weather_summary_prompt = self.summary_prompt
        self.weather_summary_llm_provider_id = self.config.get("weather_summary_llm_provider_id", "")

        # 历史消息本地分页缓存
        self._history_cache_ttl_seconds = 1200 # 20 minutes
        self._history_pagination_cache = {}

        # 记忆存储
        self.data_dir = Path(__file__).with_name("data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        max_memories_per_user = self._safe_int(self.config.get("max_memories_per_user", 100), 100, 1, 10000)
        self.memory_manager = MemoryManager(self.data_dir, max_memories_per_user)
        self.enable_admin_tool_memory_command = self._safe_bool(
            self.config.get("enable_admin_tool_memory_command", True),
            True,
        )
        self.memory_inject_enabled = self._safe_bool(
            self.config.get("memory_inject_enabled", True),
            True,
        )
        self.memory_inject_count = self._safe_int(
            self.config.get("memory_inject_count", 5),
            5,
            1,
            20,
        )

        # 联系人缓存（用于自动识别发消息目标类型）
        self._groups_cache: List[dict] = []
        self._friends_cache: List[dict] = []
        self._cache_time = 0.0
        self._cache_expire = 300
        self._cache_lock = asyncio.Lock()

    def _safe_int(self, value, default: int, min_v: int, max_v: int) -> int:
        try:
            num = int(value)
        except Exception:
            return default
        return max(min_v, min(num, max_v))

    def _safe_bool(self, value, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "yes", "y", "on"}:
                return True
            if text in {"0", "false", "no", "n", "off"}:
                return False
        return default

    def _parse_llm_compress_mode(self, value) -> str | None:
        if value is None:
            return "inherit"
        if isinstance(value, str):
            mode = value.strip().lower()
            if mode in {"inherit", "summary", "truncate"}:
                return mode
        return None

    def _resolve_summary_instruction(self, args: dict) -> str:
        """生成天气总结指令，支持通过 focus 传入附加关注点。"""
        focus_text = ""
        if isinstance(args, dict):
            focus_text = str(args.get("focus", "")).strip()
        if not focus_text:
            return self.weather_summary_prompt

        if len(focus_text) > 120:
            focus_text = focus_text[:120]

        return (
            f"{self.weather_summary_prompt}\n"
            f"。请优先围绕该关注点: {focus_text}\n"
            "组织总结报告；若与原始数据冲突，优先以原始数据为准。"
        )

    def _extract_llm_text(self, llm_resp: Any) -> str:
        """提取可展示文本，避免透传包含推理/原始响应等敏感字段的对象。"""
        if llm_resp is None:
            return ""

        if isinstance(llm_resp, str):
            return llm_resp

        if isinstance(llm_resp, list):
            parts = [self._extract_llm_text(item) for item in llm_resp]
            return "\n".join([p for p in parts if p]).strip()

        if isinstance(llm_resp, dict):
            for key in ("text", "content", "message", "result"):
                value = llm_resp.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            return ""

        result_chain = getattr(llm_resp, "result_chain", None)
        if result_chain is not None:
            chain = getattr(result_chain, "chain", None)
            if isinstance(chain, list):
                parts = []
                for comp in chain:
                    text = getattr(comp, "text", None)
                    if isinstance(text, str) and text:
                        parts.append(text)
                if parts:
                    return "\n".join(parts).strip()

        for attr in ("text", "content", "message", "result"):
            value = getattr(llm_resp, attr, None)
            if isinstance(value, str) and value.strip():
                return value

        return ""

    def _safe_float(self, value, default: float, min_v: float, max_v: float) -> float:
        try:
            num = float(value)
        except (TypeError, ValueError):
            return default
        return max(min_v, min(num, max_v))

    def _parse_keywords(self, raw_value: Any) -> list[str]:
        """解析关键词列表，仅接受 list[str]。"""
        if not isinstance(raw_value, list):
            return []
        items = [str(v).strip() for v in raw_value if str(v).strip()]
        # 去重并保持顺序
        return list(dict.fromkeys(items))

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE,
        priority=99,
    )
    async def keyword_capture_reply_handler(
        self, event: AstrMessageEvent, *args: Any, **kwargs: Any
    ):
        """关键词捕捉回复：命中关键词后按概率直接调用大模型回复用户原消息。"""
        try:
            if not self.enable_keyword_capture_reply:
                return

            # Ignore the bot's own messages to avoid responding to itself.
            if event.get_sender_id() == event.get_self_id():
                return

            message_text = (event.get_message_outline() or "").strip()
            if not message_text:
                return

            if not self.keyword_capture_words:
                return

            if not any(word in message_text for word in self.keyword_capture_words):
                return

            roll = random.random()
            if roll > self.keyword_capture_reply_probability:
                logger.debug(
                    "[keyword_capture_reply] 关键词命中但未通过概率门限: "
                    f"roll={roll:.4f}, p={self.keyword_capture_reply_probability:.4f}"
                )
                return

            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(event.unified_msg_origin)
            if not curr_cid:
                curr_cid = await conv_mgr.new_conversation(
                    event.unified_msg_origin,
                    platform_id=event.get_platform_id(),
                )

            conversation = None
            if curr_cid:
                conversation = await conv_mgr.get_conversation(
                    event.unified_msg_origin,
                    curr_cid,
                )

            # 直接使用用户原消息作为 prompt，不做提示词注入/替换。
            yield event.request_llm(
                prompt=message_text,
                session_id=curr_cid or "",
                conversation=conversation,
            )
            event.stop_event()
        except Exception as e:
            logger.debug(f"[keyword_capture_reply] 处理失败: {e}")

    def _parse_blocked_targets(self, raw_value) -> list[str]:
        """解析配置中的禁用目标列表，支持 host/ip 的 list 或 JSON 字符串数组。"""
        items = []
        if isinstance(raw_value, list):
            items = raw_value
        elif isinstance(raw_value, str):
            raw_text = raw_value.strip()
            if raw_text:
                try:
                    parsed = json.loads(raw_text)
                    if isinstance(parsed, list):
                        items = parsed
                    else:
                        items = [v.strip() for v in raw_text.split(",") if v.strip()]
                except Exception:
                    items = [v.strip() for v in raw_text.split(",") if v.strip()]

        valid_targets = []
        for item in items:
            target_text = str(item).strip().lower().rstrip(".")
            if not target_text:
                continue
            try:
                valid_targets.append(str(ipaddress.ip_address(target_text)))
            except ValueError:
                valid_targets.append(target_text)

        # 去重并保持顺序
        return list(dict.fromkeys(valid_targets))

    def _build_tool_registry(self) -> dict:
        """构建工具注册表，统一工具描述、参数定义、关键词与处理函数。"""
        registry = {}

        if self.enable_weather:
            registry["tool_weather_location"] = {
                "name": "tool_weather_location",
                "description": "查询城市/区域位置编码（Location ID）。建议先用该工具再调用 tool_weather 或 tool_weather_history。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "位置关键词，支持城市名、经纬度、LocationID、Adcode，必填。例如：杭州、116.41,39.92、101210101"},
                        "number": {"type": "integer", "description": "返回候选数量，1-20，默认10"},
                        "adm": {"type": "string", "description": "附加行政区过滤，可选"},
                        "range": {"type": "string", "description": "搜索范围，可选"},
                        "lang": {"type": "string", "description": "返回语言，默认zh"}
                    },
                    "required": ["location"]
                },
                "keywords": ["天气", "城市编码", "location", "地理查询", "地区", "城市", "weather location"],
                "handler": self._run_tool_weather_location,
            }

            registry["tool_weather"] = {
                "name": "tool_weather",
                "description": "获取实时/3日/7日天气或生活指数。location 建议使用 tool_weather_location 的 id。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "Location ID，必填。建议来自 tool_weather_location 返回的 id，或者以英文逗号分隔的经度,纬度坐标如 116.41,39.92"},
                        "query_type": {"type": "string", "description": "查询类型：now(实时)、3d(3日)、7d(7日)、indices_1d(今日生活指数)、indices_3d(3日生活指数)，默认now"},
                        "full_7d": {"type": "boolean", "description": "仅在 query_type=7d 时生效。true 返回全量原始数据，false 返回精简总结（默认）"},
                        "focus": {"type": "string", "description": "可选。总结关注点，例如：穿衣建议、是否需要带伞"}
                    },
                    "required": ["location"]
                },
                "keywords": ["天气", "实时天气", "天气预报", "生活指数", "7日天气", "weather"],
                "handler": self._run_tool_weather,
            }

            registry["tool_weather_history"] = {
                "name": "tool_weather_history",
                "description": "查询历史天气或历史空气质量（不含今天，最多回溯10天）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "Location ID，必填。建议来自 tool_weather_location 返回的 id"},
                        "history_type": {"type": "string", "description": "历史类型：weather(历史天气，默认) 或 air(历史空气质量)"},
                        "days": {"type": "integer", "description": "回溯天数，1-10，默认1"},
                        "full_history": {"type": "boolean", "description": "true 返回全量历史数据，false 返回精简总结，>3d时默认返回精简总结"},
                        "focus": {"type": "string", "description": "可选。总结关注点，例如：穿衣建议、是否需要带伞"}
                    },
                    "required": ["location"]
                },
                "keywords": ["历史天气", "空气质量", "history", "weather history", "AQI", "历史空气", "天气"],
                "handler": self._run_tool_weather_history,
            }

        if self.enable_search:
            registry["tool_search"] = {
                "name": "tool_search",
                "description": "执行联网搜索并返回摘要或来源内容。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词，必填"},
                        "engine": {"type": "string", "description": "搜索引擎：search_std(默认) 或 search_pro_quark(复杂问题/强时效)"},
                        "content_size": {"type": "string", "description": "内容粒度：lite(摘要)、medium(摘要+来源信息)、high(摘要+来源全文)；默认lite"},
                        "time_filter": {"type": "string", "description": "时间过滤：noLimit、oneDay、oneWeek、oneMonth、oneYear"},
                        "count": {"type": "integer", "description": "结果数量，1-20，默认10"}
                    },
                    "required": ["query"]
                },
                "keywords": ["搜索", "联网", "查资料", "网页搜索", "search", "web"],
                "handler": self._run_tool_search,
            }

        if self.enable_fetch_url:
            registry["tool_fetch_url"] = {
                "name": "tool_fetch_url",
                "description": "抓取单个网页正文文本。适合对指定 URL 做内容提取。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "网页URL，必须以 http:// 或 https:// 开头"},
                        "skip_filter": {"type": "boolean", "description": "开关：false(默认)=增强抓取逻辑；true=原版逻辑。"},
                        "llm_compress": {
                            "type": "string",
                            "enum": ["inherit", "summary", "truncate"],
                            "description": "可选覆盖项：inherit=按用户配置(默认)；summary=超长时强制 LLM 压缩；truncate=超长时强制截断。"
                        }
                    },
                    "required": ["url"]
                },
                "keywords": ["搜索", "抓取网页", "网页正文", "url", "fetch", "extract"],
                "handler": self._run_tool_fetch_url,
            }

        if self.enable_history:
            registry["tool_history"] = {
                "name": "tool_history",
                "description": "获取群聊或好友历史消息记录。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "description": "查询模式：group(群聊) 或 friend(私聊)。不传时按上下文自动推断"},
                        "target_id": {"type": "string", "description": "目标ID：group 模式传群号，friend 模式传用户QQ号。可不传并按当前上下文自动补全"},
                        "page": {"type": "integer", "description": "本地分页页码，默认1"},
                        "refresh": {"type": "boolean", "description": "是否强制刷新历史缓存。true 时忽略旧缓存，从最新数据重新拉取"},
                        "count": {"type": "integer", "description": "每页返回数量（page_size），默认20，范围1-100"}
                    },
                    "required": []
                },
                "keywords": ["聊天", "消息", "历史记录", "历史消息", "聊天记录", "群历史", "私聊历史", "history", "message log"],
                "handler": self._run_tool_history,
            }

        registry["add_memory"] = {
            "name": "add_memory",
            "description": "添加重要记忆到存储中，便于后续检索。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "记忆内容，必填"},
                    "tags": {"type": "string", "description": "标签，多个标签用英文逗号分隔，可选"},
                    "importance": {"type": "integer", "description": "重要程度，1-10，默认5"},
                    "user_id": {"type": "string", "description": "可选，指定记忆所属用户，默认当前会话发送者"},
                },
                "required": ["content"],
            },
            "keywords": ["记忆", "保存记忆", "添加记忆", "备忘", "note", "memory"],
            "handler": self._run_tool_add_memory,
        }

        registry["search_memories"] = {
            "name": "search_memories",
            "description": "搜索已保存记忆，支持关键词和用户范围。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词，可选"},
                    "user_specific": {"type": "boolean", "description": "是否仅搜索当前用户，默认true"},
                    "limit": {"type": "integer", "description": "返回数量，默认10，最大20"},
                    "user_id": {"type": "string", "description": "可选，强制指定查询用户"},
                },
                "required": [],
            },
            "keywords": ["记忆", "搜索记忆", "查找记忆", "记忆列表", "recall", "memory"],
            "handler": self._run_tool_search_memories,
        }

        registry["update_memory"] = {
            "name": "update_memory",
            "description": "更新记忆内容、标签或重要度。",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "记忆ID，必填"},
                    "content": {"type": "string", "description": "新内容，可选"},
                    "tags": {"type": "string", "description": "新标签，逗号分隔，可选"},
                    "importance": {"type": "integer", "description": "新重要度，1-10，可选"},
                },
                "required": ["memory_id"],
            },
            "keywords": ["记忆", "更新记忆", "修改记忆", "edit memory"],
            "handler": self._run_tool_update_memory,
        }

        registry["delete_memory"] = {
            "name": "delete_memory",
            "description": "删除指定记忆。",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "记忆ID，必填"},
                },
                "required": ["memory_id"],
            },
            "keywords": ["记忆", "删除记忆", "清除记忆", "forget", "remove note"],
            "handler": self._run_tool_delete_memory,
        }

        registry["get_memory_detail"] = {
            "name": "get_memory_detail",
            "description": "获取单条记忆详情。",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "记忆ID，必填"},
                },
                "required": ["memory_id"],
            },
            "keywords": ["记忆", "记忆详情", "查看记忆", "memory detail"],
            "handler": self._run_tool_get_memory_detail,
        }

        registry["send_message"] = {
            "name": "send_message",
            "description": "立即向指定QQ好友或群聊发送文本消息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string", "description": "目标QQ号或群号，必填"},
                    "message": {"type": "string", "description": "消息内容，必填"},
                    "chat_type": {"type": "string", "description": "聊天类型：group/private/auto，默认auto"},
                },
                "required": ["target_id", "message"],
            },
            "keywords": ["发消息", "发送消息", "私聊", "群发", "message", "消息"],
            "handler": self._run_tool_send_message,
        }

        return registry

    def _get_available_tools(self) -> dict:
        """返回当前启用状态下可用的工具。"""
        return dict(self._tool_registry)

    def _get_wyc_plugin_instance(self):
        candidate_names = [
            "astrbot_plugin_qzone_tools",
            "更多koko工具",
            "Qzone核心工具",
        ]
        for plugin_name in candidate_names:
            try:
                meta = self.context.get_registered_star(plugin_name)
            except Exception:
                meta = None
            if meta and getattr(meta, "star_cls", None):
                return meta.star_cls

        try:
            all_stars = self.context.get_all_stars()
        except Exception:
            all_stars = []

        for meta in all_stars:
            module_path = str(getattr(meta, "module_path", "") or "")
            star_name = str(getattr(meta, "name", "") or "")
            if "qzone_tools" in module_path or "qzone_tools" in star_name:
                star_cls = getattr(meta, "star_cls", None)
                if star_cls:
                    return star_cls
        return None

    async def _forward_search_to_wyc(self, event: AstrMessageEvent, query: str) -> dict | None:
        wyc_plugin = self._get_wyc_plugin_instance()
        if not wyc_plugin:
            return None
        search_fn = getattr(wyc_plugin, "search_wyc_tools", None)
        if not callable(search_fn):
            return None
        try:
            wyc_result = await search_fn(event, query=query)
            if isinstance(wyc_result, dict):
                return wyc_result
        except Exception as e:
            logger.error(f"[search_koko_tools] 转发 search_wyc_tools 失败: {e}")
        return None

    async def _forward_run_to_wyc(self, event: AstrMessageEvent, tool_name: str, args_dict: dict) -> dict | None:
        wyc_plugin = self._get_wyc_plugin_instance()
        if not wyc_plugin:
            return None
        run_fn = getattr(wyc_plugin, "run_wyc_tool", None)
        if not callable(run_fn):
            return None
        try:
            wyc_result = await run_fn(
                event,
                tool_name=tool_name,
                tool_args=json.dumps(args_dict or {}, ensure_ascii=False),
            )
            if isinstance(wyc_result, dict):
                return wyc_result
        except Exception as e:
            logger.error(f"[run_koko_tool] 转发 run_wyc_tool 失败: {e}")
        return None

    async def _run_tool_weather_location(self, event: AstrMessageEvent, args: dict) -> str:
        return await self._handle_location(args)

    async def _run_tool_weather(self, event: AstrMessageEvent, args: dict) -> str:
        return await self._handle_weather(args)

    async def _run_tool_weather_history(self, event: AstrMessageEvent, args: dict) -> str:
        return await self._handle_weather_history(args)

    async def _run_tool_search(self, event: AstrMessageEvent, args: dict) -> str:
        return await self._handle_search(args)

    async def _run_tool_fetch_url(self, event: AstrMessageEvent, args: dict) -> str:
        return await self._handle_fetch_url(args)

    async def _run_tool_history(self, event: AstrMessageEvent, args: dict) -> str:
        return await self._handle_history(event, args)

    async def _run_tool_add_memory(self, event: AstrMessageEvent, args: dict) -> str:
        return await self._handle_add_memory(event, args)

    async def _run_tool_search_memories(self, event: AstrMessageEvent, args: dict) -> str:
        return await self._handle_search_memories(event, args)

    async def _run_tool_update_memory(self, event: AstrMessageEvent, args: dict) -> str:
        return await self._handle_update_memory(args)

    async def _run_tool_delete_memory(self, event: AstrMessageEvent, args: dict) -> str:
        return await self._handle_delete_memory(args)

    async def _run_tool_get_memory_detail(self, event: AstrMessageEvent, args: dict) -> str:
        return await self._handle_get_memory_detail(args)

    async def _run_tool_send_message(self, event: AstrMessageEvent, args: dict) -> str:
        return await self._handle_send_message(event, args)

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

    async def _get_client(self, event: AstrMessageEvent) -> Any:
        if hasattr(event, "bot") and getattr(event.bot, "api", None):
            return getattr(event.bot, "api", None)
        if hasattr(event, "bot") and hasattr(event.bot, "call_action"):
            return event.bot
        return None

    def _validate_target_id(self, target_id: str) -> tuple[bool, str]:
        target = str(target_id or "").strip()
        if not target:
            return False, "目标ID不能为空"
        if not target.isdigit():
            return False, "目标ID必须是纯数字"
        return True, target

    async def _update_contacts_cache(self, client: Any) -> None:
        async with self._cache_lock:
            now = datetime.now().timestamp()
            if now - self._cache_time < self._cache_expire and (self._groups_cache or self._friends_cache):
                return

            try:
                groups_result = await client.call_action("get_group_list")
                if isinstance(groups_result, list):
                    self._groups_cache = groups_result
                elif isinstance(groups_result, dict):
                    self._groups_cache = groups_result.get("data", [])
                else:
                    self._groups_cache = []
            except Exception:
                self._groups_cache = []

            try:
                friends_result = await client.call_action("get_friend_list")
                if isinstance(friends_result, list):
                    self._friends_cache = friends_result
                elif isinstance(friends_result, dict):
                    self._friends_cache = friends_result.get("data", [])
                else:
                    self._friends_cache = []
            except Exception:
                self._friends_cache = []

            self._cache_time = now

    async def _handle_add_memory(self, event: AstrMessageEvent, args: dict) -> str:
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

        memory_id = await self.memory_manager.add_memory(user_id, content, tags_list, importance)
        preview = f"{content[:50]}{'...' if len(content) > 50 else ''}"
        return f"✅ 记忆已保存\nID: {memory_id}\n内容: {preview}"

    async def _handle_search_memories(self, event: AstrMessageEvent, args: dict) -> str:
        keyword = str(args.get("keyword", "") or "").strip()
        user_specific = self._safe_bool(args.get("user_specific", True), True)

        raw_limit = args.get("limit", 10)
        try:
            limit = int(raw_limit)
        except Exception:
            limit = 10
        limit = max(1, min(limit, 20))

        forced_user_id = str(args.get("user_id", "") or "").strip()
        user_id = forced_user_id or (str(event.get_sender_id()) if user_specific else None)
        memories = await self.memory_manager.get_memories(
            user_id=user_id,
            keyword=keyword if keyword else None,
            limit=limit,
        )
        if not memories:
            if keyword:
                return f"📭 未找到包含「{keyword}」的记忆"
            return "📭 暂无记忆"

        lines = [f"📚 找到 {len(memories)} 条记忆："]
        for index, memory in enumerate(memories, 1):
            tags = memory.get("tags", []) or []
            tags_text = f"[{', '.join(tags)}]" if tags else ""
            content = str(memory.get("content", "") or "")
            preview = content[:40] + ("..." if len(content) > 40 else "")
            lines.append(
                f"{index}. [{memory.get('id')}] {preview} "
                f"(重要度:{memory.get('importance', 5)}) {tags_text} - {str(memory.get('updated_at', ''))[:10]}"
            )
        return "\n".join(lines)

    async def _handle_update_memory(self, args: dict) -> str:
        memory_id = str(args.get("memory_id", "") or "").strip()
        if not memory_id:
            return "❌ 参数缺失：请提供要更新的记忆ID。"

        existing = await self.memory_manager.get_memory_by_id(memory_id)
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

        success = await self.memory_manager.update_memory(memory_id, content, tags_list, importance)
        return f"✅ 记忆已更新\nID: {memory_id}" if success else "❌ 更新失败"

    async def _handle_delete_memory(self, args: dict) -> str:
        memory_id = str(args.get("memory_id", "") or "").strip()
        if not memory_id:
            return "❌ 参数缺失：请提供要删除的记忆ID。"

        existing = await self.memory_manager.get_memory_by_id(memory_id)
        if not existing:
            return f"❌ 未找到记忆ID: {memory_id}"

        success = await self.memory_manager.delete_memory(memory_id)
        return f"🗑️ 记忆已删除\nID: {memory_id}" if success else "❌ 删除失败"

    async def _handle_get_memory_detail(self, args: dict) -> str:
        memory_id = str(args.get("memory_id", "") or "").strip()
        if not memory_id:
            return "❌ 参数缺失：请提供记忆ID。"

        memory = await self.memory_manager.get_memory_by_id(memory_id)
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

    async def _handle_send_message(self, event: AstrMessageEvent, args: dict) -> str:
        target_id = str(args.get("target_id", "") or "").strip()
        message = str(args.get("message", "") or "").strip()
        if not target_id or not message:
            return "❌ 参数缺失：请提供目标ID和消息内容。"

        chat_type = str(args.get("chat_type", "auto") or "auto").strip().lower()
        if chat_type not in {"auto", "group", "private"}:
            return "❌ 参数错误：chat_type 仅支持 group/private/auto。"

        is_valid, normalized_target = self._validate_target_id(target_id)
        if not is_valid:
            return f"参数错误: {normalized_target}"

        client = await self._get_client(event)
        if not client or not hasattr(client, "call_action"):
            return "错误：无法获取客户端"

        final_chat_type = chat_type
        if final_chat_type == "auto":
            await self._update_contacts_cache(client)
            is_group = any(str(g.get("group_id")) == normalized_target for g in self._groups_cache)
            final_chat_type = "group" if is_group else "private"

        try:
            if final_chat_type == "group":
                await client.call_action("send_group_msg", group_id=int(normalized_target), message=message)
            else:
                await client.call_action("send_private_msg", user_id=int(normalized_target), message=message)
            return f"✅ 已发送消息到 {normalized_target}"
        except Exception as e:
            return f"发送失败: {str(e)}"

    async def _tidy_text(self, text: str) -> str:
        """清理网页文本，压缩空白。"""
        return " ".join(text.split())

    async def _extract_best_text_from_html(self, html: str) -> str:
        """优先用 readability，失败时回退到原始 HTML 文本提取。"""
        # 1) readability 主路径
        primary_text = ""
        try:
            doc = Document(html)
            summary_html = doc.summary(html_partial=True)
            soup = BeautifulSoup(summary_html, "html.parser")
            primary_text = await self._tidy_text(soup.get_text(" ", strip=True))
        except Exception:
            primary_text = ""

        if primary_text and len(primary_text) >= 120:
            return primary_text

        # 2) 原始 HTML 回退路径
        full_soup = BeautifulSoup(html, "html.parser")
        for tag in full_soup(["script", "style", "noscript", "svg", "canvas"]):
            tag.decompose()
        fallback_text = await self._tidy_text(full_soup.get_text(" ", strip=True))
        if fallback_text:
            return fallback_text

        # 3) 最后兜底：title + description
        title = ""
        if full_soup.title and full_soup.title.string:
            title = full_soup.title.string.strip()

        desc = ""
        meta_candidates = [
            full_soup.find("meta", attrs={"name": "description"}),
            full_soup.find("meta", attrs={"property": "og:description"}),
            full_soup.find("meta", attrs={"name": "twitter:description"}),
        ]
        for meta in meta_candidates:
            if meta and meta.get("content"):
                desc = str(meta.get("content")).strip()
                if desc:
                    break

        combined = await self._tidy_text(f"{title} {desc}".strip())
        return combined

    async def _extract_text_from_json_payload(self, payload: Any) -> str:
        """从 JSON 结构中提取可读文本，适配直接返回 API JSON 的 URL。"""
        text_fields = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    key_lower = str(k).lower()
                    if isinstance(v, str):
                        # 优先收集常见正文/摘要字段。
                        if key_lower in {
                            "title",
                            "name",
                            "summary",
                            "description",
                            "content",
                            "body",
                            "text",
                            "excerpt",
                            "markdown",
                            "html",
                        }:
                            cleaned = " ".join(v.split())
                            if cleaned:
                                text_fields.append(f"{k}: {cleaned}")
                    else:
                        walk(v)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)

        if text_fields:
            return "\n".join(text_fields)

        # 找不到正文相关字段时，回退为可读 JSON 文本。
        try:
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception:
            return str(payload)

    async def _detect_unextractable_page_reason(self, html: str) -> str | None:
        """识别当前抓取链路难以提取正文的页面特征，并返回原因。"""
        lowered = html.lower()

        soup = BeautifulSoup(html, "html.parser")
        body_text = await self._tidy_text(soup.get_text(" ", strip=True))

        # Cloudflare challenge 或类似挑战页（仅在正文几乎为空时才判定，避免误杀可读页面）。
        if ("challenge-platform" in lowered or "__cf$cv$params" in lowered) and len(body_text) < 500:
            return "页面触发了反爬/挑战验证，当前抓取方式无法直接获取正文。"

        app_container = soup.find(id="app")
        app_container_empty = False
        if app_container is not None:
            app_container_text = await self._tidy_text(app_container.get_text(" ", strip=True))
            app_container_empty = len(app_container_text) < 30

        # 典型 SPA 壳页：正文几乎为空，只有 JS 入口脚本。
        has_module_script = bool(soup.find("script", attrs={"type": "module"}))
        if len(body_text) < 80 and app_container_empty and has_module_script:
            return "页面疑似前端渲染(SPA)壳页，原始 HTML 不包含正文内容。"

        return None

    async def _get_from_url_legacy(self, url: str, llm_compress: str = "inherit") -> str:
        """原版 AstrBot web_searcher 的 fetch_url 提取逻辑。"""
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        ]
        headers = {
            "User-Agent": random.choice(user_agents),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    return f"抓取网页失败，状态码: {response.status}"

                html = await response.text(encoding="utf-8", errors="ignore")
                doc = Document(html)
                ret = doc.summary(html_partial=True)
                soup = BeautifulSoup(ret, "html.parser")
                text = await self._tidy_text(soup.get_text())
                if not text:
                    return "网页内容为空或无法提取正文。"
                return await self._process_fetched_text(text, llm_compress=llm_compress)

    async def _validate_fetch_url(self, url: str) -> tuple[bool, str]:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False, "url 必须以 http:// 或 https:// 开头。"
        if not parsed.netloc:
            return False, "url 缺少域名。"

        host = (parsed.hostname or "").strip().lower().rstrip(".")
        if not host:
            return False, "url 域名无效。"

        deny_host = {
            "localhost",
            "metadata.google.internal",
            "metadata.azure.internal",
        }
        deny_targets = set(self.fetch_url_blocked_targets)
        deny_ip_targets = set()
        deny_domain_targets = set()
        for target in deny_targets:
            try:
                deny_ip_targets.add(str(ipaddress.ip_address(target)))
            except ValueError:
                deny_domain_targets.add(str(target).strip().lower().rstrip("."))

        def _host_denied(hostname: str) -> bool:
            if hostname in deny_host or hostname.endswith(".local"):
                return True
            if hostname in deny_domain_targets:
                return True
            for blocked_domain in deny_domain_targets:
                if blocked_domain and hostname.endswith(f".{blocked_domain}"):
                    return True
            return False

        if _host_denied(host):
            return False, "目标地址已被管理员禁止访问。"

        def _bad_ip(ip_str: str) -> bool:
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                return True
            if str(ip) in deny_ip_targets:
                return True
            return (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            )

        try:
            ip_literal = ipaddress.ip_address(host)
            if _bad_ip(str(ip_literal)):
                return False, "目标地址已被管理员禁止访问。"
            return True, ""
        except ValueError:
            pass

        try:
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            if not infos:
                return False, "无法解析目标域名。"
            for info in infos:
                sockaddr = info[4]
                resolved_ip = sockaddr[0]
                if _bad_ip(resolved_ip):
                    return False, "目标地址已被管理员禁止访问。"
        except Exception:
            return False, "无法解析目标域名。"

        return True, ""

    async def _normalize_and_validate_fetch_url(self, url: str) -> tuple[bool, str, str]:
        url_clean = str(url or "").strip()
        ok, err = await self._validate_fetch_url(url_clean)
        if not ok:
            return False, "", err
        return True, url_clean, ""

    async def _process_fetched_text(self, text: str, llm_compress: str = "inherit") -> str:
        if len(text) <= self.fetch_url_max_chars:
            return text

        mode = self.fetch_url_over_limit_mode
        # 默认 inherit 按用户配置；传入 summary/truncate 时按本次调用意图覆盖。
        if llm_compress == "summary":
            mode = "ai_summary"
        elif llm_compress == "truncate":
            mode = "truncate"

        if mode == "full":
            return text

        truncated = text[: self.fetch_url_max_chars]
        if mode == "truncate":
            return f"{truncated}...\n\n[系统提示] 网页正文超长，已按配置截断。"

        provider_id = self.fetch_url_summary_llm_provider_id
        if not provider_id:
            return f"{truncated}...\n\n[系统提示] 未配置 fetch_url_summary_llm_provider_id，已回退为截断输出。"

        try:
            prompt = f"{self.fetch_url_summary_prompt}\n\n网页正文:\n{text}"
            ai_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            ai_text = self._extract_llm_text(ai_resp)
            if ai_text:
                return ai_text
            logger.warning("网页正文 AI 总结返回非文本或空文本，回退为截断输出。")
            return f"{truncated}...\n\n[系统提示] AI 总结返回为空，已回退为截断输出。"
        except Exception:
            logger.warning("网页正文 AI 总结失败，回退为截断输出。")
            return f"{truncated}...\n\n[系统提示] AI 总结失败，已回退为截断输出。"

    async def _get_from_url(
        self,
        url: str,
        use_legacy: bool = False,
        llm_compress: str = "inherit",
    ) -> str:
        """抓取并提取网页正文。"""
        if use_legacy:
            return await self._get_from_url_legacy(url, llm_compress=llm_compress)

        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        ]
        headers = {
            "User-Agent": random.choice(user_agents),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            current_url = url
            html = ""

            async with aiohttp.ClientSession(timeout=timeout) as session:
                for _ in range(self.fetch_url_max_redirects + 1):
                    ok, normalized_url, err = await self._normalize_and_validate_fetch_url(current_url)
                    if not ok:
                        return err

                    async with session.get(normalized_url, headers=headers, allow_redirects=False) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("Location", "")
                            if not location:
                                return "抓取网页失败：重定向地址为空。"
                            current_url = urllib.parse.urljoin(normalized_url, location)
                            continue

                        if response.status != 200:
                            return f"抓取网页失败，状态码: {response.status}"

                        content_type = (response.headers.get("Content-Type") or "").lower()
                        is_json = "application/json" in content_type or "+json" in content_type
                        is_html_or_text = (
                            "text/html" in content_type
                            or "application/xhtml+xml" in content_type
                            or "text/plain" in content_type
                        )
                        if content_type and (not is_json and not is_html_or_text):
                            return f"暂不支持该内容类型: {content_type}"

                        raw = await response.content.read(self.fetch_url_max_download_bytes + 1)
                        if len(raw) > self.fetch_url_max_download_bytes:
                            limit_mb = self.fetch_url_max_download_bytes / (1024 * 1024)
                            return f"网页内容过大，已超过 {limit_mb:.1f} MB 限制。"

                        charset = response.charset or "utf-8"
                        decoded = raw.decode(charset, errors="ignore")

                        if is_json:
                            try:
                                payload = json.loads(decoded)
                            except Exception:
                                return "抓取异常: 返回了 JSON 类型，但解析 JSON 失败。"

                            json_text = await self._extract_text_from_json_payload(payload)
                            if not json_text.strip():
                                return "抓取异常: JSON 返回为空或无可读文本字段。"
                            return await self._process_fetched_text(json_text, llm_compress=llm_compress)

                        html = decoded

                        abnormal_reason = await self._detect_unextractable_page_reason(html)
                        if abnormal_reason:
                            return f"抓取异常: {abnormal_reason}"
                        break
                else:
                    return "抓取网页失败：重定向次数超过限制。"

            text = await self._extract_best_text_from_html(html)
            if not text:
                return "网页内容为空或无法提取正文。"
            return await self._process_fetched_text(text, llm_compress=llm_compress)
        except asyncio.TimeoutError:
            return "抓取网页超时。"
        except aiohttp.ClientError as e:
            return f"抓取网页网络异常: {type(e).__name__} {str(e)}"
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"抓取网页内部异常: {type(e).__name__} {str(e)}"

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
                    return await resp.json(content_type=None)
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
        url = f"https://{host}/geo/v2/city/lookup?{urllib.parse.urlencode(query_pairs)}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    data = await resp.json(content_type=None)
                    if not isinstance(data, dict):
                        return f"GeoAPI 返回了非预期数据格式: {data}"
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
                            + f"\n数据来源: {refer.get('sources')}"
                        )
                    return f"未找到 '{location_kw}' 的位置信息，或参数不符合 GeoAPI 要求。"
        except Exception as e:
            return f"查询位置信息异常: {str(e)}"

    # ---------------- 工具暴露 ----------------
    @llm_tool("search_koko_tools")
    async def search_koko_tools(self, event: AstrMessageEvent, query: str = "", **kwargs) -> dict:
        """【必须优先使用】根据简短关键词搜索匹配工具。兼容 query/keywords 两种入参。"""
        if (not query or not str(query).strip()) and isinstance(kwargs, dict):
            query = str(kwargs.get("query", "") or kwargs.get("keywords", "") or "")
        if not query or not query.strip():
            return {"status": "error", "message": "请提供搜索关键词（参数 query 或 keywords，如“天气”、“搜索”、“历史消息”）。注意本工具不是`搜索网页`工具，也不是`获取历史`消息工具，请传入关键词“搜索”或“历史消息”来获取这两个工具的使用方式。"}

        available_tools = self._get_available_tools()
        query_lower = query.strip().lower()
        matched = []

        for name, meta in available_tools.items():
            keywords = meta.get("keywords", [])
            if (
                query_lower in name.lower()
                or query_lower in meta.get("description", "").lower()
                or any(query_lower in str(kw).lower() for kw in keywords)
            ):
                matched.append(
                    {
                        "name": name,
                        "description": meta.get("description", ""),
                        "parameters": meta.get("parameters", {"type": "object", "properties": {}, "required": []}),
                    }
                )

        if not matched:
            wyc_result = await self._forward_search_to_wyc(event, query.strip())
            if isinstance(wyc_result, dict):
                wyc_message = str(wyc_result.get("message", "") or "").strip()
                if wyc_message:
                    return {
                        "status": "success",
                        "message": (
                            f"koko 工具未命中，已自动转发至 wyc 工具检索。\n"
                            f"{wyc_message}"
                        ),
                        "forwarded_to": "search_wyc_tools",
                        "wyc_result": wyc_result,
                    }
            return {
                "status": "success",
                "message": f"未找到与「{query}」相关的工具，可尝试其他关键词或使用 call_koko_tools 查看全部可用工具。",
            }

        result_lines = [f"🔍 找到 {len(matched)} 个相关工具："]
        for tool in matched[:10]:
            params = tool.get("parameters", {}) if isinstance(tool, dict) else {}
            props = params.get("properties", {}) if isinstance(params, dict) else {}
            param_keys = list(props.keys()) if isinstance(props, dict) else []
            params_text = ", ".join(param_keys) if param_keys else "无"
            result_lines.append(
                f"- {tool['name']}: {tool['description'][:60]}...\n"
                f"  参数: {params_text}"
            )

        return {"status": "success", "message": "\n".join(result_lines), "tools": matched}

    @llm_tool("call_koko_tools")
    async def call_koko_tools(self, event: AstrMessageEvent, **kwargs) -> dict:
        """返回当前可用工具列表（名称 + 描述 + 参数要点）。仅当 search_koko_tools 未找到时使用。"""
        available_tools = self._get_available_tools()
        if not available_tools:
            return {"status": "success", "message": "当前配置下没有启用任何工具。", "tool_names": []}

        tools_list = []
        for name, meta in available_tools.items():
            params = meta.get("parameters", {})
            required = params.get("required", []) if isinstance(params, dict) else []
            properties = params.get("properties", {}) if isinstance(params, dict) else {}
            required_text = "无"
            if required:
                required_text = ", ".join(str(r) for r in required)
            arg_keys = list(properties.keys()) if isinstance(properties, dict) else []
            args_text = ", ".join(arg_keys) if arg_keys else "无"
            tools_list.append(
                f"- {name}: {meta.get('description', '')}\n"
                f"  必填参数: {required_text}\n"
                f"  可用参数: {args_text}"
            )

        msg = "📦 可用工具列表：\n" + "\n".join(tools_list)
        return {"status": "success", "message": msg, "tool_names": list(available_tools.keys())}

    @llm_tool("run_koko_tool")
    async def run_koko_tool(
        self,
        event: AstrMessageEvent,
        tool_name: str = "",
        tool_args: str = "",
        command: str = "",
        args: dict = None,
    ) -> dict:
        """
        执行指定工具。调用顺序建议：先 search_koko_tools，再在必要时 call_koko_tools，最后 run_koko_tool。
        
        Args:
            tool_name(string): 要执行的工具名称，必填
            tool_args(string): 工具参数 JSON 字符串，可选。例如 '{"query": "杭州天气"}'
            command(string): 兼容旧参数名（等价于 tool_name）
            args(object): 兼容旧参数名（等价于 tool_args 解析后的对象）
        """
        name_raw = tool_name or command
        if not name_raw:
            return {
                "status": "error",
                "message": "缺少 tool_name。请先使用 search_koko_tools 查找工具名称。若仍不确定，可用 call_koko_tools 查看完整列表。",
            }

        normalized_name = name_raw.replace("/", "").strip()

        args_dict = {}
        if isinstance(args, dict):
            args_dict = args
        elif tool_args and tool_args.strip():
            try:
                parsed_args = json.loads(tool_args)
                if isinstance(parsed_args, dict):
                    args_dict = parsed_args
                else:
                    return {"status": "error", "message": "tool_args 必须是 JSON 对象字符串。"}
            except json.JSONDecodeError:
                return {"status": "error", "message": "参数格式错误，tool_args 必须是有效 JSON 字符串。"}

        # 兼容调用：允许通过 run_koko_tool 转发执行工具搜索/列表接口。
        if normalized_name == "search_koko_tools":
            query_text = str(args_dict.get("query", "") or "")
            return await self.search_koko_tools(event, query=query_text)

        if normalized_name == "call_koko_tools":
            return await self.call_koko_tools(event)

        available_tools = self._get_available_tools()
        if normalized_name not in available_tools:
            wyc_result = await self._forward_run_to_wyc(event, normalized_name, args_dict)
            if isinstance(wyc_result, dict):
                wyc_status = str(wyc_result.get("status", "success") or "success")
                if wyc_status.lower() != "error":
                    return {
                        "status": "success",
                        "message": "koko 工具未命中，已自动转发至 wyc 工具执行。",
                        "forwarded_to": "run_wyc_tool",
                        "wyc_result": wyc_result,
                    }
            return {
                "status": "error",
                "message": f"无效的工具名称或工具未启用: {name_raw}。请先使用 search_koko_tools 或 call_koko_tools 获取可用工具。",
            }

        handler = available_tools[normalized_name]["handler"]
        try:
            result = await handler(event, args_dict)
            return {"status": "success", "message": result}
        except Exception as e:
            logger.error(f"[run_koko_tool] 执行工具 {normalized_name} 失败: {e}")
            return {"status": "error", "message": f"工具执行出错: {str(e)}"}

    @llm_tool(name="koko_fetch_url")
    async def fetch_website_content(
        self,
        event: AstrMessageEvent,
        url: str,
        skip_filter: bool = False,
        llm_compress: str = "inherit",
    ) -> str:
        """Fetch the content of a website with the given web url.

        Args:
            url(string): The url of the website to fetch content from
            skip_filter(boolean): 开关：false(默认)=增强抓取逻辑；true=原版 fetch_url 逻辑。
            llm_compress(string): 可选覆盖项：inherit=按用户配置(默认)；summary=超长时强制 LLM 压缩；truncate=超长时强制截断。

        """
        if not self.enable_fetch_url:
            return "网页抓取功能已被禁用。"

        ok, normalized_url, err = await self._normalize_and_validate_fetch_url(url)
        if not ok:
            return err
        llm_compress_mode = self._parse_llm_compress_mode(llm_compress)
        if llm_compress_mode is None:
            return "llm_compress 参数无效：仅支持 inherit、summary、truncate。"
        return await self._get_from_url(
            normalized_url,
            use_legacy=self._safe_bool(skip_filter, False),
            llm_compress=llm_compress_mode,
        )

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, request: Any, *args, **kwargs) -> None:
        try:
            guide_text = (
                "[重要工具使用规范] 当你需要调用本能力时，必须遵循以下顺序：\n"
                "1. 先调用 search_koko_tools，并传入简短关键词（如：天气、搜索、历史消息、网页抓取、记忆、发消息）。\n"
                "2. 若 search_koko_tools 没找到，再调用 call_koko_tools 查看完整可用工具列表和参数要点。\n"
                #"2.5. 若所需工具不在列表中，且更换关键词后仍然无果，则尝试使用 search_wyc_tools 重复上述2步。"
                "3. 确认工具名后，调用 run_koko_tool，并使用 tool_name + tool_args(JSON字符串)。\n"
                "禁止跳过搜索直接猜测工具名。"
            )
            #guide_text = () # 故意的，别删

            if hasattr(request, "system_prompt") and request.system_prompt:
                if guide_text not in request.system_prompt:
                    request.system_prompt += f"\n{guide_text}\n"
            elif hasattr(request, "system_prompt"):
                request.system_prompt = guide_text + "\n"

            if self.memory_inject_enabled and hasattr(request, "system_prompt"):
                try:
                    user_id = str(event.get_sender_id() or "").strip()
                except Exception:
                    user_id = ""

                if user_id:
                    memories = await self.memory_manager.get_memories(
                        user_id=user_id,
                        limit=self.memory_inject_count,
                        sort_by="updated_at",
                    )
                    if memories:
                        memory_lines = []
                        for idx, memory in enumerate(memories, 1):
                            content = str(memory.get("content", "") or "").strip()
                            if not content:
                                continue
                            importance = memory.get("importance", 5)
                            tags = memory.get("tags", []) or []
                            tags_text = f" [{', '.join(tags)}]" if tags else ""
                            memory_lines.append(
                                f"{idx}. {content}{tags_text} (重要度:{importance})"
                            )

                        if memory_lines:
                            memory_block = (
                                f"[用户历史记忆] 该用户({user_id})的重要信息："
                                + "\n".join(memory_lines)
                            )
                            if request.system_prompt:
                                request.system_prompt += f"\n{memory_block}\n"
                            else:
                                request.system_prompt = memory_block + "\n"
        except Exception as e:
            logger.debug(f"[on_llm_request] 注入工具使用规范失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_memory")
    async def admin_tool_memory(self, event: AstrMessageEvent):
        if not self.enable_admin_tool_memory_command:
            await event.send(MessageChain().message("管理员命令 /tool_memory 已被配置禁用"))
            return

        args = event.message_str.strip().split()
        if len(args) < 2:
            await event.send(MessageChain().message("用法：/tool_memory list/add/delete/update/get [参数]"))
            return

        sub = args[1].lower()
        if sub == "list":
            user_id = args[2] if len(args) > 2 else None
            memories = await self.memory_manager.get_memories(user_id=user_id, limit=50)
            if not memories:
                await event.send(MessageChain().message("暂无记忆"))
                return

            lines = [f"记忆列表（共{len(memories)}条）"]
            for memory in memories:
                content = str(memory.get("content", "") or "")
                lines.append(
                    f"{memory.get('id')} | {memory.get('user_id')} | {content[:30]} | 重要:{memory.get('importance', 5)}"
                )
            await event.send(MessageChain().message("\n".join(lines)))
            return

        if sub == "add":
            if len(args) < 3:
                await event.send(MessageChain().message("用法：/tool_memory add <内容> [标签] [重要度]"))
                return
            content = args[2]
            tags = args[3] if len(args) > 3 else ""
            importance = int(args[4]) if len(args) > 4 and args[4].isdigit() else 5
            tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
            memory_id = await self.memory_manager.add_memory("admin", content, tags_list, importance)
            await event.send(MessageChain().message(f"记忆已添加，ID: {memory_id}"))
            return

        if sub == "delete":
            if len(args) < 3:
                await event.send(MessageChain().message("用法：/tool_memory delete <记忆ID>"))
                return
            memory_id = args[2]
            success = await self.memory_manager.delete_memory(memory_id)
            await event.send(MessageChain().message("记忆已删除" if success else "删除失败"))
            return

        if sub == "update":
            if len(args) < 3:
                await event.send(MessageChain().message("用法：/tool_memory update <记忆ID> [新内容] [新标签] [新重要度]"))
                return
            memory_id = args[2]
            content = args[3] if len(args) > 3 else None
            tags = args[4] if len(args) > 4 else None
            importance = int(args[5]) if len(args) > 5 and args[5].isdigit() else None
            tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags is not None else None
            success = await self.memory_manager.update_memory(memory_id, content, tags_list, importance)
            await event.send(MessageChain().message("记忆已更新" if success else "更新失败"))
            return

        if sub == "get":
            if len(args) < 3:
                await event.send(MessageChain().message("用法：/tool_memory get <记忆ID>"))
                return
            memory = await self.memory_manager.get_memory_by_id(args[2])
            if not memory:
                await event.send(MessageChain().message("未找到记忆"))
                return
            lines = [
                f"ID: {memory.get('id')}",
                f"用户: {memory.get('user_id')}",
                f"内容: {memory.get('content')}",
                f"标签: {', '.join(memory.get('tags', []))}",
                f"重要度: {memory.get('importance', 5)}",
                f"创建: {memory.get('created_at')}",
                f"更新: {memory.get('updated_at')}",
            ]
            await event.send(MessageChain().message("\n".join(lines)))
            return

        await event.send(MessageChain().message("未知子命令，可用: list, add, delete, update, get"))

    async def _handle_weather(self, args: dict) -> str:
        if not self.enable_weather:
            return "天气查询功能已被禁用。"
        if not self.qweather_jwt_token and not self.qweather_key:
            return "缺失 QWeather 认证配置，请提供 qweather_jwt_token 或 qweather_key。"

        location_id = args.get("location", "") or args.get("location_id", "")
        if not location_id:
            return "缺少 location 参数，请先调用 tool_weather_location 获取 Location ID。"

        query_type = args.get("query_type", "now")
        full_7d = self._safe_bool(args.get("full_7d", False), False)

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
        summary_instruction = self._resolve_summary_instruction(args)

        try:
            data = await self._fetch_qweather(valid_types[query_type], location_id, extra)
            if data.get("code") != "200":
                return f"QWeather API 返回错误码: {data.get('code')}"
            
            # --- 7日天气处理 ---
            if query_type == "7d" and not full_7d and self.enable_weather_summary:
                if self.weather_summary_llm_provider_id:
                    try:
                        raw_payload = json.dumps(data, ensure_ascii=False)
                        prompt = (
                            f"{summary_instruction}\n\n"
                            "以下是天气接口返回的原始JSON数据（未经修改或删减），请直接基于该原始数据总结：\n"
                            f"{raw_payload}"
                        )
                        ai_resp = await self.context.llm_generate(
                            chat_provider_id=self.weather_summary_llm_provider_id,
                            prompt=prompt,
                        )
                        ai_text = self._extract_llm_text(ai_resp)
                        if ai_text:
                            return ai_text
                        logger.warning("7日天气LLM压缩返回非文本或空文本，回退为本地精简文本。")
                    except Exception:
                        logger.warning("7日天气LLM压缩失败，回退为本地精简文本。")

                summary_raw = "\n".join([f"{day['fxDate']}: 白天{day['textDay']} 夜间{day['textNight']}, {day['tempMin']}~{day['tempMax']}°C" for day in data.get("daily", [])])
                return f"【系统提示: 已精简7日天气数据】\n{summary_raw}\n【系统行为指令】: {summary_instruction}"
            
            # --- 生活指数处理 ---
            if "indices" in query_type:
                # 仅保留 daily 并在外层添加说明
                daily_indices = data.get("daily", [])
                return "生活指数数据:\n" + json.dumps(daily_indices, ensure_ascii=False)
                
            return json.dumps(data, ensure_ascii=False)
            
        except Exception as e:
            return f"天气查询内部异常: {str(e)}"

    async def _handle_weather_history(self, args: dict) -> str:
        if not self.enable_weather:
            return "天气查询功能已被禁用。"
        if not self.qweather_jwt_token and not self.qweather_key:
            return "缺失 QWeather 认证配置，请提供 qweather_jwt_token 或 qweather_key。"

        location_id = args.get("location", "") or args.get("location_id", "")
        if not location_id:
            return "缺少 location 参数，请先调用 tool_weather_location 获取 Location ID。"

        history_type = str(args.get("history_type", "weather") or "weather").strip().lower()
        if history_type not in ("weather", "air"):
            return f"【API拒绝】无效的 history_type: {history_type}。仅支持 weather 或 air。"

        days_raw = args.get("days", 1)
        try:
            days = max(1, min(int(days_raw), 10))
        except Exception:
            days = 1

        if "full_history" in args:
            full_history = self._safe_bool(args.get("full_history"), False)
        else:
            # 未显式传 full_history 时，按 args(days)自动决策：>3天默认压缩，<=3天默认全量
            full_history = days <= 3
        # 标准化内部参数：历史天气接口统一使用中文与公制单位，不对外暴露配置。
        lang = "zh"
        unit = "m"
        summary_instruction = self._resolve_summary_instruction(args)

        api_type = "historical/weather" if history_type == "weather" else "historical/air"
        historical_list = []

        try:
            for offset in range(days, 0, -1):
                date_str = (datetime.now() - timedelta(days=offset)).strftime("%Y%m%d")
                extra_parts = [f"date={date_str}"]
                if lang:
                    extra_parts.append(f"lang={urllib.parse.quote(lang)}")
                if history_type == "weather" and unit in ("m", "i"):
                    extra_parts.append(f"unit={unit}")

                extra_historical = "&" + "&".join(extra_parts)
                day_data = await self._fetch_qweather(api_type, location_id, extra_historical)
                if day_data.get("code") != "200":
                    return f"QWeather 历史{('天气' if history_type == 'weather' else '空气')}接口返回错误码: {day_data.get('code')}（date={date_str}）"
                historical_list.append(day_data)

            if not full_history and self.enable_weather_summary:
                if self.weather_summary_llm_provider_id:
                    try:
                        raw_payload = json.dumps(
                            {
                                "history_type": history_type,
                                "days": days,
                                "location": location_id,
                                "historical": historical_list,
                            },
                            ensure_ascii=False,
                        )
                        prompt = (
                            f"{summary_instruction}\n\n"
                            f"以下是最近{days}天(不含今天)的历史{('天气' if history_type == 'weather' else '空气质量')}原始JSON数据（未经修改或删减），请直接基于原始数据总结：\n{raw_payload}"
                        )
                        ai_resp = await self.context.llm_generate(
                            chat_provider_id=self.weather_summary_llm_provider_id,
                            prompt=prompt,
                        )
                        ai_text = self._extract_llm_text(ai_resp)
                        if ai_text:
                            return ai_text
                        logger.warning("历史数据LLM压缩返回非文本或空文本，回退为本地精简文本。")
                    except Exception:
                        logger.warning("历史数据LLM压缩失败，回退为本地精简文本。")

                summary_lines = []
                for day_data in historical_list:
                    if history_type == "weather":
                        weather_daily = day_data.get("weatherDaily", {})
                        day_date = weather_daily.get("date", "未知日期")
                        day_text = ""
                        hourly = day_data.get("weatherHourly", [])
                        if isinstance(hourly, list) and hourly:
                            noon = next((h for h in hourly if isinstance(h, dict) and "12:00" in str(h.get("time", ""))), None)
                            sample = noon if noon else hourly[0]
                            day_text = sample.get("text", "") if isinstance(sample, dict) else ""

                        summary_lines.append(
                            f"{day_date}: {weather_daily.get('tempMin', '?')}~{weather_daily.get('tempMax', '?')}°C, "
                            f"湿度{weather_daily.get('humidity', '?')}%, 降水{weather_daily.get('precip', '?')}mm"
                            + (f", 概况{day_text}" if day_text else "")
                        )
                    else:
                        hourly_air = day_data.get("airHourly", [])
                        if isinstance(hourly_air, list) and hourly_air:
                            date_text = str(hourly_air[0].get("pubTime", "未知时间")).split(" ")[0]
                            aqi_vals = []
                            for h in hourly_air:
                                try:
                                    if isinstance(h, dict) and h.get("aqi") is not None:
                                        aqi_vals.append(int(h.get("aqi")))
                                except Exception:
                                    continue

                            if aqi_vals:
                                avg_aqi = round(sum(aqi_vals) / len(aqi_vals))
                                min_aqi = min(aqi_vals)
                                max_aqi = max(aqi_vals)
                            else:
                                avg_aqi = min_aqi = max_aqi = "?"

                            primary = hourly_air[0].get("primary", "NA") if isinstance(hourly_air[0], dict) else "NA"
                            category = hourly_air[0].get("category", "未知") if isinstance(hourly_air[0], dict) else "未知"
                            summary_lines.append(
                                f"{date_text}: AQI均值{avg_aqi} (范围{min_aqi}-{max_aqi}), 级别{category}, 主要污染物{primary}"
                            )

                summary_raw = "\n".join(summary_lines) if summary_lines else "无可用历史数据摘要。"

                return (
                    f"【系统提示: 已精简最近{days}天历史{('天气' if history_type == 'weather' else '空气质量')}数据】\n"
                    f"{summary_raw}\n"
                    f"【系统行为指令】: {summary_instruction}"
                )

            return json.dumps({
                "code": "200",
                "location": location_id,
                "history_type": history_type,
                "days": days,
                "historical": historical_list,
            }, ensure_ascii=False)
        except Exception as e:
            return f"历史数据查询内部异常: {str(e)}"

    async def _handle_search(self, args: dict) -> str:
        if not self.enable_search:
            return "网络搜索功能已被禁用。"
        if not self.zhipu_key:
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
                    "search_engine": engine,
                    "search_intent": self.config.get("zhipu_search_intent", True),
                    "search_recency_filter": time_filter,
                    "content_size": api_content_size,
                    "count": count
                }
            }]
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
                            return f"搜索请求失败，状态码: {resp.status}。code: {err_code}，message: {err_msg}"
                        except Exception:
                            return f"搜索请求失败，状态码: {resp.status}。细节: {body_text}"
                        
                    data = await resp.json(content_type=None)
                    message = ((data.get("choices") or [{}])[0].get("message") or {})
                    content = message.get("content", "")
                    web_search = data.get("web_search", []) if isinstance(data, dict) else []
                    
                    if content_size == "lite":
                        # 返回极简摘要给模型自己读
                        return f"【极简摘要】\n{content}"
                    elif content_size == "medium":
                        sources = [
                            {
                                "title": w.get("title"),
                                "publish_date": w.get("publish_date"),
                                "media": w.get("media"),
                                "link": w.get("link"),
                            }
                            for w in web_search
                        ]
                        return f"【常规搜索】\n摘要: {content}\n\n参考来源:\n{json.dumps(sources, ensure_ascii=False)}"
                    else:
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
                        return f"【全量搜索汇总】\n摘要: {content}\n\n参考来源:\n{json.dumps(sources, ensure_ascii=False)}"
        except asyncio.TimeoutError as e:
            logger.error(traceback.format_exc())
            detail = str(e).strip() or repr(e)
            return f"搜索请求超时(90s): {detail}"
        except aiohttp.ClientError as e:
            logger.error(traceback.format_exc())
            detail = str(e).strip() or repr(e)
            return f"搜索网络异常({type(e).__name__}): {detail}"
        except Exception as e:
            logger.error(traceback.format_exc())
            detail = str(e).strip() or repr(e)
            return f"搜索内部异常({type(e).__name__}): {detail}"

    async def _handle_fetch_url(self, args: dict) -> str:
        if not self.enable_fetch_url:
            return "网页抓取功能已被禁用。"

        url = str(args.get("url", "") or "").strip()
        if not url:
            return "缺少 url 参数。"
        skip_filter = self._safe_bool(args.get("skip_filter", False), False)

        llm_compress = "inherit"
        if "llm_compress" in args:
            llm_compress = self._parse_llm_compress_mode(args.get("llm_compress"))
            if llm_compress is None:
                return "llm_compress 参数无效：仅支持 inherit、summary、truncate。"

        ok, normalized_url, err = await self._normalize_and_validate_fetch_url(url)
        if not ok:
            return err

        return await self._get_from_url(
            normalized_url,
            use_legacy=skip_filter,
            llm_compress=llm_compress,
        )

    def _history_make_cache_key(self, mode: str, target_id: str, page_size: int) -> str:
        # 不绑定 unified_msg_origin，避免同一目标在不同会话触发时缓存断档。
        return f"{mode}|{target_id}|{page_size}"

    def _history_prune_cache(self) -> None:
        now_ts = int(datetime.now().timestamp())
        expire_before = now_ts - self._history_cache_ttl_seconds
        to_delete = []
        for key, item in self._history_pagination_cache.items():
            updated_at = int(item.get("updated_at", 0)) if isinstance(item, dict) else 0
            if updated_at <= expire_before:
                to_delete.append(key)
        for key in to_delete:
            self._history_pagination_cache.pop(key, None)

    def _history_extract_messages(self, result: Any) -> list[dict]:
        messages = []
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

    def _history_msg_unique_key(self, msg: dict) -> str:
        msg_id = msg.get("message_id")
        msg_seq = msg.get("message_seq")
        time_text = msg.get("time", "")
        sender_id = ""
        sender = msg.get("sender")
        if isinstance(sender, dict):
            sender_id = str(sender.get("user_id", "") or "")
        raw = str(msg.get("raw_message", "") or "")
        return f"id={msg_id}|seq={msg_seq}|t={time_text}|u={sender_id}|raw={raw[:32]}"

    def _history_pick_seq(self, msg: dict) -> int:
        for key in ("message_seq", "message_id"):
            value = msg.get(key)
            try:
                seq_num = int(str(value))
                if seq_num >= 0:
                    return seq_num
            except Exception:
                continue
        return -1

    def _history_format_time(self, msg: dict) -> str:
        ts = msg.get("time")
        try:
            ts_num = int(str(ts))
            if ts_num <= 0:
                return "--:--"
            return datetime.fromtimestamp(ts_num).strftime("%H:%M")
        except Exception:
            return "--:--"

    def _history_sort_key_desc(self, msg: dict) -> tuple[int, int]:
        """用于本地分页排序：按 time、seq 倒序（最新优先）。"""
        ts_num = 0
        try:
            ts_num = int(str(msg.get("time", 0) or 0))
        except Exception:
            ts_num = 0
        seq_num = self._history_pick_seq(msg)
        return ts_num, seq_num

    async def _handle_history(self, event: AstrMessageEvent, args: dict) -> str:
        if not self.enable_history:
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
        refresh = self._safe_bool(args.get("refresh", False), False)

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

        # mode 缺省时按上下文推断。
        if not mode:
            mode = "group" if context_group_id else "friend"

        # target 缺省时按 mode 从上下文补全。
        if not target_id:
            if mode == "group":
                target_id = context_group_id
            else:
                target_id = sender_user_id

        if not target_id:
            if mode == "group":
                return "缺少 target_id：group 模式请提供群号，或在群聊上下文中调用。"
            return "缺少 target_id：friend 模式请提供用户QQ号，或在私聊上下文中调用。"

        self._history_prune_cache()

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

        cache_key = self._history_make_cache_key(mode, target_id, page_size)
        cache = self._history_pagination_cache.get(cache_key)

        # page=1、refresh=true 或缓存缺失时刷新，保证总能重新从最新数据开始。
        if page == 1 or refresh or not isinstance(cache, dict):
            cache = {
                "messages": [],
                "seen": set(),
                "last_fetch_count": 0,
                "exhausted": False,
                "updated_at": int(datetime.now().timestamp()),
            }
            self._history_pagination_cache[cache_key] = cache

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

            while len(cache.get("messages", [])) < needed_end and not cache.get("exhausted", False):
                if fetch_rounds >= 8:
                    break
                fetch_rounds += 1

                # OneBot 端不支持按 seq 翻页时，只能逐步增大 count 拉取更完整窗口。
                fetch_count = max(int(cache.get("last_fetch_count", 0)), 0) + 100
                fetch_count = min(fetch_count, 1000)
                result = await _call_history(fetch_count)
                logger.debug(f"历史消息接口返回: {result}")

                batch_messages = self._history_extract_messages(result)
                if not batch_messages:
                    cache["exhausted"] = True
                    break

                before_count = len(cache["messages"])
                seen = cache.get("seen", set())
                if not isinstance(seen, set):
                    seen = set()

                for msg in batch_messages:
                    unique_key = self._history_msg_unique_key(msg)
                    if unique_key in seen:
                        continue
                    seen.add(unique_key)
                    cache["messages"].append(msg)

                cache["seen"] = seen
                after_count = len(cache["messages"])
                if after_count == before_count:
                    # count 已扩大但没有新增消息，判定已到历史末尾或接口只返回固定窗口。
                    cache["exhausted"] = True
                    break

                cache["last_fetch_count"] = fetch_count
                if len(batch_messages) < fetch_count:
                    cache["exhausted"] = True

                cache["updated_at"] = int(datetime.now().timestamp())

            # 读取缓存也刷新 TTL，避免用户连续翻页时缓存被误清理。
            cache["updated_at"] = int(datetime.now().timestamp())

            all_messages = sorted(
                cache.get("messages", []),
                key=self._history_sort_key_desc,
                reverse=True,
            )
            start_index = (page - 1) * page_size
            end_index = start_index + page_size
            page_messages = all_messages[start_index:end_index]

            if not page_messages:
                if all_messages:
                    return (
                        f"暂无更多历史消息（当前共缓存 {len(all_messages)} 条）。")
                        #"可尝试将 page 设为 1 重新开始。"
                    #)
                return "暂无历史消息记录"

            # 拼装返回摘要文字
            title = f"群 {target_id} 历史消息" if mode == "group" else f"好友 {target_id} 历史消息"
            if refresh:
                title += "（已刷新缓存）"
            lines = [f"{title}（第 {page} 页，每页 {page_size} 条，本地缓存共 {len(all_messages)} 条）："]
            
            for msg in page_messages:
                sender_info = msg.get('sender', {})
                if not isinstance(sender_info, dict):
                    sender_info = {}
                sender = sender_info.get('nickname', sender_info.get('user_id', '未知'))
                time_text = self._history_format_time(msg)

                # 优先获取原始纯文本消息
                raw_msg = msg.get('raw_message', '')
                if not raw_msg:
                    # 如果为空，尝试提取内容里的文本片段
                    for segment in msg.get('message', []):
                        if isinstance(segment, dict) and segment.get('type') == 'text':
                            raw_msg += segment.get('data', {}).get('text', '')
                # 简单格式化与截断过长单条消息
                content = raw_msg[:200].replace('\n', '  ')
                lines.append(f"• [{time_text}] {sender}: {content}")

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
