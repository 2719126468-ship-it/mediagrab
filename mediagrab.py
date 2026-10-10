#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mediagrab —— 通用媒体下载器（视频 / 图片 / 小说）

一个单文件、多站点可插拔的下载器框架，面向 Linux / Termux(Android) 环境，无需 root。

公开版只包含**通用技术内核**：抓取降级、镜像池、媒体解析、下载引擎、本地 Web UI。
站点适配器（搜索 / 解析 / 图集 / 小说站点配置）由使用者自行按模板补充，
仓库内置的是可运行的示例模板，不含任何具体站点的业务数据。

能力概览：
  1. 多级降级抓取：requests → cloudscraper → curl → selenium，并识别 Cloudflare 挑战页。
  2. 镜像/候选域池：同 kind 域名互相替换（保持 path+query），成功记录写入状态文件成为优选。
  3. 媒体解析：<source>/<video>、data-* 属性、裸 m3u8、JS 赋值、og:video/meta、
     LD+JSON contentUrl、播放器 setVideo*() 调用，以及 HLS master playlist 分档。
  4. 下载：ffmpeg -c copy 直出（带 Referer/UA/Cookie 头）；缺 ffmpeg 时降级为原生
     HTTP Range 断点续传（仅直链 mp4）。
  5. 本地 Web UI：Token 鉴权 + Host 校验 + 速率限制、任务轮询、Range 文件服务、
     缩略图代理、收藏 / 历史 / 媒体库。
  6. 小说抓取：CSS 选择器配置化的正文抽取，产出单文件深色主题 HTML 阅读页。

用法：
    python mediagrab.py <页面URL> [输出文件名] [cookies.txt] [选项]
    python mediagrab.py --web
    python mediagrab.py --search "关键词" --download-first -q 720p

退出码：
  0 成功 / 1 通用错误 / 2 全部候选域名失败 / 3 站点不支持 / 4 下载失败 / 5 未找到媒体
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import asyncio
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse, urlunparse, parse_qs, quote, unquote, unquote_plus

VERSION = "1.0.0"

# ---------------------------------------------------------------- 基础常量
UA = "Mozilla/5.0 (Linux; Android 10) Chrome/120.0.0.0"
UA_FULL = UA + " Mobile Safari/537.36"

# 所有路径都可用环境变量覆盖，默认不写死任何平台的绝对路径。
_HOME = Path.home()
DEFAULT_OUTDIR = os.environ.get("MEDIAGRAB_OUTDIR") or str(_HOME / "Downloads" / "MediaGrab")
DEFAULT_MEDIA_DIR = os.environ.get("MEDIAGRAB_MEDIA_DIR") or str(_HOME / "Downloads" / "MediaGrab" / "video")
DEFAULT_GALLERY_DIR = os.environ.get("MEDIAGRAB_GALLERY_DIR") or str(_HOME / "Downloads" / "MediaGrab" / "gallery")
DEFAULT_IMAGE_DIR = os.environ.get("MEDIAGRAB_IMAGE_DIR") or str(_HOME / "Downloads" / "MediaGrab" / "image")
NOVEL_DIR = os.environ.get("MEDIAGRAB_NOVEL_DIR") or str(_HOME / "Downloads" / "MediaGrab" / "novel")

MAX_LINE = 200                      # 单行日志截断，避免终端卡顿
STATE_HOME = Path(os.environ.get("MEDIAGRAB_HOME") or (_HOME / ".config" / "mediagrab"))
STATE_FILE = STATE_HOME / "domain-state.json"
FAVORITES_FILE = STATE_HOME / "favorites.json"
HISTORY_FILE = STATE_HOME / "history.json"
RESULT_MARK = "@@RESULT@@"

LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_ALL_DOMAINS_FAILED = 2
EXIT_UNSUPPORTED = 3
EXIT_DOWNLOAD_FAILED = 4
EXIT_NO_MEDIA = 5


# ---------------------------------------------------------------- 站点候选池
# 站点池是**示例模板**：把你自己的站点按同样结构填进来即可。
# kind 决定「镜像家族」——只有同 kind 的域名才会被当作镜像互相替换，
# 避免把 A 站的路径套到 B 站上。priority 越小越优先。
#
#   域名一律写不带 www 的形式（代码会用 canon_host() 去掉 www 再匹配）。
SITE_POOL = [
    {"domain": "example.com",       "kind": "demo",    "priority": 10, "note": "占位示例：请替换为你的主站点"},
    {"domain": "example.net",       "kind": "demo",    "priority": 20, "note": "占位示例：同 kind 才会被当作镜像"},
    {"domain": "media.example.org", "kind": "hls",     "priority": 30, "note": "占位示例：HLS 流站点"},
]

# 站点形态分类 -> 中文标签（仅用于 --list-domains 展示）
KIND_LABEL = {
    "demo": "示例站点", "html": "HTML 站点", "json": "JSON API 站点",
    "hls": "HLS 流", "generic": "通用站点", "nav": "索引/导航站",
    "drm": "官方/带 DRM",
}

# 页面 <title> 里常见「标题 - 站点名」的尾巴，解析标题时按这个正则去掉。
# 换站点改这里即可，不写死任何具体站点。
TITLE_SUFFIX_RE = r"\s*[-|｜]\s*(?:在线观看|線上看|高清|免费|官方站|Video|Site)"

# 命中这些路径片段时，视为图集/阅读页而非视频页，不尝试媒体解析。
GALLERY_HINT = ("/gallery", "/comic", "/comics", "/manga", "/photos")


class Log:
    """带级别与行截断的中文日志器；--json 时全部走 stderr，保证 stdout 干净。"""

    def __init__(self, level: str = "info", json_mode: bool = False):
        self.level = LEVELS.get(level, LEVELS["info"])
        self.json_mode = json_mode
        self.stream = sys.stderr if json_mode else sys.stdout

    def _emit(self, lvl: str, msg: str) -> None:
        if LEVELS[lvl] < self.level:
            return
        tag = {"debug": "调试", "info": "信息", "warn": "警告", "error": "错误"}[lvl]
        for line in str(msg).splitlines() or [""]:
            if len(line) > MAX_LINE:
                line = line[:MAX_LINE] + "…（已截断，共 %d 字符）" % len(line)
            print("[%s] %s" % (tag, line), file=self.stream, flush=True)

    def debug(self, m: str) -> None: self._emit("debug", m)
    def info(self, m: str) -> None: self._emit("info", m)
    def warn(self, m: str) -> None: self._emit("warn", m)
    def error(self, m: str) -> None: self._emit("error", m)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_HOME.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


def note_domain(state: dict, domain: str, ok: bool, err: str | None = None) -> None:
    rec = state.setdefault(domain, {"ok": 0, "fail": 0, "last_ok": None, "last_error": None})
    if ok:
        rec["ok"] += 1
        rec["last_ok"] = int(time.time())
        rec["last_error"] = None
    else:
        rec["fail"] += 1
        rec["last_error"] = (err or "")[:160]
    save_state(state)


def domain_score(state: dict, domain: str):
    rec = state.get(domain, {})
    ok = rec.get("ok", 0)
    fail = rec.get("fail", 0)
    return (-(ok > 0), fail, -ok)


def canon_host(domain: str) -> str:
    """Strip the leading www. so pool lookups match www-prefixed hosts.

    Without this, `www.example.com` misses its pool entry and falls back to the
    generic kind — which silently routes it to the wrong resolver and lets the
    mirror pool swap its path onto unrelated sites.
    """
    host = (domain or "").strip().lower()
    return host[4:] if host.startswith("www.") else host


def pool_entry(domain: str):
    host = canon_host(domain)
    for e in SITE_POOL:
        if e["domain"] == host:
            return e
    return None


def kind_of(domain: str) -> str:
    e = pool_entry(domain)
    return e["kind"] if e else "generic"


def mirror_domains(domain: str, extra=None):
    """返回与 domain 同家族（同 kind）的候选域名，按状态优选排序。"""
    state = load_state()
    k = kind_of(domain)
    if extra:
        cands = list(dict.fromkeys(extra))
    else:
        cands = [e["domain"] for e in SITE_POOL if e["kind"] == k]
    canon = canon_host(domain)
    cands = [d for d in cands if canon_host(d) != canon]
    cands.sort(key=lambda d: (domain_score(state, d), pool_entry(d)["priority"] if pool_entry(d) else 50))
    return cands


def swap_host(url: str, new_host: str) -> str:
    """保持 path / query / fragment 不变，只替换域名（协议沿用 https）。"""
    p = urlparse(url)
    return urlunparse(("https", new_host, p.path, p.params, p.query, p.fragment))


def cmd_list_domains(log: Log) -> dict:
    state = load_state()
    rows = []
    for e in SITE_POOL:
        rec = state.get(e["domain"], {})
        rows.append({
            "domain": e["domain"], "kind": e["kind"], "kind_label": KIND_LABEL.get(e["kind"], e["kind"]),
            "priority": e["priority"], "note": e["note"],
            "ok": rec.get("ok", 0), "fail": rec.get("fail", 0),
            "preferred": bool(rec.get("ok", 0)) and rec.get("fail", 0) == 0,
            "last_error": rec.get("last_error"),
        })
    rows.sort(key=lambda r: (r["priority"], r["domain"]))
    # 摘要始终输出：--json 时走 stderr，人类可读；stdout 只留给 @@RESULT@@ 行。
    out = log.stream
    print("\n候选域名池（共 %d 条，状态文件：%s）" % (len(rows), STATE_FILE), file=out, flush=True)
    print("%-26s%-14s%4s%5s  %s" % ("域名", "类型", "成功", "失败", "说明"), file=out, flush=True)
    print("-" * 88, file=out, flush=True)
    for r in rows:
        star = "★" if r["preferred"] else " "
        print("%s%-25s%-14s%4d%5d  %s" % (star, r["domain"], r["kind_label"], r["ok"], r["fail"], r["note"]),
              file=out, flush=True)
    print("\n★ = 优选域名（有成功记录且无失败记录）\n", file=out, flush=True)
    return {"ok": True, "domains": rows, "state_file": str(STATE_FILE)}


def load_cookies(path: str):
    """支持 Netscape cookies.txt（Kiwi/Chrome 插件导出）与 JSON 导出两种格式。"""
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError("Cookie 文件不存在：%s" % p)
    text = p.read_text("utf-8", errors="replace").strip()

    if text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                data = data.get("cookies", [])
            out = []
            for c in data:
                if isinstance(c, dict) and c.get("name"):
                    out.append({"domain": c.get("domain", ""), "name": c["name"], "value": c.get("value", "")})
            return out
        except Exception:
            pass

    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            out.append({"domain": parts[0], "name": parts[5], "value": parts[6]})
    return out


def cookie_header_for(cookies, url: str) -> str:
    host = urlparse(url).hostname or ""
    picked = []
    for c in cookies:
        d = (c.get("domain") or "").lstrip(".")
        if d and (host == d or host.endswith("." + d)):
            picked.append("%s=%s" % (c["name"], c["value"]))
    return "; ".join(picked)


def _response_text(response) -> str:
    """把响应解成文本。

    Header 没声明 charset 时 requests 会退回 latin-1，中文站点标题就会变成乱码
    文件名（"示例视频" → "ç¤ºä¾...")。这里按探测编码解码再兜底。
    """
    content_type = str(response.headers.get("Content-Type") or "")
    if "charset=" in content_type.lower():
        return response.text
    try:
        encoding = getattr(response, "apparent_encoding", None)
    except Exception:
        encoding = None
    body = getattr(response, "content", None)
    if encoding and body is not None:
        try:
            return body.decode(encoding, "replace")
        except Exception:
            pass
    # 没有编码探测库时，UTF-8 是绝大多数现代站点的正确选择；
    # 直接退回 response.text 会得到 latin-1 乱码。
    if body is not None:
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return response.text


def probe_media_size(url, referer=None, cookie=None, proxy=None) -> int:
    """用 Range 探测媒体真实字节数（失败返回 0，不影响下载流程）。

    拿到期望大小后，才能在 ffmpeg 输出被截断时判定失败，而不是把一个
    只有几百字节的残档当成「下载完成」。
    """
    try:
        import requests
    except ImportError:
        return 0
    headers = {"User-Agent": UA_FULL, "Range": "bytes=0-0"}
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        with requests.get(url, headers=headers, stream=True, timeout=20, proxies=proxies) as r:
            match = re.search(r"/(\d+)\s*$", r.headers.get("Content-Range", ""))
            if match:
                return int(match.group(1))
            if r.status_code == 200 and r.headers.get("Content-Length"):
                return int(r.headers["Content-Length"])
    except Exception:
        return 0
    return 0


@dataclass
class FetchResult:
    ok: bool
    status: int = 0
    text: str = ""
    url: str = ""
    engine: str = ""
    error: str = ""


