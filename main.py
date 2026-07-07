import asyncio
import json
import random
import inspect
import ipaddress
from pathlib import Path
from datetime import datetime
from typing import Any, List

from astrbot.api.star import Context, Star, register
from astrbot.api.all import llm_tool
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.platform import MessageType
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .core.config import extract_grouped_runtime_config, load_schema_defaults
from .core.memory_manager import MemoryManager as CoreMemoryManager
from .core.kc_context import KCContextManager
from .core.image_caption import ImageCaptionHandler
from .core.content_audit import ContentAuditLoop

# lazy imports for tools (avoid ModuleNotFoundError when called via LLM tool executor)
from .tools.weather import run_weather_location, run_weather, run_weather_history
from .tools.search import run_search
from .tools.fetch_url import (
    run_fetch_url,
    _get_from_url,
    _normalize_and_validate_fetch_url,
    _parse_llm_compress_mode,
)
from .tools.history import run_history
from .tools.local_memory import (
    run_add_memory,
    run_search_memories,
    run_search_memory_vector,
    run_list_memory_vector,
    run_list_records_memory_vector,
    run_remember_memory_vector,
    run_delete_record_memory_vector,
    run_update_memory,
    run_delete_memory,
    run_get_memory_detail,
)
from .tools.send_msg import run_send_message
from .tools.calendar import run_get_calendar


