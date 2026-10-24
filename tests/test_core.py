"""mediagrab 内核测试：媒体解析、画质选择、镜像池、抓取降级、适配器形状、内嵌前端语法。

全部用本地 fixture，不访问真实站点。
"""
import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mediagrab as app


# ---------------------------------------------------------------- fixtures
PAGE_HTML = """
<html><head>
<title>示例视频 - 示例站点</title>
<meta property="og:title" content="示例视频">
<meta property="og:image" content="https://cdn.example.com/cover.jpg">
</head><body>
<video src="/media/720p.mp4" poster="p.jpg"></video>
<source src="https://cdn.example.com/1080p.mp4" size="1080">
<video src="https://ads.example.com/banner.mp4"></video>
<div data-src="https://cdn.example.com/extra.mp4"></div>
<script>
  var hls = "https://cdn.example.com/master.m3u8";
  player.setVideoUrlHigh("https://cdn.example.com/high.mp4");
  var relative = "/watch/999";
</script>
</body></html>
"""

MASTER_PLAYLIST = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
360/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720
720/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
1080/index.m3u8
"""

SEARCH_CARDS_HTML = """
<div class="video-card">
  <a href="/watch/abc-1" title="示例标题一"><img data-src="https://cdn.example.com/1.jpg" alt="示例标题一"></a>
  <span class="duration">12:34</span>
</div>
<div class="video-card">
  <a href="/watch/abc-2"><h3 class="title">示例标题二</h3>
    <img src="https://cdn.example.com/2.jpg"><span class="duration">05:06</span></a>
