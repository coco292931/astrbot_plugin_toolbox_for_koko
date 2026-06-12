"""Google Calendar / iCal 日历查询工具。

通过 iCal (.ics) URL 拉取日历数据，解析并返回指定时间范围内的事件列表。
支持通过配置或工具参数传入代理地址。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, date
from typing import Any

import aiohttp

from astrbot.api import logger


# ---------------------------------------------------------------------------
# iCal 极简解析器（不依赖 icalendar 库）
# ---------------------------------------------------------------------------

def _unfold_lines(text: str) -> list[str]:
    """按 RFC 5545 展开折叠行（续行以空格或 TAB 开头）。"""
    lines: list[str] = []
    for raw in text.splitlines():
        if raw and raw[0] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_dt(value: str, tzinfo_hint: Any = None) -> datetime | None:
    """把 iCal 日期/日期时间字符串解析为 aware datetime（UTC 标准化）。"""
    value = value.strip()
    # 去掉 TZID= 参数前缀（如 TZID=Asia/Shanghai:20260613T120000）
    if ":" in value:
        value = value.split(":", 1)[-1].strip()

    # 纯日期 YYYYMMDD → 当天 00:00 UTC
    if re.fullmatch(r"\d{8}", value):
        try:
            d = datetime.strptime(value, "%Y%m%d")
            return d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    # 带 Z 后缀（UTC）
    if value.endswith("Z"):
        try:
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return None

    # 无时区标记 → 假定 UTC+8
    if re.fullmatch(r"\d{8}T\d{6}", value):
        try:
            naive = datetime.strptime(value, "%Y%m%dT%H%M%S")
            return naive.replace(tzinfo=timezone(timedelta(hours=8)))
        except ValueError:
            return None

    return None


def _unescape(value: str) -> str:
    """反转义 iCal 文本字段。"""
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _parse_ical(ics_text: str) -> list[dict]:
    """解析 iCal 文本，返回 VEVENT 列表（dict）。"""
    lines = _unfold_lines(ics_text)
    events: list[dict] = []
    current: dict | None = None

    for line in lines:
        if line.upper() == "BEGIN:VEVENT":
            current = {}
        elif line.upper() == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            # 属性名可能带参数：DTSTART;TZID=Asia/Shanghai:20260613T120000
            prop_full, _, value = line.partition(":")
            prop_name = prop_full.split(";")[0].upper()
            current[prop_name] = value.strip()

    return events


def _to_utc(dt: datetime) -> datetime:
    """确保返回 aware UTC datetime。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt_local(dt: datetime, offset_hours: int = 8) -> str:
    """格式化为本地时间字符串（默认 UTC+8）。"""
    local = dt.astimezone(timezone(timedelta(hours=offset_hours)))
    return local.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _get_astrbot_tz_offset(plugin: Any) -> int:
    """尝试从 AstrBot 系统配置读取 timezone（IANA 名称）并转换为小时偏移。
    读不到时回退 8（UTC+8）。
    """
    try:
        import zoneinfo
        ctx = getattr(plugin, "context", None)
        if ctx is None:
            return 8
        # AstrBot 将系统配置挂在 context.base_config 或 context._base_config
        base_cfg = getattr(ctx, "base_config", None) or getattr(ctx, "_base_config", None)
        if base_cfg is None:
            return 8
        tz_name = ""
        # 支持 dict 和对象两种形式
        if isinstance(base_cfg, dict):
            tz_name = str(base_cfg.get("timezone", "") or "").strip()
        else:
            tz_name = str(getattr(base_cfg, "timezone", "") or "").strip()
        if not tz_name:
            return 8
        zi = zoneinfo.ZoneInfo(tz_name)
        # 用当前时间求 UTC 偏移（考虑夏令时）
        offset = datetime.now(zi).utcoffset()
        if offset is None:
            return 8
        return int(offset.total_seconds() / 3600)
    except Exception:
        return 8


