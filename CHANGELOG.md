# 🧰 Koko 多功能工具箱 (Toolbox for Koko) 更新日志

## [1.3.2] - 2026-05-27

### 🔧 变更

- **审核 LLM 调用改为后台异步任务**：`_call_audit_llm` 移至 `asyncio.create_task` 后台执行，不再阻塞 `on_llm_response`，用户可即时收到 AI 回复。
- **连续触发上下文累积机制**：当审核任务正在运行时再次触发审核，新请求入队等待。前一任务完成后自动从水印位置取新累积的消息继续审核，不重复审旧内容。
- **队列首位合并**：队列中只保留一个等待占位，后续多次触发自动合并，避免无限累积。
- **最小触发间隔**：新增 `content_audit_min_rounds` 配置，关键词触发时若距上次审核不足最小轮数则设保持位等待，达标后再触发。
- **水印时序修复**：修复 `finally` 中水印在出队前被清理导致下一个任务取整个缓冲区而非新增消息的问题。
- **`_audit_queue` 类型注释修正**：`list[dict]` → `list[bool]`。
- **`_inject_ready` 双标记防丢失**：后台审核完成后同时设 `_corrections` 和 `_inject_ready`，`inject_to_request` 先检查 `_inject_ready` 再 pop 结果，避免后台未完成时结果被误 pop 丢失。

### ✨ 新增

- **`content_audit_min_rounds` 配置项**（int, 默认 2）— 最小触发间隔轮数。防止刚审核完又被关键词触发。

## [1.3.0] - 2026-05-26

### ✨ 新增

- **自动内容审核校正（`core/content_audit.py`）**：
  - 新增 `ContentAuditLoop` 类，提供独立的 LLM 回复审核校正能力，与 keyword_capture 上下文管理完全解耦。
  - **双触发机制**：支持轮数触发和关键词触发两种审核触发方式。
    - 轮数触发：每 N 条 AI 回复自动触发一次审核，审核后计数器重置。
    - 关键词触发：AI 回复命中审核关键词时立即触发审核，不消耗轮数计数器。
  - 审核流程：从 `_session_chats` 抓取最近对话 → 合并审核标准与上次校正方向 → 调用审核 LLM → 存储结果。
  - 校正注入：下一条用户消息时以 `<system_WARNING>` 标签注入到 `extra_user_content_parts`，LLM 据此调整回复。
  - 纯内存设计：计数器与校正结果均为内存存储，重启即重置，零外部依赖。
  - 审核 LLM 结果解析：支持"无需调整"和"调整方向:xxx"两种输出格式。

- **新增 `content_audit` 独立配置组**：
  - `content_audit_enabled`（bool, 默认 false）— 总开关。
  - `content_audit_rounds`（int, 默认 5）— 审核触发消息条数阈值。
  - `content_audit_fetch_rounds`（int, 默认 10）— 审核时抓取的消息条数。
  - `content_audit_criteria`（string）— 审核标准文本，LLM 据此判断回复质量。
  - `content_audit_keywords`（list）— 审核关键词列表，命中即触发。

### 🔧 变更

- **`kc_context_recorder` 守卫条件放宽**：由仅 `keyword_capture_manage_context` 改为 `keyword_capture_manage_context or content_audit_enabled`，审核链路可独立使用上下文缓冲区。
- **`config.py` 同步**：`extract_grouped_runtime_config` 新增 `content_audit` 分组读取。
- **`on_llm_response` 重构**：审核链路（独立）放在 keyword_capture 守卫之前，确保对所有 LLM 回复生效。
- **`_extract_reply_text` 增强**：增加 `str` 类型直接返回和 `dict` 类型兜底提取，覆盖更多 response 结构。

### 🏗 项目结构

- 新增 `core/content_audit.py` — 自动内容审核校正器模块，与 `core/kc_context.py` 解耦。

### ⚙ 配置更新

- 新增 `content_audit` 配置组，包含 5 个配置项。详见 README。

### ✨ 新增