@register(
    "astrbot_plugin_toolbox_for_koko",
    "coco",
    "多功能工具箱",
    "1.6.7",
    "https://github.com/coco292931/astrbot_plugin_toolbox_for_koko",
)
class ToolboxPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        schema_defaults = load_schema_defaults()
        incoming = extract_grouped_runtime_config(
            config if isinstance(config, dict) else {}
        )
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
        self.qweather_weather_host = self.config.get(
            "qweather_weather_host", "devapi.qweather.com"
        )
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
        self.keyword_capture_base_probability = self._safe_float(
            self.config.get("keyword_capture_base_probability", 0.0),
            0.0,
            0.0,
            1.0,
        )
        self.keyword_capture_whitelist = self._parse_keywords(
            self.config.get("keyword_capture_whitelist", [])
        )
        self.keyword_capture_session_mode = (
            str(self.config.get("keyword_capture_session_mode", "auto_new"))
            .strip()
            .lower()
        )
        self.keyword_capture_manage_context = self._safe_bool(
            self.config.get("keyword_capture_manage_context", False), False
        )
        self.keyword_capture_context_max_cnt = self._safe_int(
            self.config.get("keyword_capture_context_max_cnt", 100), 100, 1, 500
        )
        self.keyword_capture_context_history_limit = self._safe_int(
            self.config.get("keyword_capture_context_history_limit", 50), 50, 1, 500
        )
        self.keyword_capture_context_image_limit = self._safe_int(
            self.config.get("keyword_capture_context_image_limit", 3), 3, 0, 20
        )
        self.keyword_capture_context_prompt = str(
            self.config.get("keyword_capture_context_prompt", "") or ""
        ).strip()
        self.keyword_capture_bypass_probability_on_at = self._safe_bool(
            self.config.get("keyword_capture_bypass_probability_on_at", False), False
        )

        # 群聊上下文管理器（仅管理上下文，不与关键词触发耦合）
        self.kc_context = KCContextManager(self)

        # ---- content_audit 配置加载 ----
        _raw_audit_enabled = self.config.get("content_audit_enabled", "KEY_NOT_FOUND")
        # logger.debug(
        #     f"[content_audit] __init__ 配置检查: "
        #     f"config keys={list(self.config.keys())}, "
        #     f"raw content_audit_enabled={_raw_audit_enabled!r}, "
        #     f"type={type(_raw_audit_enabled).__name__}"
        # )
        self.content_audit_enabled = self._safe_bool(
            self.config.get("content_audit_enabled", False), False
        )
        self.content_audit_rounds = self._safe_int(
            self.config.get("content_audit_rounds", 5), 5, 1, 50
        )
        self.content_audit_fetch_rounds = self._safe_int(
            self.config.get("content_audit_fetch_rounds", 5), 5, 1, 50
        )
        self.content_audit_min_interval = self._safe_int(
            self.config.get("content_audit_min_interval", 2), 2, 0, 50
        )
        self.content_audit_criteria = str(
            self.config.get("content_audit_criteria", "") or ""
        ).strip()
        self.content_audit_keyword_enabled = self._safe_bool(
            self.config.get("content_audit_keyword_enabled", True), True
        )
        self.content_audit_keywords = self._parse_keywords(
            self.config.get("content_audit_keywords", [])
        )
        self.content_audit_debug = self._safe_bool(
            self.config.get("content_audit_debug", False), False
        )
        self.content_audit_inject_mode = (
            str(
                self.config.get("content_audit_inject_mode", "conversation")
                or "conversation"
            )
            .strip()
            .lower()
        )
        if self.content_audit_inject_mode not in ("prompt", "conversation"):
            self.content_audit_inject_mode = "conversation"
        # logger.debug(
        #     f"[content_audit] __init__ 实例化条件: "
        #     f"content_audit_enabled={self.content_audit_enabled}"
        # )

        # ---- persona_audit 配置加载 ----
        self.persona_audit_enabled = self._safe_bool(
            self.config.get("persona_audit_enabled", False), False
        )
        self.persona_audit_rounds = self._safe_int(
            self.config.get("persona_audit_rounds", 5), 5, 1, 50
        )
        self.persona_audit_prompt = str(
            self.config.get("persona_audit_prompt", "") or ""
        ).strip()
        self.persona_audit_inject_mode = (
            str(
                self.config.get("persona_audit_inject_mode", "conversation")
                or "conversation"
            )
            .strip()
            .lower()
        )
        if self.persona_audit_inject_mode not in ("prompt", "conversation"):
            self.persona_audit_inject_mode = "conversation"
        self.use_astrbot_persona = self._safe_bool(
            self.config.get("use_astrbot_persona", False), False
        )
        self.select_persona = str(self.config.get("select_persona", "") or "").strip()
        # logger.debug(
        #     f"[persona_audit] __init__ 配置: "
        #     f"enabled={self.persona_audit_enabled}, "
        #     f"rounds={self.persona_audit_rounds}, "
        #     f"prompt_len={len(self.persona_audit_prompt)}, "
        #     f"use_astrbot_persona={self.use_astrbot_persona}, "
        #     f"select_persona={self.select_persona!r}"
        # )

        if self.content_audit_enabled:
            self.content_audit = ContentAuditLoop(self)
            logger.info("[content_audit] ContentAuditLoop 已实例化")

        # 图片转述前处理（在 on_llm_request 中接管图片转述）
        self.image_caption_hook_enabled = self._safe_bool(
            self.config.get("image_caption_hook_enabled", True), True
        )
        self.image_caption_tool_enabled = self._safe_bool(
            self.config.get("image_caption_tool_enabled", True), True
        )
        self.image_caption_prompt_template = str(
            self.config.get("image_caption_prompt_template", "") or ""
        ).strip()
        self.image_caption_parse_error_keywords = self._parse_string_list(
            self.config.get("image_caption_parse_error_keywords", [])
        )
        self.image_caption_sensitive_fallback_enabled = self._safe_bool(
            self.config.get("image_caption_sensitive_fallback_enabled", False),
            False,
        )
        self.image_caption_sensitive_error_keywords = self._parse_string_list(
            self.config.get("image_caption_sensitive_error_keywords", [])
        )
        self.image_caption_sensitive_fallback_provider_ids = self._parse_string_list(
            self.config.get("image_caption_sensitive_fallback_provider_ids", [])
        )
        self.image_caption_sensitive_fallback_system_prompt = str(
            self.config.get("image_caption_sensitive_fallback_system_prompt", "") or ""
        ).strip()
        self.image_caption_sensitive_fallback_max_tokens = self._safe_int(
            self.config.get("image_caption_sensitive_fallback_max_tokens", 300),
            300,
            32,
            4096,
        )

        self.image_caption_concurrency = self._safe_int(
            self.config.get("image_caption_concurrency", 3),
            3,
            1,
            10,
        )
        self.image_caption_timeout = self._safe_int(
            self.config.get("image_caption_timeout", 120),
            120,
            15,
            600,
        )

        self.image_caption_handler = ImageCaptionHandler(self)

        # 网页抓取配置
        self.fetch_url_max_chars = self._safe_int(
            self.config.get("fetch_url_max_chars"), 6000, 200, 200000
        )
        self.fetch_url_over_limit_mode = (
            str(self.config.get("fetch_url_over_limit_mode", "truncate") or "truncate")
            .strip()
            .lower()
        )
        if self.fetch_url_over_limit_mode not in {"truncate", "ai_summary", "full"}:
            self.fetch_url_over_limit_mode = "truncate"
        summary_prompt_default = "请你作为一名资深气象分析师，根据系统提供的多日天气数据，生成一份简短、口语化、亲切友好的天气趋势总结。"
        fetch_url_summary_prompt_default = (
            "请根据以下网页正文提炼关键信息，给出准确、简洁的中文总结。"
        )
        self.summary_prompt = self.config.get(
            "summary_prompt",
            self.config.get("weather_summary_prompt", summary_prompt_default),
        )
        self.fetch_url_summary_prompt = self.config.get(
            "fetch_url_summary_prompt",
            fetch_url_summary_prompt_default,
        )
        self.fetch_url_summary_llm_provider_id = self.config.get(
            "fetch_url_summary_llm_provider_id", ""
        )
        self.fetch_url_blocked_targets = self._parse_blocked_targets(
            self.config.get("fetch_url_blocked_targets", [])
        )
        # 默认放宽到 6MB，并允许按配置上调（上限 30MB），提升长文抓取成功率。
        self.fetch_url_max_download_bytes = self._safe_int(
            self.config.get("fetch_url_max_download_bytes", 6 * 1024 * 1024),
            6 * 1024 * 1024,
            500_000,
            30 * 1024 * 1024,
        )
        self.fetch_url_max_redirects = self._safe_int(
            self.config.get("fetch_url_max_redirects", 4), 4, 0, 10
        )

        # Calendar 配置
        self.calendar_ical_url = str(
            self.config.get("calendar_ical_url", "") or ""
        ).strip()
        self.calendar_proxy = str(self.config.get("calendar_proxy", "") or "").strip()

        # 构建工具注册表（用于 call-search-run 三段式调用）
        self._tool_registry = self._build_tool_registry()

        # 7日天气压缩大模型设定的指令
        self.enable_weather_summary = self.config.get("enable_weather_summary", True)
        self.weather_summary_prompt = self.summary_prompt
        self.weather_summary_llm_provider_id = self.config.get(
            "weather_summary_llm_provider_id", ""
        )

        # 历史消息本地分页缓存
        self._history_cache_ttl_seconds = 1200  # 20 minutes
        self._history_pagination_cache = {}

        # 记忆存储
        self.data_dir = self._resolve_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        max_memories_per_user = self._safe_int(
            self.config.get("max_memories_per_user", 100), 100, 1, 10000
        )
        self.memory_manager = CoreMemoryManager(self.data_dir, max_memories_per_user)
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

        # Mnemosyne / 向量数据库配置（用于内部向量检索 + Mnemosyne 插件转发）
        self.mnemosyne_embedding_provider_id = str(
            self.config.get("embedding_provider_id", "") or ""
        ).strip()
        self.mnemosyne_milvus_lite_path = str(
            self.config.get("milvus_lite_path", "") or ""
        ).strip()
        self.mnemosyne_address = str(self.config.get("address", "") or "").strip()
        self.mnemosyne_db_name = str(self.config.get("db_name", "") or "").strip()
        self.mnemosyne_collection_name = str(
            self.config.get("collection_name", "") or ""
        ).strip()
        self.mnemosyne_use_session_filtering = self._safe_bool(
            self.config.get("use_session_filtering", False),
            False,
        )
        self.mnemosyne_platform_blacklist = self._parse_platform_blacklist(
            self.config.get("platform_blacklist", [])
        )
        auth_raw = self.config.get("authentication")
        self.mnemosyne_authentication = auth_raw if isinstance(auth_raw, dict) else {}

        # 联系人缓存（用于自动识别发消息目标类型）
        self._groups_cache: List[dict] = []
        self._friends_cache: List[dict] = []
        self._cache_time = 0.0
        self._cache_expire = 300
        self._cache_lock = asyncio.Lock()

        # Goal 存储：key = unified_msg_origin，value = {"things": str, "location": "system"|"user"}
        # 持久化到 data_dir/goals.json，重启后保留
        self._goals_lock = asyncio.Lock()
        self._goals_file = self.data_dir / "goals.json"
        self._goals: dict[str, dict] = self._load_goals()

        # Recall 一次性注入标记：key = unified_msg_origin，value = 人格 prompt 文本
        # 用户发送 /recall 时设置，下一次 on_llm_request 注入后立即清除
        self._recall_pending: dict[str, str] = {}
        self._recall_pending_lock = asyncio.Lock()

    def _load_goals(self) -> dict[str, dict]:
        """从磁盘加载 goals.json，返回 dict。"""
        try:
            if self._goals_file.exists():
                raw = json.loads(self._goals_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return raw
        except Exception as e:
            logger.warning(f"[goal] 加载 goals.json 失败，使用空字典: {e}")
        return {}

    def _save_goals(self) -> None:
        """将当前 _goals 同步写入 goals.json。"""
        try:
            self._goals_file.write_text(
                json.dumps(self._goals, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[goal] 保存 goals.json 失败: {e}")

    def _resolve_data_dir(self) -> Path:
        try:
            return Path(StarTools.get_data_dir("astrbot_plugin_toolbox_for_koko"))
        except RuntimeError:
            return (
                Path(get_astrbot_data_path())
                / "plugin_data"
                / "astrbot_plugin_toolbox_for_koko"
            )

    def get_data_dir(self) -> str:
        return str(self.data_dir)

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

    def _build_qweather_auth(self) -> tuple[dict, bool]:
        """构建 QWeather 认证信息。

        Returns:
            (headers, use_query_key): headers 用于 HTTP 请求头，
            use_query_key 为 True 时需要在 query string 中附带 key 参数。
        """
        if self.qweather_jwt_token:
            return {"Authorization": f"Bearer {self.qweather_jwt_token}"}, False
        if self.qweather_key:
            return {}, True
        return {}, True

    def _get_geo_host(self, use_query_key: bool) -> str:
        """Geo Host 选择规则：key 模式默认与 weather host 一致；JWT 模式默认 geoapi。"""
        if self.qweather_geo_host:
            return self.qweather_geo_host
        if use_query_key:
            return self.qweather_weather_host or "devapi.qweather.com"
        return "geoapi.qweather.com"

    def _get_weather_host(self, use_query_key: bool = True) -> str:
        """获取天气 API 主机地址。"""
        if self.qweather_weather_host:
            return self.qweather_weather_host
        if use_query_key:
            return "devapi.qweather.com"
        return "api.qweather.com"

    def _get_air_host(self, use_query_key: bool = True) -> str:
        """获取空气质量 API 主机地址。"""
        if use_query_key:
            return "devapi.qweather.com"
        return "api.qweather.com"

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

    def _extract_reply_text(self, response: Any) -> str:
        """从 LLMResponse 对象中提取回复文本。"""
        if response is None:
            return ""
        if isinstance(response, str):
            return response
        if hasattr(response, "completion_text") and response.completion_text:
            return response.completion_text
        if hasattr(response, "result_chain") and response.result_chain:
            chain = getattr(response.result_chain, "chain", None)
            if isinstance(chain, list):
                parts = []
                for comp in chain:
                    text = getattr(comp, "text", None)
                    if isinstance(text, str) and text.strip():
                        parts.append(text)
                if parts:
                    return "\n".join(parts).strip()
        # 兜底：尝试从 dict 中提取
        if isinstance(response, dict):
            for key in ("completion_text", "text", "content", "message"):
                val = response.get(key)
                if isinstance(val, str) and val.strip():
                    return val
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

    def _parse_string_list(self, raw_value: Any) -> list[str]:
        """解析字符串列表，支持 list 或 JSON 数组字符串。"""
        items: list[Any] = []
        if isinstance(raw_value, list):
            items = raw_value
        elif isinstance(raw_value, str):
            raw_text = raw_value.strip()
            if not raw_text:
                return []
            try:
                parsed = json.loads(raw_text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                items = parsed
            else:
                items = [v.strip() for v in raw_text.split(",") if v.strip()]
        else:
            return []

        normalized = [str(v).strip() for v in items if str(v).strip()]
        return list(dict.fromkeys(normalized))

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE,
        priority=90,
    )
    async def keyword_capture_reply_handler(
        self, event: AstrMessageEvent, *args: Any, **kwargs: Any
    ):
        """
        关键词捕捉回复 + 群聊主动回复。

        功能说明:
          - 关键词命中时，使用 keyword_capture_reply_probability
          - 未命中关键词但处于群聊时，使用 keyword_capture_base_probability（可用于活跃氛围）
          - 私聊消息始终使用关键词逻辑
          - 启用 keyword_capture_manage_context 时注入群聊上下文

        与 AstrBot LTM 的关系:
          - 这是一个完全独立的实现，不依赖 AstrBot 内置 LongTermMemory
          - 当 keyword_capture_manage_context=True 时接管群聊上下文管理
          - 在 on_llm_request 钩子中撤销 LTM 的上下文追加，避免重复
        """
        try:
            if not self.enable_keyword_capture_reply:
                return

            if event.get_sender_id() == event.get_self_id():
                return

            message_text = (event.get_message_outline() or "").strip()
            if not message_text:
                return

            # 白名单检查
            if self.keyword_capture_whitelist:
                group_id = event.get_group_id()
                in_whitelist = (
                    event.unified_msg_origin in self.keyword_capture_whitelist
                    or (group_id and group_id in self.keyword_capture_whitelist)
                )
                if not in_whitelist:
                    return

            # 判断触发类型：关键词命中 OR 基础概率
            is_keyword_hit = bool(
                self.keyword_capture_words
                and any(word in message_text for word in self.keyword_capture_words)
            )
            if is_keyword_hit:
                probability = self.keyword_capture_reply_probability
            else:
                # 非关键词模式仅群聊生效（私聊必须命中关键词）
                if event.get_message_type() != MessageType.GROUP_MESSAGE:
                    return
                probability = self.keyword_capture_base_probability
                if probability <= 0.0:
                    return

            # 被 @ 时跳过概率门限（如果配置开启）
            is_mentioned = False
            if self.keyword_capture_bypass_probability_on_at:
                self_id = event.get_self_id()
                for comp in event.get_messages():
                    if hasattr(comp, "qq") and str(getattr(comp, "qq", "")) == str(
                        self_id
                    ):
                        is_mentioned = True
                        break
            if is_mentioned:
                probability = 1.0
                logger.debug("[keyword_capture] 被 @ 触发，跳过概率门限")

            # 概率门限
            roll = random.random()
            if roll > probability:
                logger.debug(
                    f"[keyword_capture] 未通过概率门限: roll={roll:.4f} >"
                    f"p={probability:.4f}, keyword_hit={is_keyword_hit}"
                )
                return

            # 会话管理
            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(event.unified_msg_origin)

            if not curr_cid:
                if self.keyword_capture_session_mode == "active_only":
                    logger.debug("[keyword_capture] 无活跃会话且模式=active_only，跳过")
                    return
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

            # 构建 prompt（注入群聊上下文，如果启用）
            if self.keyword_capture_manage_context:
                # logger.info(f"[keyword_capture] 开始构建上下文 prompt，会话: {event.unified_msg_origin}")
                final_prompt = await self.kc_context.build_prompt(
                    event,
                    message_text,
                    image_limit=self.keyword_capture_context_image_limit,
                )
                # logger.info(f"[keyword_capture] 上下文 prompt 构建完成，长度: {len(final_prompt)} 字符"）
            else:
                final_prompt = message_text

            # 标记此请求由 keyword_capture 触发（供 on_llm_request 识别）
            event.set_extra("is_keyword_capture_request", True)

            logger.info(
                f"[keyword_capture] 触发 LLM 请求 - 会话: {event.unified_msg_origin}, "
                f"触发: {'关键词' if is_keyword_hit else '基础概率'}, "
                f"prob={probability}, prompt_len={len(final_prompt)}"
            )

            yield event.request_llm(
                prompt=final_prompt,
                session_id=curr_cid or "",
                conversation=conversation,
            )
            event.stop_event()
        except Exception as e:
            logger.debug(f"[keyword_capture] 处理失败: {e}")

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE,
        priority=100,
    )
    async def kc_context_recorder(
        self, event: AstrMessageEvent, *args: Any, **kwargs: Any
    ):
        """
        上下文记录器。

        独立于 keyword_capture_reply_handler 运行，无差别记录所有群聊/私聊消息。
        当 keyword_capture_manage_context 或 content_audit_enabled 任一开启时生效。
        图片不在此处转述（触发回复时统一转述），仅存 URL。
        """
        if not self.enable_keyword_capture_reply:
            return
        if not self.keyword_capture_manage_context and not self.content_audit_enabled:
            return
        await self.kc_context.record_message(event)
        logger.debug(
            f"[kc] kc_context_recorder 已记录消息: {event.get_message_outline()[:40]}"
        )

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
                        "location": {
                            "type": "string",
                            "description": "位置关键词，支持城市名、经纬度、LocationID、Adcode，必填。例如：杭州、116.41,39.92、101210101",
                        },
                        "number": {
                            "type": "integer",
                            "description": "返回候选数量，1-20，默认10",
                        },
                        "adm": {
                            "type": "string",
                            "description": "附加行政区过滤，可选",
                        },
                        "range": {"type": "string", "description": "搜索范围，可选"},
                        "lang": {"type": "string", "description": "返回语言，默认zh"},
                    },
                    "required": ["location"],
                },
                "keywords": [
                    "天气",
                    "城市编码",
                    "location",
                    "地理查询",
                    "地区",
                    "城市",
                    "weather location",
                ],
                "handler": self._run_tool_weather_location,
            }

            registry["tool_weather"] = {
                "name": "tool_weather",
                "description": "获取实时/3日/7日天气或生活指数。location 建议使用 tool_weather_location 的 id。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "Location ID，必填。建议来自 tool_weather_location 返回的 id，或者以英文逗号分隔的经度,纬度坐标如 116.41,39.92",
                        },
                        "query_type": {
                            "type": "string",
                            "description": "查询类型：now(实时)、3d(3日)、7d(7日)、indices_1d(今日生活指数)、indices_3d(3日生活指数)，默认now",
                        },
                        "full_7d": {
                            "type": "boolean",
                            "description": "仅在 query_type=7d 时生效。true 返回全量原始数据，false 返回精简总结（默认）",
                        },
                        "focus": {
                            "type": "string",
                            "description": "可选。总结关注点，例如：穿衣建议、是否需要带伞",
                        },
                    },
                    "required": ["location"],
                },
                "keywords": [
                    "天气",
                    "实时天气",
                    "天气预报",
                    "生活指数",
                    "7日天气",
                    "weather",
                ],
                "handler": self._run_tool_weather,
            }

            registry["tool_weather_history"] = {
                "name": "tool_weather_history",
                "description": "查询历史天气或历史空气质量（不含今天，最多回溯10天）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "Location ID，必填。建议来自 tool_weather_location 返回的 id",
                        },
                        "history_type": {
                            "type": "string",
                            "description": "历史类型：weather(历史天气，默认) 或 air(历史空气质量)",
                        },
                        "days": {
                            "type": "integer",
                            "description": "回溯天数，1-10，默认1",
                        },
                        "full_history": {
                            "type": "boolean",
                            "description": "true 返回全量历史数据，false 返回精简总结，>3d时默认返回精简总结",
                        },
                        "focus": {
                            "type": "string",
                            "description": "可选。总结关注点，例如：穿衣建议、是否需要带伞",
                        },
                    },
                    "required": ["location"],
                },
                "keywords": [
                    "历史天气",
                    "空气质量",
                    "history",
                    "weather history",
                    "AQI",
                    "历史空气",
                    "天气",
                    "历史",
                    "历史记录",
                ],
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
                        "engine": {
                            "type": "string",
                            "description": "搜索引擎：search_std(默认) 或 search_pro_quark(复杂问题/强时效)",
                        },
                        "content_size": {
                            "type": "string",
                            "description": "内容粒度：lite(摘要)、medium(摘要+来源信息)、high(摘要+来源全文)；默认lite",
                        },
                        "time_filter": {
                            "type": "string",
                            "description": "时间过滤：noLimit、oneDay、oneWeek、oneMonth、oneYear",
                        },
                        "count": {
                            "type": "integer",
                            "description": "结果数量，1-20，默认10",
                        },
                    },
                    "required": ["query"],
                },
                "keywords": [
                    "搜索",
                    "联网",
                    "查资料",
                    "网页搜索",
                    "search",
                    "web",
                    "网页",
                ],
                "handler": self._run_tool_search,
            }

        if self.enable_fetch_url:
            registry["tool_fetch_url"] = {
                "name": "tool_fetch_url",
                "description": "抓取单个网页正文文本。适合对指定 URL 做内容提取。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "网页URL，必须以 http:// 或 https:// 开头",
                        },
                        "skip_filter": {
                            "type": "boolean",
                            "description": "开关：false(默认)=增强抓取逻辑；true=原版逻辑。",
                        },
                        "llm_compress": {
                            "type": "string",
                            "enum": ["inherit", "summary", "truncate"],
                            "description": "可选覆盖项：inherit=按用户配置(默认)；summary=超长时强制 LLM 压缩；truncate=超长时强制截断。",
                        },
                    },
                    "required": ["url"],
                },
                "keywords": ["搜索", "抓取网页", "网页正文", "url", "fetch", "extract"],
                "handler": self._run_tool_fetch_url,
            }

        if self.image_caption_tool_enabled:
            registry["tool_image_caption"] = {
                "name": "tool_image_caption",
                "description": "对一张或多张图片做识图/转述。支持分别通过 paths、urls、base64_list、data_urls 四类列表传图，也可回退使用当前消息内附图。可自定义 prompt 和 provider_id。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "本地文件路径列表。每项为本地路径或 file:// 路径。",
                        },
                        "urls": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "网络图片 URL 列表。每项为 http:// 或 https:// 图片地址。",
                        },
                        "base64_list": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "base64 图片数据列表。每项为纯 base64 字符串，不含 data:image 前缀。",
                        },
                        "data_urls": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "data URL 图片列表。每项形如 data:image/png;base64,...。",
                        },
                        "use_event_images": {
                            "type": "boolean",
                            "description": "当未显式传入图片时，是否回退使用当前消息内附图。默认 true。",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "可选，自定义传给 LLM 的提示词模板。支持 {image_type}、{index}、{total}、{source} 占位符；敏感兜底链路也复用这份 prompt。",
                        },
                        "provider_id": {
                            "type": "string",
                            "description": "可选，指定用于识图的 AstrBot Provider ID；留空则沿用默认图片转述 Provider。",
                        },
                    },
                    "required": [],
                },
                "keywords": [
                    "识图",
                    "图片转述",
                    "图像转述",
                    "看图",
                    "caption image",
                    "image caption",
                    "image describe",
                    "图片列表",
                    "base64 图片",
                ],
                "handler": self._run_tool_image_caption,
            }

        if self.enable_history:
            registry["tool_history"] = {
                "name": "tool_history",
                "description": "获取群聊或好友历史消息记录。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "description": "查询模式：group(群聊) 或 friend(私聊)。不传时按上下文自动推断",
                        },
                        "target_id": {
                            "type": "string",
                            "description": "目标ID：group 模式传群号，friend 模式传用户QQ号。可不传并按当前上下文自动补全",
                        },
                        "page": {
                            "type": "integer",
                            "description": "本地分页页码，默认1",
                        },
                        "refresh": {
                            "type": "boolean",
                            "description": "是否强制刷新历史缓存。true 时忽略旧缓存，从最新数据重新拉取",
                        },
                        "count": {
                            "type": "integer",
                            "description": "每页返回数量（page_size），默认20，范围1-100",
                        },
                    },
                    "required": [],
                },
                "keywords": [
                    "聊天",
                    "消息",
                    "历史记录",
                    "历史消息",
                    "聊天记录",
                    "群历史",
                    "私聊历史",
                    "history",
                    "message log",
                ],
                "handler": self._run_tool_history,
            }

        registry["add_memory"] = {
            "name": "add_memory",
            "description": "添加重要记忆到存储中，便于后续检索。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "记忆内容，必填"},
                    "tags": {
                        "type": "string",
                        "description": "标签，多个标签用英文逗号分隔，可选",
                    },
                    "importance": {
                        "type": "integer",
                        "description": "重要程度，1-10，默认5",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "可选，指定记忆所属用户，默认当前会话发送者",
                    },
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
                    "user_specific": {
                        "type": "boolean",
                        "description": "是否仅搜索当前用户，默认true",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回数量，默认10，最大20",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "可选，强制指定查询用户",
                    },
                },
                "required": [],
            },
            "keywords": [
                "记忆",
                "搜索记忆",
                "查找记忆",
                "记忆列表",
                "recall",
                "memory",
            ],
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
                    "importance": {
                        "type": "integer",
                        "description": "新重要度，1-10，可选",
                    },
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

        registry["search_memory_vector"] = {
            "name": "search_memory_vector",
            "description": "在 Mnemosyne 向量数据库中进行相似度查找（向量检索）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "查询文本，必填"},
                    "top_k": {
                        "type": "integer",
                        "description": "返回条数，默认5，最大50",
                    },
                    "collection_name": {
                        "type": "string",
                        "description": "集合名，可选，覆盖配置中的 collection_name",
                    },
                },
                "required": ["query"],
            },
            "keywords": [
                "向量",
                "向量检索",
                "相似度",
                "记忆向量",
                "mnemosyne",
                "milvus",
                "vector",
                "search",
            ],
            "handler": self._run_tool_search_memory_vector,
        }

        registry["list_memory_vector"] = {
            "name": "list_memory_vector",
            "description": "列出 Mnemosyne 中可用的记忆集合（collections）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "keywords": ["记忆", "向量", "集合", "collection", "mnemosyne", "milvus"],
            "handler": self._run_tool_list_memory_vector,
        }

        registry["list_records_memory_vector"] = {
            "name": "list_records_memory_vector",
            "description": "列出 Mnemosyne 指定集合中的记录（records）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "collection_name": {
                        "type": "string",
                        "description": "集合名，可选",
                    },
                    "limit": {"type": "integer", "description": "返回条数，默认5"},
                },
                "required": [],
            },
            "keywords": ["记忆", "向量", "记录", "records", "mnemosyne", "milvus"],
            "handler": self._run_tool_list_records_memory_vector,
        }

        registry["remember_memory_vector"] = {
            "name": "remember_memory_vector",
            "description": "向 Mnemosyne 记忆库写入一条记忆（remember）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "记忆内容，必填"}
                },
                "required": ["content"],
            },
            "keywords": ["记忆", "写入", "保存", "remember", "mnemosyne"],
            "handler": self._run_tool_remember_memory_vector,
        }

        registry["delete_record_memory_vector"] = {
            "name": "delete_record_memory_vector",
            "description": "从 Mnemosyne 记忆库删除指定记录（delete_record）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "记录ID(memory_id)，必填",
                    },
                    "session_id": {"type": "string", "description": "会话ID，可选"},
                    "confirm": {
                        "type": "string",
                        "description": "确认参数，可选（由 Mnemosyne 定义）",
                    },
                },
                "required": ["memory_id"],
            },
            "keywords": ["记忆", "删除", "delete", "delete_record", "mnemosyne"],
            "handler": self._run_tool_delete_record_memory_vector,
        }

        registry["send_message"] = {
            "name": "send_message",
            "description": "立即向指定QQ好友或群聊发送文本消息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "目标QQ号或群号，必填",
                    },
                    "message": {"type": "string", "description": "消息内容，必填"},
                    "chat_type": {
                        "type": "string",
                        "description": "聊天类型：group/private/auto，默认auto",
                    },
                },
                "required": ["target_id", "message"],
            },
            "keywords": ["发消息", "发送消息", "私聊", "群发", "message", "消息"],
            "handler": self._run_tool_send_message,
        }

        # ---- Goal 管理（也走 registry，支持 run_koko_tool 调用） ----
        registry["set_goal"] = {
            "name": "goal",
            "description": "管理当前会话的 Goal（目标指引）。支持 get/set/clear。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "操作类型：get（查看）、set（设置）、clear（清除），默认 get",
                    },
                    "things": {
                        "type": "string",
                        "description": "Goal 内容，action=set 时必填",
                    },
                    "location": {
                        "type": "string",
                        "description": "注入位置：system（默认）或 user，action=set 时生效",
                    },
                },
                "required": [],
            },
            "keywords": ["goal", "目标", "设置目标", "清除目标", "查看目标"],
            "handler": self._run_tool_set_goal,
        }

        registry["get_goal"] = {
            "name": "goal",
            "description": "查看当前会话已设置的 Goal。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": '"get"',
                    }
                },
                "required": [],
            },
            "keywords": ["goal", "目标", "查看目标", "获取目标"],
            "handler": self._run_tool_get_goal,
        }

        # ---- 日历查询（走 registry + @filter.llm_tool 双渠道） ----
        registry["tool_get_calendar"] = {
            "name": "tool_get_calendar",
            "description": "获取 iCal 日历中指定时间范围内的事件列表。支持 Google Calendar 等标准 iCal 地址。支持绝对日期范围（date_from/date_to）和相对天数（days_ahead/days_back）两种模式。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ical_url": {
                        "type": "string",
                        "description": "iCal URL，可选（覆盖配置中的 calendar_ical_url）",
                    },
                    "proxy": {
                        "type": "string",
                        "description": "HTTP 代理地址，可选（如 http://127.0.0.1:7890）",
                    },
                    "date_from": {
                        "type": "string",
                        "description": "起始日期，YYYY-MM-DD 格式，可选。传入后优先于 days_back。",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "结束日期，YYYY-MM-DD 格式，可选。传入后优先于 days_ahead。",
                    },
                    "days_ahead": {
                        "type": "integer",
                        "description": "查询未来多少天，默认 7，最大 365（date_from/date_to 未传入时生效）",
                    },
                    "days_back": {
                        "type": "integer",
                        "description": "查询过去多少天，默认 0（date_from/date_to 未传入时生效）",
                    },
                    "max_events": {
                        "type": "integer",
                        "description": "最多返回事件数，默认 20，最大 100",
                    },
                    "tz_offset": {
                        "type": "integer",
                        "description": "本地时区偏移（小时），默认自动读取 AstrBot 系统时区",
                    },
                },
                "required": [],
            },
            "keywords": ["日历", "calendar", "日程", "事件", "ical", "schedule"],
            "handler": self._run_tool_calendar,
        }

        return registry

    def _get_available_tools(self) -> dict:
        """返回当前启用状态下可用的工具。"""
        return dict(self._tool_registry)

    def _get_wyc_plugin_instance(self):
        candidate_names = [
            "wyc_tools",
            "wyc_plugin_qzone_tools",
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

    def _parse_platform_blacklist(self, raw_value) -> list[str]:
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

        normalized = []
        for item in items:
            text = str(item).strip()
            if text:
                normalized.append(text)
        return list(dict.fromkeys(normalized))

    def _get_mnemosyne_plugin_instance(self):
        """获取 Mnemosyne 插件实例（Star 实例）。

        采用：候选名称优先 + 扫描 module_path/name 兜底。
        """

        candidate_names = [
            "astrbot_plugin_mnemosyne",
            "mnemosyne",
            "Mnemosyne",
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
            if "mnemosyne" in module_path.lower() or "mnemosyne" in star_name.lower():
                star_cls = getattr(meta, "star_cls", None)
                if star_cls:
                    return star_cls
        return None

    def _build_mnemosyne_config_dict(self) -> dict:
        """构造与 Mnemosyne 一致的配置字典（供桥接/转发时更新对方实例）。"""

        cfg = {
            "embedding_provider_id": self.mnemosyne_embedding_provider_id,
            "milvus_lite_path": self.mnemosyne_milvus_lite_path,
            "address": self.mnemosyne_address,
            "db_name": self.mnemosyne_db_name,
            "collection_name": self.mnemosyne_collection_name,
            "use_session_filtering": self.mnemosyne_use_session_filtering,
            "platform_blacklist": list(self.mnemosyne_platform_blacklist),
            "authentication": {
                "token": str(self.mnemosyne_authentication.get("token", "") or ""),
                "user": str(self.mnemosyne_authentication.get("user", "") or ""),
                "password": str(
                    self.mnemosyne_authentication.get("password", "") or ""
                ),
            },
        }

        # 清理空值，避免把空配置强行覆盖对方实例
        cleaned = {}
        for k, v in cfg.items():
            if v is None:
                continue
            if isinstance(v, str) and v.strip() == "":
                continue
            if isinstance(v, dict) and not any(
                str(x or "").strip() for x in v.values()
            ):
                continue
            if isinstance(v, list) and len(v) == 0:
                continue
            cleaned[k] = v
        return cleaned

    def _bridge_config_to_mnemosyne(self, mnemo_plugin) -> None:
        """将本插件的 Mnemosyne 配置尽力桥接到对方插件实例。"""
        if not mnemo_plugin:
            return

        cfg = self._build_mnemosyne_config_dict()
        if not cfg:
            return

        try:
            if hasattr(mnemo_plugin, "config") and isinstance(
                mnemo_plugin.config, dict
            ):
                mnemo_plugin.config.update(cfg)
            else:
                setattr(mnemo_plugin, "config", dict(cfg))
        except Exception:
            pass

        for key, value in cfg.items():
            try:
                if hasattr(mnemo_plugin, key):
                    setattr(mnemo_plugin, key, value)
            except Exception:
                continue

    async def _forward_to_mnemosyne(
        self, event: AstrMessageEvent, fn_name: str, **kwargs
    ):
        mnemo_plugin = self._get_mnemosyne_plugin_instance()
        if not mnemo_plugin:
            await event.send(
                MessageChain().message(
                    "未找到 Mnemosyne 插件实例，无法转发记忆相关操作"
                )
            )
            return

        self._bridge_config_to_mnemosyne(mnemo_plugin)

        fn = getattr(mnemo_plugin, fn_name, None)
        if not callable(fn):
            await event.send(
                MessageChain().message(f"Mnemosyne 未提供方法 {fn_name}，无法转发")
            )
            return

        try:
            result = fn(event, **(kwargs or {}))
            if inspect.isasyncgen(result):
                async for item in result:
                    yield item
            elif inspect.isawaitable(result):
                awaited = await result
                if awaited is not None:
                    yield awaited
            elif result is not None:
                yield result
        except Exception as e:
            logger.error(f"[mnemosyne] 转发 Mnemosyne 失败 {fn_name}: {e}")
            await event.send(MessageChain().message(f"转发 Mnemosyne 失败：{e}"))

    def _get_mnemosyne_auth_params(self) -> dict:
        auth = (
            self.mnemosyne_authentication
            if isinstance(self.mnemosyne_authentication, dict)
            else {}
        )
        token = str(auth.get("token", "") or "").strip()
        user = str(auth.get("user", "") or "").strip()
        password = str(auth.get("password", "") or "").strip()

        params = {}
        if token:
            params["token"] = token
        if user:
            params["user"] = user
        if password:
            params["password"] = password
        return params

    def _parse_milvus_connect_kwargs(self) -> tuple[str | None, dict]:
        """根据配置生成 pymilvus 连接参数。"""
        alias = "mnemosyne_memory"

        # Milvus Lite 优先
        if self.mnemosyne_milvus_lite_path:
            return alias, {
                "uri": self.mnemosyne_milvus_lite_path,
                **self._get_mnemosyne_auth_params(),
            }

        addr = (self.mnemosyne_address or "").strip()
        if not addr:
            return None, {}

        lowered = addr.lower()
        if lowered.startswith("http://") or lowered.startswith("https://"):
            kw = {"uri": addr, **self._get_mnemosyne_auth_params()}
            if self.mnemosyne_db_name:
                kw["db_name"] = self.mnemosyne_db_name
            return alias, kw

        # host:port
        host = addr
        port = "19530"
        if ":" in addr:
            host, port = addr.rsplit(":", 1)
            host = host.strip()
            port = port.strip() or port
        kw = {"host": host, "port": port, **self._get_mnemosyne_auth_params()}
        if self.mnemosyne_db_name:
            kw["db_name"] = self.mnemosyne_db_name
        return alias, kw

    async def _mnemosyne_vector_search(
        self,
        query: str,
        top_k: int = 5,
        collection_name: str | None = None,
    ) -> list[dict]:
        try:
            from pymilvus import connections, Collection, utility
        except Exception as e:
            raise RuntimeError(
                "缺少依赖 pymilvus，无法执行向量查找。请在插件环境安装 pymilvus 后重试。"
            ) from e

        if not query.strip():
            return []

        effective_collection_name = (
            collection_name or ""
        ).strip() or self.mnemosyne_collection_name
        if not effective_collection_name:
            raise RuntimeError(
                "缺少 collection_name：请在工具参数传入 collection_name，或在配置中设置 mnemosyne.collection_name"
            )

        provider = None
        provider_id = (self.mnemosyne_embedding_provider_id or "").strip()
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
            # 与 Mnemosyne 插件保持一致：留空时自动使用第一个 Embedding Provider
            try:
                providers = self.context.get_all_embedding_providers()
            except Exception:
                providers = []
            provider = providers[0] if providers else None

        get_embedding = getattr(provider, "get_embedding", None) if provider else None
        if not callable(get_embedding):
            raise RuntimeError(
                "未配置可用的 EmbeddingProvider：请在配置中填写 mnemosyne.embedding_provider_id，"
                "或在 AstrBot 中至少启用一个 Embedding Provider 以便自动选择"
            )

        query_vector = await get_embedding(query)
        if not isinstance(query_vector, list) or not query_vector:
            raise RuntimeError("获取 embedding 失败或返回为空")

        alias, connect_kwargs = self._parse_milvus_connect_kwargs()
        if not alias:
            raise RuntimeError(
                "缺少 Milvus 连接配置：请填写 address 或 milvus_lite_path"
            )

        # 幂等连接：重复 connect 同 alias 会覆盖/复用
        connections.connect(alias=alias, **connect_kwargs)

        if not utility.has_collection(effective_collection_name, using=alias):
            raise RuntimeError(f"Milvus 集合不存在：{effective_collection_name}")

        collection = Collection(effective_collection_name, using=alias)
        try:
            collection.load()
        except Exception:
            # 某些部署/版本 load 可能不是必须，失败时继续 search 试试
            pass

        search_params = {"metric_type": "L2", "params": {"nprobe": 10}}
        raw_results = collection.search(
            data=[query_vector],
            anns_field="embedding",
            param=search_params,
            limit=max(1, min(int(top_k or 5), 50)),
            output_fields=["content", "create_time", "memory_id"],
        )

        formatted = []
        for hits in raw_results:
            for hit in hits:
                entity = hit.entity.to_dict() if getattr(hit, "entity", None) else {}
                content = str(entity.get("content", "") or "")
                if "<MNEMO_META>" in content:
                    content = content.split("<MNEMO_META>")[0].strip()
                dist = getattr(hit, "distance", None)
                try:
                    score = 1.0 / (1.0 + float(dist)) if dist is not None else None
                except Exception:
                    score = None
                formatted.append(
                    {
                        "memory_id": str(entity.get("memory_id", "") or ""),
                        "distance": dist,
                        "score": score,
                        "content": content,
                        "create_time": entity.get("create_time", ""),
                    }
                )
        return formatted

    async def _forward_search_to_wyc(
        self, event: AstrMessageEvent, query: str
    ) -> dict | None:
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

    async def _forward_run_to_wyc(
        self, event: AstrMessageEvent, tool_name: str, args_dict: dict
    ) -> dict | None:
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

    async def _run_tool_weather_location(
        self, event: AstrMessageEvent, args: dict
    ) -> str:
        return await run_weather_location(self, args)

    async def _run_tool_weather(self, event: AstrMessageEvent, args: dict) -> str:
        return await run_weather(self, args)

    async def _run_tool_weather_history(
        self, event: AstrMessageEvent, args: dict
    ) -> str:
        return await run_weather_history(self, args)

    async def _run_tool_search(self, event: AstrMessageEvent, args: dict) -> str:
        return await run_search(self, args)

    async def _run_tool_fetch_url(self, event: AstrMessageEvent, args: dict) -> str:
        return await run_fetch_url(self, event, args)

    async def _run_tool_image_caption(self, event: AstrMessageEvent, args: dict) -> str:
        return await self.image_caption_handler.caption_tool(event, args)

    async def _run_tool_history(self, event: AstrMessageEvent, args: dict) -> str:
        return await run_history(self, event, args)

    async def _run_tool_add_memory(self, event: AstrMessageEvent, args: dict) -> str:
        return await run_add_memory(self, event, args)

    async def _run_tool_search_memories(
        self, event: AstrMessageEvent, args: dict
    ) -> str:
        return await run_search_memories(self, event, args)

    async def _run_tool_search_memory_vector(
        self, event: AstrMessageEvent, args: dict
    ) -> str:
        return await run_search_memory_vector(self, event, args)

    async def _run_tool_list_memory_vector(
        self, event: AstrMessageEvent, args: dict
    ) -> str:
        return await run_list_memory_vector(self, event, args)

    async def _run_tool_list_records_memory_vector(
        self, event: AstrMessageEvent, args: dict
    ) -> str:
        return await run_list_records_memory_vector(self, event, args)

    async def _run_tool_remember_memory_vector(
        self, event: AstrMessageEvent, args: dict
    ) -> str:
        return await run_remember_memory_vector(self, event, args)

    async def _run_tool_delete_record_memory_vector(
        self, event: AstrMessageEvent, args: dict
    ) -> str:
        return await run_delete_record_memory_vector(self, event, args)

    async def _run_tool_update_memory(self, event: AstrMessageEvent, args: dict) -> str:
        return await run_update_memory(self, event, args)

    async def _run_tool_delete_memory(self, event: AstrMessageEvent, args: dict) -> str:
        return await run_delete_memory(self, event, args)

    async def _run_tool_get_memory_detail(
        self, event: AstrMessageEvent, args: dict
    ) -> str:
        return await run_get_memory_detail(self, event, args)

    async def _run_tool_send_message(self, event: AstrMessageEvent, args: dict) -> str:
        return await run_send_message(self, event, args)

    async def _run_tool_calendar(self, event: AstrMessageEvent, args: dict) -> str:
        return await run_get_calendar(self, args)

    async def _run_tool_set_goal(self, event: AstrMessageEvent, args: dict) -> str:
        result = await self.goal(
            event,
            action=str(args.get("action", "get") or "get"),
            things=str(args.get("things", "") or ""),
            location=str(args.get("location", "system") or "system"),
        )
        return result.get("message", str(result))

    async def _run_tool_get_goal(self, event: AstrMessageEvent, args: dict) -> str:
        result = await self.goal(event, action="get")
        if not result.get("has_goal"):
            return result.get("message", "当前会话没有设置 Goal。")
        lines = [
            f"Goal: {result.get('things', '')}",
            f"注入位置: {result.get('location', '')}",
            f"设定时间: {result.get('set_at', '')}",
            f"设定者: {result.get('set_by', '')}",
        ]
        return "\n".join(lines)

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
            if now - self._cache_time < self._cache_expire and (
                self._groups_cache or self._friends_cache
            ):
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

    async def _collect_forwarded_output_text(
        self, event: AstrMessageEvent, fn_name: str, **kwargs
    ) -> str:
        parts: list[str] = []
        async for item in self._forward_to_mnemosyne(event, fn_name, **(kwargs or {})):
            text = self._extract_llm_text(item)
            if not text:
                try:
                    text = json.dumps(item, ensure_ascii=False)
                except Exception:
                    text = str(item)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()

    # ---------------- 工具暴露 ----------------
    @filter.llm_tool(name="search_koko_tools")
    async def search_koko_tools(self, event: AstrMessageEvent, query: str) -> dict:
        """【必须优先使用】根据简短关键词搜索匹配工具。支持多个关键词（通过空格分隔）。

        - 本插件工具优先命中；
        - 对于未命中关键词，会自动转发到 wyc 的 `search_wyc_tools` 做兼容检索。

        Args:
            query(string): 搜索关键词（如“天气 搜索 历史消息”），必填
        """
        if not query or not query.strip():
            return {
                "status": "error",
                "message": "请提供搜索关键词（简短词语，如“天气 搜索”）",
            }

        available_tools = self._get_available_tools()

        seen_words = set()
        query_words: list[str] = []
        for word in query.split():
            w = word.strip().lower()
            if not w or w in seen_words:
                continue
            seen_words.add(w)
            query_words.append(w)

        local_matched_by_name: dict[str, dict] = {}
        wyc_messages: list[str] = []
        wyc_results: list[dict] = []

        for query_word in query_words:
            word_matched_any = False
            for name, meta in available_tools.items():
                description = str(meta.get("description", "") or "")
                keywords = meta.get("keywords", []) or []
                if (
                    query_word in str(name).lower()
                    or query_word in description.lower()
                    or any(query_word in str(kw).lower() for kw in keywords)
                ):
                    word_matched_any = True
                    if name not in local_matched_by_name:
                        local_matched_by_name[name] = {
                            "name": name,
                            "description": description,
                            "parameters": meta.get(
                                "parameters",
                                {"type": "object", "properties": {}, "required": []},
                            ),
                        }

            # 本地未命中：按关键词转发 wyc
            if not word_matched_any:
                wyc_result = await self._forward_search_to_wyc(event, query_word)
                if isinstance(wyc_result, dict):
                    wyc_message = str(wyc_result.get("message", "") or "").strip()
                    if wyc_message:
                        wyc_messages.append(f"【{query_word}】\n{wyc_message}")
                    wyc_results.append(
                        {"keyword": query_word, "wyc_result": wyc_result}
                    )

        local_matched = list(local_matched_by_name.values())

        if not local_matched and not wyc_messages:
            return {
                "status": "success",
                "message": f"未找到与「{query}」相关的工具，可尝试其他关键词或使用 call_koko_tools 查看全部可用工具。注意本工具不是`搜索网页`工具，也不是`获取历史`消息工具，请传入关键词“搜索”或“历史消息”来获取这两个工具的使用方式。",
            }

        lines: list[str] = []
        if local_matched:
            lines.append(f"🔍 (koko) 找到 {len(local_matched)} 个相关工具：")
            for tool in local_matched[:10]:
                lines.append(
                    f"- {tool['name']}: {str(tool.get('description', '') or '')[:60]}..."
                )

        if wyc_messages:
            if lines:
                lines.append("\n🔁 未命中关键词已转发 wyc：")
            else:
                lines.append("🔁 未命中关键词已转发 wyc：")
            lines.extend(wyc_messages)

        resp: dict = {"status": "success", "message": "\n".join(lines)}
        if local_matched:
            resp["tools"] = local_matched
        if wyc_results:
            resp["forwarded_to"] = "search_wyc_tools"
            resp["wyc_results"] = wyc_results
        return resp

    @llm_tool("call_koko_tools")
    async def call_koko_tools(self, event: AstrMessageEvent, **kwargs) -> dict:
        """返回当前可用工具列表（名称 + 描述 + 参数要点）。仅当 search_koko_tools 未找到时使用。"""
        available_tools = self._get_available_tools()
        if not available_tools:
            return {
                "status": "success",
                "message": "当前配置下没有启用任何工具。",
                "tool_names": [],
            }

        tools_list = []
        for name, meta in available_tools.items():
            params = meta.get("parameters", {})
            required = params.get("required", []) if isinstance(params, dict) else []
            properties = (
                params.get("properties", {}) if isinstance(params, dict) else {}
            )
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
        return {
            "status": "success",
            "message": msg,
            "tool_names": list(available_tools.keys()),
        }

    @filter.llm_tool(name="tool_image_caption")
    async def tool_image_caption(
        self,
        event: AstrMessageEvent,
        paths: list[str] | None = None,
        urls: list[str] | None = None,
        base64_list: list[str] | None = None,
        data_urls: list[str] | None = None,
        use_event_images: bool = True,
        prompt: str = "",
        provider_id: str = "",
    ) -> dict:
        """直接执行图片识图/转述。

        支持通过 paths、urls、base64_list、data_urls 四类列表传图，以及当前消息内附图。

        Args:
            paths(list[string]): 可选。本地图片路径列表。每项可为本地路径或 file:// 路径。
            urls(list[string]): 可选。网络图片 URL 列表。每项应为 http:// 或 https:// 图片地址。
            base64_list(list[string]): 可选。base64 图片数据列表。每项为纯 base64 字符串，不含 data:image 前缀。
            data_urls(list[string]): 可选。data URL 图片列表。每项形如 data:image/png;base64,...。
            use_event_images(boolean): 可选。未显式传图时是否回退使用当前消息内附图，默认 true。
            prompt(string): 可选。统一传给 LLM 的提示词模板，支持 {image_type}、{index}、{total}、{source} 占位符；敏感兜底链路也复用这份 prompt。
            provider_id(string): 可选。指定用于识图的 AstrBot Provider ID；留空则沿用默认图片转述 Provider。
        """
        if not self.image_caption_tool_enabled:
            return {
                "status": "error",
                "message": "tool_image_caption 已被配置禁用。",
            }

        args = {
            "paths": paths,
            "urls": urls,
            "base64_list": base64_list,
            "data_urls": data_urls,
            "use_event_images": use_event_images,
            "prompt": prompt,
            "provider_id": provider_id,
        }
        message = await self.image_caption_handler.caption_tool(event, args)
        return {"status": "success", "message": message}

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
                    return {
                        "status": "error",
                        "message": "tool_args 必须是 JSON 对象字符串。",
                    }
            except json.JSONDecodeError:
                return {
                    "status": "error",
                    "message": "参数格式错误，tool_args 必须是有效 JSON 字符串。",
                }

        # 兼容调用：允许通过 run_koko_tool 转发执行工具搜索/列表接口。
        if normalized_name == "search_koko_tools":
            query_text = str(args_dict.get("query", "") or "")
            return await self.search_koko_tools(event, query=query_text)

        if normalized_name == "call_koko_tools":
            return await self.call_koko_tools(event)

        available_tools = self._get_available_tools()
        if normalized_name not in available_tools:
            wyc_result = await self._forward_run_to_wyc(
                event, normalized_name, args_dict
            )
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

        ok, normalized_url, err = await _normalize_and_validate_fetch_url(self, url)
        if not ok:
            return err
        llm_compress_mode = _parse_llm_compress_mode(llm_compress)
        if llm_compress_mode is None:
            return "llm_compress 参数无效：仅支持 inherit、summary、truncate。"
        return await _get_from_url(
            self,
            normalized_url,
            use_legacy=skip_filter,
            llm_compress=llm_compress_mode,
        )

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, request: Any, *args, **kwargs
    ) -> None:
        try:
            # ---- 图片转述前处理：接管图片转述，不让 AstrBot 处理 ----
            # 独立模块 ImageCaptionHandler 负责下载、降级、类型识别
            await self.image_caption_handler.process(event, request)

            # ---- 撤销 AstrBot LTM 追加的群聊上下文（避免重复） ----
            if event.get_extra("is_keyword_capture_request"):
                ltm_marker = (
                    "You are now in a chatroom. The chat history is as follows:"
                )
                if (
                    hasattr(request, "system_prompt")
                    and ltm_marker in request.system_prompt
                ):
                    idx = request.system_prompt.find(ltm_marker)
                    request.system_prompt = request.system_prompt[:idx].rstrip()
                    logger.debug("[keyword_capture] 已撤销 LTM 群聊上下文追加")

                # 清理 <system_reminder>（运行时上下文标记，keyword_capture 不需要）
                cleaned = 0
                if hasattr(request, "extra_user_content_parts"):
                    before = len(request.extra_user_content_parts)
                    request.extra_user_content_parts = [
                        part
                        for part in request.extra_user_content_parts
                        if not (
                            hasattr(part, "text")
                            and "<system_reminder>" in str(part.text)
                        )
                    ]
                    cleaned = before - len(request.extra_user_content_parts)
                if cleaned > 0:
                    logger.info(
                        f"[keyword_capture] 已清理 {cleaned} 条 <system_reminder>"
                    )

            guide_text = (
                "[重要工具使用规范] 当你需要调用本能力时，必须遵循以下顺序：\n"
                "1. 先调用 search_koko_tools，并传入简短关键词（如：天气、搜索、历史消息、网页抓取、记忆、发消息）。\n"
                "2. 若 search_koko_tools 没找到，再调用 call_koko_tools 查看完整可用工具列表和参数要点。\n"
                # "2.5. 若所需工具不在列表中，且更换关键词后仍然无果，则尝试使用 search_wyc_tools 重复上述2步。"
                "3. 确认工具名后，调用 run_koko_tool，并使用 tool_name + tool_args(JSON字符串)。\n"
                "禁止跳过搜索直接猜测工具名。"
            )
            # guide_text = () # 故意的，别删

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

            # ---- 注入内容审核校正指示 ----
            audit_loop = getattr(self, "content_audit", None)
            if audit_loop:
                try:
                    await audit_loop.inject_to_request(event, request)
                except Exception as e:
                    logger.debug(f"[content_audit] 注入校正指示失败: {e}")

            # ---- 注入 Goal ----
            goal_entry = self._goals.get(event.unified_msg_origin)
            if goal_entry:
                things = str(goal_entry.get("things", "") or "").strip()
                loc = str(goal_entry.get("location", "system") or "system").lower()
                if things:
                    goal_block = f"[Goal] {things}"
                    if loc == "system":
                        # 追加到系统提示词
                        if hasattr(request, "system_prompt"):
                            if request.system_prompt:
                                request.system_prompt += f"\n{goal_block}"
                            else:
                                request.system_prompt = goal_block
                    else:
                        # 追加到当前用户消息末尾（不写入 contexts/历史）
                        # 优先复用已有 part 的类型（TextPart，带 model_dump_for_context），
                        # 避免使用 Plain 组件导致 'Plain' object has no attribute 'model_dump_for_context'
                        if (
                            hasattr(request, "extra_user_content_parts")
                            and request.extra_user_content_parts
                        ):
                            try:
                                request.extra_user_content_parts.append(
                                    type(request.extra_user_content_parts[0])(
                                        text=f"\n{goal_block}"
                                    )
                                )
                            except Exception as e:
                                logger.debug(
                                    f"[goal] 注入到 extra_user_content_parts 失败: {e}"
                                )
                        elif hasattr(request, "prompt"):
                            current = (
                                request.prompt
                                if isinstance(request.prompt, str)
                                else ""
                            )
                            request.prompt = current.rstrip() + f"\n\n{goal_block}"

            # ---- 注入 Recall（一次性人格回顾） ----
            try:
                async with self._recall_pending_lock:
                    recall_content = self._recall_pending.pop(
                        event.unified_msg_origin, None
                    )
                if recall_content:
                    recall_block = (
                        f"<system_WARNING>用户手动触发了人格召回！ 这说明你的回复已经严重背离了人格设定！这是极度危险的行为！\n"
                        f"请你立刻回顾你的人格设定，认真思考用户召回的原因，并且谨慎做出接下来的回答！\n"
                        f"请严格遵守：\n"
                        f"{recall_content}</system_WARNING>"
                    )
                    injected = False
                    if (
                        hasattr(request, "extra_user_content_parts")
                        and request.extra_user_content_parts
                    ):
                        try:
                            request.extra_user_content_parts.append(
                                type(request.extra_user_content_parts[0])(
                                    text=f"\n{recall_block}"
                                )
                            )
                            logger.info(
                                f"[recall] 已一次性注入人格回顾(conversation)，"
                                f"session={event.unified_msg_origin}"
                            )
                            injected = True
                        except Exception as e:
                            logger.debug(
                                f"[recall] 注入到 extra_user_content_parts 失败: {e}"
                            )
                    if not injected and hasattr(request, "system_prompt"):
                        if request.system_prompt:
                            request.system_prompt += f"\n{recall_block}"
                        else:
                            request.system_prompt = recall_block
                        logger.info(
                            f"[recall] 已一次性注入人格回顾(system_prompt)，"
                            f"session={event.unified_msg_origin}"
                        )
                        injected = True
                    if not injected:
                        logger.warning(
                            f"[recall] 无可用的注入点位，session={event.unified_msg_origin}"
                        )
            except Exception as e:
                logger.debug(f"[recall] 注入人格回顾失败: {e}")
        except Exception as e:
            logger.debug(f"[on_llm_request] 注入工具使用规范失败: {e}")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: Any) -> None:
        """LLM 响应后：内容审核（独立） + 记录回复到上下文缓冲区（keyword_capture）。"""
        logger.debug(
            f"[content_audit] on_llm_response 入口, "
            f"会话={event.unified_msg_origin}, "
            f"has_content_audit={hasattr(self, 'content_audit')}, "
            f"content_audit_enabled={getattr(self, 'content_audit_enabled', 'N/A')}"
        )
        try:
            reply_text = self._extract_reply_text(response)
            logger.debug(
                f"[content_audit] 提取回复文本, "
                f"reply_text 长度={len(reply_text)}, "
                f"response type={type(response).__name__}"
            )

            # ---- 审核链路（独立于 keyword_capture） ----
            audit_loop = getattr(self, "content_audit", None)
            if audit_loop:
                if reply_text:
                    try:
                        provider = self.context.get_using_provider()
                        # 内容审核
                        await audit_loop.on_ai_reply(
                            event,
                            reply_text,
                            provider,
                        )
                    except Exception as e:
                        logger.debug(f"[content_audit] 审核失败: {e}")

                    # 人格遵循审核（并行，与内容审核独立）
                    try:
                        if self.persona_audit_enabled:
                            await audit_loop.on_ai_reply_persona(
                                event,
                                reply_text,
                                provider,
                            )
                    except Exception as e:
                        logger.debug(f"[persona_audit] 人格审核失败: {e}")
                else:
                    logger.debug(
                        f"[content_audit] 跳过审核: reply_text 为空, "
                        f"response type={type(response).__name__}, "
                        f"response dir={[a for a in dir(response) if not a.startswith('_')][:10]}"
                    )
            else:
                logger.debug(
                    f"[content_audit] 跳过审核: content_audit 对象不存在, "
                    f"content_audit_enabled={getattr(self, 'content_audit_enabled', 'N/A')}"
                )

            # ---- keyword_capture 链路 ----
            if not event.get_extra("is_keyword_capture_request"):
                return
            if not self.keyword_capture_manage_context:
                return
            if reply_text:
                await self.kc_context.record_reply(event, reply_text)
                logger.info(
                    f"[keyword_capture] AI 回复已记录到上下文缓冲区，"
                    f"长度: {len(reply_text)} 字符"
                )
        except Exception as e:
            logger.debug(f"[on_llm_response] 处理失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tool_memory")
    async def admin_tool_memory(self, event: AstrMessageEvent):
        if not self.enable_admin_tool_memory_command:
            await event.send(
                MessageChain().message("管理员命令 /tool_memory 已被配置禁用")
            )
            return

    # ---------------- Goal 目标管理 ----------------

    @filter.command("goal")
    async def goal_command(self, event: AstrMessageEvent):
        """Goal 目标管理命令。

        用法：
          /goal                         查看当前会话的 goal
          /goal get                     查看当前会话的 goal
          /goal set <内容>              设置 goal，默认注入位置 system（系统提示词）
          /goal set system <内容>       设置 goal，注入到系统提示词
          /goal set user <内容>         设置 goal，注入到每条用户消息末尾（不落盘）
          /goal clear                   清除当前会话的 goal
        """
        raw = (event.get_message_outline() or "").strip()
        parts = raw.split(None, 1)
        sub_raw = parts[1].strip() if len(parts) > 1 else ""
        sub_lower = sub_raw.lower()

        umo = event.unified_msg_origin

        # 无参数或 get → 查看
        if not sub_raw or sub_lower == "get":
            entry = self._goals.get(umo)
            if not entry:
                await event.send(MessageChain().message("当前会话没有设置 Goal。"))
            else:
                loc_label = (
                    "系统提示词"
                    if entry.get("location", "system") == "system"
                    else "用户消息末尾（不落盘）"
                )
                set_by = entry.get("set_by", "")
                set_at = entry.get("set_at", "")
                await event.send(
                    MessageChain().message(
                        f"🎯 当前 Goal（注入位置：{loc_label}）：\n"
                        f"{entry.get('things', '')}\n"
                        f"设定时间：{set_at}  设定者：{set_by}"
                    )
                )
            event.stop_event()
            return

        # clear
        if sub_lower == "clear":
            removed = umo in self._goals
            self._goals.pop(umo, None)
            async with self._goals_lock:
                self._save_goals()
            if removed:
                await event.send(MessageChain().message("✅ Goal 已清除。"))
            else:
                await event.send(MessageChain().message("当前会话没有设置 Goal。"))
            event.stop_event()
            return

        # set [location] <things>
        if sub_lower.startswith("set"):
            after_set = sub_raw[3:].strip()
            location, things = self._parse_goal_args(after_set)
            if not things:
                await event.send(
                    MessageChain().message(
                        "用法：/goal set [system|user] <目标内容>\n"
                        "  system（默认）：注入到系统提示词\n"
                        "  user：注入到每条用户消息末尾（不落盘到历史）"
                    )
                )
                event.stop_event()
                return
            self._goals[umo] = {
                "things": things,
                "location": location,
                "set_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "set_by": "user",
            }
            async with self._goals_lock:
                self._save_goals()
            loc_label = (
                "系统提示词" if location == "system" else "用户消息末尾（不落盘）"
            )
            await event.send(
                MessageChain().message(
                    f"✅ Goal 已设置（注入位置：{loc_label}）：\n{things}"
                )
            )
            event.stop_event()
            return

        # 未识别的子命令 → 显示帮助
        await event.send(
            MessageChain().message(
                "用法：\n"
                "  /goal          查看当前 Goal\n"
                "  /goal get      查看当前 Goal\n"
                "  /goal set [system|user] <目标内容>\n"
                "  /goal clear    清除 Goal"
            )
        )
        event.stop_event()

    def _parse_goal_args(self, text: str) -> tuple[str, str]:
        """从文本中解析 location 和 things。

        格式：[system|user] <things>
        location 默认为 system。
        """
        text = text.strip()
        parts = text.split(None, 1)
        if parts and parts[0].lower() in ("system", "user"):
            location = parts[0].lower()
            things = parts[1].strip() if len(parts) > 1 else ""
        else:
            location = "system"
            things = text
        return location, things

    # ---------------- Recall 人格回顾命令（一次性注入） ----------------

    @filter.command("recall")
    async def recall_command(self, event: AstrMessageEvent):
        """Recall 人格回顾命令。从 AstrBot 读取当前对话的人格设定，
        以下一次 LLM 请求时以 <system_WARNING> 标签一次性注入，
        引导 AI 回顾自身人格设定。

        用法：
          /recall    读取人格设定并排队注入（一次性，下次 LLM 请求后自动清除）

        原理：从 AstrBot PersonaManager 读取当前对话的人格 prompt，
        设置到 _recall_pending 标记中，on_llm_request 检测到该标记后
        以 <system_WARNING> 注入到 extra_user_content_parts，注入后立即清除标记。
        """
        umo = event.unified_msg_origin

        try:
            prompt_text, persona_name = await self._resolve_recall_prompt(event)
            if not prompt_text:
                await event.send(
                    MessageChain().message(
                        "未读取到当前对话的 AstrBot 人格设定，无法注入。\n"
                        "可在插件配置「人格遵循审核」中设置 persona_audit_prompt 作为回退人格，"
                        "或开启 use_astrbot_persona 以读取 AstrBot 人格管理器。"
                    )
                )
                event.stop_event()
                return

            # 设置一次性注入标记
            async with self._recall_pending_lock:
                self._recall_pending[umo] = prompt_text

            name_tag = f"（{persona_name}）" if persona_name else ""
            preview = prompt_text[:100]
            if len(prompt_text) > 100:
                preview += "..."

            await event.send(
                MessageChain().message(
                    f"🧠 Recall 已就绪！人格：{name_tag}。\n下次对话时将注入人格回顾：\n{preview}"
                )
            )
            logger.info(
                f"[recall] 已设置一次性注入标记，session={umo}, prompt_len={len(prompt_text)}"
            )
        except Exception as e:
            logger.error(f"[recall] 读取人格设定失败: {e}")
            await event.send(MessageChain().message(f"读取人格设定失败：{e}"))

        event.stop_event()

    async def _resolve_recall_prompt(self, event: AstrMessageEvent) -> tuple[str, str]:
        """解析 Recall 要注入的人格 prompt。

        Returns:
            (prompt_text, persona_name): prompt 文本和人格名称。
            读取失败时返回 ("", "")。
        """
        umo = getattr(event, "unified_msg_origin", None)

        # 优先级 1: use_astrbot_persona 启用时从 PersonaManager 读取
        if self.use_astrbot_persona:
            try:
                persona_mgr = getattr(self.context, "persona_manager", None)
                if persona_mgr:
                    # 1a. v3 当前对话默认人格（推荐，兼容性更好）
                    personality = await persona_mgr.get_default_persona_v3(umo=umo)
                    if personality:
                        text = ""
                        name = ""
                        if isinstance(personality, dict):
                            text = personality.get("prompt", "")
                            name = personality.get("name", "")
                        elif hasattr(personality, "prompt"):
                            text = personality.prompt
                            name = getattr(personality, "name", "")
                        if isinstance(text, str) and text.strip():
                            return text.strip(), name.strip() or "AstrBot 默认人格"

                    # 1b. v2 指定人格 ID（兜底）
                    select_id = (self.select_persona or "").strip()
                    if select_id:
                        try:
                            persona = await persona_mgr.get_persona(select_id)
                            if persona:
                                text = ""
                                name = select_id
                                if isinstance(persona, dict):
                                    text = persona.get("system_prompt") or persona.get(
                                        "prompt", ""
                                    )
                                    name = persona.get("name") or persona.get(
                                        "persona_name", select_id
                                    )
                                elif hasattr(persona, "system_prompt"):
                                    text = persona.system_prompt
                                    name = getattr(persona, "name", select_id)
                                elif hasattr(persona, "prompt"):
                                    text = persona.prompt
                                    name = getattr(persona, "name", select_id)
                                if isinstance(text, str) and text.strip():
                                    return text.strip(), str(name).strip()
                        except Exception:
                            pass
            except Exception as e:
                logger.debug(f"[recall] 从 PersonaManager 读取人格失败: {e}")

        # 优先级 2: 回退到静态 persona_audit_prompt
        if self.persona_audit_prompt:
            return self.persona_audit_prompt, "插件静态配置"

        return "", ""

    def _goal_get_response(self, umo: str) -> dict:
        """读取并返回当前 Goal 信息的公共逻辑。"""
        entry = self._goals.get(umo)
        if not entry:
            return {
                "status": "success",
                "has_goal": False,
                "message": "当前会话没有设置 Goal。",
            }
        return {
            "status": "success",
            "has_goal": True,
            "things": entry.get("things", ""),
            "location": entry.get("location", "system"),
            "set_at": entry.get("set_at", ""),
            "set_by": entry.get("set_by", ""),
        }

    @filter.llm_tool(name="goal")
    async def goal(
        self,
        event: AstrMessageEvent,
        action: str = "get",
        things: str = "",
        location: str = "system",
    ) -> dict:
        """管理当前会话的 Goal（目标指引）。Goal 会在每次 LLM 请求时自动注入，引导对话方向。

        Args:
            action(string): 操作类型：get（查看，默认）、set（设置）、clear（清除）
            things(string): Goal 内容，action=set 时必填；例如"今天专注讨论旅行计划"
            location(string): 注入位置：system（默认，追加到系统提示词）或 user（追加到每条用户消息末尾，不落盘到历史）
        """
        act = (action or "get").strip().lower()
        umo = event.unified_msg_origin

        if act == "get" or not act:
            return self._goal_get_response(umo)

        if act == "clear":
            removed = umo in self._goals
            self._goals.pop(umo, None)
            async with self._goals_lock:
                self._save_goals()
            if removed:
                return {"status": "success", "message": "Goal 已清除。"}
            return {"status": "success", "message": "当前会话没有设置 Goal。"}

        if act == "set":
            things = (things or "").strip()
            if not things:
                return {"status": "error", "message": "action=set 时 things 不能为空。"}
            loc = (location or "system").strip().lower()
            if loc not in ("system", "user"):
                loc = "system"
            self._goals[umo] = {
                "things": things,
                "location": loc,
                "set_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "set_by": "llm",
            }
            async with self._goals_lock:
                self._save_goals()
            loc_label = "系统提示词" if loc == "system" else "用户消息末尾（不落盘）"
            return {
                "status": "success",
                "message": f"Goal 已设置（注入位置：{loc_label}）：{things}",
            }

        return {
            "status": "error",
            "message": f"未识别的 action：{action}。支持 get / set / clear",
        }

    @filter.llm_tool(name="tool_get_calendar")
    async def tool_get_calendar(
        self,
        event: AstrMessageEvent,
        ical_url: str = "",
        proxy: str = "",
        date_from: str = "",
        date_to: str = "",
        days_ahead: int = 7,
        days_back: int = 0,
        max_events: int = 20,
        tz_offset: int = -999,
    ) -> dict:
        """获取 iCal 日历中指定时间范围内的事件列表。支持 Google Calendar 等标准 iCal 地址。

        Args:
            ical_url(string): iCal URL，可选（覆盖配置中的 calendar_ical_url）
            proxy(string): HTTP 代理地址，可选（如 http://127.0.0.1:7890）
            date_from(string): 起始日期，YYYY-MM-DD 格式，可选。传入后优先于 days_back。
            date_to(string): 结束日期，YYYY-MM-DD 格式，可选。传入后优先于 days_ahead。
            days_ahead(int): 查询未来多少天，默认 7（date_from/date_to 未传入时生效）
            days_back(int): 查询过去多少天，默认 0（date_from/date_to 未传入时生效）
            max_events(int): 最多返回事件数，默认 20
            tz_offset(int): 本地时区偏移（小时），不传则自动读取 AstrBot 系统时区
        """
        args = {
            "ical_url": ical_url,
            "proxy": proxy,
            "date_from": date_from,
            "date_to": date_to,
            "days_ahead": days_ahead,
            "days_back": days_back,
            "max_events": max_events,
            # -999 为哨兵值，表示用户未传入，calendar.py 会自动读取系统时区
            "tz_offset": tz_offset,
        }
        message = await run_get_calendar(self, args)
        return {"status": "success", "message": message}

    # ---------------- Mnemosyne 向量查找（LLM 内部工具） ----------------
    @filter.llm_tool(name="search_memory_vector")
    async def search_memory_vector(
        self,
        event: AstrMessageEvent,
        query: str,
        top_k: int = 5,
        collection_name: str = "",
    ) -> dict:
        """向量库查找（Mnemosyne/Milvus）。

        注意：这是给 LLM 函数调用的内部工具，不提供为用户指令。

        Args:
            query(string): 查询文本，必填
            top_k(int): 返回条数，默认 5，最大 50
            collection_name(string): 可选，覆盖配置中的 collection_name
        """

        query = (query or "").strip()
        if not query:
            return {"status": "error", "message": "缺少 query 参数"}

        try:
            results = await self._mnemosyne_vector_search(
                query=query,
                top_k=top_k,
                collection_name=collection_name,
            )
        except Exception as e:
            return {"status": "error", "message": f"向量查找失败：{e}"}

        return {
            "status": "success",
            "query": query,
            "top_k": max(1, min(int(top_k or 5), 50)),
            "results": results,
        }

    @filter.llm_tool(name="list_memory_vector")
    async def list_memory_vector(self, event: AstrMessageEvent) -> dict:
        """列出 Mnemosyne 中所有集合（供 LLM 内部调用）。"""
        try:
            text = await self._collect_forwarded_output_text(
                event, "list_collections_cmd"
            )
            return {"status": "success", "message": text}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @filter.llm_tool(name="list_records_memory_vector")
    async def list_records_memory_vector(
        self, event: AstrMessageEvent, collection_name: str = "", limit: int = 5
    ) -> dict:
        """列出 Mnemosyne 集合中的记录（供 LLM 内部调用）。

        Args:
            collection_name(string): 集合名，可选
            limit(int): 返回条数，默认 5，最大 50
        """
        try:
            collection_name = (collection_name or "").strip() or None
            limit = max(1, min(int(limit or 5), 50))
            text = await self._collect_forwarded_output_text(
                event,
                "list_records_cmd",
                collection_name=collection_name,
                limit=limit,
            )
            return {
                "status": "success",
                "message": text,
                "collection_name": collection_name,
                "limit": limit,
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @filter.llm_tool(name="remember_memory_vector")
    async def remember_memory_vector(
        self, event: AstrMessageEvent, content: str
    ) -> dict:
        """写入记忆到 Mnemosyne（供 LLM 内部调用）。

        Args:
            content(string): 记忆内容，必填
        """
        content = (content or "").strip()
        if not content:
            return {"status": "error", "message": "缺少 content 参数"}
        try:
            text = await self._collect_forwarded_output_text(
                event, "remember_cmd", content=content
            )
            return {"status": "success", "message": text}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @filter.llm_tool(name="delete_record_memory_vector")
    async def delete_record_memory_vector(
        self,
        event: AstrMessageEvent,
        memory_id: str,
        session_id: str = "",
        confirm: str = "",
    ) -> dict:
        """删除 Mnemosyne 记录（供 LLM 内部调用）。

        Args:
            memory_id(string): 记录ID(memory_id)，必填
            session_id(string): 会话ID，可选
            confirm(string): 确认参数，可选（由 Mnemosyne 定义）
        """
        memory_id = (memory_id or "").strip()
        if not memory_id:
            return {"status": "error", "message": "缺少 memory_id 参数"}
        session_id = (session_id or "").strip() or None
        confirm = (confirm or "").strip() or None
        try:
            text = await self._collect_forwarded_output_text(
                event,
                "delete_record_cmd",
                memory_id=memory_id,
                session_id=session_id,
                confirm=confirm,
            )
            return {"status": "success", "message": text}
        except Exception as e:
            return {"status": "error", "message": str(e)}
