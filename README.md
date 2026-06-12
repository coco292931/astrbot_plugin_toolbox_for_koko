# 🧰 Koko 多功能工具箱 (Toolbox for Koko)

> AstrBot 专属增强插件，为机器人提供丰富的内置能力集，包含天气查询、强大的搜索引擎、网页抓取，并且支持提取群聊与好友的历史记录以复盘上下文。

## ✨ 功能特性

- 💬 **关键词捕捉与群聊主动回复 (Interaction)**：监听特定关键词并按自定义概率触发对话回复。支持基础主动回复概率，未命中关键词时也可在群聊中按概率主动参与对话。内置独立的群聊上下文管理器（`KCContextManager`），记录群聊消息并注入上下文，支持延迟图片转述、自定义 prompt 模板，与 AstrBot LTM 完全解耦。
- 🖼️ **图片转述后处理 (ImageCaption)**：在 `on_llm_request` 钩子中检测 AstrBot 图片转述失败，自动从原始消息重新提取图片进行降级转述（下载、压缩、GIF 取帧）。自动清理 AstrBot 的失败标记，仅保留转述成功的内容。与群聊上下文管理完全解耦。
- 🎨 **生图结果自动识图 (ImageGenerationResult)**：当 `astrbot_plugin_image_generation` 的后台生图任务完成并唤醒 AI 继续发送结果时，自动读取任务里的生成图片路径，先做一次识图摘要，再把摘要注入到该轮上下文里，方便 AI 在发图时顺手带上识图说明。
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
- 🎯 **Goal 目标指引**：会话级目标设置，用户与 LLM 均可通过 `/goal` 命令或 `set_goal` 工具设置/清除当前对话的 Goal，自动注入到每次 LLM 请求中引导对话方向。持久化保存，重启后自动恢复。
- 📅 **iCal 日历查询**：通过 `tool_get_calendar` 工具拉取 Google Calendar 等标准 iCal 日历，返回指定时间范围内的事件列表。支持代理、自定义时区（自动读取 AstrBot 系统时区），零外部依赖。

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
│   ├── memory_manager.py     # 内存管理器（本地 JSON 存储）
│   ├── kc_context.py         # 群聊上下文管理器（KCContextManager）
│   ├── image_caption.py      # 图片转述前处理器（ImageCaptionHandler）
│   ├── image_generation_result.py # 生图结果识图注入处理器（ImageGenerationResultHandler）
│   └── content_audit.py      # 自动内容审核校正器（ContentAuditLoop）
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

### 💬 交互触发与群聊上下文管理 (interaction)

**基础设置：**
- **enable_keyword_capture_reply**: 总开关。关闭后关键词触发和主动回复均不工作。
- **keyword_capture_words**: 触发回复的关键词列表（如 `["koko", "可可"]`）。
- **keyword_capture_reply_probability**: 关键词命中后回复的概率（`0` ~ `1.0`）。
- **keyword_capture_base_probability**: 未命中关键词时在群聊中主动回复的基础概率。设为 `0` 关闭。
- **keyword_capture_bypass_probability_on_at**: 被 @ 时跳过概率直接回复。开启后消息中包含 @机器人 时必定触发。
- **keyword_capture_whitelist**: 群聊白名单，仅列表中的群 ID 才会触发。为空不启用。

**会话管理：**
- **keyword_capture_session_mode**: 会话要求模式。`auto_new`（自动新建）、`active_only`（仅匹配活跃）、`always_new`（每次都新建）。

**群聊上下文管理：**
- **keyword_capture_manage_context**: 是否由本插件管理群聊上下文并注入。关闭不影响关键词触发。
- **keyword_capture_context_max_cnt**: Session 缓冲区最大消息数（默认 100）。
- **keyword_capture_context_history_limit**: 注入后 LLM 对话历史条数（默认 50）。
- **keyword_capture_context_image_limit**: 上下文中最多转述的图片数（默认 3）。设为 0 关闭图片转述。
- **keyword_capture_context_prompt**: 上下文注入 prompt 模板。可用 `{context}` 和 `{prompt}` 占位符。留空使用内置中文模板。

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

### 🖼️ 图片转述后处理 (image_caption)