- **图片转述后处理（`core/image_caption.py`）**：
  - 新增 `ImageCaptionHandler`，在 `on_llm_request` 钩子中检测 AstrBot 图片转述失败（`[Image Captioning Failed]` 标记），从原始消息重新提取图片进行降级转述。
  - 支持自动识别 Provider：优先读取 AstrBot 配置的 `image_caption_provider_id`，其次回退到当前对话模型。
  - 支持 URL 直传和降级（下载 → PIL GIF 取帧 → 本地路径重试）两种模式，降级后自动读取 AstrBot 压缩配置进行压缩。
  - 清理 `[Image Attachment: path ...]` 和 `[Image Captioning Failed]` 标记，仅保留转述成功的 `[GIF: 描述]`。
  - 新增 `image_caption` 独立配置组，包含 `image_caption_hook_enabled`（开关）和 `image_caption_prompt_template`（自定义提示词模板，支持 `{image_type}` 占位符）。

- **被 @ 时跳过概率直接回复**：
  - 新增 `keyword_capture_bypass_probability_on_at` 配置项（默认 false）。
  - 开启后，当消息中包含 @机器人 时，忽略关键词概率和基础概率，必定触发回复。
  - 检测逻辑：遍历消息组件中的 `At` 类型，匹配 `qq` 与机器人自身 ID。

### 🔧 变更

- **`ImageCaptionHandler` 从「前处理」改为「后处理」**：不再拦截 `req.image_urls`（此时 AstrBot 已处理完毕），改为检测 `extra_user_content_parts` 中的失败标记。
- **`_resolve_caption_provider` 统一探测逻辑**：与 `kc_context._resolve_image_caption_provider` 一致的优先级（LTM 配置 → 当前 Provider），确保私聊也使用正确的图片转述模型。
- **`kc_context._transcribe_images` 增强**：添加图片转述缓存 TTL（3600s），同 URL 不重复调用；GIF 下载后取第一帧转 JPEG 降级。
- **`kc_context.record_message` 移除群聊限制**：私聊消息也可被上下文管理器记录。
- **`kc_context.build_prompt` 无历史时返回格式化消息**：无上下文注入时返回 `[昵称/时间]: 消息` 格式，保持发送者信息一致性。

### 🏗 项目结构

- 新增 `core/image_caption.py` — 图片转述后处理器模块，与 `core/kc_context.py` 完全解耦。
  - 检测 `req.image_urls` 有内容时立即清空并自行转述，支持 URL 直传和降级（下载→PIL GIF 取帧→本地路径重试）两种模式。
  - 自动识别图片类型（通过 OneBot `sub_type` 区分普通图片和表情包），提示词模板支持 `{image_type}` 占位符。
  - 下载图片后自动读取 AstrBot 压缩配置（`image_compress_enabled/image_compress_options`）进行压缩，避免超大图片导致 Provider 报错。
  - 新增 `image_caption` 独立配置组，包含 `image_caption_hook_enabled`（开关）和 `image_caption_prompt_template`（自定义提示词模板）。
  - `image_caption_hook_enabled` 关闭时不影响 `kc_context._transcribe_images`（群聊上下文中的图片转述），两者解耦。

### 🔧 变更

- **`on_llm_request` 重构**：图片处理逻辑从内联代码抽离为独立的 `ImageCaptionHandler.process()` 调用，代码量减少 80%+，逻辑更清晰。
- **`kc_context_recorder` 扩展**：filter 从 `GROUP_MESSAGE` 扩展为 `GROUP_MESSAGE | PRIVATE_MESSAGE`，私聊消息也可被上下文管理器记录。
- **`record_message` 移除群聊限制**：不再限制消息类型，私聊消息同样可以存入上下文缓冲区。
- **`_transcribe_images` 稳定性增强**：URL 转述失败时自动降级（下载→PIL GIF 取帧→本地路径重试），并写入 TTL 缓存避免重复调用。
- **无历史上下文时返回格式化消息**：`build_prompt` 在无历史注入时返回 `[昵称/时间]: 消息` 格式，保持与有上下文时一致的发送者信息。

### 🏗 项目结构

- 新增 `core/image_caption.py` — 图片转述前处理器模块，与 `core/kc_context.py` 完全解耦，独立维护。

### ⚙ 配置更新

- 新增 `image_caption` 配置组：
  - `image_caption_hook_enabled`（bool, 默认 true）
  - `image_caption_prompt_template`（string, 默认 ""）
  - 新增 `keyword_capture_bypass_probability_on_at`（bool, 默认 false，位于 `interaction` 分组）。

## [1.1.0] - 2026-05-21

### ✨ 新增

