# mediagrab

通用媒体下载器框架：**视频 / 图片 / 小说**。

单文件 Python 实现，面向 Linux 与 Termux(Android) 环境，无需 root。
本仓库只包含**通用技术内核**——抓取降级、镜像池、媒体解析、下载引擎、本地 Web UI，
以及可直接照抄的站点适配器模板；不包含任何具体站点的业务数据。

## 特性

**抓取**
- 多级降级：`requests` → `cloudscraper` → `curl` → `selenium`，逐级回退
- 识别 Cloudflare 挑战页并继续降级，而不是把挑战页当正常内容
- 页头未声明 charset 时按探测编码 / UTF-8 解码，中文标题不会变成乱码文件名
- 请求重试 + 线性退避（`_source_get_html`、`_cg_request` 均可配置次数）

**镜像池**
- 候选域名按 `kind` 分组，**只有同族域名互相替换**，替换时保持 path + query 不变
- 成功/失败记录写入状态文件，命中过的域名成为「优选」，下次优先尝试
- `canon_host()` 统一去掉 `www.`，避免 `www.example.com` 匹配不到池里的 `example.com`
  而退回通用解析路径

**解析**
- 覆盖 `<source>` / `<video>` / `data-*` 属性 / 裸 `.m3u8` / JS 赋值 /
  `og:video` / LD+JSON `contentUrl` / 播放器 `setVideo*()` 调用
- 自动展开 HLS master playlist 的多档清晰度（`EXT-X-STREAM-INF`）
- 画质选择策略：精确命中 → 不高于目标的最高档 → 最高档
- 广告/预览目录过滤（不会误杀文件名含 `sample-` / `preview-` 的正常媒体）

**下载**
- `ffmpeg -c copy` 直出 mp4，自动带上 `Referer` / `User-Agent` / `Cookie`
- 下载前探测媒体真实大小；ffmpeg 输出明显偏小时判定失败并丢弃残档
  （ffmpeg 有时会在流被截断后仍以 0 退出）
- 缺 ffmpeg 或 ffmpeg 失败时降级为原生 HTTP **Range 断点续传**（`.part` + 续传）

**本地 Web UI**
- 仅绑定 `127.0.0.1`，Token 鉴权 + Host 校验 + POST 速率限制
- 任务轮询、Range 文件服务（视频可拖动预览）、缩略图代理（按图床设置 Referer）
- 收藏夹、搜索历史、下载历史、本地媒体库
- 内嵌单文件前端；测试会用 `node --check` 校验内嵌 JS 语法

**小说**
- CSS 选择器配置化：编码、标题/目录/正文选择器、排序属性、章节间隔全部写在配置里
- 产出单文件深色主题 HTML 阅读页（带目录锚点，可离线阅读）

## 安装

```bash
pip install -r requirements.txt

# 可选：TLS 指纹伪装（绕过部分站点的指纹校验）
pip install curl_cffi
# 可选：浏览器兜底
pip install selenium && pkg install chromium -y   # Termux 另需自行放置 chromedriver
```

外部命令：`ffmpeg`（推荐，HLS 合并与转封装必需；缺失时降级为原生 mp4 下载）。

## 用法

```bash
# 下载（默认最高画质，输出到 ~/Downloads/MediaGrab）
python mediagrab.py 'https://example.com/watch/123'

# 指定文件名 / cookies / 画质
python mediagrab.py 'https://example.com/watch/123' 我的视频.mp4 cookies.txt -q 1080p

# 只看信息（标题 / 封面 / 可用档位），不下载
python mediagrab.py 'https://example.com/watch/123' --info

# 系列模式
python mediagrab.py 'https://example.com/watch/123' --series --series-limit 12

# 关键词搜索（用 --source 选适配器）
python mediagrab.py --search '关键词' --source demo-html --download-first -q 720p

# 域名池状态
python mediagrab.py --list-domains

# 本地 Web UI
python mediagrab.py --web          # 终端会打印带 token 的访问地址
python mediagrab.py --web --no-auth   # 调试用：关闭鉴权
```

