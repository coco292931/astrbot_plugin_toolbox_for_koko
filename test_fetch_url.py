#!/usr/bin/env python3
"""fetch_url 自测：微信公众号适配 + include_img 图片标记。

覆盖三件事：

1. **公众号适配**：mp.weixin.qq.com 使用微信/桌面 UA 与 Referer 头，
   且不再使用会被判为"环境异常"的 Linux UA；正文取自 ``#js_content``。
2. **反爬识别与换 UA 重试**：命中"环境异常"/只剩页面骨架时判定为异常，
   并自动更换 UA 重试，而不是把验证页当正文返回。
3. **include_img 参数**：默认只在图片位置插入 ``[图片: 释义]`` 标记，
   开启后追加去掉跟踪参数后的图片直链。

全程离线（不发网络请求）。运行::

    python test_fetch_url.py
    python test_fetch_url.py -v
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# AstrBot 桩模块
# ---------------------------------------------------------------------------


class _Logger:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def debug(self, *a, **k):
        pass

    def info(self, msg, *a, **k):
        self.infos.append(str(msg))

    def warning(self, msg, *a, **k):
        self.warnings.append(str(msg))

    def error(self, msg, *a, **k):
        self.errors.append(str(msg))


LOGGER = _Logger()


def _install_astrbot_stubs() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    api_mod.logger = LOGGER
    astrbot_mod.api = api_mod
    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod


_install_astrbot_stubs()

from bs4 import BeautifulSoup  # noqa: E402

from tools import fetch_url as fetch  # noqa: E402


WECHAT_URL = "https://mp.weixin.qq.com/s/VDh0lkg3_H9c_s8GVxHcIQ"
GENERIC_URL = "https://example.com/article"


class _Plugin:
    """fetch_url 需要的最小插件配置面。"""

    fetch_url_max_chars = 6000
    fetch_url_over_limit_mode = "full"
    fetch_url_summary_llm_provider_id = ""
    fetch_url_summary_prompt = ""
    fetch_url_blocked_targets: list[str] = []
    fetch_url_proxy = ""
    fetch_url_max_download_bytes = 6 * 1024 * 1024
    fetch_url_max_redirects = 4
    enable_fetch_url = True

    def _safe_bool(self, value, default=False):
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return bool(value)


def _wechat_article_html(
    body_html: str = "<p>正文第一段</p>",
    *,
    title: str = "测试标题",
    account: str = "测试公众号",
    desc: str = "测试摘要",
) -> str:
    return f"""<html><head>
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
</head><body>
<div class="rich_media_area_primary">
  <h1 class="rich_media_title" id="activity-name">{title}</h1>
  <a id="js_name">{account}</a>
  <div class="rich_media_content" id="js_content">{body_html}</div>
</div>
</body></html>"""


VERIFY_PAGE_HTML = """<html><body>
<div class="weui-msg">
  <div class="weui-msg__title">环境异常</div>
  <div class="weui-msg__desc">当前环境异常，完成验证后即可继续访问。</div>