- **群聊上下文管理与主动回复增强**：
  - 新增 `KCContextManager`（`core/kc_context.py`）— 独立于 AstrBot LTM 的群聊上下文管理模块，支持消息记录、延迟图片转述、自定义 prompt 模板注入。
  - 新增 `keyword_capture_manage_context` 配置开关，与关键词触发功能解耦，可独立开启/关闭。
  - 新增 `keyword_capture_base_probability` 基础主动回复概率，未命中关键词时也可按概率在群聊中主动回复，活跃群聊氛围。
  - 新增 `keyword_capture_whitelist` 群聊白名单，支持按群 ID 过滤触发范围。
  - 新增 `keyword_capture_session_mode` 会话模式，支持 `auto_new`（自动新建）、`active_only`（仅匹配活跃）、`always_new`（每次都新建）三种策略。
  - 新增 `keyword_capture_context_prompt` 自定义 prompt 模板，支持 `{context}` 和 `{prompt}` 占位符。
  - 新增 `kc_context_recorder` handler（priority=98），独立于关键词 handler 无差别记录群聊消息，图片仅存 URL 不转述。
  - 新增 `on_llm_response` 钩子，AI 回复自动记录回上下文缓冲区，形成完整对话闭环。
  - 新增 `clear_session` 方法清理指定群聊的上下文。

- **配置项扩展**：
  - `interaction` 配置组从 3 项扩展至 **13 项**，完整支持群聊上下文管理。
  - 图片转述 Provider 自动探测：优先读取 AstrBot 配置的 `provider_ltm_settings.image_caption_provider_id`，其次尝试第一个可用 Provider。
  - 图片转述提示词自动读取 AstrBot 配置 `provider_settings.image_caption_prompt`，无需重复配置。
  - 上下文缓冲区最大消息数（默认 100）和注入 LLM 消息数（默认 50）分别独立配置。

### 🔧 变更

- **`keyword_capture_reply_handler` 重写**：
  - 支持两种触发模式：关键词命中（使用 `keyword_capture_reply_probability`）和基础主动回复（使用 `keyword_capture_base_probability`）。
  - 私聊消息必须命中关键词才触发，群聊可同时使用两种模式。
  - 通过 `event.set_extra("is_keyword_capture_request", True)` 标记请求来源，供 `on_llm_request` 识别。
  - 在 `on_llm_request` 中自动撤销 AstrBot LTM 追加的群聊上下文（`"You are now in a chatroom..."`），避免与 Toolbox 自注入上下文重复。

- **`<system_reminder>` 过滤**：在 `on_llm_request` 中自动清理 AstrBot 框架注入的 `<system_reminder>` 运行时上下文标记，避免出现在 keyword_capture 的 LLM 上下文中。
- **导入优化**：移除 `main.py` 中未使用的 `collections.defaultdict`、`Optional`、`Dict`、`At`、`Image`、`Plain` 导入。

### 🏗 项目结构

- 新增 `core/kc_context.py` — 群聊上下文管理器模块，与 `main.py` 的 handler 层分离，便于后续维护和扩展。

### ⚙ 配置更新

- 新增配置项（位于 `interaction` 分组下）：
  - `keyword_capture_base_probability`（float, 默认 0.0）
  - `keyword_capture_whitelist`（list, 默认 []）
  - `keyword_capture_session_mode`（string, 默认 "auto_new"）
  - `keyword_capture_manage_context`（bool, 默认 false）
  - `keyword_capture_context_max_cnt`（int, 默认 100）
  - `keyword_capture_context_history_limit`（int, 默认 50）
  - `keyword_capture_context_image_limit`（int, 默认 3）
  - `keyword_capture_context_prompt`（string, 默认 ""）

## [1.0.0] - 2026-05-09

### ✨ 新增

- **代码架构重构 — 模块化拆分**：
  - 将原本 1600+ 行的单体 `main.py` 拆分为多个可维护的子模块，项目结构更清晰：
    - `tools/` — 天气、搜索、网页抓取、历史消息、本地记忆、消息发送、桥接等工具的实现。
    - `handlers/` — 命令处理器层，提供统一的 `TOOL_HANDLER_MAP` 工具名称到处理函数的映射。
    - `core/` — 核心基础设施，包含配置加载（`config.py`）与内存管理器（`memory_manager.py`）。
    - `package.py` — 统一的导出入口，方便其他插件或脚本引用。
  - 新增 `main.py` 中的延迟导入（lazy imports）机制，避免 `ModuleNotFoundError`。