- **image_caption_hook_enabled**: 启用图片转述后处理。开启后检测 AstrBot 图片转述失败并自动降级。关闭时不影响群聊上下文中的图片转述。
- **image_caption_tool_enabled**: 启用同级 LLM 工具 `tool_image_caption`。开启后可分别通过 `paths`、`urls`、`base64_list`、`data_urls` 四类列表，或直接使用消息附图做识图/转述。
- **image_caption_prompt_template**: 图片转述提示词模板，可用 `{image_type}` 代表图片类型。留空使用默认模板。
- **image_caption_parse_error_keywords**: 解析错误关键词列表。默认已预填常见格式/解析错误关键词；命中后跳过 URL 直传，直接走下载/压缩/GIF 取帧降级。
- **image_caption_sensitive_fallback_enabled**: 启用敏感内容兜底识图。当 AstrBot 因不安全/敏感内容等错误拒绝图片转述时，尝试改走已配置的 AstrBot Provider 列表。
- **image_caption_sensitive_error_keywords**: 敏感错误关键词列表。默认已预填常见敏感/安全拒绝关键词；命中后才触发敏感内容兜底 Provider。
- **image_caption_sensitive_fallback_provider_ids**: 敏感内容兜底 Provider ID 列表。按顺序填写 AstrBot 中已配置好的支持图片输入的 Provider。
- **image_caption_sensitive_fallback_system_prompt**: 敏感内容兜底 Provider 的前置提示词。留空使用插件内置默认前置提示；运行时会与实际发送给 LLM 的图片转述提示词拼接，不再区分单独工具入参。
- **image_caption_sensitive_fallback_max_tokens**: 敏感内容兜底 Provider 调用的 `max_tokens`，默认 `300`。
- **image_generation_result_hook_enabled**: 启用生图结果自动识图。开启后，当 `astrbot_plugin_image_generation` 完成任务并唤醒 AI 交付结果时，toolbox 会先识别生成图再注入摘要。
- **image_generation_result_prompt_template**: 生图结果识图提示词模板，可用 `{task_id}`、`{image_index}`、`{image_count}` 占位。留空使用默认模板。
- **image_generation_result_max_images**: 单次任务最多识别多少张生成图，默认 `1`。

### 🎯 Goal 目标指引

会话级目标指引，支持用户与 LLM 双侧设置，在每次 LLM 请求时自动注入，引导对话方向。Goal 持久化保存，重启后自动恢复。

**用户命令：**
- `/goal set <内容>` — 设置 Goal，默认注入到系统提示词
- `/goal set system <内容>` — 注入到系统提示词（全局生效）
- `/goal set user <内容>` — 注入到每条用户消息末尾（不落盘到历史对话）
- `/goal clear` — 清除当前会话的 Goal

**LLM 工具 `set_goal`：** LLM 可在对话中自主调用，参数为 `action`（`set`/`clear`）、`things`（内容）、`location`（`system`/`user`）。
**LLM 工具 `get_goal`：** 只读工具，返回当前会话已设置的 Goal 内容、注入位置、设定时间和设定者。

**用户命令 `/goal` 也可直接查看当前 Goal：** 不带子命令时返回当前会话的 Goal 状态。
### � iCal 日历查询 (calendar)

通过标准 iCal URL 拉取日历数据，返回指定时间范围内的事件列表。零外部依赖，内置 iCal 解析器。

- **calendar_ical_url**: iCal 订阅地址，例如 Google Calendar 的 `.ics` 私有链接。
- **calendar_proxy**: 拉取 iCal 时使用的 HTTP 代理（如 `http://127.0.0.1:7890`），留空不使用。

**LLM 工具 `tool_get_calendar`：** 支持参数 `ical_url`（覆盖配置）、`proxy`（覆盖配置）、`date_from`（起始日期 YYYY-MM-DD）、`date_to`（结束日期 YYYY-MM-DD）、`days_ahead`（未来天数，默认 7）、`days_back`（过去天数，默认 0）、`max_events`（最多返回条数，默认 20）、`tz_offset`（时区偏移小时数，不传时自动读取 AstrBot 系统时区配置，失败则回退 UTC+8）。

> `date_from`/`date_to` 优先于 `days_ahead`/`days_back`；不传绝对日期时以相对天数计算范围。

### �📋 自动内容审核校正 (content_audit)

自动审核 AI 回复质量，支持轮数触发和关键词触发两种方式。审核结果会在下一条用户消息时以 `<system_WARNING>` 标签注入，引导 LLM 调整回复风格。
> 本功能与1.3.2更新为异步处理，审核llm回复后才插入用户消息当中，避免阻塞。

- **content_audit_enabled**: 总开关。开启后每 N 条 AI 回复触发一次审核。
- **content_audit_rounds**: 审核触发消息条数阈值（默认 5）。每 N 条 AI 回复触发一次审核。
- **content_audit_fetch_rounds**: 审核时抓取的消息条数（默认 10）。
- **content_audit_criteria**: 审核标准文本。定义 AI 回复应遵守的标准，LLM 将据此判断回复质量。
- **content_audit_keywords**: 审核关键词列表（如 `["我不确定", "抱歉"]`）。AI 回复命中关键词时立即触发审核。
- **content_audit_min_interval**: 最小触发间隔轮数（默认 2）。两次审核之间最少间隔的轮数，避免刚审完又触发。设为 0 关闭。

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
