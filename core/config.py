from __future__ import annotations

import json
from pathlib import Path

from astrbot.api import logger


def load_schema_defaults(schema_config_path: Path | None = None) -> dict:
    """Load default values from local _conf_schema_config.json when available."""
    cfg_path = (
        schema_config_path
        or Path(__file__).resolve().parent.parent / "_conf_schema_config.json"
    )
    if not cfg_path.exists():
        return {}

    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}

        defaults: dict = {}

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


def extract_grouped_runtime_config(raw: dict) -> dict:
    """读取分组配置结构，并拍平成运行时键值。"""
    if not isinstance(raw, dict):
        return {}

    incoming: dict = {}

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
            "keyword_capture_base_probability",
            "keyword_capture_whitelist",
            "keyword_capture_session_mode",
            "keyword_capture_manage_context",
            "keyword_capture_context_max_cnt",
            "keyword_capture_context_history_limit",
            "keyword_capture_context_image_limit",
            "keyword_capture_context_prompt",
        ):
            if key in interaction_cfg:
                incoming[key] = interaction_cfg.get(key)

    image_caption_cfg = raw.get("image_caption", {})
    if isinstance(image_caption_cfg, dict):
        for key in (
            "image_caption_hook_enabled",
            "image_caption_prompt_template",
        ):
            if key in image_caption_cfg:
                incoming[key] = image_caption_cfg.get(key)

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

    mnemosyne_cfg = raw.get("mnemosyne", {})
    if isinstance(mnemosyne_cfg, dict):
        for key in (
            "embedding_provider_id",
            "milvus_lite_path",
            "address",
            "db_name",
            "collection_name",
            "use_session_filtering",
            "platform_blacklist",
        ):
            if key in mnemosyne_cfg:
                incoming[key] = mnemosyne_cfg.get(key)

        auth_cfg = mnemosyne_cfg.get("authentication")
        if isinstance(auth_cfg, dict):
            incoming["authentication"] = {
                "token": auth_cfg.get("token", ""),
                "user": auth_cfg.get("user", ""),
                "password": auth_cfg.get("password", ""),
            }

    if "summary_prompt" in raw:
        incoming["summary_prompt"] = raw.get("summary_prompt")

    return incoming
