"""
网页抓取工具 - fetch_url / 图片批量下载
"""

import asyncio
import base64
import io
import json
import ipaddress
import os
import re
import random
import socket
import traceback
from datetime import datetime
from typing import Any, Optional
import urllib.parse
from urllib.parse import urlparse

from astrbot.api import logger

import aiohttp

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False

try:
    from readability import Document

    _HAS_READABILITY = True
except ImportError:
    _HAS_READABILITY = False

try:
    import html2text
    _HAS_HTML2TEXT = True
except ImportError:
    _HAS_HTML2TEXT = False

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False


def _parse_llm_compress_mode(value) -> str | None:
    if value is None:
        return "inherit"
    if isinstance(value, str):
        mode = value.strip().lower()
        if mode in {"inherit", "summary", "truncate"}:
            return mode
    return None


async def _tidy_text(text: str) -> str:
    return " ".join(text.split())


async def _extract_best_text_from_html(html: str) -> str:
    primary_text = ""
    if _HAS_READABILITY and _HAS_BS4:
        try:
            doc = Document(html)
            summary_html = doc.summary(html_partial=True)
            soup = BeautifulSoup(summary_html, "html.parser")
            primary_text = await _tidy_text(soup.get_text(" ", strip=True))
        except Exception:
            primary_text = ""

    if primary_text and len(primary_text) >= 120:
        return primary_text

    if _HAS_BS4:
        full_soup = BeautifulSoup(html, "html.parser")
        for tag in full_soup(["script", "style", "noscript", "svg", "canvas"]):
            tag.decompose()
        fallback_text = await _tidy_text(full_soup.get_text(" ", strip=True))
        if fallback_text:
            return fallback_text

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

        combined = await _tidy_text(f"{title} {desc}".strip())
        return combined

    return (await _tidy_text(html))[:5000]


async def _extract_text_from_json_payload(payload: Any) -> str:
    text_fields: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                key_lower = str(k).lower()
                if isinstance(v, str):
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

    try:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception:
        return str(payload)


async def _detect_unextractable_page_reason(html: str) -> str | None:
    if not _HAS_BS4:
        return None

    lowered = html.lower()
    soup = BeautifulSoup(html, "html.parser")
    body_text = await _tidy_text(soup.get_text(" ", strip=True))

    if ("challenge-platform" in lowered or "__cf$cv$params" in lowered) and len(
        body_text
    ) < 500:
        return "页面触发了反爬/挑战验证，当前抓取方式无法直接获取正文。"

    app_container = soup.find(id="app")
    app_container_empty = False
    if app_container is not None:
        app_container_text = await _tidy_text(app_container.get_text(" ", strip=True))
        app_container_empty = len(app_container_text) < 30

    has_module_script = bool(soup.find("script", attrs={"type": "module"}))
    if len(body_text) < 80 and app_container_empty and has_module_script:
        return "页面疑似前端渲染(SPA)壳页，原始 HTML 不包含正文内容。"

    return None


async def _validate_fetch_url(plugin, url: str) -> tuple[bool, str]:
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
    deny_targets = set(getattr(plugin, "fetch_url_blocked_targets", []) or [])
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


async def _normalize_and_validate_fetch_url(plugin, url: str) -> tuple[bool, str, str]:
    url_clean = str(url or "").strip()
    ok, err = await _validate_fetch_url(plugin, url_clean)
    if not ok:
        return False, "", err
    return True, url_clean, ""