- **新增开发调试工具脚本**：
  - `check_dead_handles.py` — 检查未使用的 handle 注册项。
  - `find_dead_methods.py` / `find_methods.py` — 查找死方法/分析方法定义。
  - `find_handle_bounds.py` / `find_qweather_dead.py` / `find_qweather_ranges.py` — 分析工具边界和 QWeather 相关代码。
- **`.gitignore` 更新**：新增 `main.old.py` 忽略规则，避免旧版文件被误提交。

### 🔧 变更

- **模块化导入**：`main.py` 中所有核心工具函数均改为从 `tools.*` 子模块延迟导入，降低启动耦合度。
- **配置工具函数迁移**：`_load_schema_defaults`、`_extract_grouped_runtime_config` 等底层配置函数迁移至 `core/config.py`。
- **内存管理器迁移**：`MemoryManager` 类迁移至 `core/memory_manager.py`，`main.py` 中通过 `from .core.memory_manager import MemoryManager as CoreMemoryManager` 引用。

### ⚙ 配置更新

- 元数据版本号更新至 `1.0.0`，标记架构级重构里程碑。

## [0.4.0] - 2026-05-07

### ✨ 新增

- **Mnemosyne 向量记忆库集成**：
  - 新增完整的向量数据库前置配置组 (`mnemosyne`)，支持在 WebUI 无缝配置 Milvus/Milvus Lite（如 `milvus_lite_path`、`address`、`collection_name`）及认证参数。
  - 新增桥接层，用来配合 [astrbot_plugin_mnemosyne](https://github.com/lxfight/astrbot_plugin_mnemosyne.git) 插件使用，可以直接在这里配好参数并注入给对方实例。
  - 新增 LLM 内部工具（非用户命令），用于对 Mnemosyne 进行集合/记录管理转发，并提供向量记忆查询能力。

### 🔧 变更

- **依赖环境更新**：在 `requirements.txt` 中正式引入了 `pymilvus>=2.4.6` 安装要求以支持与 Milvus 环境对接。

## [0.3.0] - 2026-04-23

### ✨ 新增

- **内存管理功能**：
  - 支持内存的增删改查操作。
  - 新增 `/tool_memory` 管理命令，便于管理员操作内存。
  - 自动将用户内存注入到 LLM 上下文中，提升对话的智能性。

### ⚙ 配置更新

- 新增 `memory_inject_enabled` 配置项，用于控制内存注入功能的启用。
- 新增 `memory_inject_count` 配置项，用于设置注入的内存条目数量。

## [0.2.0] - 2026-04-18

### ✨ 新增

- **交互触发配置 (Interaction)**：
  - 新增关键词捕捉机制 (`keyword_capture_reply_handler`)，可监听群聊或私聊中的特定关键词，并基于设定的概率 (`keyword_capture_reply_probability`) 自动触发大模型回复。
  - 支持后台自定义关键词列表 (`keyword_capture_words`)，为机器人增加随机互动性和趣味性。

### 🔧 变更

- **天气及历史数据精简优化**：
  - 文案更新，将设置与提示词中的"压缩"统一优化为"精简"，更符合直觉。
  - **高保真数据传递**：在使用 LLM 精简天气或空气质量数据时，改为直接将接口返回的原始 JSON 或结构化数据直传给 AI（而非拼接好的字符串），使大模型能够更准确全面地理解原始信息。
- **网页抓取与系统配置体验优化**：
  - `fetch_url_blocked_targets`（禁用目标列表）类型变更为标准 `list`，在后台可视化编辑中更直观。
  - `fetch_url_max_redirects`（最大重定向次数）新增滑动条控件 (`slider`)，方便快速调节。
- **元数据与配置更新**：版本号更新至 `0.2.0`，修正描述信息并补充代码仓库地址。

## [0.1.0] - 2026-04-18

### ✨ 新增

- **网页抓取工具 `koko_fetch_url` / `tool_fetch_url`**：支持抓取单个网页正文（基于astrbot 4.22.0的fetch_url接口修改）。
  - **安全性 (SSRF 防护)**：内置对 `localhost`、内网 IP 及元数据地址的拦截，并支持自定义禁用目标列表。
  - **内容处理**：支持多种超限策略（截断 `truncate`、AI 总结 `ai_summary`、完整输出 `full`）。配置 `fetch_url_summary_llm_provider_id` 后可自动总结长网页。
  - **重定向控制**：可配置最大跟随重定向次数。
- **智能化工具调用体系 (Search-Call-Run)**：
  - **`search_koko_tools`**：根据关键词搜索匹配工具，提升大模型检索效率。
  - **`call_koko_tools`**：获取可用工具列表及参数详情。
  - **`run_koko_tool`**：统一的工具执行入口，支持 JSON 字符串传参。
- **系统提示词自动注入**：通过 `on_llm_request` 自动向大模型注入工具使用规范，确保模型遵循"先搜索、后调用"的逻辑。

### 🔧 变更

- **配置结构重构**：将配置项按功能模块化分组（`weather` / `search` / `web_fetch`），提升后台管理界面的逻辑性。旧版配置项在启动时会自动映射到新结构。
- **配置解析增强**：新增针对 `int`、`bool` 等类型的安全解析与范围限制，并支持 JSON 数组格式解析禁用目标列表。
- **天气摘要逻辑统一**：将 `weather_summary_prompt` 统一为 `summary_prompt` 体系，支持各工具共享总结指令。
- **工具注册机制**：采用统一的工具注册表（Registry）管理，包含关键词特征，支持更精准的匹配。

### ⚠️ 破坏性变更

- **工具调用流程变更**：大模型现在被要求优先使用 `search_koko_tools` 定位工具，原有的 `list_koko_tools` 已被弃用。
- **配置节点变化**：由于引入了分组配置，手动编辑配置文件时需注意层级关系。

### 📚 文档

- 更新 README 以反映新的配置结构与网页抓取功能。
- 更新工具建议调用说明，引入 Search-Call-Run 三段式调用建议。

## [0.0.2] - 2026-04-17

### ✨ 新增

- **历史天气/空气质量查询工具 `tool_weather_history`**：支持按天回溯（1-10 天，不含当天），可选 `weather` / `air` 两种历史类型，并支持 `full_history` 返回全量原始数据。
- **历史数据自动摘要**：在未启用全量返回时，自动生成多日历史摘要；若配置了 `weather_summary_llm_provider_id`，则可进一步调用 LLM 进行压缩总结。
- **新增搜索配置项 `zhipu_search_intent`**：可控制智谱搜索接口是否启用意图增强，降低不必要的返回开销。

### 🔧 变更

- **配置加载逻辑增强**：插件启动时会自动读取 `_conf_schema_config.json` 的默认值，并与运行时配置合并；`None` 与空字符串不再覆盖默认配置，减少"看似传参却导致默认值失效"的问题。
- **GeoAPI 路径统一修正**：位置查询接口改为 `/geo/v2/city/lookup`，与和风天气当前路径规范保持一致。
- **天气位置工具命名调整**：`tool_location` 更名为 `tool_weather_location`，并同步更新帮助文案与错误提示中的引导信息。
- **搜索工具输出结构优化**：
  - `lite`：返回极简摘要。
  - `medium`：返回摘要 + 来源标题信息。
  - `high`：返回摘要 + 来源正文内容。
- **搜索参数能力增强**：新增 `count` 参数（1-20），支持更精细地控制检索结果规模。

### 🐛 修复

- 修复部分接口在响应 `Content-Type` 异常时的 JSON 解析失败问题（改为 `resp.json(content_type=None)`）。
- 修复 Geo 查询结果在返回非对象结构时可能触发后续字段访问异常的问题，新增格式兜底与明确错误提示。
- 修复搜索接口错误信息过于笼统的问题：现在会优先解析并返回服务端 `code/message`，排查更直观。
- 修复搜索请求超时与网络异常时的反馈不明确问题，新增 `asyncio.TimeoutError` 与 `aiohttp.ClientError` 分支处理。

### ⚠️ 破坏性变更

- **工具名变更**：`tool_location` 已替换为 `tool_weather_location`。依赖旧工具名的提示词、脚本或调用链需要同步更新。

### 📚 文档

- 更新工具帮助文本，补充 `tool_weather_history` 用法说明与参数解释。
- 更新 README 中 Geo API 路径示例，避免旧路径导致调用失败。

## [0.0.1] - 2026-04-15

### 🎉 初始化提交

- 发布 `toolbox_for_koko` 插件初始版本，完成基础工程结构与插件注册。
- 提供首批核心工具能力：
  - 天气查询（位置查询 + 实时/多日天气）。
  - 网络搜索（多搜索粒度返回）。
  - 历史消息读取（群聊/私聊场景）。
- 建立基础配置体系（含 `_conf_schema.json`），支持在配置中启停主要功能。
- 完成首版文档与元数据文件（`README.md`、`metadata.yaml`），可直接安装并使用。