</div>
</body></html>"""

SHELL_PAGE_HTML = """<html><head>
<meta property="og:url" content="https://mp.weixin.qq.com/s/xxx">
</head><body>
<div class="rich_media_area_primary"></div>
<div class="rich_media_tool">： ， 。 视频 小程序 赞 在看</div>
<script>window.biz = "appmsg";</script>
</body></html>"""


def run(coro):
    return asyncio.run(coro)


class TestImageMarkers(unittest.TestCase):
    def test_clean_image_url_drops_query_and_fragment(self):
        cleaned = fetch._clean_image_url(
            "https://mmbiz.qpic.cn/mmbiz_png/xxx/640?wx_fmt=png&amp;from=appmsg#imgIndex=1"
        )
        self.assertEqual(cleaned, "https://mmbiz.qpic.cn/mmbiz_png/xxx/640")

    def test_clean_image_url_expands_protocol_relative(self):
        self.assertEqual(
            fetch._clean_image_url("//cdn.example.com/a.png?x=1"),
            "https://cdn.example.com/a.png",
        )

    def test_clean_image_url_rejects_non_http(self):
        self.assertEqual(fetch._clean_image_url("data:image/png;base64,AAA"), "")


class TestImageMarkerText(unittest.TestCase):
    def _marker(self, img_html: str, include_img: bool) -> str:
        soup = BeautifulSoup(f"<div>{img_html}</div>", "html.parser")
        img = soup.find("img")
        return fetch._build_image_marker(img, include_img)

    def test_meaningful_alt_kept(self):
        marker = self._marker('<img alt="成绩图表" src="https://a.com/1.png">', False)
        self.assertEqual(marker, "[图片: 成绩图表]")

    def test_generic_alt_dropped(self):
        for alt in ("image.png", "image", "图片", "IMG_1.JPG"):
            marker = self._marker(f'<img alt="{alt}" src="https://a.com/1.png">', False)
            self.assertEqual(marker, "[图片]", alt)

    def test_include_img_appends_clean_url(self):
        marker = self._marker(
            '<img alt="图" src="https://a.com/1.png?wx_fmt=png&amp;tp=webp">', True
        )
        self.assertEqual(marker, "[图片: 图](https://a.com/1.png)")

    def test_include_img_prefers_lazy_attributes(self):
        marker = self._marker(
            '<img src="data:image/gif;base64,AAA" '
            'data-src="https://a.com/real.png?wx_fmt=png">',
            True,
        )
        self.assertEqual(marker, "[图片](https://a.com/real.png)")


class TestWechatExtraction(unittest.TestCase):
    def test_wechat_host_detection(self):
        self.assertTrue(fetch._is_wechat_article_url(WECHAT_URL))
        self.assertTrue(
            fetch._is_wechat_article_url("https://mp.weixin.qq.com/s?__biz=xxx")
        )
        self.assertFalse(fetch._is_wechat_article_url(GENERIC_URL))

    def test_wechat_profiles_prefer_wechat_ua_and_avoid_linux(self):
        profiles = fetch._build_request_profiles(WECHAT_URL)
        self.assertGreaterEqual(len(profiles), 2)
        for headers in profiles:
            self.assertNotIn("X11; Linux", headers["User-Agent"])
            self.assertEqual(headers.get("Referer"), "https://mp.weixin.qq.com/")
        self.assertIn("MicroMessenger", profiles[0]["User-Agent"])

    def test_generic_profiles_have_no_wechat_headers(self):
        profiles = fetch._build_request_profiles(GENERIC_URL)
        self.assertEqual(len(profiles), len(fetch._DEFAULT_USER_AGENTS))
        for headers in profiles:
            self.assertNotIn("Referer", headers)

    def test_wechat_content_from_js_content_with_images(self):
        html = _wechat_article_html(
            '<p>第一段</p><img data-src="https://mmbiz.qpic.cn/a/640?wx_fmt=png" alt="架构图">'
            "<p>第二段</p>"
        )
        text = run(
            fetch._extract_best_text_from_html(
                html, include_img=False, source_url=WECHAT_URL
            )
        )
        self.assertIn("标题：测试标题", text)
        self.assertIn("公众号：测试公众号", text)
        self.assertIn("摘要：测试摘要", text)
        self.assertIn("[图片: 架构图]", text)
        self.assertNotIn("mmbiz.qpic.cn", text)
        # 图片标记应落在两段之间
        self.assertLess(text.index("第一段"), text.index("[图片: 架构图]"))
        self.assertLess(text.index("[图片: 架构图]"), text.index("第二段"))

    def test_wechat_content_include_img(self):
        html = _wechat_article_html(
            '<p>正文</p><img data-src="https://mmbiz.qpic.cn/a/640?wx_fmt=png" alt="架构图">'
        )
        text = run(
            fetch._extract_best_text_from_html(
                html, include_img=True, source_url=WECHAT_URL
            )
        )
        self.assertIn("[图片: 架构图](https://mmbiz.qpic.cn/a/640)", text)
        self.assertNotIn("wx_fmt", text)

    def test_plain_page_has_no_wechat_header(self):
        html = "<html><body><article><p>" + ("普通段落内容。" * 40) + "</p></article></body></html>"
        text = run(fetch._extract_best_text_from_html(html, source_url=GENERIC_URL))
        self.assertNotIn("标题：", text)
        self.assertIn("普通段落内容", text)


class TestAbnormalDetection(unittest.TestCase):
    def test_detects_wechat_verify_page(self):
        reason = run(fetch._detect_unextractable_page_reason(VERIFY_PAGE_HTML))
        self.assertIsNotNone(reason)
        self.assertTrue(fetch._should_retry_with_another_ua(reason))

    def test_detects_empty_wechat_shell(self):
        reason = run(fetch._detect_unextractable_page_reason(SHELL_PAGE_HTML))
        self.assertIsNotNone(reason)
        self.assertTrue(fetch._should_retry_with_another_ua(reason))

    def test_normal_page_not_flagged(self):
        html = "<html><body>" + ("正常正文。" * 100) + "</body></html>"
        self.assertIsNone(run(fetch._detect_unextractable_page_reason(html)))

    def test_spa_shell_is_not_ua_retryable(self):
        html = (
            '<html><body><div id="app"></div>'
            '<script type="module" src="/app.js"></script></body></html>'
        )
        reason = run(fetch._detect_unextractable_page_reason(html))
        self.assertIsNotNone(reason)
        self.assertFalse(fetch._should_retry_with_another_ua(reason))


class TestRetryFlow(unittest.TestCase):
    def test_retries_with_next_ua_on_verify_page(self):
        calls: list[str] = []
        article = _wechat_article_html("<p>真正的正文内容</p>")

        async def fake_fetch(plugin, url, headers, **kwargs):
            calls.append(headers["User-Agent"])
            if len(calls) == 1:
                return "html", VERIFY_PAGE_HTML, ""
            return "html", article, ""

        original = fetch._fetch_page_once
        fetch._fetch_page_once = fake_fetch
        try:
            out = run(fetch._get_from_url(_Plugin(), WECHAT_URL))
        finally:
            fetch._fetch_page_once = original

        self.assertEqual(len(calls), 2, calls)
        self.assertNotEqual(calls[0], calls[1])
        self.assertIn("真正的正文内容", out)
        self.assertTrue(any("重试" in msg for msg in LOGGER.infos))

    def test_all_profiles_blocked_returns_error(self):
        async def fake_fetch(plugin, url, headers, **kwargs):
            return "html", VERIFY_PAGE_HTML, ""

        original = fetch._fetch_page_once
        fetch._fetch_page_once = fake_fetch
        try:
            out = run(fetch._get_from_url(_Plugin(), WECHAT_URL))
        finally:
            fetch._fetch_page_once = original

        self.assertTrue(out.startswith("抓取异常"), out)

    def test_legacy_mode_single_attempt(self):
        calls: list[str] = []

        async def fake_fetch(plugin, url, headers, **kwargs):
            calls.append(headers["User-Agent"])
            return "html", VERIFY_PAGE_HTML, ""

        original = fetch._fetch_page_once
        fetch._fetch_page_once = fake_fetch
        try:
            run(fetch._get_from_url(_Plugin(), WECHAT_URL, use_legacy=True))
        finally:
            fetch._fetch_page_once = original

        self.assertEqual(len(calls), 1)

    def test_network_error_not_retried(self):
        calls: list[str] = []

        async def fake_fetch(plugin, url, headers, **kwargs):
            calls.append(headers["User-Agent"])
            return "error", "", "抓取网页超时。"

        original = fetch._fetch_page_once
        fetch._fetch_page_once = fake_fetch
        try:
            out = run(fetch._get_from_url(_Plugin(), WECHAT_URL))
        finally:
            fetch._fetch_page_once = original

        self.assertEqual(len(calls), 1)
        self.assertEqual(out, "抓取网页超时。")


class TestToolArgs(unittest.TestCase):
    def test_handle_fetch_url_passes_include_img(self):
        seen: dict = {}

        async def fake_get(plugin, url, **kwargs):
            seen.update(kwargs)
            return "ok"

        original = fetch._get_from_url
        fetch._get_from_url = fake_get
        try:
            out = run(
                fetch.handle_fetch_url(_Plugin(), {"url": GENERIC_URL, "include_img": True})
            )
        finally:
            fetch._get_from_url = original

        self.assertEqual(out, "ok")
        self.assertTrue(seen.get("include_img"))

    def test_include_img_defaults_to_false(self):
        seen: dict = {}

        async def fake_get(plugin, url, **kwargs):
            seen.update(kwargs)
            return "ok"

        original = fetch._get_from_url
        fetch._get_from_url = fake_get
        try:
            run(fetch.handle_fetch_url(_Plugin(), {"url": GENERIC_URL}))
        finally:
            fetch._get_from_url = original

        self.assertFalse(seen.get("include_img"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
