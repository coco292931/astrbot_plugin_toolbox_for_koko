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
- 🧠 **支持 Mnemosyne 向量记忆库**：配合 [astrbot_plugin_mnemosyne](https://github.com/lxfight/astrbot_plugin_mnemosyne.git) 使用，提供向量检索与记忆管理能力。
- 📦 **内存管理功能**：
  - 支持内存的增删改查操作。
  - 新增 `/tool_memory` 管理命令，便于管理员操作内存。
  - 自动将用户内存注入到 LLM 上下文中，提升对话的智能性。
- 📤 **主动消息发送**：支持向指定 QQ 好友或群聊发送文本消息。

## 🏗️ 项目架构

从 v1.0.0 开始，项目完成了模块化架构重构，结构如下：

```text
astrbot_plugin_toolbox_for_koko/
├── main.py                   # 插件入口，配置加载、工具注册表、LLM 工具暴露
├── metadata.yaml             # 插件元数据
├── package.py                # 统一导出入口
├── core/
│   ├── __init__.py
│   ├── config.py             # 配置加载与解析工具函数
│   └── memory_manager.py     # 内存管理器（本地 JSON 存储）
├── tools/
│   ├── __init__.py
│   ├── weather.py            # 天气查询（位置、实时、多日、历史）
│   ├── search.py             # 联网搜索
│   ├── fetch_url.py          # 网页抓取
│   ├── history.py            # 历史消息
│   ├── local_memory.py       # 本地内存管理（增删改查）
│   ├── send_msg.py           # 主动消息发送
│   └── bridge.py             # Mnemosyne / Qzone 工具桥接
├── handlers/
│   ├── __init__.py
│   └── command_handlers.py   # 命令处理器映射表 (TOOL_HANDLER_MAP)
├── _conf_schema.json         # AstrBot WebUI 配置 schema
├── _conf_schema_config.json  # 配置项默认值
├── requirements.txt          # 项目依赖
├── CHANGELOG.md              # 更新日志
```

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

### 📦 内存管理 (memory)

- **max_memories_per_user**: 每个用户最大记忆条数（默认 100）。
- **enable_admin_tool_memory_command**: 是否启用 `/tool_memory` 管理命令。
- **memory_inject_enabled**: 是否启用内存注入功能。
- **memory_inject_count**: 注入的内存条目数量（默认 5）。

### 🧠 向量数据库引擎 (mnemosyne)

- **embedding_provider_id**: 向量嵌入使用的 Embedding Provider ID；留空则自动使用 AstrBot 中配置的第一个 Embedding Provider。
- **milvus_lite_path / address**: 本地或远程的 Milvus 数据库地址。配置 `milvus_lite_path` 优先使用单文件 SQLite版 Milvus。
- **collection_name / db_name**: 数据存放的集合。
- **use_session_filtering**: 是否启用会话过滤。
- **platform_blacklist**: 平台黑名单列表。

## 🚀 智能化工具调用机制

本插件不再依赖大模型凭空猜测工具名，而是采用 **Search-Call-Run** 三段式引导：

1. **search_koko_tools**: 大模型通过关键词搜索确认是否存在对应工具。
2. **run_koko_tool**: 使用搜索确认的 `tool_name` 及其参数执行。
3. **call_koko_tools**: 兜底方案，当搜索无法定位时查看完整工具列表。

这种机制极大提升了复杂任务下的指令准确度与容错性。

## 🧭 使用建议

1. **天气查询**：先调 `tool_weather_location` 查 ID，再调 `tool_weather` 查详情，可准确避开同名地名。
2. **网页阅读**：当搜索结果中的摘要不足以回答问题时，大模型会通过 `tool_fetch_url` 深入阅读特定网页。
3. **历史消息**：使用 `tool_history` 拉取群聊或私聊消息，支持分页和缓存刷新。
4. **记忆管理**：大模型可通过 `add_memory`、`search_memories` 等工具自动管理用户记忆，提升个性化交互体验。

## 🌐 天气 API 路径说明

- Weather: `https://{weather_host}/v7/weather/...`
- Geo: `https://{geo_host}/geo/v2/city/lookup?...`

## 🔧 开发调试

项目提供了多个辅助脚本用于代码分析和调试：

```bash
# 检查未使用的 handle 注册项
python check_dead_handles.py

# 查找未使用的工具方法（死方法）
python find_dead_methods.py

# 分析方法边界 / QWeather 相关代码
python find_handle_bounds.py
python find_qweather_dead.py
python find_qweather_ranges.py

# 列出所有工具方法定义
python find_methods.py