class Fetcher:
    """多级降级抓取：requests -> cloudscraper -> curl -> selenium。"""

    def __init__(self, log, cookie_file=None, proxy=None, use_selenium=False,
                 selenium_wait=8.0, timeout=25.0):
        self.log = log
        self.cookies = []
        self.cookie_file = cookie_file
        if cookie_file:
            try:
                self.cookies = load_cookies(cookie_file)
                log.info("已加载 Cookie %d 条：%s" % (len(self.cookies), cookie_file))
            except Exception as exc:
                log.warn("Cookie 加载失败，忽略：%s" % exc)
        self.proxy = proxy
        self.use_selenium = use_selenium
        self.selenium_wait = selenium_wait
        self.timeout = timeout
        self._scraper = None
        self.tried = []

    def headers(self, url: str, referer=None) -> dict:
        h = {
            "User-Agent": UA_FULL,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": referer or ("https://%s/" % urlparse(url).hostname),
            "Upgrade-Insecure-Requests": "1",
        }
        ck = cookie_header_for(self.cookies, url)
        if ck:
            h["Cookie"] = ck
        return h

    def _try_requests(self, url, referer):
        try:
            import requests
        except ImportError:
            return FetchResult(False, error="requests 未安装")
        try:
            sess = requests.Session()
            r = sess.get(url, headers=self.headers(url, referer), timeout=self.timeout,
                         proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None)
            if r.status_code >= 400:
                return FetchResult(False, r.status_code, _response_text(r)[:4000], r.url,
                                   "requests", "HTTP %d" % r.status_code)
            return FetchResult(True, r.status_code, _response_text(r), r.url, "requests")
        except Exception as exc:
            return FetchResult(False, error="%s: %s" % (type(exc).__name__, exc))

    def _scraper_session(self):
        if self._scraper is not None:
            return self._scraper
        import cloudscraper
        attempts = (
            {"interpreter": "hybrid", "turbo_mode": True},
            {"interpreter": "hybrid"},
            {"interpreter": "native"},
            {},
        )
        last = None
        for kw in attempts:
            try:
                self._scraper = cloudscraper.create_scraper(**kw)
                self.log.debug("cloudscraper 初始化成功：%s" % (kw,))
                return self._scraper
            except Exception as exc:
                last = exc
                continue
        raise RuntimeError("cloudscraper 初始化失败：%s" % last)

    def _try_cloudscraper(self, url, referer):
        try:
            s = self._scraper_session()
        except ImportError:
            return FetchResult(False, error="cloudscraper 未安装")
        except Exception as exc:
            return FetchResult(False, error=str(exc))
        try:
            r = s.get(url, headers=self.headers(url, referer), timeout=self.timeout,
                      proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None)
            if r.status_code >= 400:
                return FetchResult(False, r.status_code, _response_text(r)[:4000], r.url,
                                   "cloudscraper", "HTTP %d" % r.status_code)
            return FetchResult(True, r.status_code, _response_text(r), r.url, "cloudscraper")
        except Exception as exc:
            return FetchResult(False, error="%s: %s" % (type(exc).__name__, exc))

    def _try_curl(self, url, referer):
        curl = shutil.which("curl")
        if not curl:
            return FetchResult(False, error="curl 不可用")
        cmd = [curl, "-sSL", "--max-time", str(int(self.timeout)), "-A", UA_FULL,
               "-H", "Referer: %s" % (referer or ("https://%s/" % urlparse(url).hostname)),
               "-H", "Accept-Language: zh-CN,zh;q=0.9"]
        ck = cookie_header_for(self.cookies, url)
        if ck:
            cmd += ["-H", "Cookie: %s" % ck]
        if self.proxy:
            cmd += ["-x", self.proxy]
        cmd.append(url)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout + 10)
            if p.returncode != 0 or not p.stdout:
                return FetchResult(False, error="curl 退出码 %d: %s" % (p.returncode, p.stderr.strip()[:200]))
            return FetchResult(True, 200, p.stdout, url, "curl")
        except Exception as exc:
            return FetchResult(False, error="%s: %s" % (type(exc).__name__, exc))

    def _try_selenium(self, url, referer=None):
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
        except ImportError:
            return FetchResult(False, error="selenium 未安装（pip install selenium；pkg install chromium）")
        if not shutil.which("chromedriver") and not shutil.which("chromium") and not shutil.which("chrome"):
            self.log.warn("未检测到 chromium/chromedriver；Termux 下可 pkg install chromium -y 后重试")
        opts = Options()
        for a in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--disable-blink-features=AutomationControlled", "--user-agent=%s" % UA_FULL,
                  "--window-size=1280,1600"):
            opts.add_argument(a)
        try:
            driver = webdriver.Chrome(options=opts)
        except Exception as exc:
            return FetchResult(False, error="Selenium 启动失败：%s: %s" % (type(exc).__name__, exc))
        try:
            driver.get(url)
            time.sleep(self.selenium_wait)
            return FetchResult(True, 200, driver.page_source, driver.current_url, "selenium")
        except Exception as exc:
            return FetchResult(False, error="Selenium 抓取失败：%s" % exc)
        finally:
            try:
                driver.quit()
            except Exception:
                pass

    def fetch(self, url: str, referer=None, use_selenium=None) -> FetchResult:
        self.tried = []
        chain = [("requests", self._try_requests),
                 ("cloudscraper", self._try_cloudscraper),
                 ("curl", self._try_curl)]
        if use_selenium is None:
            use_selenium = self.use_selenium
        if use_selenium:
            chain.append(("selenium", self._try_selenium))

        last = FetchResult(False, error="未尝试任何引擎")
        for name, fn in chain:
            self.log.debug("尝试引擎：%s" % name)
            res = fn(url, referer)
            self.tried.append("%s:%s" % (name, "OK" if res.ok else (res.error or res.status)))
            challenge = res.ok and re.search(
                r"(?:cf-browser-verification|cf-chl-|cloudflare ray id|just a moment\.\.\.|"
                r"checking your browser|attention required! \| cloudflare)", res.text, re.I)
            if challenge:
                res = FetchResult(False, res.status, res.text[:4000], res.url, name,
                                  "检测到 Cloudflare 挑战页")
                self.tried[-1] = "%s:%s" % (name, res.error)
            if res.ok and res.text.strip():
                self.log.info("抓取成功（%s，HTTP %s，%d 字符）" % (name, res.status, len(res.text)))
                return res
            last = res
            if res.status in (403, 429, 503) or "HTTP" in (res.error or ""):
                self.log.warn("%s 被拦截或失败：%s，降级到下一引擎" % (name, res.error or res.status))
            else:
                self.log.debug("%s 失败：%s" % (name, res.error))
        return last


def _plain_text(fragment: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", " ",
                  fragment, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


# ---------------------------------------------------------------- 抓取器
def _source_get_html(url, referer, retries=2, extra_headers=None):
    """抓取页面 HTML：curl_cffi（TLS 指纹伪装）优先，requests 兜底，带重试与线性退避。

    curl_cffi 未安装时自动跳过，不影响其他引擎。
    """
    headers = {"User-Agent": UA_FULL, "Referer": referer,
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
               "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    headers.update(extra_headers or {})
    errors = []

    for attempt in range(retries + 1):
        try:
            from curl_cffi import requests as cffi_requests
            response = cffi_requests.get(url, impersonate="chrome120", timeout=30, headers=headers)
            response.raise_for_status()
            return response.text, response.url or url
        except ImportError:
            break
        except Exception as exc:
            errors.append(exc)
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))

    for attempt in range(retries + 1):
        try:
            import requests
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            return response.text, response.url or url
        except Exception as exc:
            errors.append(exc)
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))

    raise RuntimeError("页面抓取失败：%s" % (errors[-1] if errors else "未知错误"))


def _video_card_container(node):
    """Find a nearby card/container without depending on one site's exact class name."""
    current = node
    for _ in range(5):
        if not current:
            break
        if getattr(current, "name", None) in ("article", "li"):
            return current
        classes = current.get("class") if hasattr(current, "get") else None
        class_text = " ".join(classes) if isinstance(classes, list) else str(classes or "")
        if re.search(r"(?:video|item|thumb|card|post|media|result)", class_text, re.I):
            return current
        current = current.parent
    return node.parent or node


def _video_card_image(card, base_url):
    img = card.select_one("img[src], img[data-src], img[data-original], img[data-lazy]")
    if not img:
        return ""
    for attr in ("data-src", "data-original", "data-lazy", "src"):
        value = img.get(attr, "")
        if value and not str(value).startswith("data:"):
            return urljoin(base_url, html_lib.unescape(str(value)))
    return ""


def _video_card_text(card):
    return card.get_text(" ", strip=True) if card else ""


def _video_duration_from_card(card):
    if not card:
        return ""
    node = card.select_one(".duration, .video-duration, .time, .length, [data-duration]")
    if node:
        value = node.get("data-duration") or node.get_text(" ", strip=True)
        if value:
            return str(value).strip()
    text = _video_card_text(card)
    match = re.search(r"\b(?:\d{1,2}:)?\d{1,2}:\d{2}\b", text)
    return match.group(0) if match else ""


def _video_card_title(link, card, image=""):
    candidates = []
    # Prefer explicit title/aria labels and heading/title nodes so duration/meta
    # text from the whole anchor does not become part of the displayed title.
    for node in (
        link,
        card.select_one("h2, h3, h4, .title, .video-title, .name, .caption") if card else None,
        card.select_one("img[alt], img[title]") if card else None,
    ):
        if not node:
            continue
        if hasattr(node, "get"):
            candidates.extend([node.get("title", ""), node.get("aria-label", "")])
            if getattr(node, "name", "") == "img":
                candidates.append(node.get("alt", ""))
        if node is not link:
            text = node.get_text(" ", strip=True) if hasattr(node, "get_text") else ""
            if text:
                candidates.append(text)
    # Finally use the full link text as a fallback.
    if link is not None and hasattr(link, "get_text"):
        candidates.append(link.get_text(" ", strip=True))
    for value in candidates:
        value = html_lib.unescape(str(value)).strip()
        if value and len(value) > 1:
            return value[:200]
    return image.rsplit("/", 1)[-1][:200] if image else "未命名视频"


def _parse_video_search_html(html, base_url, href_pattern):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    results, seen = [], set()
    for link in soup.select("a[href]"):
        href = str(link.get("href", ""))
        if not re.search(href_pattern, href, re.I):
            continue
        url = urljoin(base_url, href).split("#", 1)[0]
        if url in seen:
            continue
        card = _video_card_container(link)
        image = _video_card_image(card, base_url)
        title = _video_card_title(link, card, image)
        duration = _video_duration_from_card(card)
        author_node = card.select_one(".author, .username, .uploader, [rel=author]") if card else None
        author = author_node.get_text(" ", strip=True) if author_node else ""
        seen.add(url)
        results.append({"title": title, "url": url, "image": image,
                        "thumbnail": image, "duration": duration, "author": author[:100]})
    return results


def _format_duration_seconds(value):
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return str(value or "")[:30]
    if seconds < 0:
        return ""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


# ---------------------------------------------------------------- 通用搜索适配器（模板）
def _fill(template: str, **values) -> str:
    """把 {query} / {page} / {sort} 占位符安全替换成编码后的值。"""
    text = str(template or "")
    for key, value in values.items():
        text = text.replace("{%s}" % key, quote(str(value), safe=""))
    return text


def _dig(obj, path):
    """按点号路径取值：_dig(item, "user.name")。"""
    current = obj
    for part in str(path).split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


def fetch_html_search(query, page=1, sort="", *, base_url, search_path="/search",
                      href_pattern=r"/watch/[^/?#]+", params=None, referer=None,
                      extra_headers=None, total_pages_from_pagination=True):
    """通用 HTML 搜索适配器：抓搜索页 → 按 href_pattern 提取卡片。

    写自己的适配器时照抄这个形状即可（签名固定为 fetch(q, page, sort)）：

        def fetch_mysite(q, page=1, sort="popular"):
            return fetch_html_search(
                q, page, sort,
                base_url="https://mysite.example",
                search_path="/search",
                href_pattern=r"^/watch/[^/?#]+/?$",
                params={"q": "{query}", "page": "{page}"},
            )

    params 的取值支持 {query} / {page} / {sort} 占位符，会被 URL 编码后填入。
    """
    base_url = str(base_url).rstrip("/")
    page = max(1, int(page))
    filled = {}
    for key, value in (params or {"q": "{query}", "page": "{page}"}).items():
        filled[key] = _fill(str(value), query=query, page=page, sort=sort)
    search_url = base_url + search_path + ("?" + urlencode(filled) if filled else "")

    headers = dict(extra_headers or {})
    html, final_url = _source_get_html(search_url, referer or base_url + "/",
                                       extra_headers=headers)
    results = _parse_video_search_html(html, final_url or search_url, href_pattern)

    total_pages = None
    if total_pages_from_pagination and results:
        numbers = [int(n) for n in re.findall(r"[?&]page=(\d+)", html, re.I)]
        total_pages = max(numbers) if numbers else page
    elif not results:
        total_pages = page
    return {"results": results, "current_page": page,
            "total_pages": total_pages, "source": urlparse(base_url).hostname}


def fetch_json_search(query, page=1, sort="", *, base_url, api_path, item_path="results",
                      field_map=None, params=None, referer=None, extra_headers=None,
                      page_size=20):
    """通用 JSON API 搜索适配器：按 item_path 取列表，按 field_map 映射字段。

    field_map 形如 {"title": "name", "url": "link", "image": "cover"}，
    值支持点号路径（如 "user.name"）；url/image 为相对路径时会用 base_url 补全。
    """
    from urllib.request import Request, urlopen

    base_url = str(base_url).rstrip("/")
    page = max(1, int(page))
    fields = {"title": "title", "url": "url", "image": "thumbnail",
              "duration": "duration", "author": ""}
    fields.update(field_map or {})

    query_string = "&".join(
        "%s=%s" % (key, _fill(str(value), query=query, page=page, sort=sort))
        for key, value in (params or {"q": "{query}", "page": "{page}"}).items()
    )
    api_url = base_url + api_path + ("?" + query_string if query_string else "")

    headers = {"User-Agent": UA_FULL, "Accept": "application/json,text/plain,*/*",
               "Referer": referer or base_url + "/"}
    headers.update(extra_headers or {})
    try:
        with urlopen(Request(api_url, headers=headers), timeout=25) as response:
            payload = json.loads(response.read().decode("utf-8", "ignore"))
    except Exception as exc:
        raise RuntimeError("JSON 搜索请求失败（%s）：%s" % (api_url, exc))

    items = _dig(payload, item_path) if item_path else payload
    if not isinstance(items, list):
        items = []

    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _dig(item, fields["title"]) if fields["title"] else ""
        url = _dig(item, fields["url"]) if fields["url"] else ""
        image = _dig(item, fields["image"]) if fields["image"] else ""
        duration = _dig(item, fields["duration"]) if fields["duration"] else ""
        author = _dig(item, fields["author"]) if fields["author"] else ""
        if not url or not title:
            continue
        url = urljoin(base_url + "/", str(url))
        image = urljoin(base_url + "/", str(image)) if image else ""
        results.append({"title": str(title)[:200], "url": url,
                        "image": image, "thumbnail": image,
                        "duration": str(duration or "")[:30],
                        "author": str(author or "")[:100]})

    total_pages = payload.get("total_pages") if isinstance(payload, dict) else None
    try:
        total_pages = int(total_pages)
    except (TypeError, ValueError):
        total_pages = page + 1 if len(results) >= page_size else page
    return {"results": results, "current_page": page,
            "total_pages": total_pages,
            "source": urlparse(base_url).hostname}


def fetch_demo_html(query, page=1, sort=""):
    """示例适配器（指向占位域名，返回空结果是正常的）。替换成你自己的站点即可。"""
    return fetch_html_search(
        query, page, sort,
        base_url="https://example.com",
        search_path="/search",
        href_pattern=r"^/watch/[^/?#]+/?$",
        params={"q": "{query}", "page": "{page}"},
    )


