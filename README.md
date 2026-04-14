# 🧰 Koko 多功能工具箱 (Toolbox for Koko)

> AstrBot 专属增强插件，为虚拟形象 (如 Koko) 提供丰富的内置能力集，包含天气查询、强大的搜索引擎，并且支持提取群聊与好友的历史记录以复盘上下文。

## ✨ 功能特性

- 🌤️ **多维天气预报与生活指数**：基于和风天气 (QWeather)，支持实时、3日、7日的天气预报以及生活指数的细粒度查询。内置自定义提示词系统，可以将长段的7天预报交由大模型进行压缩和定制化总结。
- 🔍 **智能联网网页搜索**：集成智谱大模型官方提供的最新搜索接口，具有灵活多样的配置选项：
  - 引擎选择：普通搜索 (search_std) 和困难问题的深入查询 (search_pro_quark)。
  - 信息长度提取：本地极简摘要处理 (lite)、常规云端摘要抽取 (medium) 以及带有完整原链接数据的详细内容 (high)。
  - 时效过滤：不限、过去一天、一周、一月甚至一年内的搜索结果。
- 📜 **历史聊天记录寻回**：使用底层原生协议 (get_group_msg_history 等接口)，针对群聊和私聊都可以按精确的起始序号或直接进行“分页”拉取群/个人历史消息。智能剔除各种冗余参数，向大模型传输精简后带有 sender、内容结构 和 seq的洁净 JSON；大幅降低复盘与分析场景下的 token 花销。

## ⚙️ 核心前置配置及开关

请转到 AstrBot 的后台管理面板，填入相应的工具密钥与开启功能：

- **qweather_jwt_token**: 和风天气 JWT Token（推荐）。若填写，将优先使用 `Authorization: Bearer` 认证。
- **qweather_key**: 和风天气 API Key（兼容模式）。未填写 JWT 时会回退使用该字段。
- **qweather_weather_host**: 天气 API Host。默认 `devapi.qweather.com`，你可填自己的专属域名。
- **qweather_geo_host**: 可选覆盖项。通常留空即可：
  - 在 `key` 模式下，GeoAPI 默认与 `qweather_weather_host` 共用同一 host。
  - 在 `JWT` 模式下，GeoAPI 默认使用 `geoapi.qweather.com`（也可手动覆盖）。
- **zhipu_key**: 智谱官方发布的 API 密钥，可前往 [智谱大模型开放平台](https://open.bigmodel.cn/) 申请。
- **zhipu_search_model**: 执行联网搜索时使用的模型，默认 `glm-4.7-flash`。
- **功能按需开关**:
  - `enable_weather`: 是否启用天气与城市查询工具
  - `enable_search`: 是否启用联网搜索工具
  - `enable_history`: 是否启用群聊/私聊历史记录工具
  - `enable_weather_summary`: 是否启用 7 日天气压缩流程
- **weather_summary_prompt**: 开启天气精简时的自定义回复约束说明。
- **weather_summary_llm_provider_id**: 可选。填写后会通过 AstrBot 平台的 `chat_provider_id` 调用对应模型先做 7 日天气压缩。
  - 不填写：不会自动选模型，也不会调用智谱搜索模型；将回退为本地精简文本返回。
  - 填写：按该 provider id 调用平台模型（不是 `zhipu_key` 直连）。

## 🚀 灵活的使用机制 (自动路由)

你不需要手动使用带 / 斜杠的指令跟机器人交流。本插件向核心会话大模型仅暴露了两个收放自如的工具函数：

1. **list_koko_tools**: 大模型会先探测当前开启的功能权限及具体的指令定义文档。
2. **run_koko_tool**: 根据上面的文档，大模型可以独立完成组合指令传参、智能容错与后续分析。

当你自然语言问询类似于：“帮我把前面的聊天历史总结一下”、“看看后天去广州天河区要不要带衣服”、“目前最新的 AI 新闻是什么”时，大语言模型将全自动静默获取目标资料并亲切回复。

## 🧭 天气工具建议调用顺序

1. 先调用 `tool_location`，通过关键词获取候选地区及 `Location ID`。
2. 再调用 `tool_weather`，把选中的 `location` 传入查询实时/3日/7日天气或天气指数。

这样可以避免重名城市导致的误查。

## 🌐 天气 API 路径说明

- Weather: `https://{weather_host}/v7/weather/...`
- Geo: `https://{geo_host}/v2/city/lookup?...`

说明：Geo 查询路径使用 `/v2/city/lookup`（不是 `/geo/v2/city/lookup`）。