常用参数：`--engine {auto,ffmpeg,native}`、`--proxy`、`--to-album`、`--album-mode {copy,move}`、
`--domain-pool`、`--json`（结构化结果，日志走 stderr）、`--log-level`。

退出码：`0` 成功 · `1` 通用错误 · `2` 全部候选域名失败 · `3` 站点不支持 · `4` 下载失败 · `5` 未找到媒体。

## 接入你自己的站点

**1. 加进域名池**（决定走哪个解析器、和谁互为镜像）

```python
SITE_POOL = [
    {"domain": "mysite.example", "kind": "mysite", "priority": 10, "note": "主站"},
    {"domain": "mysite-cdn.example", "kind": "mysite", "priority": 20, "note": "镜像"},
]
```

**2. 写搜索适配器**（有现成模板，只填参数）

```python
def fetch_mysite(q, page=1, sort="popular"):
    return fetch_html_search(
        q, page, sort,
        base_url="https://mysite.example",
        search_path="/search",
        href_pattern=r"^/watch/[^/?#]+/?$",
        params={"q": "{query}", "page": "{page}"},
    )

SEARCH_SOURCES["mysite"] = fetch_mysite
```

JSON API 站点用 `fetch_json_search(...)`，配合 `item_path` 与 `field_map`（支持 `user.name` 点号路径）。

**3. 挑解析器**（多数站点不需要自己写）

`RESOLVERS` 里已经有几种通用形态：`resolve_generic`（正则扫页面）、`resolve_page_scan`
（顺带扫站内 `/api/`、`/ajax/` 端点）、`resolve_ldjson`（LD+JSON `contentUrl`）、
`resolve_setvideo`（播放器 JS 赋值）。把自己的 `kind` 指过去即可。
解析器允许返回**媒体列表**或 **`(媒体列表, 标题, 响应)` 元组**两种形状。

**4. 小说站点**：在 `NOVEL_SITES` 里加一份选择器配置，然后用 `--novel` 或 Web UI 抓取。

## 目录结构

```
mediagrab.py            主程序（抓取 / 解析 / 下载 / Web UI / 小说，单文件）
tests/test_core.py      内核测试（全部离线 fixture，不访问真实站点）
requirements.txt
```

## 环境变量

| 变量 | 作用 |
|---|---|
| `MEDIAGRAB_OUTDIR` / `MEDIAGRAB_MEDIA_DIR` / `MEDIAGRAB_GALLERY_DIR` / `MEDIAGRAB_IMAGE_DIR` / `MEDIAGRAB_NOVEL_DIR` | 各输出目录 |
| `MEDIAGRAB_HOME` | 状态目录（域名池状态、收藏、历史），默认 `~/.config/mediagrab` |
| `MEDIAGRAB_FALLBACK_DIR` | 媒体库同步的后备目录（多个用 `:` 分隔），首个目录不可写时依次尝试 |
| `MEDIAGRAB_PORT` | Web UI 端口，默认 `8080` |
| `MEDIAGRAB_NO_AUTH` | 设为 `1` 等价于 `--no-auth` |
| `http_proxy` / `https_proxy` | 代理，也可用 `--proxy` 指定 |

## 测试

```bash
python -m pytest tests/ -q
```

覆盖：媒体提取各形态、广告过滤、HLS 分档、画质选择、镜像池与 `www` 归一、
抓取降级链与 Cloudflare 挑战识别、cookie 解析、两种 resolver 返回形状、
下载截断检测、小说配置化抓取、内嵌前端 JS 语法（`node --check`）与函数唯一性。

## 使用须知

- 本工具只做「按 URL 抓取并保存」，不自带任何站点数据；请自行接入你有权访问的目标。
- 请遵守目标站点的服务条款与所在地法律，仅下载你有权获取的内容；不要用于绕过付费或版权保护。
- 请控制请求频率（`delay`、并发数、退避参数都可调），避免给目标站点造成压力。
- 仓库不含任何账号、Cookie、Token 或私有配置。

## License

MIT，见 [LICENSE](LICENSE)。