def fetch_demo_json(query, page=1, sort=""):
    """示例适配器（JSON API 形状）。"""
    return fetch_json_search(
        query, page, sort,
        base_url="https://media.example.org",
        api_path="/api/search",
        item_path="items",
        field_map={"title": "title", "url": "url", "image": "thumbnail",
                   "duration": "duration", "author": ""},
        params={"q": "{query}", "page": "{page}"},
    )


# 搜索源注册表：键名 = Web UI 下拉取值 / CLI --source 取值。
# 这里放的是可运行的示例，按 fetch(q, page, sort) 签名补自己的站点即可。
SEARCH_SOURCES = {
    "demo-html": fetch_demo_html,
    "demo-json": fetch_demo_json,
}


@dataclass
class Media:
    url: str
    kind: str = "mp4"          # mp4 | hls
    quality: str = ""
    height: int = 0
    size: int = 0
    label: str = ""
    origin: str = ""

    def to_dict(self) -> dict:
        return {"url": self.url, "kind": self.kind, "quality": self.quality,
                "height": self.height, "size": self.size, "label": self.label, "origin": self.origin}


def _clean_html(html: str) -> str:
    return (html.replace("\\/", "/").replace("&amp;", "&")
                .replace("\\u002F", "/").replace("\\u002f", "/").replace("&#38;", "&"))


def guess_quality(text: str):
    """从 URL/标签里猜画质标签与高度。"""
    for pat in (r"(\d{3,4})p\b", r"[-_/](\d{3,4})\.(?:mp4|m3u8|ts)", r"size=[\"']?(\d{3,4})",
                r"height=[\"']?(\d{3,4})", r"(\d{3,4})x\d{3,4}"):
        m = re.search(pat, text, re.I)
        if m:
            h = int(m.group(1))
            if 120 <= h <= 4320:
                return "%dp" % h, h
    m = re.search(r"\b(2k|4k|8k)\b", text, re.I)
    if m:
        h = {"2k": 1440, "4k": 2160, "8k": 4320}[m.group(1).lower()]
        return m.group(1).upper(), h
    return "", 0