async def run_get_calendar(plugin: Any, args: dict) -> str:
    """获取 iCal 日历事件并格式化返回。

    Args（来自 LLM 工具参数）：
        ical_url    : iCal URL，可选（覆盖配置中的 calendar_ical_url）
        proxy       : HTTP 代理地址，可选（覆盖配置）
        days_ahead  : 查询未来多少天，默认 7
        days_back   : 查询过去多少天，默认 0
        max_events  : 最多返回事件数，默认 20
        tz_offset   : 本地时区偏移（小时），不传则自动从 AstrBot 系统配置读取，读不到回退 8（UTC+8）
    """
    # 参数读取
    ical_url = (
        str(args.get("ical_url", "") or "").strip()
        or str(getattr(plugin, "calendar_ical_url", "") or "").strip()
    )
    if not ical_url:
        return "未配置 iCal URL，请在配置中填写 calendar_ical_url 或通过工具参数传入 ical_url。"

    proxy = (
        str(args.get("proxy", "") or "").strip()
        or str(getattr(plugin, "calendar_proxy", "") or "").strip()
        or None
    )

    try:
        days_ahead = max(0, min(int(args.get("days_ahead", 7) or 7), 365))
    except Exception:
        days_ahead = 7

    try:
        days_back = max(0, min(int(args.get("days_back", 0) or 0), 365))
    except Exception:
        days_back = 0

    try:
        max_events = max(1, min(int(args.get("max_events", 20) or 20), 100))
    except Exception:
        max_events = 20

    raw_tz = args.get("tz_offset", None)
    if raw_tz is None:
        # 未传入时自动从 AstrBot 系统配置读取 timezone
        tz_offset = _get_astrbot_tz_offset(plugin)
    else:
        try:
            tz_offset = int(raw_tz)
        except Exception:
            tz_offset = _get_astrbot_tz_offset(plugin)

    # 时间范围：支持绝对日期（date_from/date_to，格式 YYYY-MM-DD）和相对天数（days_ahead/days_back）
    # 绝对日期优先。
    now_utc = datetime.now(timezone.utc)
    local_tz = timezone(timedelta(hours=tz_offset))

    date_from_str = str(args.get("date_from", "") or "").strip()
    date_to_str = str(args.get("date_to", "") or "").strip()

    if date_from_str or date_to_str:
        # 绝对日期模式：把本地日期转为 UTC
        try:
            if date_from_str:
                d = datetime.strptime(date_from_str, "%Y-%m-%d")
                range_start = d.replace(tzinfo=local_tz).astimezone(timezone.utc)
            else:
                range_start = now_utc - timedelta(days=30)
        except ValueError:
            return f"date_from 格式错误，请使用 YYYY-MM-DD，如 2026-06-01。"
        try:
            if date_to_str:
                d = datetime.strptime(date_to_str, "%Y-%m-%d")
                # 包含当天结束时刷23:59:59
                range_end = (d.replace(hour=23, minute=59, second=59, tzinfo=local_tz)
                             .astimezone(timezone.utc))
            else:
                range_end = now_utc + timedelta(days=30)
        except ValueError:
            return f"date_to 格式错误，请使用 YYYY-MM-DD，如 2026-06-30。"
    else:
        # 相对天数模式
        range_start = now_utc - timedelta(days=days_back)
        range_end = now_utc + timedelta(days=days_ahead)

    # 拉取 iCal
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(
            timeout=timeout, connector=connector
        ) as session:
            async with session.get(
                ical_url,
                proxy=proxy,
                headers={"User-Agent": "AstrBot-Toolbox-Calendar/1.0"},
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    return f"拉取日历失败，HTTP 状态码：{resp.status}"
                ics_text = await resp.text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"[calendar] 拉取 iCal 失败: {e}")
        return f"拉取日历失败：{e}"

    # 解析
    raw_events = _parse_ical(ics_text)
    if not raw_events:
        return "日历数据解析成功，但未找到任何事件（VEVENT）。"

    # 过滤时间范围
    matched: list[dict] = []
    for ev in raw_events:
        dtstart_raw = ev.get("DTSTART", "")
        dtend_raw = ev.get("DTEND", ev.get("DUE", ""))

        dt_start = _parse_dt(dtstart_raw) if dtstart_raw else None
        if dt_start is None:
            continue
        dt_start = _to_utc(dt_start)

        # 跨天事件：只要有任何部分在范围内
        dt_end = _parse_dt(dtend_raw) if dtend_raw else None
        if dt_end is not None:
            dt_end = _to_utc(dt_end)
            if dt_end <= range_start or dt_start >= range_end:
                continue
        else:
            if not (range_start <= dt_start < range_end):
                continue

        matched.append({
            "dt_start": dt_start,
            "dt_end": dt_end,
            "summary": _unescape(ev.get("SUMMARY", "(无标题)")),
            "location": _unescape(ev.get("LOCATION", "")),
            "description": _unescape(ev.get("DESCRIPTION", "")),
            "uid": ev.get("UID", ""),
        })

    if not matched:
        tz_label = f"UTC{tz_offset:+d}"
        start_label = (range_start.astimezone(timezone(timedelta(hours=tz_offset)))).strftime("%Y-%m-%d")
        end_label = (range_end.astimezone(timezone(timedelta(hours=tz_offset)))).strftime("%Y-%m-%d")
        return f"在 {start_label} 至 {end_label}（{tz_label}）范围内没有找到日历事件。"

    # 按开始时间排序，截取
    matched.sort(key=lambda e: e["dt_start"])
    matched = matched[:max_events]

    # 格式化输出
    tz_label = f"UTC{tz_offset:+d}"
    lines: list[str] = [
        f"📅 日历事件（{tz_label}，共 {len(matched)} 条）：",
        "",
    ]
    for i, ev in enumerate(matched, 1):
        start_str = _fmt_local(ev["dt_start"], tz_offset)
        end_str = _fmt_local(ev["dt_end"], tz_offset) if ev["dt_end"] else ""
        time_str = f"{start_str}" + (f" → {end_str}" if end_str else "")
        lines.append(f"{i}. 【{ev['summary']}】")
        lines.append(f"   🕐 {time_str}")
        if ev["location"]:
            lines.append(f"   📍 {ev['location']}")
        if ev["description"]:
            desc = ev["description"][:120].replace("\n", " ")
            if len(ev["description"]) > 120:
                desc += "…"
            lines.append(f"   📝 {desc}")
        lines.append("")

    return "\n".join(lines).rstrip()