</div>
"""

NOVEL_DETAIL_HTML = '<html><div class="booknav2"><h1><a>示例书名</a></h1></div></html>'
NOVEL_TOC_HTML = """<html><div id="catalog">
<li data-num="2"><a href="/txt/2.html">第二章</a></li>
<li data-num="1"><a href="/txt/1.html">第一章</a></li>
</ul></div></html>"""
NOVEL_CHAPTER_HTML = '<html><div class="txtnav"><h1>第一章</h1>正文第一段。<br>正文第二段。</div></html>'


class MediaExtractionTests(unittest.TestCase):
    def test_extract_media_finds_all_shapes(self):
        medias = app.extract_media(PAGE_HTML, "https://example.com/watch/1")
        urls = {m.url for m in medias}
        self.assertIn("https://example.com/media/720p.mp4", urls)      # <video src> 相对路径补全
        self.assertIn("https://cdn.example.com/1080p.mp4", urls)       # <source size=>
        self.assertIn("https://cdn.example.com/extra.mp4", urls)       # data-src
        self.assertIn("https://cdn.example.com/master.m3u8", urls)     # JS 赋值
        self.assertIn("https://cdn.example.com/high.mp4", urls)        # 播放器 JS
        self.assertNotIn("https://example.com/watch/999", urls)        # 站内相对链接不是媒体

    def test_extract_media_filters_ads_and_dedupes(self):
        html = PAGE_HTML + PAGE_HTML          # 同一份内容再来一遍
        medias = app.extract_media(html, "https://example.com/watch/1")
        urls = [m.url for m in medias]
        self.assertEqual(len(urls), len(set(urls)), "重复地址应被去重")
        self.assertFalse([u for u in urls if "/ads/" in u or "banner" in u], "广告地址应被过滤")

    def test_response_text_honours_encoding(self):
        """页头没声明 charset 时不能退回 latin-1，否则中文标题变乱码文件名。"""
        class NoCharset:
            headers = {"Content-Type": "text/html"}
            content = "<title>示例视频</title>".encode("utf-8")
            text = content.decode("latin-1")

        self.assertEqual(app._response_text(NoCharset()), "<title>示例视频</title>")

        class DeclaredGbk:
            headers = {"Content-Type": "text/html; charset=gbk"}
            content = "标题".encode("gbk")
            text = "标题"
            apparent_encoding = "utf-8"

        self.assertEqual(app._response_text(DeclaredGbk()), "标题")

    def test_probe_media_size(self):
        class Ranged:
            headers = {"Content-Range": "bytes 0-0/2848208"}
            status_code = 206

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with patch("requests.get", return_value=Ranged()):
            self.assertEqual(app.probe_media_size("https://cdn.example.com/a.mp4"), 2848208)
        with patch("requests.get", side_effect=OSError("boom")):
            self.assertEqual(app.probe_media_size("https://cdn.example.com/a.mp4"), 0)

    def test_ad_filter_keeps_sample_named_media(self):
        """广告过滤只丢弃广告/预览目录，文件名含 sample-/preview- 的媒体不能误杀。"""
        html = ('<video src="https://cdn.example.com/mp4/sample-5s.mp4"></video>'
                '<video src="https://cdn.example.com/mp4/preview-1080p.mp4"></video>'
                '<video src="https://cdn.example.com/ads/promo.mp4"></video>'
                '<video src="https://cdn.example.com/sample.mp4"></video>')
        urls = {m.url for m in app.extract_media(html, "https://example.com/watch/1")}
        self.assertIn("https://cdn.example.com/mp4/sample-5s.mp4", urls)
        self.assertIn("https://cdn.example.com/mp4/preview-1080p.mp4", urls)
        self.assertNotIn("https://cdn.example.com/ads/promo.mp4", urls)
        self.assertNotIn("https://cdn.example.com/sample.mp4", urls)

    def test_size_attribute_wins_for_quality(self):
        medias = app.extract_media(PAGE_HTML, "https://example.com/watch/1")
        chosen = [m for m in medias if m.url.endswith("1080p.mp4")][0]
        self.assertEqual(chosen.height, 1080)
        self.assertEqual(chosen.kind, "mp4")

    def test_guess_quality_variants(self):
        self.assertEqual(app.guess_quality("https://x/video_720p.mp4"), ("720p", 720))
        self.assertEqual(app.guess_quality("https://x/1080.mp4"), ("1080p", 1080))
        self.assertEqual(app.guess_quality("https://x/film-4k.mp4"), ("4K", 2160))
        self.assertEqual(app.guess_quality("https://x/video.mp4"), ("", 0))


class PlaylistTests(unittest.TestCase):
    def test_parse_master_playlist(self):
        medias = app.parse_master_playlist(MASTER_PLAYLIST, "https://cdn.example.com/hls/master.m3u8")
        self.assertEqual([m.height for m in medias], [360, 720, 1080])
        self.assertEqual(medias[2].url, "https://cdn.example.com/hls/1080/index.m3u8")
        self.assertTrue(all(m.kind == "hls" for m in medias))

    def test_pick_media_prefers_exact_then_lower(self):
        log = app.Log("error", True)
        medias = app.parse_master_playlist(MASTER_PLAYLIST, "https://cdn.example.com/hls/master.m3u8")
        self.assertEqual(app.pick_media(medias, "720p", log).height, 720)
        self.assertEqual(app.pick_media(medias, "480p", log).height, 360, "没有 480p 时应退到不高于它的档位")
        self.assertEqual(app.pick_media(medias, "2160p", log).height, 1080, "超出最高档时取最高")
        self.assertEqual(app.pick_media(medias, "", log).height, 1080, "不指定时取最高")
        self.assertIsNone(app.pick_media([], "720p", log))


class ResolverTests(unittest.TestCase):
    def test_resolve_ldjson(self):
        html = ('<script type="application/ld+json">'
                '{"contentUrl":"https://cdn.example.com/a.mp4"}</script><title>片子</title>')
        medias, title, _ = app.resolve_ldjson(html, "https://example.com/v/1", None, app.Log("error", True))
        self.assertEqual(medias[0].url, "https://cdn.example.com/a.mp4")
        self.assertEqual(title, "片子")

    def test_resolve_ldjson_rejects_relative(self):
        html = '<script type="application/ld+json">{"contentUrl":"/local/a.mp4"}</script>'
        with self.assertRaises(LookupError):
            app.resolve_ldjson(html, "https://example.com/v/1", None, app.Log("error", True))

    def test_resolve_setvideo(self):
        html = ('<script>setVideoUrlHigh("https://cdn.example.com/hi.mp4");'
                'setVideoHLS("https://cdn.example.com/m.m3u8");</script>')
        medias, _, _ = app.resolve_setvideo(html, "https://example.com/v/1", None, app.Log("error", True))
        urls = {m.url for m in medias}
        self.assertIn("https://cdn.example.com/hi.mp4", urls)
        self.assertIn("https://cdn.example.com/m.m3u8", urls)
        kinds = {m.url: m.kind for m in medias}
        self.assertEqual(kinds["https://cdn.example.com/m.m3u8"], "hls")

    def test_resolve_accepts_both_resolver_shapes(self):
        """resolver 有返回 list 的，也有返回元组的，resolve() 必须两种都吃。"""
        class FakeFetcher:
            def fetch(self, url, referer=None, use_selenium=None):
                return SimpleNamespace(ok=True, status=200, error="", url=url,
                                       text=PAGE_HTML, engine="fake")

        with patch.dict(app.RESOLVERS, {"demo": lambda html, url, fetcher, log:
                                        [app.Media("https://cdn.example.com/list.mp4", "mp4", "720p", 720)]}), \
             patch.object(app, "kind_of", return_value="demo"):
            medias, _, _ = app.resolve("https://example.com/watch/1", FakeFetcher(), app.Log("error", True))
            self.assertEqual(medias[0].url, "https://cdn.example.com/list.mp4")

        with patch.dict(app.RESOLVERS, {"demo": lambda html, url, fetcher, log: (
                [app.Media("https://cdn.example.com/tuple.mp4", "mp4", "1080p", 1080)],
                "元组标题", SimpleNamespace(text="", url=url, ok=True))}), \
             patch.object(app, "kind_of", return_value="demo"):
            medias, title, _ = app.resolve("https://example.com/watch/1", FakeFetcher(), app.Log("error", True))
            self.assertEqual(medias[0].url, "https://cdn.example.com/tuple.mp4")
            self.assertEqual(title, "元组标题")

    def test_resolve_rejects_gallery_and_nav(self):
        with patch.object(app, "kind_of", return_value="nav"):
            with self.assertRaises(app.Unsupported):
                app.resolve("https://example.com/", None, app.Log("error", True))
        with self.assertRaises(app.Unsupported):
            app.resolve("https://example.com/gallery/1", None, app.Log("error", True))


class DomainPoolTests(unittest.TestCase):
    def test_canon_host_strips_www(self):
        self.assertEqual(app.canon_host("www.example.com"), "example.com")
        self.assertEqual(app.canon_host("EXAMPLE.com"), "example.com")
        self.assertEqual(app.canon_host("example.com"), "example.com")

    def test_kind_lookup_matches_www_host(self):
        """www 前缀必须能命中池子，否则 kind 会退回 generic 并走错解析器。"""
        self.assertEqual(app.kind_of("example.net"), "demo")
        self.assertEqual(app.kind_of("www.example.net"), "demo")
        self.assertEqual(app.kind_of("unknown.invalid"), "generic")

    def test_mirror_domains_same_kind_only(self):
        mirrors = app.mirror_domains("example.com")
        self.assertIn("example.net", mirrors)
        self.assertNotIn("media.example.org", mirrors, "不同 kind 不应互为镜像")
        self.assertNotIn("example.com", mirrors, "候选里不应包含自己")

    def test_swap_host_keeps_path_and_query(self):
        self.assertEqual(app.swap_host("https://a.example.com/x/y?z=1#f", "b.example.net"),
                         "https://b.example.net/x/y?z=1#f")


class FetchFallbackTests(unittest.TestCase):
    def _fetcher(self):
        return app.Fetcher(app.Log("error", True))

    def test_falls_through_engines_in_order(self):
        fetcher = self._fetcher()
        calls = []

        def fail(name):
            def inner(url, referer=None):
                calls.append(name)
                return app.FetchResult(False, error="%s 失败" % name)
            return inner

        fetcher._try_requests = fail("requests")
        fetcher._try_cloudscraper = fail("cloudscraper")
        fetcher._try_curl = lambda url, referer: (calls.append("curl") or
                                                 app.FetchResult(True, 200, "<html>ok</html>", url, "curl"))
        result = fetcher.fetch("https://example.com/")
        self.assertTrue(result.ok)
        self.assertEqual(result.engine, "curl")
        self.assertEqual(calls, ["requests", "cloudscraper", "curl"])
        self.assertTrue(any(t.startswith("curl:OK") for t in fetcher.tried))

    def test_cloudflare_challenge_is_treated_as_failure(self):
        fetcher = self._fetcher()
        fetcher._try_requests = lambda url, referer=None: app.FetchResult(
            True, 200, "<html><title>Just a moment...</title>cf-browser-verification</html>",
            url, "requests")
        fetcher._try_cloudscraper = lambda url, referer=None: app.FetchResult(
            True, 200, "<html>真页面</html>", url, "cloudscraper")
        fetcher._try_curl = lambda url, referer=None: app.FetchResult(False, error="不该走到这里")
        result = fetcher.fetch("https://example.com/")
        self.assertEqual(result.engine, "cloudscraper")
        self.assertIn("Cloudflare", fetcher.tried[0])

    def test_empty_body_is_treated_as_failure(self):
        fetcher = self._fetcher()
        fetcher._try_requests = lambda url, referer=None: app.FetchResult(True, 200, "   ", url, "requests")
        fetcher._try_cloudscraper = lambda url, referer=None: app.FetchResult(True, 200, "<html>x</html>", url, "cloudscraper")
        fetcher._try_curl = lambda url, referer=None: app.FetchResult(False, error="未使用")
        self.assertEqual(fetcher.fetch("https://example.com/").engine, "cloudscraper")


class CookieTests(unittest.TestCase):
    def test_netscape_and_json_cookie_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            netscape = Path(tmp) / "cookies.txt"
            netscape.write_text(
                "# Netscape HTTP Cookie File\n"
                ".example.com\tTRUE\t/\tFALSE\t0\tsid\tabc123\n", encoding="utf-8")
            cookies = app.load_cookies(str(netscape))
            self.assertEqual(cookies[0]["name"], "sid")
            self.assertEqual(app.cookie_header_for(cookies, "https://www.example.com/x"), "sid=abc123")
            self.assertEqual(app.cookie_header_for(cookies, "https://other.invalid/x"), "")

            js = Path(tmp) / "cookies.json"
            js.write_text(json.dumps([{"domain": ".example.com", "name": "k", "value": "v"}]),
                          encoding="utf-8")
            cookies = app.load_cookies(str(js))
            self.assertEqual(app.cookie_header_for(cookies, "https://example.com/"), "k=v")


class SearchAdapterTests(unittest.TestCase):
    def test_html_search_adapter_shape(self):
        with patch.object(app, "_source_get_html",
                          return_value=(SEARCH_CARDS_HTML, "https://example.com/search?q=x")):
            payload = app.fetch_html_search("x", page=1, base_url="https://example.com",
                                            href_pattern=r"^/watch/[^/?#]+$")
        self.assertEqual(len(payload["results"]), 2)
        first = payload["results"][0]
        self.assertEqual(first["title"], "示例标题一")
        self.assertEqual(first["url"], "https://example.com/watch/abc-1")
        self.assertEqual(first["duration"], "12:34")
        self.assertEqual(first["image"], "https://cdn.example.com/1.jpg")
        self.assertEqual(payload["current_page"], 1)

    def test_json_search_adapter_shape(self):
        payload_data = {"items": [
            {"title": "条目", "url": "/video/1", "thumbnail": "/t.jpg",
             "duration": 372, "author": {"name": "作者"}}],
            "total_pages": 7}
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(payload_data).encode()

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            return FakeResponse()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            payload = app.fetch_json_search(
                "关键词 x", page=2, base_url="https://media.example.org", api_path="/api/search",
                item_path="items",
                field_map={"title": "title", "url": "url", "image": "thumbnail",
                           "duration": "duration", "author": "author.name"})
        self.assertIn("q=", captured["url"])
        self.assertIn("page=2", captured["url"])
        item = payload["results"][0]
        self.assertEqual(item["url"], "https://media.example.org/video/1")
        self.assertEqual(item["image"], "https://media.example.org/t.jpg")
        self.assertEqual(item["author"], "作者")
        self.assertEqual(item["duration"], "372")
        self.assertEqual(payload["total_pages"], 7)

    def test_registry_wiring(self):
        self.assertTrue(app.SEARCH_SOURCES)
        for name, handler in app.SEARCH_SOURCES.items():
            self.assertTrue(callable(handler), "%s 不是可调用对象" % name)
        for kind in ("generic", "demo", "hls", "ldjson", "setvideo"):
            self.assertIn(kind, app.RESOLVERS)


class NovelTests(unittest.TestCase):
    def test_config_driven_scrape(self):
        pages = {
            "https://novel.example.com/book/42.htm": NOVEL_DETAIL_HTML,
            "https://novel.example.com/book/42/": NOVEL_TOC_HTML,
            "https://novel.example.com/txt/1.html": NOVEL_CHAPTER_HTML,
            "https://novel.example.com/txt/2.html": NOVEL_CHAPTER_HTML,
        }

        def fake_get_soup(url, referer=None):
            from bs4 import BeautifulSoup
            if url not in pages:
                raise AssertionError("未预期的 URL：%s" % url)
            return BeautifulSoup(pages[url], "html.parser")

        with patch.object(app, "_novel_fetcher", return_value=fake_get_soup), \
             patch.dict(app.NOVEL_SITES, {"demo": dict(app.NOVEL_SITES["demo"], delay=0)}):
            book = app.scrape_novel("https://novel.example.com/book/42.htm", site="demo")

        self.assertEqual(book["title"], "示例书名")
        self.assertEqual([c["title"] for c in book["chapters"]], ["第一章", "第二章"],
                         "章节应按 toc_num_attr 排序")
        self.assertEqual(book["failed"], 0)
        self.assertIn("正文第一段", book["chapters"][0]["text"])

    def test_unknown_site_config_raises(self):
        with self.assertRaises(ValueError):
            app.novel_site_config("no-such-site")

    def test_build_novel_html_has_toc_and_escapes(self):
        html = app.build_novel_html("书名<script>", [{"title": "第一章", "text": "a\nb"}])
        self.assertIn('id="ch1"', html)
        self.assertIn("&lt;script&gt;", html)
        # 阅读页自带一段阅读器 JS（唯一的 <script>），书名里的 <script> 必须仍是转义文本
        self.assertEqual(html.count("<script>"), 1, "标题注入的 script 未转义或出现了多余脚本标签")

    def test_reader_settings_present_in_page(self):
        html = app.build_novel_html("书名", [{"title": "第一章", "text": "正文"}])
        for marker in ('class="bar"', "__mg.step", "__mg.theme", 'id="drawer"', "class=\"progress\""):
            self.assertIn(marker, html, "阅读页缺少阅读设置组件：%s" % marker)

    def test_parse_novel_text_splits_chapters(self):
        book = app.parse_novel_text("《书名》\n\n第一章 起\n\n甲段\n\n第2章 承\n\n乙段\n", title="书名")
        self.assertEqual([c["title"] for c in book["chapters"]], ["第一章 起", "第2章 承"])
        self.assertIn("甲段", book["chapters"][0]["text"])
        self.assertEqual(book["failed"], 0)

    def test_parse_novel_text_without_headings_single_chapter(self):
        book = app.parse_novel_text("整篇没有章节标题\n第二行", title="孤本")
        self.assertEqual(len(book["chapters"]), 1)
        self.assertEqual(book["chapters"][0]["title"], "孤本")

    def test_parse_novel_text_rejects_empty(self):
        with self.assertRaises(ValueError):
            app.parse_novel_text("   \n  ", title="空")

    def test_import_novel_files_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            txt = Path(tmp) / "书.txt"
            txt.write_text("第一章 甲\n\n正文甲\n\n第二章 乙\n\n正文乙\n", encoding="utf-8")
            gbk = Path(tmp) / "旧编码.txt"
            gbk.write_bytes("第一章 丙\n\n正文丙\n".encode("gbk"))
            missing = Path(tmp) / "不存在.txt"
            with patch.object(app, "NOVEL_DIR", tmp):
                results = app.import_novel_files([str(txt), str(gbk), str(missing)])
                self.assertEqual(results[0]["chapters"], 2)
                self.assertEqual(results[1]["chapters"], 1, "gbk 编码应自动识别")
                self.assertIn("error", results[2])
                shelf = app.list_novel_books()["books"]
                self.assertEqual({b["name"] for b in shelf}, {"书.html", "旧编码.html"})
                html = (Path(tmp) / "书.html").read_text(encoding="utf-8")
                self.assertIn('id="ch2"', html)


class WebFrontendTests(unittest.TestCase):
    def _dropdown_modes(self):
        match = re.search(r'<select id="searchMode">(.*?)</select>', app.WEB_HTML, re.S)
        self.assertIsNotNone(match, "找不到 searchMode 下拉框")
        return re.findall(r'<option value="([^"]+)"', match.group(1))

    def test_embedded_js_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不可用，跳过内嵌 JS 语法校验")
        match = re.search(r"<script>(.*)</script>", app.WEB_HTML, re.S)
        self.assertIsNotNone(match)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(match.group(1))
            path = handle.name
        try:
            proc = subprocess.run([node, "--check", path], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0,
                             "内嵌 JS 语法错误：%s" % (proc.stderr or proc.stdout)[:600])
        finally:
            Path(path).unlink(missing_ok=True)

    def test_dropdown_modes_are_all_handled(self):
        special = {"novel"}
        for mode in self._dropdown_modes():
            if mode in special:
                continue
            with self.subTest(mode=mode):
                handled = (mode in app.SEARCH_SOURCES
                           or mode in ("bing_image", "baidu_image", "so_image"))
                self.assertTrue(handled, "下拉项 %s 没有任何后端分支处理" % mode)

    def test_single_definition_of_core_js_functions(self):
        for name in ("async function search()", "async function downloadItem(",
                     "async function poll(", "function renderCards("):
            with self.subTest(fn=name):
                self.assertEqual(app.WEB_HTML.count(name), 1,
                                 "%s 定义了多次，后者会静默覆盖前者" % name)


class DownloadGuardTests(unittest.TestCase):
    """ffmpeg 可能在流被截断后仍以 0 退出并留下残档，光看「文件非空」会误报成功。"""

    def _media(self, expected):
        return app.Media("https://cdn.example.com/a.mp4", "mp4", "720p", 720, size=expected)

    def _fake_ffmpeg(self, write_bytes):
        class FakeProc:
            returncode = 0

            def __init__(self):
                self.stdout = iter([])
                self.stderr = io.StringIO("Stream ends prematurely")

            def wait(self):
                return 0

        def fake_popen(cmd, **kwargs):
            with open(cmd[-1], "wb") as handle:
                handle.write(b"x" * write_bytes)
            return FakeProc()

        return fake_popen

    def test_truncated_output_is_rejected_and_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "v.mp4"
            media = self._media(10_000_000)
            with patch.object(app, "ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
                 patch("subprocess.Popen", side_effect=self._fake_ffmpeg(1024)):
                ok = app.download_ffmpeg(media, out, "https://example.com/", "", None,
                                         app.Log("error", True))
            self.assertFalse(ok, "输出远小于期望时必须判失败")
            self.assertFalse(out.exists(), "残档应被丢弃，避免被当成成品")

    def test_complete_output_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "v.mp4"
            media = self._media(2048)
            with patch.object(app, "ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
                 patch("subprocess.Popen", side_effect=self._fake_ffmpeg(2048)):
                ok = app.download_ffmpeg(media, out, "https://example.com/", "", None,
                                         app.Log("error", True))
            self.assertTrue(ok)
            self.assertTrue(out.exists())


class MiscTests(unittest.TestCase):
    def test_sanitize_filename(self):
        # <f> 会被当 HTML 标签剥掉，其余非法字符各自替成下划线
        self.assertEqual(app.sanitize_filename('a/b:c*d?"e<f>g|h'), "a_b_c_d__eg_h")
        self.assertEqual(app.sanitize_filename("<b>粗体</b>标题"), "粗体标题")
        self.assertEqual(app.sanitize_filename("   "), "video")
        self.assertEqual(len(app.sanitize_filename("x" * 500)), 110)

    def test_fmt_size(self):
        self.assertEqual(app._fmt_size(512), "512.0B")
        self.assertEqual(app._fmt_size(1536), "1.5KB")
        self.assertEqual(app._fmt_size(5 * 1024 * 1024), "5.0MB")

    def test_page_title_uses_suffix_regex(self):
        html = "<title>示例标题 - Video</title>"
        self.assertEqual(app.page_title(html), "示例标题")

    def test_exit_codes_are_distinct(self):
        codes = [app.EXIT_OK, app.EXIT_GENERIC, app.EXIT_ALL_DOMAINS_FAILED,
                 app.EXIT_UNSUPPORTED, app.EXIT_DOWNLOAD_FAILED, app.EXIT_NO_MEDIA]
        self.assertEqual(len(codes), len(set(codes)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