def extract_media(html: str, base_url: str):
    """从 HTML 中提取所有可下载媒体地址（mp4 直链 + m3u8）。"""
    t = _clean_html(html)
    found = []

    # 1) <source src=...> / <video src=...>（很多站点用 <source size=> 下发多档直链 mp4）
    for tag in re.findall(r"<(?:source|video)\b[^>]*>", t, re.I):
        src = re.search(r'\bsrc\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if not src:
            continue
        u = urljoin(base_url, src.group(1).strip())
        if not re.search(r"\.(mp4|m3u8|m4v|webm|ts)(\?|$)", u, re.I):
            continue
        size = re.search(r'\bsize\s*=\s*["\']?(\d{2,4})', tag, re.I)
        if size:
            found.append(Media(u, "mp4", "%sp" % size.group(1), int(size.group(1)), origin="<source size=>"))
        else:
            q, h = guess_quality(u + " " + tag)
            found.append(Media(u, "mp4", q, h, origin="<source>/<video>"))

    # 2) data-* 属性（部分站点把地址放在 data-src / data-url / data-video）
    for m in re.finditer(r'\bdata-(?:src|url|video|file|source|m3u8|play)\s*=\s*["\']([^"\']+)["\']', t, re.I):
        u = urljoin(base_url, m.group(1).strip())
        if re.search(r"\.(mp4|m3u8|m4v)(\?|$)", u, re.I):
            q, h = guess_quality(u)
            found.append(Media(u, "hls" if ".m3u8" in u.lower() else "mp4", q, h, origin="data-*"))

    # 3) 裸 m3u8 绝对/相对链接
    for m in re.finditer(r'["\']([^"\'\s<>\\]+\.m3u8[^"\'\s<>\\]*)["\']', t, re.I):
        u = urljoin(base_url, m.group(1))
        q, h = guess_quality(u)
        found.append(Media(u, "hls", q, h, origin="m3u8 正则"))

    # 4) JS 赋值：source / file / url / video_url / play_url / src
    for m in re.finditer(
            r'(?:source|file|src|url|video_?url|play_?url|videoUrl|playUrl|hls)\s*[:=]\s*["\']'
            r'([^"\']+\.(?:m3u8|mp4|m4v)[^"\']*)["\']', t, re.I):
        u = urljoin(base_url, m.group(1))
        q, h = guess_quality(u)
        found.append(Media(u, "hls" if ".m3u8" in u.lower() else "mp4", q, h, origin="JS 赋值"))

    # 5) 裸 mp4 直链
    for m in re.finditer(r'["\'](https?://[^"\'\s<>\\]+\.mp4[^"\'\s<>\\]*)["\']', t, re.I):
        u = m.group(1)
        q, h = guess_quality(u)
        found.append(Media(u, "mp4", q, h, origin="mp4 正则"))

    # 6) og:video / meta 里的地址
    for m in re.finditer(r'<meta[^>]+(?:property|name)=["\'](?:og:video|twitter:player:stream)["\'][^>]*>', t, re.I):
        c = re.search(r'content=["\']([^"\']+)["\']', m.group(0), re.I)
        if c and re.search(r"\.(mp4|m3u8)", c.group(1), re.I):
            u = urljoin(base_url, c.group(1))
            q, h = guess_quality(u)
            found.append(Media(u, "hls" if ".m3u8" in u.lower() else "mp4", q, h, origin="meta"))

    # 去重 + 过滤广告/占位
    seen, out = set(), []
    for md in found:
        key = md.url.split("#")[0]
        if key in seen:
            continue
        # 只丢弃「广告/预览」目录下的地址，或文件名恰好是 preview/sample 之类占位；
        # 文件名里含 sample- 或 preview- 的正常媒体（如 sample-5s.mp4）不能被误杀。
        if re.search(r"/(?:ads?|banner|previews?|samples?|trailers?)/", md.url, re.I) \
                or re.search(r"/(?:ads?|banner|preview|sample|trailer)\.(?:mp4|m3u8|webm|ts)($|\?)",
                             md.url, re.I):
            continue
        seen.add(key)
        out.append(md)
    return out


def parse_master_playlist(text: str, base_url: str):
    """解析 HLS master playlist，取出各档清晰度。"""
    out = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        res = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        bw = re.search(r"BANDWIDTH=(\d+)", line)
        uri = ""
        for j in range(i + 1, len(lines)):
            if not lines[j].startswith("#"):
                uri = lines[j]
                break
        if not uri:
            continue
        u = urljoin(base_url, uri)
        h = int(res.group(2)) if res else 0
        q = ("%dp" % h) if h else (("%dkbps" % (int(bw.group(1)) // 1000)) if bw else "")
        out.append(Media(u, "hls", q, h, origin="master playlist"))
    return out


def pick_media(medias, want, log):
    if not medias:
        return None
    ranked = sorted(medias, key=lambda m: (m.height, m.size), reverse=True)
    if want:
        w = str(want).strip().lower()
        mnum = re.match(r"^(\d{3,4})p?$", w)
        target = int(mnum.group(1)) if mnum else 0
        if target:
            exact = [m for m in ranked if m.height == target]
            if exact:
                return exact[0]
            lower = [m for m in ranked if 0 < m.height <= target]
            if lower:
                log.warn("没有 %s，退而选择 %s" % (want, lower[0].quality or lower[0].label))
                return lower[0]
            log.warn("没有不高于 %s 的档位，选择最高档 %s" % (want, ranked[0].quality))
            return ranked[0]
        hit = [m for m in ranked if w in (m.quality or "").lower() or w in m.url.lower()]
        if hit:
            return hit[0]
        log.warn("未匹配到画质 %s，使用最高档" % want)
    return ranked[0]


class Unsupported(Exception):
    """站点形态不支持（DRM / App-only / 漫画）。"""


def og(html: str, prop: str) -> str:
    m = re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*content=["\']([^"\']*)["\']' % re.escape(prop),
                  html, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']%s["\']' % re.escape(prop),
                      html, re.I)
    return m.group(1).strip() if m else ""


def page_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    t = m.group(1) if m else (og(html, "og:title") or "")
    t = re.sub(r"&nbsp;?", " ", t)
    t = re.sub(r"&amp;", "&", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.split(TITLE_SUFFIX_RE, t, flags=re.I)[0].strip()
    return t or "未命名视频"


def resolve_page_scan(html, url, fetcher, log):
    medias = extract_media(html, url)
    if not medias:
        log.debug("页面未直接含媒体地址，尝试扫描 /api/ 与 /ajax/ 端点")
        for m in re.finditer(r'["\'](/api/[^"\']+|/ajax/[^"\']+)["\']', html):
            ep = urljoin(url, m.group(1))
            r = fetcher.fetch(ep, referer=url, use_selenium=False)
            if r.ok:
                medias += extract_media(r.text, ep)
    return medias


def resolve_generic(html, url, fetcher, log):
    medias = extract_media(html, url)
    if not medias:
        iframes = re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.I)[:4]
        for src in iframes:
            u = urljoin(url, src)
            if urlparse(u).hostname == urlparse(url).hostname:
                continue
            log.debug("深入 iframe：%s" % urlparse(u).hostname)
            r = fetcher.fetch(u, referer=url, use_selenium=False)
            if r.ok:
                medias += extract_media(r.text, u)
                if medias:
                    break
    return medias


# ---------------------------------------------------------------- 通用解析器（模板）
def resolve_ldjson(html, url, fetcher, log):
    """通用 resolver：从页面 LD+JSON 里取 media 直链（contentUrl / url）。

    适用大多数现代站点：详情页会内嵌 <script type="application/ld+json"> VideoObject。
    """
    from types import SimpleNamespace

    title = page_title(html) or "未命名媒体"
    medias, seen = [], set()
    patterns = (
        r'"contentUrl"\s*:\s*"([^"]+\.(?:mp4|m3u8|webm)[^"]*)"',
        r'"url"\s*:\s*"([^"]+\.(?:mp4|m3u8|webm)[^"]*)"',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, html, re.I):
            raw_url = html_lib.unescape(match.group(1)).strip().replace("\\/", "/")
            if raw_url.startswith("//"):
                raw_url = "https:" + raw_url
            # 相对路径不是媒体地址（通常是站内链接），跳过。
            if not re.match(r"^https?://", raw_url, re.I) or raw_url in seen:
                continue
            seen.add(raw_url)
            quality, height = guess_quality(raw_url)
            medias.append(Media(raw_url, "hls" if ".m3u8" in raw_url.lower() else "mp4",
                                quality or "source", height, origin="LD+JSON contentUrl"))

    if not medias:
        medias = extract_media(html, url)
    if not medias:
        raise LookupError("页面已取到，但未找到可下载的视频地址")
    return medias, str(title), SimpleNamespace(text=html, url=url, ok=True)


def resolve_setvideo(html, url, fetcher, log):
    """通用 resolver：解析播放器 JS 里的媒体赋值。

    覆盖两类常见写法：
      setVideoUrlHigh("https://…/1080p.mp4")、setVideoHLS("https://…/master.m3u8")
      var file = "https://…/video.mp4"; source: "…", hls: "…"
    函数名后缀（UrlHigh/UrlLow/HLS/URL）会作为画质标签的兜底。
    """
    from html import unescape
    from types import SimpleNamespace

    title = page_title(html) or "未命名媒体"
    medias, seen = [], set()

    def add(raw_url, label=""):
        raw_url = unescape(unquote(str(raw_url))).strip().replace("\\/", "/")
        if raw_url.startswith("//"):
            raw_url = "https:" + raw_url
        # 播放器脚本里也会出现站内相对链接，那些不是媒体。
        if not re.match(r"^https?://", raw_url, re.I) or raw_url in seen:
            return
        seen.add(raw_url)
        kind = "hls" if ".m3u8" in raw_url.lower() else "mp4"
        quality, height = guess_quality(raw_url + " " + label)
        if not quality:
            quality = {"urlhigh": "high", "urllow": "low", "hls": "hls"}.get(
                label.lower(), "source")
        medias.append(Media(raw_url, kind, quality, height,
                            origin="播放器 JS" + (("(%s)" % label) if label else "")))

    for match in re.finditer(r"setVideo(\w*)\s*\(\s*([\"'])(.*?)\2\s*\)", html, re.I):
        add(match.group(3), match.group(1))
    for match in re.finditer(
            r"(?:file|source|src|hls|video_?url|play_?url)\s*[:=]\s*([\"'])"
            r"([^\"']+\.(?:mp4|m3u8|webm)[^\"']*)\1", html, re.I):
        add(match.group(2))

    if not medias:
        medias = extract_media(html, url)
    if not medias:
        raise LookupError("页面已取到，但未找到可下载的视频地址")
    return medias, str(title), SimpleNamespace(text=html, url=url, ok=True)


# kind -> resolver。示例池里的 kind 都能命中；自己加站点时按需登记。
RESOLVERS = {
    "generic": resolve_generic,
    "demo": resolve_generic,
    "hls": resolve_generic,
    "html": resolve_page_scan,
    "json": resolve_ldjson,
    "ldjson": resolve_ldjson,
    "setvideo": resolve_setvideo,
}


def resolve(url: str, fetcher, log):
    """解析页面 URL → (媒体列表, 标题, 抓取结果)；站点形态不支持时抛 Unsupported。"""
    host = (urlparse(url).hostname or "").lower()
    kind = kind_of(host)

    if any(hint in url.lower() for hint in GALLERY_HINT):
        raise Unsupported("这是图集/阅读页，没有视频媒体可下载；请用图集适配器处理。")
    if kind == "nav":
        raise Unsupported("%s 是索引/导航站，不含播放页，请把链接换成具体媒体页。" % host)
    if kind == "drm":
        raise Unsupported("%s 带 DRM，本工具不涉及解密；仅可用于信息查看。" % host)

    res = fetcher.fetch(url)
    if not res.ok:
        raise RuntimeError(res.error or ("HTTP %d" % res.status))

    html, final_url = res.text, (res.url or url)
    fn = RESOLVERS.get(kind, resolve_generic)
    # resolver 不是同一形状：resolve_generic/resolve_page_scan 返回媒体列表，
    # resolve_ldjson/resolve_setvideo 返回 (medias, title, response)。两种都接受，
    # 并让 resolver 自带标题优先于页面 <title>。
    resolved = fn(html, final_url, fetcher, log)
    override_title = ""
    if isinstance(resolved, tuple):
        medias = list(resolved[0] or [])
        override_title = str(resolved[1] or "") if len(resolved) > 1 else ""
    else:
        medias = list(resolved or [])

    expanded = []
    for md in medias:
        if md.kind == "hls":
            try:
                r = fetcher.fetch(md.url, referer=final_url, use_selenium=False)
                if r.ok and "#EXT-X-STREAM-INF" in r.text:
                    subs = parse_master_playlist(r.text, md.url)
                    if subs:
                        expanded += subs
                        continue
            except Exception:
                pass
        expanded.append(md)

    if not expanded:
        raise LookupError("页面已取到，但未找到任何 m3u8/mp4 媒体地址")
    return expanded, (override_title or page_title(html)), res


def build_info(html: str, url: str, medias) -> dict:
    info = {
        "title": page_title(html),
        "cover": og(html, "og:image"),
        "description": og(html, "og:description") or og(html, "description"),
        "duration": og(html, "og:video:duration") or og(html, "video:duration"),
        "url": url,
        "qualities": [{"quality": m.quality or m.label or "未知", "kind": m.kind,
                       "size": m.size, "origin": m.origin} for m in medias],
    }
    tags = re.findall(r'<a[^>]+href=["\'][^"\']*/(?:tag|genre|tags|category)[^"\']*["\'][^>]*>(.*?)</a>',
                      html, re.S | re.I)
    info["tags"] = [re.sub(r"<[^>]+>|\s+", " ", t).strip() for t in tags[:12] if t.strip()]
    return info


def discover_series(html: str, url: str, log, limit: int):
    """从页面的“相关/系列”区块里找出同系列视频页。

    区块正则按 `</div>` 就近闭合，遇到嵌套 div 会提前截断，所以先扫区块、
    扫不到再退回整页扫描（`--series-limit` 负责封顶）。
    """
    host = urlparse(url).hostname
    blocks = re.findall(
        r'<(?:div|section)[^>]+class=["\'][^"\']*(?:related|series|season|episode|推薦|相关|系列)[^"\']*["\'][^>]*>'
        r"(.*?)</(?:div|section)>", html, re.S | re.I)

    out, index_of = [], {}
    noise = re.compile(r"thumb_up|万次|^\d+%$|^\s*$")

    def nearby_title(scope: str, start: int, end: int) -> str:
        """卡片标题常在 <a> 之外的兄弟节点里（播放列表卡片常如此）。"""
        window = scope[max(0, start - 400):end + 1500]
        for m in re.finditer(r'<[^>]+class=["\'][^"\']*title[^"\']*["\'][^>]*>([^<]{2,200})',
                             window, re.I):
            txt = re.sub(r"\s+", " ", m.group(1)).strip()
            if txt and not noise.search(txt):
                return txt
        return ""

    def scan(scope: str) -> None:
        for m in re.finditer(r'(<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>)(.*?)</a>', scope, re.S | re.I):
            if len(out) >= limit:
                return
            tag, inner = m.group(1), m.group(3)
            u = urljoin(url, m.group(2))
            p = urlparse(u)
            if p.hostname not in (host, "www." + host):
                continue
            # 只认“视频页”链接，排除同一视频的下载页 / 播放页自身
            if not re.search(r"([?&]v=\d+|/watch/|/videos/|/video/|/w/)", u):
                continue
            if re.search(r"/(?:download|d)\?", u):
                continue
            key = u.split("#")[0]
            # 标题依次尝试：a[title] → img[alt] → 链接文本 → 邻近的 *title* 元素
            tm = re.search(r'\btitle=["\']([^"\']+)["\']', tag, re.I) \
                or re.search(r'<img[^>]+\balt=["\']([^"\']+)["\']', inner, re.I)
            if tm:
                label = tm.group(1).strip()
            else:
                label = re.sub(r"<[^>]+>|\s+", " ", inner).strip()
                label = re.sub(r"^(?:\d{1,2}:\d{2}\s*)+", "", label).strip()
                if noise.search(label):
                    label = nearby_title(scope, m.start(), m.end()) or label
            if key in index_of:
                # 同一链接常先以缩略图卡片出现（文本是时长/播放量），
                # 随后标题元素再出现一次；用干净的那个升级已有条目。
                prev = out[index_of[key]]["title"]
                if noise.search(prev) and label and not noise.search(label):
                    out[index_of[key]]["title"] = label[:120]
                continue
            index_of[key] = len(out)
            out.append({"url": u, "title": (label or key)[:120]})

    if blocks:
        scan("\n".join(blocks))
    if not out:
        log.debug("相关区块未命中，退回整页扫描")
        scan(html)

    log.info("系列发现：%d 个候选（上限 %d）" % (len(out), limit))
    return out


def sanitize_filename(name: str, maxlen: int = 110) -> str:
    name = re.sub(r"<[^>]+>", "", name)
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip(" ._")
    name = re.sub(r"\s+", " ", name)
    if len(name) > maxlen:
        name = name[:maxlen].rstrip()
    return name or "video"


def ffmpeg_bin():
    return shutil.which("ffmpeg")


def _fmt_size(n) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f%s" % (n, unit)
        n /= 1024
    return "%.1fTB" % n


def download_ffmpeg(media, out_path, referer, cookie, proxy, log) -> bool:
    ff = ffmpeg_bin()
    if not ff:
        log.error("未找到 ffmpeg，请先 pkg install ffmpeg -y")
        return False
    hdr = "Referer: %s\r\n" % referer
    if cookie:
        hdr += "Cookie: %s\r\n" % cookie
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-nostats", "-progress", "pipe:1",
           "-headers", hdr, "-user_agent", UA_FULL,
           "-protocol_whitelist", "file,http,https,tcp,tls,crypto"]
    if proxy:
        cmd += ["-http_proxy", proxy]
    cmd += ["-i", media.url, "-c", "copy"]
    if media.kind == "hls":
        cmd += ["-bsf:a", "aac_adtstoasc"]
    cmd += ["-movflags", "+faststart", str(out_path)]

    log.info("ffmpeg 开始下载（%s，%s）" % (media.kind, media.quality or "未知画质"))
    log.debug("媒体地址：" + media.url[:120])
    last_print = 0.0
    duration = 0.0
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    except Exception as exc:
        log.error("ffmpeg 启动失败：%s" % exc)
        return False

    for line in p.stdout:
        line = line.strip()
        if line.startswith("out_time="):
            v = line.split("=", 1)[1]
            if v != "N/A":
                try:
                    hh, mm, ss = v.split(":")
                    duration = int(hh) * 3600 + int(mm) * 60 + float(ss)
                except Exception:
                    pass
        now = time.time()
        if now - last_print >= 5 and duration > 0:
            last_print = now
            log.info("下载中… 已处理 %.0f 秒音视频" % duration)
    p.wait()
    err = (p.stderr.read() or "").strip() if p.stderr else ""
    # ffmpeg occasionally exits non-zero after producing a usable file (for
    # example because of a recoverable segment warning). Trust the artifact.
    if not out_path.exists() or out_path.stat().st_size == 0:
        if p.returncode != 0:
            log.error("ffmpeg 失败（退出码 %d）：%s" % (p.returncode, err[:400]))
        else:
            log.error("ffmpeg 报告成功但输出文件为空")
        return False
    if p.returncode != 0 and err:
        log.warn("ffmpeg 非致命警告：%s" % err[:400])
    got = out_path.stat().st_size
    # ffmpeg 有时会在流被截断后仍以 0 退出并留下残档，光看「文件非空」会漏判。
    if media.size > 0 and got < media.size * 0.98:
        log.error("ffmpeg 输出不完整：%s / 期望 %s，丢弃残档并降级重试"
                  % (_fmt_size(got), _fmt_size(media.size)))
        try:
            out_path.unlink()
        except OSError:
            pass
        return False
    log.info("下载完成：%s（%s）" % (out_path.name, _fmt_size(got)))
    return True


def download_native_mp4(media, out_path, referer, cookie, proxy, log, retries: int = 3,
                        title: str = "") -> bool:
    """直链 mp4 的原生断点续传下载（ffmpeg 缺失/失败时的降级方案）。"""
    try:
        import requests
    except ImportError:
        log.error("requests 未安装，无法使用原生下载")
        return False
    if media.kind != "mp4":
        log.error("原生降级下载只支持直链 mp4；HLS 请安装 ffmpeg（pkg install ffmpeg -y）")
        return False

    headers = {"User-Agent": UA_FULL, "Referer": referer}
    if cookie:
        headers["Cookie"] = cookie
    proxies = {"http": proxy, "https": proxy} if proxy else None

    part_path = out_path.with_name(out_path.name + ".part")
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    if not part_path.exists() and tmp_path.exists():
        part_path = tmp_path
    have = part_path.stat().st_size if part_path.exists() else 0
    for attempt in range(retries + 1):
        h = dict(headers)
        mode = "wb"
        if have:
            h["Range"] = "bytes=%d-" % have
            mode = "ab"
        total = 0
        try:
            with requests.get(media.url, headers=h, stream=True, timeout=30, proxies=proxies) as r:
                if have and r.status_code == 200:
                    log.warn("服务端不支持断点续传，从头下载")
                    have, mode = 0, "wb"
                elif r.status_code not in (200, 206):
                    r.raise_for_status()
                total = int(r.headers.get("content-length", 0)) + have
                with open(part_path, mode) as f:
                    last = time.time()
                    for chunk in r.iter_content(1024 * 256):
                        if not chunk:
                            continue
                        f.write(chunk)
                        have += len(chunk)
                        if time.time() - last >= 5:
                            last = time.time()
                            if total:
                                log.info("下载中… %.1f%%（%s/%s）" % (have * 100.0 / total, _fmt_size(have), _fmt_size(total)))
                            else:
                                log.info("下载中… 已写入 %s" % _fmt_size(have))
            if media.size <= 0 or have >= media.size:
                part_path.replace(out_path)
                log.info("下载完成：%s（%s）" % (out_path.name, _fmt_size(have)))
                return True
            if attempt < retries:
                log.warn("⚠️ 下载中断，第 %d 次重试..." % (attempt + 1))
                time.sleep(4)
        except Exception as exc:
            if attempt < retries:
                log.warn("⚠️ 下载中断，第 %d 次重试..." % (attempt + 1))
                time.sleep(4)
    log.error("\033[31m❌ 任务 [%s] 下载失败，保留部分文件在 %s，跳过此任务\033[0m" %
              (title or out_path.stem, part_path))
    return False


def download_media(media, out_path, referer, cookie, proxy, log, engine: str = "auto",
                   title: str = "") -> bool:
    if media.kind == "mp4" and media.size <= 0:
        size = probe_media_size(media.url, referer, cookie, proxy)
        if size:
            media.size = size
            log.debug("媒体期望大小：%s" % _fmt_size(size))
    if engine in ("auto", "ffmpeg"):
        if download_ffmpeg(media, out_path, referer, cookie, proxy, log):
            return True
        if engine == "ffmpeg":
            return False
        log.warn("ffmpeg 路线失败，降级到原生 HTTP 下载")
    return download_native_mp4(media, out_path, referer, cookie, proxy, log, title=title)


def sync_to_album(video_path: Path, album_dir: str, mode: str, log):
    """复制/移动已完成的视频到媒体库目录；首个目录不可写时依次尝试后备目录。

    后备目录由 MEDIAGRAB_FALLBACK_DIR 指定（多个用 os.pathsep 分隔）。
    Termux 下共享存储挂载失效时，一般把它指向 ~/storage/shared 下的同级目录。
    """
    requested = Path(album_dir).expanduser()
    candidates = [requested]
    for raw in (os.environ.get("MEDIAGRAB_FALLBACK_DIR") or "").split(os.pathsep):
        raw = raw.strip()
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        if candidate != requested and candidate not in candidates:
            candidates.append(candidate)

    last_error = None
    for target_dir in candidates:
        try:
            # 保留显式 os.makedirs 调用，兼容 Termux 的共享存储挂载路径。
            os.makedirs(target_dir, exist_ok=True)
            if not os.access(target_dir, os.W_OK):
                raise PermissionError("目录不可写")
            target = target_dir / video_path.name
            if target.resolve() == video_path.resolve():
                log.info("✅ 已同步到相册目录: %s" % target)
                return target
            if mode == "move":
                shutil.move(str(video_path), str(target))
            else:
                shutil.copy2(str(video_path), str(target))
            log.info("✅ 已同步到相册目录: %s" % target)
            return target
        except Exception as exc:
            last_error = exc
            if target_dir != candidates[-1]:
                log.warn("目录不可写：%s；尝试后备目录 %s" % (target_dir, candidates[-1]))
            continue
    log.warn("相册同步失败（不会影响下载文件）：%s" % last_error)
    return None


def build_candidate_urls(url: str, domain_pool, log):
    host = (urlparse(url).hostname or "").lower()
    cands = [url]
    for m in mirror_domains(host, domain_pool):
        cands.append(swap_host(url, m))
    log.debug("候选 URL %d 条：%s" % (len(cands), [urlparse(c).hostname for c in cands]))
    return cands


# ---------------------------------------------------------------- 本地媒体库
LIBRARY_ROOTS = (Path(DEFAULT_MEDIA_DIR), Path(DEFAULT_GALLERY_DIR), Path(DEFAULT_OUTDIR))
VIDEO_SUFFIXES = (".mp4", ".webm", ".mkv", ".mov", ".avi")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")

# 搜索接口会按「已下载」给结果置灰，扫描目录有成本，这里做 60 秒缓存。
_SCAN_CACHE = {"at": 0.0, "data": set()}


def normalize(value):
    """Normalize video names for exact, format-insensitive comparisons."""
    value = str(value or "").strip().lower()
    value = re.sub(r"\.(?:mp4|webm|mkv)$", "", value, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value)


def _raise_walk_error(exc):
    raise exc


def scan_downloaded_videos():
    now = time.time()
    if now - _SCAN_CACHE["at"] < 60 and _SCAN_CACHE["data"]:
        return _SCAN_CACHE["data"]
    downloaded = set()
    for root in (DEFAULT_MEDIA_DIR, DEFAULT_OUTDIR):
        try:
            for directory, _, files in os.walk(root, onerror=_raise_walk_error):
                downloaded.update(name for name in files
                                  if name.lower().endswith((".mp4", ".webm", ".mkv")))
        except Exception as exc:
            print("警告：扫描视频目录 %s 失败：%s" % (root, exc), file=sys.stderr)
    _SCAN_CACHE["at"], _SCAN_CACHE["data"] = now, downloaded
    return downloaded


def _library_files(root):
    try:
        return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() not in (".part", ".tmp", ".json")]
    except (OSError, PermissionError):
        return []


def scan_media_library():
    videos = []
    for root in (Path(DEFAULT_MEDIA_DIR),):
        for path in _library_files(root):
            if path.suffix.lower() not in VIDEO_SUFFIXES:
                continue
            try: stat = path.stat()
            except OSError: continue
            videos.append({"name": path.name, "path": str(path), "size": stat.st_size,
                           "mtime": stat.st_mtime, "url": "/api/library/file?path=" + quote(str(path))})
    galleries = []
    cg_root = Path(DEFAULT_GALLERY_DIR)
    try: children = [p for p in cg_root.iterdir() if p.is_dir()]
    except OSError: children = []
    for directory in children:
        files = [p for p in _library_files(directory) if p.suffix.lower() in IMAGE_SUFFIXES]
        if not files: continue
        try:
            stat = directory.stat(); total = sum(p.stat().st_size for p in files)
        except OSError: continue
        files.sort(key=lambda p: p.name.lower())
        galleries.append({"name": directory.name, "path": str(directory), "file_count": len(files),
                          "size": total, "mtime": stat.st_mtime,
                          "cover": "/api/library/file?path=" + quote(str(files[0])),
                          "files": ["/api/library/file?path=" + quote(str(p)) for p in files]})
    videos.sort(key=lambda item: item["mtime"], reverse=True)
    galleries.sort(key=lambda item: item["mtime"], reverse=True)
    return {"videos": videos, "cg_galleries": galleries}


def _library_path(raw_path):
    candidate = Path(unquote_plus(raw_path)).resolve()
    for root in LIBRARY_ROOTS:
        try:
            candidate.relative_to(root.resolve())
            if candidate.is_file() and candidate.suffix.lower() not in (".part", ".tmp", ".json"):
                return candidate
        except (ValueError, OSError):
            continue
    return None


def _load_favorites():
    try:
        data = json.loads(FAVORITES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as exc:
        print("警告：读取收藏夹失败：%s" % exc, file=sys.stderr)
        return []


def _save_favorites(items):
    STATE_HOME.mkdir(parents=True, exist_ok=True)
    temporary = FAVORITES_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(FAVORITES_FILE)


def _load_history():
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"searches": [], "downloads": []}
    except Exception:
        return {"searches": [], "downloads": []}


def _save_history(data):
    STATE_HOME.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _record_search(query, search_type):
    data = _load_history(); data.setdefault("searches", []).insert(0, {"query": query, "type": search_type, "time": int(time.time())})
    data["searches"] = data["searches"][:100]; _save_history(data)


def _record_download(result):
    data = _load_history(); data.setdefault("downloads", []).insert(0, {"title": result.get("title", ""), "time": int(time.time()), "path": result.get("output") or ""})
    data["downloads"] = data["downloads"][:100]; _save_history(data)


def search_bing_images(query, page=1):
    import requests
    from bs4 import BeautifulSoup
    first = max(0, (int(page) - 1) * 35)
    response = requests.get("https://www.bing.com/images/search", params={"q": query, "first": first},
                            headers={"User-Agent": UA_FULL, "Referer": "https://www.bing.com/"}, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    items = []
    for card in soup.select("a.iusc"):
        raw = card.get("m", "")
        try:
            meta = json.loads(raw)
        except (TypeError, ValueError):
            continue
        full_url, thumb = meta.get("murl"), meta.get("turl")
        if not full_url or not thumb:
            continue
        items.append({"title": meta.get("t") or card.get("title") or query,
                      "thumbnail": thumb, "full_url": full_url, "source": "bing",
                      "width": meta.get("w"), "height": meta.get("h")})
    return items


def fetch_baidu_image(q, page=1):
    import requests
    response = requests.get("https://image.baidu.com/search/acjson",
                            params={"tn": "resultjson_com", "word": q,
                                    "pn": (int(page) - 1) * 30, "rn": 30,
                                    "ie": "utf-8", "oe": "utf-8"},
                            headers={"User-Agent": UA_FULL,
                                     "Referer": "https://image.baidu.com/"}, timeout=25)
    response.raise_for_status()
    items = []
    for entry in response.json().get("data", []):
        if not isinstance(entry, dict):
            continue
        thumbnail = entry.get("middleURL")
        hover = entry.get("hoverURL")
        full_url = entry.get("objURL") or hover
        if not thumbnail or not full_url:
            continue
        items.append({"title": html_lib.unescape(entry.get("fromPageTitleEnc") or q),
                      "thumbnail": thumbnail, "full_url": full_url, "source": "baidu"})
    return items


def fetch_so_image(q, page=1):
    import requests
    response = requests.get("https://xiaoqi.icofun.cn/API/so_image.php",
                            params={"msg": q, "type": "json"},
                            headers={"User-Agent": UA_FULL}, timeout=25)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        return []
    return [{"title": entry.get("title") or q,
             "thumbnail": entry["imgurl"], "full_url": entry["imgurl"],
             "width": entry.get("width"), "height": entry.get("height"),
             "source": "360"}
            for entry in data if isinstance(entry, dict) and entry.get("imgurl")]


# ---------------------------------------------------------------- 小说抓取（配置化模板）
# 每个小说站就是一份配置：编码 + 选择器。按同样结构补自己的站点即可。
NOVEL_SITES = {
    "demo": {
        "url_pattern": r"/book/(\d+)",                      # 从详情页 URL 提取书籍 id
        "detail_url": "https://novel.example.com/book/{id}.htm",
        "toc_url": "https://novel.example.com/book/{id}/",
        "link_base": "https://novel.example.com/",          # 目录里的相对链接以此为基准
        "encoding": "utf-8",                                # 常见还有 gbk
        "title_selector": "div.booknav2 h1 a",
        "toc_item_selector": "div#catalog li",
        "toc_num_attr": "data-num",                         # 用哪个属性排序（没有则按出现顺序）
        "toc_link_selector": "a",
        "content_selector": "div.txtnav",                   # 正文容器
        "strip_selectors": ("h1", "div.txtinfo", "script", "style"),
        "delay": 1.0,                                       # 章节间隔秒数，别把站点打疼
        "fallback_title": "book_{id}",
    },
}

NOVEL_DEFAULT_SITE = "demo"


def novel_site_config(name=None):
    site = NOVEL_SITES.get(name or NOVEL_DEFAULT_SITE)
    if site is None:
        raise ValueError("未知小说站点配置：%s（可用：%s）"
                         % (name, ", ".join(NOVEL_SITES) or "（无）"))
    return site


def _novel_fetcher(encoding):
    """返回一个 get_soup(url)：带重试，按配置的编码解码。"""
    from bs4 import BeautifulSoup

    def get_soup(url, referer=None):
        error = None
        for attempt in range(3):
            try:
                from curl_cffi import requests as curl_requests
                response = curl_requests.get(url, impersonate="chrome120", timeout=30,
                                             headers={"Referer": referer} if referer else None)
                response.raise_for_status()
                text = response.content.decode(encoding, "replace")
            except ImportError:
                import requests
                response = requests.get(url, timeout=30,
                                        headers={"Referer": referer} if referer else None)
                response.raise_for_status()
                text = response.content.decode(encoding, "replace")
            except Exception as exc:
                error = exc
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                continue
            return BeautifulSoup(text, "html.parser")
        raise error

    return get_soup


def scrape_novel(book_url, site=None, progress_cb=None, max_chapters=None):
    """按配置抓取一本书：详情页取书名 → 目录页取章节列表 → 逐章抓正文。

    返回 {"title": str, "chapters": [{"title", "text"}], "failed": int}
    """
    config = novel_site_config(site)
    match = re.search(config["url_pattern"], str(book_url or ""))
    if not match:
        raise ValueError("无法从链接解析书籍 id，请确认链接形态与 url_pattern 匹配")
    book_id = match.group(1)

    get_soup = _novel_fetcher(config.get("encoding", "utf-8"))
    detail_url = config["detail_url"].format(id=book_id)
    detail = get_soup(detail_url)
    title_node = detail.select_one(config["title_selector"]) if config.get("title_selector") else None
    title = (title_node.get_text(strip=True) if title_node else "") \
        or str(config.get("fallback_title", "book_{id}")).format(id=book_id)

    toc_url = config["toc_url"].format(id=book_id)
    toc = get_soup(toc_url, referer=detail_url)
    entries, seen = [], set()
    for index, node in enumerate(toc.select(config["toc_item_selector"])):
        link = node.select_one(config["toc_link_selector"])
        if not link or not link.get("href"):
            continue
        raw_num = node.get(config.get("toc_num_attr") or "", "")
        order = int(raw_num) if str(raw_num).isdigit() else index
        url = urljoin(config.get("link_base") or toc_url, link["href"])
        if url in seen:
            continue
        seen.add(url)
        entries.append((order, link.get_text(strip=True), url))
    entries.sort(key=lambda item: item[0])
    if max_chapters:
        entries = entries[:int(max_chapters)]
    if not entries:
        raise ValueError("目录为空：页面结构可能已变，或选择器需要调整")

    total = len(entries)
    if progress_cb:
        progress_cb(0, total, title)

    chapters, failed, streak = [], 0, 0
    for index, (_, chapter_title, url) in enumerate(entries, 1):
        try:
            body = get_soup(url, referer=toc_url).select_one(config["content_selector"])
            if body is None:
                raise ValueError("缺少正文容器 %s" % config["content_selector"])
            for selector in config.get("strip_selectors", ()):
                for node in body.select(selector):
                    try:
                        node.decompose()
                    except Exception:
                        pass
            text = body.get_text("\n", strip=True)
            if not text:
                raise ValueError("正文为空")
            streak = 0
        except Exception as exc:
            failed += 1
            streak += 1
            text = "【本章抓取失败：%s】" % exc
            if streak >= 5:
                raise RuntimeError("连续 5 章抓取失败，可能被站点拦截（%s）" % exc)
        chapters.append({"title": chapter_title, "text": text})
        if progress_cb:
            progress_cb(index, total, chapter_title)
        if index < total:
            time.sleep(float(config.get("delay") or 1.0))

    return {"title": title, "chapters": chapters, "failed": failed}


def _novel_safe_name(title):
    name = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff_.-]+", "_", title or "").strip("._")
    return name[:80] or "novel"


def build_novel_html(title, chapters):
    """把章节拼成单文件深色主题阅读页（带目录锚点，离线可读）。"""
    from html import escape

    css = (':root{color-scheme:dark}html{scroll-behavior:smooth}'
           'body{margin:0;background:#1a1a1a;color:#d4d4d4;line-height:1.8;font-size:18px;'
           'font-family:-apple-system,"PingFang SC","Noto Sans CJK SC",sans-serif}'
           '.wrap{max-width:720px;margin:0 auto;padding:24px 16px 96px}'
           'h1{font-size:1.6em;margin:.4em 0 1em;text-align:center}'
           'h2{font-size:1.25em;margin:2.4em 0 1em;padding-top:.6em;border-top:1px solid #333}'
           'a{color:#8ab4f8;text-decoration:none}'
           'nav ol{padding-left:1.8em;margin:0 0 2em}nav li{margin:.25em 0}'
           '.chapter p{margin:0 0 .8em;text-indent:2em}'
           '.top{position:fixed;right:16px;bottom:16px;padding:10px 14px;background:#2d2d2d;'
           'border:1px solid #444;border-radius:999px;color:#d4d4d4;font-size:14px;opacity:.92}'
           '@media(max-width:480px){body{font-size:17px}}')
    toc, sections = [], []
    for number, chapter in enumerate(chapters, 1):
        name = escape(chapter["title"])
        toc.append('<li><a href="#ch%d">%s</a></li>' % (number, name))
        paragraphs = "".join("<p>%s</p>" % escape(line)
                             for line in chapter["text"].splitlines() if line.strip())
        sections.append('<div id="ch%d"><h2>%s</h2><div class="chapter">%s</div></div>'
                        % (number, name, paragraphs))
    safe_title = escape(title)
    return ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + safe_title + '</title><style>' + css + '</style></head><body>'
            '<div class="wrap"><h1 id="top">' + safe_title + '</h1><nav><ol>' + "".join(toc)
            + '</ol></nav>' + "".join(sections) + '</div>'
            '<a class="top" href="#top">↑ 顶部</a></body></html>')


def _web_novel_scrape(job_id, url, site, max_chapters):
    def set_job(**fields):
        with WEB_JOBS_LOCK:
            WEB_JOBS[job_id] = fields

    def progress(index, total, title):
        set_job(status="running", message="[%d/%d] %s" % (index, total, title))

    set_job(status="running", message="正在读取目录…")
    try:
        book = scrape_novel(url, site=site, progress_cb=progress, max_chapters=max_chapters)
        os.makedirs(NOVEL_DIR, exist_ok=True)
        name = _novel_safe_name(book["title"]) + ".html"
        target = os.path.join(NOVEL_DIR, name)
        with open(target + ".part", "w", encoding="utf-8") as handle:
            handle.write(build_novel_html(book["title"], book["chapters"]))
        os.replace(target + ".part", target)
        note = "（%d 章抓取失败）" % book["failed"] if book["failed"] else ""
        set_job(status="done", message="✅ 已保存 %d 章%s：%s"
                 % (len(book["chapters"]), note, name))
    except Exception as exc:
        set_job(status="error", error="小说抓取失败：%s" % exc)


# ---------------------------------------------------------------- 本地 Web UI
WEB_JOBS = {}
WEB_JOBS_LOCK = threading.Lock()
FAVORITES_LOCK = threading.Lock()
FUSE_MESSAGE = "⚠️ 存储挂载点失效，请手动搬运文件或重试"

# 图片防盗链对照表：(域名后缀, Referer)。留空表示不发送 Referer。
# 加自己的图床时照抄一行即可。
IMAGE_REFERERS = (
    # ("cdn.example.com", "https://example.com/"),
)


def proxy_image(image_url, stream=False):
    """带 Referer 白名单的图片代理：绕开图床的防盗链限制。"""
    import requests

    host = (urlparse(image_url).hostname or "").lower()
    referer = next((value for suffix, value in IMAGE_REFERERS
                    if host == suffix or host.endswith("." + suffix)), None)
    headers = {"User-Agent": UA_FULL,
               "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"}
    if referer:
        headers["Referer"] = referer

    last_exc = None
    for attempt in range(3):
        try:
            return requests.get(image_url, headers=headers, timeout=25, stream=stream)
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt >= 2:
                raise
            time.sleep(1.0 * (attempt + 1))
    if last_exc:
        raise last_exc


def _web_image_download(job_id, url, filename):
    with WEB_JOBS_LOCK:
        WEB_JOBS[job_id] = {"status": "running", "message": "正在下载原图…"}
    try:
        import requests
        target_dir = Path(DEFAULT_IMAGE_DIR)
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(str(filename)).name.strip()
        if not safe_name or safe_name in (".", ".."):
            raise ValueError("文件名无效")
        target = target_dir / safe_name
        if target.exists():
            result = {"status": "done", "message": "已存在同名文件，跳过", "path": str(target)}
        else:
            response = requests.get(url, headers={"User-Agent": UA_FULL}, timeout=40, stream=True)
            response.raise_for_status()
            with target.open("wb") as stream:
                for chunk in response.iter_content(1024 * 128):
                    if chunk:
                        stream.write(chunk)
            result = {"status": "done", "message": "✅ 下载完成", "path": str(target)}
    except Exception as exc:
        result = {"status": "error", "error": str(exc)}
    with WEB_JOBS_LOCK:
        WEB_JOBS[job_id] = result


WEB_HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>mediagrab</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#0f0f0f;color:#f5f5f5;font:15px system-ui,sans-serif}
header{position:sticky;top:0;z-index:2;background:#171717;border-bottom:1px solid #303030;padding:16px}
.bar{max-width:1180px;margin:auto;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:21px;margin:0;color:#4da3ff}
input[type=search]{flex:1;min-width:220px;background:#272727;color:#fff;border:1px solid #555;padding:11px 14px;border-radius:4px}
select,button{background:#333;color:#fff;border:1px solid #555;padding:10px;border-radius:4px}
button.primary{background:#2563eb;border:0;font-weight:700;cursor:pointer}
.option{white-space:nowrap}
.grid{max-width:1180px;margin:24px auto;padding:0 16px;display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:18px}
.card{background:#202020;border-radius:6px;overflow:hidden}
.thumb{aspect-ratio:16/9;background:#303030;position:relative}
.thumb img{width:100%;height:100%;object-fit:cover}
.duration{position:absolute;right:7px;bottom:7px;background:#000d;padding:3px 6px;border-radius:3px;font-size:12px}
.body{padding:12px}
.title{height:42px;line-height:21px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.title a{color:inherit;text-decoration:none}.title a:hover{color:#4da3ff}
.meta{color:#aaa;font-size:13px;margin:8px 0}
.actions{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.status{font-size:13px;margin-top:9px;color:#bbb;min-height:18px}
.error{color:#ff6b6b;font-weight:700}
.empty{grid-column:1/-1;color:#aaa;text-align:center;padding:50px}
.drawer{position:fixed;z-index:4;left:0;top:0;bottom:0;width:min(380px,90vw);padding:18px;background:#202020;box-shadow:4px 0 20px #000;overflow:auto;transform:translateX(-105%);transition:transform .2s}
.drawer.open{transform:none}
.drawer-head{display:flex;justify-content:space-between;align-items:center}
.history-path{color:#aaa;overflow-wrap:anywhere}
</style></head><body><header><div class="bar"><h1>mediagrab</h1>
<input id="q" type="search" placeholder="输入关键词…" autocomplete="off">
<label class="option">类型 <select id="searchMode">
<option value="demo-html">视频 - 示例(HTML)</option>
<option value="demo-json">视频 - 示例(JSON API)</option>
<option value="bing_image">图片 - Bing</option>
<option value="baidu_image">图片 - 百度</option>
<option value="so_image">图片 - 360</option>
<option value="novel">📖 离线小说</option>
</select></label>
<label class="option">排序 <select id="sort"><option value="">默认</option><option value="latest">最新</option><option value="views">播放量</option></select></label>
<label class="option">画质 <select id="quality"><option>480p</option><option selected>720p</option><option>1080p</option></select></label>
<label class="option"><input id="sync" type="checkbox" checked> 下载后同步到媒体库</label>
<label class="option"><input id="move" type="checkbox" checked> 同步后删除原文件</label>
<button onclick="showFavorites()">我的收藏</button>
<button onclick="showLibrary()">📚 媒体库</button>
<button onclick="showDomainPool()">🌐 域名池</button>
<button onclick="showHistory()">📜 历史</button>
</div></header>
<aside id="history" class="drawer"><div class="drawer-head"><h2 id="drawerTitle">历史记录</h2><button onclick="document.querySelector('#history').classList.remove('open')">✕</button></div><div id="historyBody"></div></aside>
<main id="grid" class="grid"><div class="empty">输入关键词开始搜索</div></main>
<nav id="pager" hidden style="max-width:1180px;margin:0 auto 24px;padding:0 16px;text-align:center">
<button id="prevPage">上一页</button><span id="pageInfo" style="margin:0 16px"></span><button id="nextPage">下一页</button></nav>
<script>
const $ = (sel) => document.querySelector(sel);
const q = $('#q'), grid = $('#grid'), searchMode = $('#searchMode'), sort = $('#sort');
const quality = $('#quality'), sync = $('#sync'), move = $('#move');
const pager = $('#pager'), prevPage = $('#prevPage'), nextPage = $('#nextPage'), pageInfo = $('#pageInfo');
const historyDrawer = $('#history'), historyBody = $('#historyBody'), drawerTitle = $('#drawerTitle');

const SOURCE_MODES = ['demo-html', 'demo-json'];
const IMAGE_MODES = ['bing_image', 'baidu_image', 'so_image'];
const IMAGE_SOURCE = { bing_image: 'bing', baidu_image: 'baidu', so_image: '360' };
const authToken = new URLSearchParams(location.search).get('token') || sessionStorage.getItem('authToken') || '';
if (authToken) sessionStorage.setItem('authToken', authToken);

let timer, results = [], favoriteUrls = new Set(), currentPage = 1, totalPages = 1, searchRequestId = 0;

const apiFetch = (url, options = {}) =>
  fetch(url, { ...options, headers: { ...(options.headers || {}), 'X-Auth-Token': authToken } });
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtSize = (n) => { n = Number(n) || 0;
  return n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(1) + ' MB'; };

q.oninput = () => { clearTimeout(timer); timer = setTimeout(search, 350); };
searchMode.onchange = () => { searchRequestId++; currentPage = 1; grid.innerHTML = ''; search(); };
sort.onchange = () => { searchRequestId++; currentPage = 1; search(); };
prevPage.onclick = () => { if (currentPage > 1) { currentPage--; search(); } };
nextPage.onclick = () => { currentPage++; search(); };

async function loadFavoriteUrls() {
  const d = await (await apiFetch('/api/favorites')).json();
  favoriteUrls = new Set((d.favorites || []).map((x) => x.url));
}

function renderCards(items, mode) {
  results = items;
  const isImage = IMAGE_MODES.includes(mode);
  grid.innerHTML = items.length ? items.map((x, i) => {
    const image = x.thumbnail || x.image || '';
    const proxy = image
      ? `/api/proxy_image?url=${encodeURIComponent(image)}&token=${encodeURIComponent(authToken)}` : '';
    const meta = isImage
      ? `${x.width && x.height ? `${x.width}×${x.height}` : '图片'}`
      : [x.author && `作者：${esc(x.author)}`, esc(x.duration || '')].filter(Boolean).join(' · ');
    return `<article class="card">
      <div class="thumb">${proxy ? `<img src="${proxy}" loading="lazy" alt="">` : ''}
        <span class="duration">${esc(x.duration || '')}</span></div>
      <div class="body">
        <div class="title"><a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.title)}</a></div>
        <div class="meta">${meta}</div>
        <div class="actions"><span>${isImage ? '图片' : '视频'}</span><span>
          <button onclick="toggleFavorite(${i},this)">${favoriteUrls.has(x.url) ? '❤️' : '♡'}</button>
          <button class="primary" onclick="downloadItem(${i},this)">${isImage ? '下载原图' : '下载'}</button>
        </span></div>
        <div class="status"></div>
      </div></article>`;
  }).join('') : '<div class="empty">没有结果</div>';
}

async function search() {
  const myId = ++searchRequestId;
  const keyword = q.value.trim();
  grid.innerHTML = '';
  if (!keyword) { grid.innerHTML = '<div class="empty">输入关键词开始搜索</div>'; return; }

  if (searchMode.value === 'novel') { await novelStart(keyword); return; }

  grid.innerHTML = '<div class="empty">搜索中…</div>';
  try {
    const mode = searchMode.value;
    const endpoint = IMAGE_MODES.includes(mode)
      ? `/api/search_image?source=${IMAGE_SOURCE[mode]}&q=${encodeURIComponent(keyword)}&page=${currentPage}`
      : `/api/search?type=${encodeURIComponent(mode)}&q=${encodeURIComponent(keyword)}&page=${currentPage}`
        + (sort.value ? `&sort=${encodeURIComponent(sort.value)}` : '');
    const d = await (await apiFetch(endpoint)).json();
    if (myId !== searchRequestId) return;
    if (!d.results) throw new Error(d.error || '搜索失败');
    await loadFavoriteUrls();
    if (myId !== searchRequestId) return;

    renderCards(d.results, mode);
    currentPage = d.current_page || currentPage;
    if (IMAGE_MODES.includes(mode)) { pager.hidden = true; return; }
    pager.hidden = false;
    const empty = d.results.length === 0;
    totalPages = d.total_pages || null;
    pageInfo.textContent = totalPages ? `第 ${currentPage} / ${totalPages} 页` : `第 ${currentPage} 页`;
    prevPage.disabled = currentPage <= 1;
    nextPage.disabled = totalPages ? currentPage >= totalPages : empty;
  } catch (e) {
    if (myId === searchRequestId) grid.innerHTML = `<div class="empty error">${esc(e.message)}</div>`;
  }
}

async function downloadItem(index, button) {
  button.disabled = true;
  const status = button.closest('.card').querySelector('.status');
  const item = results[index];
  const isImage = IMAGE_MODES.includes(searchMode.value);
  status.className = 'status';
  status.textContent = '正在创建任务…';
  try {
    let payload, endpoint;
    if (isImage) {
      let name = (item.title || 'image').replace(/[\\/:*?"<>|]/g, '_').slice(0, 80);
      if (!/\.[a-z0-9]{2,5}$/i.test(name)) name += '.jpg';
      payload = { url: item.full_url || item.image, filename: name };
      endpoint = '/api/download_image';
    } else {
      payload = { url: item.url, quality: quality.value, sync: sync.checked, move: move.checked };
      endpoint = '/api/download';
    }
    const r = await apiFetch(endpoint, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || '创建任务失败');
    poll(d.id, status, button);
  } catch (e) {
    status.className = 'status error';
    status.textContent = e.message;
    button.disabled = false;
  }
}

async function poll(id, status, button) {
  try {
    const d = await (await apiFetch('/api/status?id=' + encodeURIComponent(id))).json();
    status.className = d.status === 'error' ? 'status error' : 'status';
    status.textContent = d.error || d.message || d.status;
    if (d.status === 'done') return;
    if (d.status === 'error') { if (button) button.disabled = false; return; }
  } catch (e) { /* 网络抖动继续轮询 */ }
  setTimeout(() => poll(id, status, button), 1200);
}

async function toggleFavorite(index, button) {
  const item = results[index];
  const saved = favoriteUrls.has(item.url);
  const r = await apiFetch('/api/favorites', {
    method: saved ? 'DELETE' : 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(item) });
  if (r.ok) { favoriteUrls[saved ? 'delete' : 'add'](item.url); button.textContent = saved ? '♡' : '❤️'; }
}

async function showFavorites() {
  const d = await (await apiFetch('/api/favorites')).json();
  favoriteUrls = new Set((d.favorites || []).map((x) => x.url));
  renderCards(d.favorites || [], SOURCE_MODES.includes(searchMode.value) ? searchMode.value : 'demo-html');
}

async function showLibrary() {
  const d = await (await apiFetch('/api/library')).json();
  const videos = d.videos || [], galleries = d.galleries || [];
  const videoCards = videos.map((x) => `<article class="card"><div class="body">
    <div class="title">${esc(x.name)}</div>
    <div class="meta">${fmtSize(x.size)} · ${new Date(x.mtime * 1000).toLocaleString()}</div>
    <div class="actions"><span>视频</span><button onclick="window.open('${x.url}','_blank')">打开</button></div>
    </div></article>`).join('');
  const galleryCards = galleries.map((x, i) => `<article class="card" onclick="showGallery(${i})">
    <div class="thumb"><img src="${x.cover}" loading="lazy" alt=""></div>
    <div class="body"><div class="title">${esc(x.name)}</div>
    <div class="meta">${x.file_count} 个文件 · ${fmtSize(x.size)}</div></div></article>`).join('');
  grid.innerHTML = (videos.length ? '<h2 style="grid-column:1/-1">已下载视频</h2>' + videoCards : '')
    + (galleries.length ? '<h2 style="grid-column:1/-1">已下载图集</h2>' + galleryCards : '')
    || '<div class="empty">还没有下载任何内容</div>';
  window.__galleries = galleries;
  pager.hidden = true;
}

function showGallery(index) {
  const gallery = (window.__galleries || [])[index];
  if (!gallery) return;
  grid.innerHTML = `<h2 style="grid-column:1/-1">${esc(gallery.name)}</h2>`
    + (gallery.files || []).map((u) => `<div class="card"><div class="thumb"><img src="${u}" loading="lazy" alt=""></div></div>`).join('');
}

async function showHistory() {
  const d = await (await apiFetch('/api/history')).json();
  drawerTitle.textContent = '📜 历史记录';
  historyBody.innerHTML = '<h3>搜索历史</h3>'
    + ((d.searches || []).map((x, i) => `<p><button onclick="repeatSearch(${i})">${esc(x.query)}（${esc(x.type || '')}）</button></p>`).join('') || '<p>暂无</p>')
    + '<h3>下载历史</h3>'
    + ((d.downloads || []).map((x) => `<p>${esc(x.title)}<br>${new Date(x.time * 1000).toLocaleString()}<br><span class="history-path">${esc(x.path || '')}</span></p>`).join('') || '<p>暂无</p>');
  window.__history = d.searches || [];
  historyDrawer.classList.add('open');
}

function repeatSearch(index) {
  const item = (window.__history || [])[index];
  if (!item) return;
  q.value = item.query;
  historyDrawer.classList.remove('open');
  search();
}

async function showDomainPool() {
  const d = await (await apiFetch('/api/domains')).json();
  drawerTitle.textContent = '🌐 候选域名池';
  historyBody.innerHTML = (d.domains || []).map((x) =>
    `<p>${x.preferred ? '★' : '·'} <b>${esc(x.domain)}</b>（${esc(x.kind_label)}）<br>
     <span class="history-path">成功 ${x.ok} / 失败 ${x.fail}｜${esc(x.note || '')}</span>
     ${x.last_error ? `<br><span class="history-path">最近错误：${esc(x.last_error)}</span>` : ''}</p>`).join('');
  historyDrawer.classList.add('open');
}

async function novelStart(url) {
  if (!/^https?:\/\//.test(url)) { grid.innerHTML = '<div class="empty">请粘贴小说详情页 URL</div>'; return; }
  grid.innerHTML = '<div class="empty">正在抓取…</div>';
  try {
    const r = await apiFetch('/api/scrape_novel', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, max_chapters: 50 }) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || '抓取失败');
    grid.innerHTML = '<article class="card"><div class="body"><div class="title">正在抓取小说</div>'
      + '<div class="status" id="novelStatus">启动中…</div></div></article>';
    novelPoll(d.id, document.querySelector('#novelStatus'));
  } catch (e) {
    grid.innerHTML = `<div class="empty error">${esc(e.message)}</div>`;
  }
}

async function novelPoll(id, status) {
  try {
    const d = await (await apiFetch('/api/status?id=' + encodeURIComponent(id))).json();
    status.textContent = d.error || d.message || d.status;
    if (d.status === 'error') { status.className = 'status error'; return; }
    if (d.status === 'done') {
      const message = d.message || '';
      const cut = message.lastIndexOf('：');
      if (cut >= 0) {
        const link = document.createElement('a');
        link.textContent = ' 📖 打开阅读';
        link.target = '_blank';
        link.href = '/api/novel_file?name=' + encodeURIComponent(message.slice(cut + 1))
          + '&token=' + encodeURIComponent(authToken);
        status.appendChild(link);
      }
      return;
    }
  } catch (e) { /* 继续轮询 */ }
  setTimeout(() => novelPoll(id, status), 1500);
}

loadFavoriteUrls().catch(() => {});
</script></body></html>'''


def _web_download(job_id, payload):
    """把一次下载交给子进程跑，父进程只负责搬运 @@RESULT@@ 结构化结果。"""
    with WEB_JOBS_LOCK:
        WEB_JOBS[job_id] = {"status": "running", "message": "正在解析并下载…"}
    command = [sys.executable, str(Path(__file__).resolve()), payload["url"],
               "--quality", payload["quality"], "--json"]
    if payload.get("sync"):
        command += ["--to-album", DEFAULT_MEDIA_DIR, "--album-mode",
                    "move" if payload.get("move") else "copy"]
    try:
        proc = subprocess.run(command, text=True, capture_output=True)
        marker = next((line[len(RESULT_MARK):].strip() for line in proc.stdout.splitlines()
                       if line.startswith(RESULT_MARK)), "")
        result = json.loads(marker) if marker else {}
        items = result.get("items") or []
        album_failed = payload.get("sync") and any(
            item.get("ok") and not item.get("album_output") for item in items)
        if album_failed:
            job = {"status": "error", "error": FUSE_MESSAGE}
        elif proc.returncode or not result.get("ok"):
            job = {"status": "error",
                   "error": result.get("error") or "下载失败，请查看终端日志"}
        else:
            size = sum(item.get("size", 0) for item in items)
            job = {"status": "done", "message": "下载完成 · %s" % _fmt_size(size), "result": result}
            for item in items:
                if item.get("ok"):
                    _record_download(item)
    except Exception as exc:
        message = FUSE_MESSAGE if "Owner died" in str(exc) else "任务失败：%s" % exc
        job = {"status": "error", "error": message}
    with WEB_JOBS_LOCK:
        WEB_JOBS[job_id] = job


def serve_web(port=8080, no_auth=False):
    """启动本地 Web UI：仅绑定 127.0.0.1，默认开启 Token + Host 校验 + 速率限制。"""
    auth_token = None if no_auth else secrets.token_hex(16)
    rate_lock = threading.Lock()
    rate_state = {}
    allowed_hosts = {"localhost", "127.0.0.1", "[::1]", "::1"}

    class Handler(BaseHTTPRequestHandler):
        def _client_ip(self):
            return self.client_address[0]

        def _reject(self, reason, status):
            print("⚠️ 拦截了来自 %s 的请求 %s，原因：%s"
                  % (self._client_ip(), self.path, reason), flush=True)
            self.send_error(status)
            return True

        def _security_check(self, is_post=False):
            if no_auth:
                return False
            try:
                host = urlparse("//" + self.headers.get("Host", "")).hostname or ""
            except ValueError:
                host = ""
            if host.lower() not in allowed_hosts:
                return self._reject("Host 错误", 403)
            parsed = urlparse(self.path)
            header_token = self.headers.get("X-Auth-Token", "")
            supplied = header_token or parse_qs(parsed.query).get("token", [""])[0]
            if supplied != auth_token:
                return self._reject("Token 错误", 403)
            if is_post:
                now = time.time()
                ip = self._client_ip()
                with rate_lock:
                    timestamps, banned_until = rate_state.get(ip, ([], 0))
                    if banned_until > now:
                        return self._reject("请求过快", 429)
                    timestamps = [t for t in timestamps if now - t < 60]
                    timestamps.append(now)
                    if len(timestamps) > 60:
                        rate_state[ip] = (timestamps, now + 300)
                        return self._reject("请求过快", 429)
                    rate_state[ip] = (timestamps, 0)
            return False

        def send_json(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, body, content_type, status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self.send_json({"ok": True, "version": VERSION})
                return
            if self._security_check():
                return
            params = dict(part.split("=", 1) if "=" in part else (part, "")
                          for part in parsed.query.split("&") if part)

            if parsed.path == "/":
                self._send_bytes(WEB_HTML.encode(), "text/html; charset=utf-8")
                return

            if parsed.path == "/api/domains":
                self.send_json(cmd_list_domains(Log("error", True)))
                return

            if parsed.path == "/api/novel_file":
                name = (parse_qs(parsed.query).get("name") or [""])[0]
                if not re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff_.-]+\.html", name) or ".." in name:
                    self.send_error(400)
                    return
                file_path = os.path.join(NOVEL_DIR, name)
                if not os.path.isfile(file_path):
                    self.send_error(404)
                    return
                with open(file_path, "rb") as handle:
                    data = handle.read()
                self._send_bytes(data, "text/html; charset=utf-8")
                return

            if parsed.path == "/api/proxy_image":
                image_url = unquote(parse_qs(parsed.query).get("url", [""])[0])
                if image_url.startswith("data:"):
                    self.send_response(204)
                    self.end_headers()
                    return
                image_parsed = urlparse(image_url)
                if image_parsed.scheme not in ("http", "https") or not image_parsed.hostname:
                    self.send_error(400, "Invalid image URL")
                    return
                try:
                    response = proxy_image(image_url)
                    if response.status_code >= 400:
                        print("[proxy_image] upstream %s %s" % (image_url, response.status_code),
                              file=sys.stderr, flush=True)
                        self.send_response(response.status_code)
                        self.end_headers()
                        return
                    content_type = response.headers.get("Content-Type", "application/octet-stream")
                    content_type = content_type.split(";", 1)[0].strip()
                    if not content_type.startswith("image/"):
                        content_type = "application/octet-stream"
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(response.content)))
                    self.send_header("Cache-Control", "public, max-age=3600")
                    self.end_headers()
                    self.wfile.write(response.content)
                except Exception as exc:
                    self.send_error(502, "Image fetch failed: %s" % exc)
                return

            if parsed.path == "/api/search":
                query = unquote_plus(params.get("q", "")).strip()
                if not query:
                    self.send_json({"results": []})
                    return
                search_type = params.get("type", "")
                handler = SEARCH_SOURCES.get(search_type)
                if handler is None:
                    self.send_json({"error": "未知搜索源"}, 400)
                    return
                try:
                    page = max(1, int(params.get("page", "1")))
                except ValueError:
                    self.send_json({"error": "invalid page"}, 400)
                    return
                try:
                    _record_search(query, search_type)
                    payload = handler(query, page=page, sort=params.get("sort", ""))
                    downloaded = {normalize(name) for name in scan_downloaded_videos()}
                    for item in payload.get("results", []):
                        item["already_downloaded"] = normalize(item.get("title", "")) in downloaded
                    self.send_json({"results": payload.get("results", []),
                                    "current_page": payload.get("current_page", page),
                                    "total_pages": payload.get("total_pages")})
                except Exception as exc:
                    self.send_json({"error": str(exc)}, 502)
                return

            if parsed.path == "/api/search_image":
                query = unquote_plus(params.get("q", "")).strip()
                if not query:
                    self.send_json({"results": []})
                    return
                source = params.get("source", "bing")
                handler = {"bing": search_bing_images, "baidu": fetch_baidu_image,
                           "360": fetch_so_image}.get(source)
                if handler is None:
                    self.send_json({"error": "unknown source"}, 400)
                    return
                try:
                    page = max(1, int(params.get("page", "1")))
                except ValueError:
                    self.send_json({"error": "invalid page"}, 400)
                    return
                try:
                    self.send_json({"results": handler(query, page), "current_page": page})
                except Exception as exc:
                    self.send_json({"error": str(exc)}, 502)
                return

            if parsed.path == "/api/favorites":
                with FAVORITES_LOCK:
                    self.send_json({"favorites": _load_favorites()})
                return

            if parsed.path == "/api/library":
                self.send_json(scan_media_library())
                return

            if parsed.path == "/api/library/file":
                target = _library_path(parse_qs(parsed.query).get("path", [""])[0])
                if target is None:
                    self.send_error(404)
                    return
                try:
                    size = target.stat().st_size
                    start, end = 0, size - 1
                    range_header = self.headers.get("Range", "")
                    if range_header.startswith("bytes="):
                        spec = range_header[6:].split(",", 1)[0]
                        left, right = (spec.split("-", 1) + [""])[:2]
                        if left:
                            start = int(left)
                        elif right:
                            start = max(0, size - int(right))
                        if right and left:
                            end = min(end, int(right))
                        if start > end or start >= size:
                            self.send_error(416)
                            return
                        status = 206
                    else:
                        status = 200
                    length = end - start + 1
                    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    self.send_response(status)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(length))
                    self.send_header("Accept-Ranges", "bytes")
                    if status == 206:
                        self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                    self.end_headers()
                    with target.open("rb") as stream:
                        stream.seek(start)
                        remaining = length
                        while remaining:
                            chunk = stream.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                except (OSError, ValueError):
                    self.send_error(404)
                return

            if parsed.path == "/api/history":
                self.send_json(_load_history())
                return

            if parsed.path == "/api/status":
                with WEB_JOBS_LOCK:
                    job = WEB_JOBS.get(unquote_plus(params.get("id", "")))
                self.send_json(job or {"error": "任务不存在", "status": "error"},
                               200 if job else 404)
                return

            self.send_error(404)

        def do_HEAD(self):
            if self._security_check():
                return
            parsed = urlparse(self.path)
            if parsed.path != "/api/proxy_image":
                self.send_error(404)
                return
            image_url = unquote(parse_qs(parsed.query).get("url", [""])[0])
            if image_url.startswith("data:"):
                self.send_response(204)
                self.end_headers()
                return
            image_parsed = urlparse(image_url)
            if image_parsed.scheme not in ("http", "https") or not image_parsed.hostname:
                self.send_error(400, "Invalid image URL")
                return
            try:
                response = proxy_image(image_url, stream=True)
                self.send_response(response.status_code if response.status_code < 400 else 200)
                content_type = response.headers.get("Content-Type", "application/octet-stream")
                self.send_header("Content-Type", content_type.split(";", 1)[0].strip())
                if response.headers.get("Content-Length"):
                    self.send_header("Content-Length", response.headers["Content-Length"])
                self.end_headers()
            except Exception as exc:
                self.send_error(502, "Image fetch failed: %s" % exc)

        def do_POST(self):
            if self._security_check(is_post=True):
                return
            path = urlparse(self.path).path
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                if path == "/api/favorites":
                    if not payload.get("url") or not payload.get("title"):
                        raise ValueError("参数无效")
                    with FAVORITES_LOCK:
                        items = _load_favorites()
                        if not any(item.get("url") == payload["url"] for item in items):
                            items.append(payload)
                            _save_favorites(items)
                    self.send_json({"favorites": items}, 201)
                    return

                if path == "/api/download":
                    if not payload.get("url"):
                        raise ValueError("参数无效")
                    job_id = uuid.uuid4().hex
                    threading.Thread(target=_web_download, args=(job_id, payload),
                                     daemon=True).start()
                    self.send_json({"id": job_id}, 202)
                    return

                if path == "/api/download_image":
                    if not payload.get("url") or not payload.get("filename"):
                        raise ValueError("参数无效")
                    job_id = uuid.uuid4().hex
                    threading.Thread(target=_web_image_download,
                                     args=(job_id, payload["url"], payload["filename"]),
                                     daemon=True).start()
                    self.send_json({"id": job_id}, 202)
                    return

                if path == "/api/scrape_novel":
                    novel_url = str(payload.get("url") or "").strip()
                    if not re.match(r"^https?://", novel_url, re.I):
                        raise ValueError("请提供小说详情页链接")
                    try:
                        max_chapters = int(payload.get("max_chapters") or 0) or None
                    except (TypeError, ValueError):
                        raise ValueError("参数无效")
                    if max_chapters is not None and not 0 < max_chapters <= 20000:
                        raise ValueError("参数无效")
                    job_id = uuid.uuid4().hex
                    threading.Thread(target=_web_novel_scrape,
                                     args=(job_id, novel_url, payload.get("site"),
                                           max_chapters), daemon=True).start()
                    self.send_json({"id": job_id}, 202)
                    return

                self.send_error(404)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)

        def do_DELETE(self):
            if self._security_check(is_post=True):
                return
            if urlparse(self.path).path != "/api/favorites":
                self.send_error(404)
                return
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                with FAVORITES_LOCK:
                    items = [item for item in _load_favorites()
                             if item.get("url") != payload.get("url")]
                    _save_favorites(items)
                self.send_json({"favorites": items})
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)

        def log_message(self, fmt, *args):
            sys.stderr.write("[Web] %s\n" % (fmt % args))

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    if no_auth:
        print("Web UI: http://127.0.0.1:%d/（--no-auth 调试模式，无鉴权）" % port, flush=True)
    else:
        print("🔐 访问地址: http://127.0.0.1:%d/?token=%s" % (port, auth_token), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return EXIT_OK


# ---------------------------------------------------------------- 主流程
def run(args):
    log = Log(args.log_level, json_mode=args.json)
    state = load_state()

    if args.list_domains:
        return EXIT_OK, cmd_list_domains(log)

    fetcher = Fetcher(log, cookie_file=args.cookies, proxy=args.proxy,
                      use_selenium=args.selenium, selenium_wait=args.selenium_wait)

    if args.search:
        source = args.source or next(iter(SEARCH_SOURCES))
        handler = SEARCH_SOURCES.get(source)
        if handler is None:
            log.error("未知搜索源 %s；可用：%s" % (source, ", ".join(SEARCH_SOURCES) or "（无）"))
            return EXIT_GENERIC, {"ok": False, "mode": "search", "error": "未知搜索源"}
        try:
            payload = handler(args.search, page=1, sort=args.sort or "")
            results = payload.get("results", [])
        except Exception as exc:
            log.error("搜索失败：%s" % exc)
            return EXIT_GENERIC, {"ok": False, "mode": "search", "query": args.search,
                                  "error": str(exc), "source": source}
        out = log.stream
        print("\n===== 搜索结果（%d） =====" % len(results), file=out, flush=True)
        for index, item in enumerate(results, 1):
            print("%d. %s" % (index, item.get("title", "")), file=out, flush=True)
            print("   %s" % item.get("url", ""), file=out, flush=True)
        print("", file=out, flush=True)
        if not results:
            log.warn("没有找到匹配的媒体")
            return EXIT_OK, {"ok": True, "mode": "search", "query": args.search,
                             "source": source, "results": []}
        if args.interactive:
            raw_selection = input("请输入要下载的编号（多个编号用逗号分隔）：").strip()
            try:
                selected = [int(value.strip()) for value in raw_selection.split(",")]
                if not selected or any(i < 1 or i > len(results) for i in selected):
                    raise ValueError
            except ValueError:
                log.error("无效的编号：%s" % raw_selection)
                return EXIT_GENERIC, {"ok": False, "mode": "search",
                                      "query": args.search, "error": "无效的编号"}
            downloads = []
            for index in selected:
                item = results[index - 1]
                log.info("选择第 %d 条：%s" % (index, item.get("title", "")))
                child = argparse.Namespace(**vars(args))
                child.search = None
                child.interactive = False
                child.download_first = False
                child.url = item["url"]
                code, result = run(child)
                downloads.append(result)
                if code != EXIT_OK:
                    return code, {"ok": False, "mode": "interactive",
                                  "query": args.search, "downloads": downloads}
            return EXIT_OK, {"ok": True, "mode": "interactive",
                             "query": args.search, "downloads": downloads}
        if not args.download_first:
            return EXIT_OK, {"ok": True, "mode": "search", "query": args.search,
                             "source": source, "results": results}
        args.url = results[0]["url"]
        log.info("自动选择第 1 条：%s" % results[0].get("title", ""))

    if not args.url:
        log.error("缺少媒体页面 URL 或 --search 关键词。")
        return EXIT_GENERIC, {"ok": False, "error": "缺少 URL"}

    outdir = Path(args.outdir).expanduser()
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        log.error("无法创建输出目录 %s：%s" % (outdir, exc))
        return EXIT_GENERIC, {"ok": False, "error": "输出目录不可写：%s" % exc}
    log.info("输出目录：%s" % outdir)
    log.info("mediagrab v%s（UA=%s）" % (VERSION, UA))

    pool_override = [d.strip() for d in (args.domain_pool or "").split(",") if d.strip()] or None
    candidates = build_candidate_urls(args.url, pool_override, log)

    failures, medias = [], []
    title, html_used, res_used = "未命名媒体", "", None
    used_url = args.url

    for index, candidate in enumerate(candidates, 1):
        host = urlparse(candidate).hostname or ""
        log.info("[%d/%d] 尝试域名 %s" % (index, len(candidates), host))
        try:
            medias, title, res = resolve(candidate, fetcher, log)
            used_url, html_used, res_used = candidate, res.text, res
            note_domain(state, host, True)
            if index > 1:
                log.info("镜像切换成功：%s（已加入优选列表）" % host)
            break
        except Unsupported as exc:
            log.error(str(exc))
            return EXIT_UNSUPPORTED, {"ok": False, "error": str(exc), "url": candidate,
                                      "unsupported": True,
                                      "domains_tried": [urlparse(c).hostname
                                                        for c in candidates[:index]]}
        except LookupError as exc:
            note_domain(state, host, False, str(exc))
            failures.append({"domain": host, "error": str(exc), "kind": "no_media"})
            log.warn("%s 未解析到媒体地址" % host)
        except Exception as exc:
            note_domain(state, host, False, str(exc))
            failures.append({"domain": host, "error": str(exc), "kind": "fetch"})
            log.warn("%s 失败：%s" % (host, exc))
        medias = []

    if not medias:
        log.error("所有候选域名均失败。错误链：")
        for item in failures:
            log.error("  - %s：%s" % (item["domain"], item["error"]))
        for tried in fetcher.tried:
            log.error("  引擎 %s" % tried)
        if html_used:
            log.error("页面片段：" + html_used[:300].replace("\n", " "))
        return EXIT_ALL_DOMAINS_FAILED, {
            "ok": False, "error": "所有候选域名均失败", "failures": failures,
            "domains_tried": [urlparse(c).hostname for c in candidates]}

    if args.info:
        info = build_info(html_used, used_url, medias)
        out = log.stream
        print("\n===== 媒体信息 =====", file=out, flush=True)
        print("标题   ：%s" % info["title"], file=out, flush=True)
        print("封面   ：%s" % (info["cover"] or "（无）"), file=out, flush=True)
        print("时长   ：%s" % (info["duration"] or "（未知）"), file=out, flush=True)
        print("标签   ：%s" % (", ".join(info["tags"]) or "（无）"), file=out, flush=True)
        print("页面   ：%s" % info["url"], file=out, flush=True)
        print("候选档位（%d）：" % len(medias), file=out, flush=True)
        for item in sorted(medias, key=lambda x: x.height, reverse=True):
            print("  - %-8s %-4s %s" % (item.quality or "未知", item.kind,
                                        item.url[:90]), file=out, flush=True)
        print("", file=out, flush=True)
        return EXIT_OK, {"ok": True, "mode": "info", "info": info}

    targets = [{"url": used_url, "title": title}]
    if args.series:
        targets += discover_series(html_used, used_url, log, args.series_limit)

    results = []
    cookie = cookie_header_for(fetcher.cookies, used_url)
    host_root = "https://%s/" % urlparse(used_url).hostname

    for index, target in enumerate(targets, 1):
        t_url, t_title = target["url"], target["title"]
        try:
            if t_url == used_url:
                t_medias, title_use = medias, title
            else:
                t_medias, title_use, _ = resolve(t_url, fetcher, log)
            log.info("===== [%d/%d] %s =====" % (index, len(targets), title_use or t_title))
            media = pick_media(t_medias, args.quality, log)
            if media is None:
                raise LookupError("无可用媒体档位")
            quality_suffix = (" [%s]" % media.quality) if media.quality else ""
            if args.output and len(targets) == 1:
                name = args.output
            else:
                name = "%s%s.mp4" % (sanitize_filename(title_use), quality_suffix)
            if not name.lower().endswith(".mp4"):
                name += ".mp4"
            out_path = outdir / name
            log.info("选用：%s（%s）" % (media.quality or "未知画质", media.origin))
            log.info("输出：%s" % out_path)
            ok = download_media(media, out_path, host_root, cookie, args.proxy,
                                log, args.engine, title_use)
            synced = None
            if ok and args.to_album:
                synced = sync_to_album(out_path, args.to_album, args.album_mode, log)
            final_output = synced if (synced and args.album_mode == "move") else out_path
            results.append({"title": title_use, "url": t_url, "ok": ok,
                            "output": str(final_output) if ok else None,
                            "album_output": str(synced) if synced else None,
                            "quality": media.quality, "media_url": media.url,
                            "size": (synced or out_path).stat().st_size
                                    if (ok and (synced or out_path).exists()) else 0})
        except Unsupported as exc:
            log.warn("跳过（不支持）：%s" % exc)
            results.append({"title": t_title, "url": t_url, "ok": False, "error": str(exc)})
        except Exception as exc:
            log.error("处理失败 %s：%s" % (t_url, exc))
            results.append({"title": t_title, "url": t_url, "ok": False, "error": str(exc)})

    good = [item for item in results if item.get("ok")]
    log.info("任务结束：成功 %d/%d" % (len(good), len(results)))
    result = {
        "ok": bool(good) and len(good) == len(results),
        "mode": "series" if args.series else "download",
        "domain_used": urlparse(used_url).hostname,
        "engine": res_used.engine if res_used else "",
        "title": title,
        "items": results,
        "output_dir": str(outdir),
        "failures": failures,
    }
    if not good:
        return EXIT_DOWNLOAD_FAILED, result
    return EXIT_OK, result


def build_parser():
    parser = argparse.ArgumentParser(
        prog="mediagrab",
        description="mediagrab —— 通用媒体下载器（视频 / 图片 / 小说，多引擎降级抓取）",
        epilog=("示例：\n"
                "  python mediagrab.py 'https://example.com/watch/123'\n"
                "  python mediagrab.py 'https://example.com/watch/123' 我的视频.mp4 cookies.txt -q 1080p\n"
                "  python mediagrab.py 'https://example.com/watch/123' --info\n"
                "  python mediagrab.py 'https://example.com/watch/123' --series --series-limit 12\n"
                "  python mediagrab.py --search '关键词' --source demo-html --download-first -q 720p\n"
                "  python mediagrab.py --list-domains\n"
                "  python mediagrab.py --web\n"),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="?", help="媒体页面 URL")
    parser.add_argument("output", nargs="?", help="输出文件名（可选，默认按标题生成）")
    parser.add_argument("cookies", nargs="?", help="cookies.txt 路径（Netscape 格式，可选）")
    parser.add_argument("--search", metavar="关键词", help="按关键词搜索（配合 --source 选择站点适配器）")
    parser.add_argument("--source", help="搜索源名称（见 SEARCH_SOURCES，默认取第一个）")
    parser.add_argument("--sort", default="", help="排序参数，由各适配器自行解释")
    parser.add_argument("--interactive", action="store_true", help="与 --search 配合，交互选择要下载的结果")
    parser.add_argument("--download-first", action="store_true", help="与 --search 配合，自动下载第一条")
    parser.add_argument("--web", action="store_true", help="启动本地 Web UI")
    parser.add_argument("--web-port", type=int, default=int(os.environ.get("MEDIAGRAB_PORT") or 8080),
                        help="Web UI 端口（默认 8080，仅绑定 127.0.0.1）")
    parser.add_argument("--no-auth", action="store_true", help="Web 调试模式：跳过 Token、Host 与速率限制")
    parser.add_argument("--to-album", nargs="?", const=DEFAULT_MEDIA_DIR, metavar="目录",
                        help="下载完成后同步到媒体库目录（默认 %s）" % DEFAULT_MEDIA_DIR)
    parser.add_argument("--album-mode", choices=("copy", "move"), default="copy",
                        help="同步方式：copy 保留原文件，move 移动原文件（默认 copy）")
    parser.add_argument("-q", "--quality", help="指定画质，如 1080p / 720p / 480p")
    parser.add_argument("-o", "--outdir", default=DEFAULT_OUTDIR,
                        help="输出目录（默认 %s）" % DEFAULT_OUTDIR)
    parser.add_argument("--domain-pool", help="自定义/覆盖候选域名列表，逗号分隔")
    parser.add_argument("--list-domains", action="store_true", help="查看候选域名池状态后退出")
    parser.add_argument("--info", action="store_true", help="仅解析媒体信息，不下载")
    parser.add_argument("--series", action="store_true", help="下载整个系列（若页面结构支持）")
    parser.add_argument("--series-limit", type=int, default=20, help="系列模式最多下载集数（默认 20）")
    parser.add_argument("--proxy", default=os.environ.get("https_proxy") or os.environ.get("http_proxy"),
                        help="HTTP(S) 代理，如 http://127.0.0.1:7890（默认读 https_proxy 环境变量）")
    parser.add_argument("--cookie", dest="cookies_opt", help="同位置参数 cookies.txt")
    parser.add_argument("--selenium", action="store_true", help="启用 Selenium + headless Chromium 兜底")
    parser.add_argument("--selenium-wait", type=float, default=8.0, help="Selenium 页面等待秒数（默认 8）")
    parser.add_argument("--engine", choices=("auto", "ffmpeg", "native"), default="auto",
                        help="下载引擎（默认 auto：ffmpeg 优先，失败降级原生断点续传）")
    parser.add_argument("--log-level", choices=("debug", "info", "warn", "error"), default="info",
                        help="日志级别（默认 info）")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果（日志走 stderr）")
    parser.add_argument("--version", action="version", version="mediagrab %s" % VERSION)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.web:
        return serve_web(port=args.web_port,
                         no_auth=args.no_auth or os.environ.get("MEDIAGRAB_NO_AUTH") == "1")
    if args.cookies_opt:
        args.cookies = args.cookies_opt
    try:
        code, result = run(args)
    except KeyboardInterrupt:
        print("\n[警告] 用户中断", file=sys.stderr)
        return 130
    except Exception as exc:
        import traceback
        print("[错误] 未捕获异常：%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        traceback.print_exc()
        return EXIT_GENERIC
    if getattr(args, "json", False):
        print(RESULT_MARK + " " + json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
