"""
桥接工具 - QZone Tools / Mnemosyne 插件桥接
"""

import json


async def run_mnemosyne_bridge(plugin, event, args: dict) -> str:
    """桥接到 Mnemosyne 插件（@llm_tool tool_mnemosyne_bridge 的实际实现）"""
    fn_name = str(args.get("fn_name", "") or "").strip()
    if not fn_name:
        return "❌ 参数缺失：请提供 fn_name。"

    payload = {}
    payload.update({k: v for k, v in args.items() if k not in ("fn_name",)})
    payload["event"] = event

    mnemo_plugin = plugin._get_mnemosyne_plugin_instance()
    if not mnemo_plugin:
        return "❌ Mnemosyne 插件未加载。"

    try:
        result = await plugin._forward_to_mnemosyne(event, fn_name, **payload)
        if isinstance(result, dict):
            return json.dumps(result, ensure_ascii=False)
        return str(result)
    except Exception as e:
        return f"❌ Mnemosyne 调用失败: {str(e)}"


async def run_qzone_bridge(plugin, event, args: dict) -> str:
    """桥接到 QZone Tools 插件"""
    fn_name = str(args.get("fn_name", "") or "").strip()
    if not fn_name:
        return "❌ 参数缺失：请提供 fn_name。"

    payload = {k: v for k, v in args.items() if k != "fn_name"}

    qzone_plugin = plugin._get_qzone_tools_plugin_instance()
    if not qzone_plugin:
        return "❌ QZone Tools 插件未加载。"

    try:
        method = getattr(qzone_plugin, fn_name, None)
        if not method:
            return f"❌ QZone Tools 中未找到方法: {fn_name}"

        result = await method(event, **payload)
        if isinstance(result, dict):
            return json.dumps(result, ensure_ascii=False)
        return str(result)
    except Exception as e:
        return f"❌ QZone Tools 调用失败: {str(e)}"