async def _process_fetched_text(plugin, text: str, llm_compress: str = "inherit") -> str:
    max_chars = int(getattr(plugin, "fetch_url_max_chars", 6000))
    if len(text) <= max_chars:
        return text

    mode = str(getattr(plugin, "fetch_url_over_limit_mode", "truncate") or "truncate")
    if llm_compress == "summary":
        mode = "ai_summary"
    elif llm_compress == "truncate":
        mode = "truncate"

    if mode == "full":
        return text

    truncated = text[:max_chars]
    if mode == "truncate":
        return f"{truncated}...\n\n[系统提示] 网页正文超长，已按配置截断。"

    provider_id = getattr(plugin, "fetch_url_summary_llm_provider_id", "")
    if not provider_id:
        return f"{truncated}...\n\n[系统提示] 未配置 fetch_url_summary_llm_provider_id，已回退为截断输出。"

    summary_prompt = str(
        getattr(plugin, "fetch_url_summary_prompt", "请总结网页正文内容，提取关键信息。")
    )
    try:
        prompt = f"{summary_prompt}\n\n网页正文:\n{text}"
        ai_resp = await plugin.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
        )
        ai_text = plugin._extract_llm_text(ai_resp)
        if ai_text:
            return ai_text
        logger.warning("网页正文 AI 总结返回非文本或空文本，回退为截断输出。")
        return f"{truncated}...\n\n[系统提示] AI 总结返回为空，已回退为截断输出。"
    except Exception:
        logger.warning("网页正文 AI 总结失败，回退为截断输出。")
        return f"{truncated}...\n\n[系统提示] AI 总结失败，已回退为截断输出。"


