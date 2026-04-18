# 🧰 Koko 多功能工具箱 (Toolbox for Koko)

> AstrBot 专属增强插件，为机器人提供丰富的内置能力集，包含天气查询、强大的搜索引擎、网页抓取，并且支持提取群聊与好友的历史记录以复盘上下文。

## ✨ 功能特性

- 💬 **关键词随机捕捉响应 (Interaction)**：监听特定关键词并按自定义概率触发对话回复，活跃群聊氛围。
- 🌤️ **多维天气预报与生活指数**：基于和风天气 (QWeather)，支持实时、3日、7日的天气预报以及生活指数查询。支持历史天气回溯。内置 LLM 总结功能，可直传原始 JSON 给大模型生成亲切的天气简报。
- 🔍 **智能联网网页搜索**：集成智谱大模型搜索接口。支持普通/深度搜索、多粒度摘要提取、时效性过滤。
- 🌐 **高安全网页抓取 (Fetch)**：支持提取指定 URL 的正文文本。
  - **SSRF 深度防御**：自动阻止私有 IP、本地回环及云平台元数据地址，防止内网穿透。
  - **AI 智能总结**：网页内容过长时，可自动调用 LLM 进行提炼，避免 Token 溢出。
- 📜 **历史聊天记录寻回**：支持群聊和私聊历史消息拉取。精简数据结构，大幅降低复盘场景下的 Token 开销。

## ⚙️ 核心前置配置

请在 AstrBot 后台管理面板中按分组配置：

### 🌤️ 天气 (weather)

- **qweather_jwt_token / qweather_key**: 和风天气认证信息。
- **enable_weather_summary**: 开启后可调用 LLM 总结 7 日预报。
- **weather_summary_llm_provider_id**: 指定用于天总结的模型 Provider ID。

### 🔍 搜索 (search)

- **zhipu_key**: 智谱官方 API 密钥。
- **zhipu_search_model**: 联网搜索使用的模型 (如 `glm-4.7-flash`)。

### 🌐 网页抓取 (web_fetch)

- **enable_fetch_url**: 是否启用网页抓取工具。
- **fetch_url_over_limit_mode**: 超限策略 (`truncate` | `ai_summary` | `full`)。
- **fetch_url_blocked_targets**: 额外禁用的 Host/IP 列表 (JSON 数组或列表)。

### 💬 交互触发 (interaction)

- **enable_keyword_capture_reply**: 开启后，消息命中关键词时会尝试自动回复。
- **keyword_capture_words**: 触发回复的关键词列表（如 `["koko", "可可"]`）。
- **keyword_capture_reply_probability**: 命中后回复的概率（`0` ~ `1.0`）。

## 🚀 智能化工具调用机制

本插件不再依赖大模型凭空猜测工具名，而是采用 **Search-Call-Run** 三段式引导：

1. **search_koko_tools**: 大模型通过关键词搜索确认是否存在对应工具。
2. **run_koko_tool**: 使用搜索确认的 `tool_name` 及其参数执行。

这种机制极大提升了复杂任务下的指令准确度与容错性。

## 🧭 使用建议

1. **天气查询**：先调 `tool_weather_location` 查 ID，再调 `tool_weather` 查详情，可准确避开同名地名。
2. **网页阅读**：当搜索结果中的摘要不足以回答问题时，大模型会通过 `tool_fetch_url` 深入阅读特定网页。

## 🌐 天气 API 路径说明

- Weather: `https://{weather_host}/v7/weather/...`
- Geo: `https://{geo_host}/geo/v2/city/lookup?...`