async def _get_from_url(
    plugin,
    url: str,
    use_legacy: bool = False,
    llm_compress: str = "inherit",
) -> str:
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

    max_redirects = int(getattr(plugin, "fetch_url_max_redirects", 5))
    max_download_bytes = int(getattr(plugin, "fetch_url_max_download_bytes", 2 * 1024 * 1024))

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        current_url = url
        html = ""

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for _ in range(max_redirects + 1):
                ok, normalized_url, err = await _normalize_and_validate_fetch_url(
                    plugin, current_url
                )
                if not ok:
                    return err

                async with session.get(
                    normalized_url,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
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

                    raw = await response.content.read(max_download_bytes + 1)
                    if len(raw) > max_download_bytes:
                        limit_mb = max_download_bytes / (1024 * 1024)
                        return f"网页内容过大，已超过 {limit_mb:.1f} MB 限制。"

                    charset = response.charset or "utf-8"
                    decoded = raw.decode(charset, errors="ignore")

                    if is_json:
                        try:
                            payload = json.loads(decoded)
                        except Exception:
                            return "抓取异常: 返回了 JSON 类型，但解析 JSON 失败。"

                        json_text = await _extract_text_from_json_payload(payload)
                        if not json_text.strip():
                            return "抓取异常: JSON 返回为空或无可读文本字段。"
                        return await _process_fetched_text(
                            plugin, json_text, llm_compress=llm_compress
                        )

                    html = decoded

                    if not use_legacy:
                        abnormal_reason = await _detect_unextractable_page_reason(html)
                        if abnormal_reason:
                            return f"抓取异常: {abnormal_reason}"
                    break
            else:
                return "抓取网页失败：重定向次数超过限制。"

        text = await _extract_best_text_from_html(html)
        if not text:
            return "网页内容为空或无法提取正文。"
        return await _process_fetched_text(plugin, text, llm_compress=llm_compress)
    except asyncio.TimeoutError:
        return "抓取网页超时。"
    except aiohttp.ClientError as e:
        return f"抓取网页网络异常: {type(e).__name__} {str(e)}"
    except Exception as e:
        logger.error(traceback.format_exc())
        return f"抓取网页内部异常: {type(e).__name__} {str(e)}"


async def handle_fetch_url(plugin, args: dict) -> str:
    if not getattr(plugin, "enable_fetch_url", True):
        return "网页抓取功能已被禁用。"

    url = str(args.get("url", "") or "").strip()
    if not url:
        return "缺少 url 参数。"

    skip_filter = False
    if hasattr(plugin, "_safe_bool"):
        skip_filter = plugin._safe_bool(args.get("skip_filter", False), False)
    else:
        skip_filter = bool(args.get("skip_filter", False))

    llm_compress = "inherit"
    if "llm_compress" in args:
        llm_compress = _parse_llm_compress_mode(args.get("llm_compress"))
        if llm_compress is None:
            return "llm_compress 参数无效：仅支持 inherit、summary、truncate。"

    ok, normalized_url, err = await _normalize_and_validate_fetch_url(plugin, url)
    if not ok:
        return err

    return await _get_from_url(
        plugin,
        normalized_url,
        use_legacy=skip_filter,
        llm_compress=llm_compress,
    )


async def run_fetch_url(plugin, event, args: dict) -> str:
    """兼容旧命名。"""
    return await handle_fetch_url(plugin, args)


async def run_batch_download(plugin, event, args: dict) -> str:
    """批量下载图片（@llm_tool tool_batch_download 的实际实现，未用@llm_tool装饰）"""
    urls = args.get("urls", [])
    if not urls:
        return "缺少 urls 参数。"

    if isinstance(urls, str):
        try:
            urls = json.loads(urls)
        except json.JSONDecodeError:
            urls = [urls]

    if not isinstance(urls, list):
        urls = [urls]

    base_dir = getattr(plugin, "batch_download_base_dir", "data/toolbox_downloads")
    if not os.path.isabs(base_dir):
        base_dir = os.path.join(plugin.get_data_dir(), base_dir)

    date_str = datetime.now().strftime("%Y%m%d")
    session_dir = os.path.join(base_dir, date_str)
    os.makedirs(session_dir, exist_ok=True)

    results = []
    success_count = 0

    max_workers = min(getattr(plugin, "batch_download_max_workers", 5), 20)

    semaphore = asyncio.Semaphore(max_workers)

    async def _download_one(url: str, index: int) -> dict:
        nonlocal success_count
        async with semaphore:
            try:
                ext = _guess_extension(url)
                filename = f"{int(datetime.now().timestamp() * 1000)}_{index}{ext}"
                filepath = os.path.join(session_dir, filename)

                timeout = max(getattr(plugin, "batch_download_timeout", 30), 10)
                max_size_mb = max(getattr(plugin, "batch_download_max_size_mb", 5), 1)

                async with plugin.session.get(url, timeout=timeout, ssl=False) as resp:
                    if resp.status != 200:
                        return {"url": url, "status": "error", "msg": f"HTTP {resp.status}"}

                    raw = bytearray()
                    async for chunk in resp.content.iter_chunked(8192):
                        raw.extend(chunk)
                        if len(raw) > max_size_mb * 1024 * 1024:
                            return {"url": url, "status": "error", "msg": f"文件超过{max_size_mb}MB限制"}

                    raw_bytes = bytes(raw)
                    if _HAS_PIL:
                        try:
                            img = Image.open(io.BytesIO(raw_bytes))
                            img.verify()
                        except Exception:
                            return {"url": url, "status": "error", "msg": "图片格式验证失败"}

                    with open(filepath, "wb") as f:
                        f.write(raw_bytes)

                success_count += 1
                size_str = _format_size(len(raw_bytes))
                return {"url": url, "status": "ok", "path": filepath, "size": size_str}

            except asyncio.TimeoutError:
                return {"url": url, "status": "error", "msg": "下载超时"}
            except Exception as e:
                return {"url": url, "status": "error", "msg": str(e)}

    tasks = [_download_one(url, i) for i, url in enumerate(urls)]
    results = await asyncio.gather(*tasks)

    summary_parts = [f"批量下载完成：成功 {success_count}/{len(urls)}"]
    for r in results:
        if r["status"] == "ok":
            summary_parts.append(f"✅ {r['url'][:80]} → {r['path']} ({r['size']})")
        else:
            summary_parts.append(f"❌ {r['url'][:80]} : {r['msg']}")
    return "\n".join(summary_parts)


def _guess_extension(url: str) -> str:
    """根据 URL 猜测文件扩展名。"""
    parsed = urlparse(url)
    path = parsed.path
    _, ext = os.path.splitext(path)
    if ext and len(ext) <= 6:
        return ext
    return ".jpg"


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes / 1024 / 1024:.1f}MB"