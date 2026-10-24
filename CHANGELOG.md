# CHANGELOG

## 1.1.0

小说阅读器增强：本机文本可以直接进书架，阅读页内置阅读设置。

- 新增 `parse_novel_text()`：把离线 .txt 按「第X章 / 第X回 / Chapter N」标题行自动切分成章节；
  识别不到标题时整篇作单章；开头书名行（《…》或与书名同名）不产生空前置章
- 新增 `import_novel_files()` 与 `--novel-import 书.txt …`：一键把本机 txt 建成阅读页写入书架，
  utf-8 / gbk 自动识别，单文件失败不阻断整批
- 新增书架：`list_novel_books()`、`GET /api/novels`、`POST /api/import_novel`、
  Web UI「📖 小说书架」面板（打开阅读页 + 批量导入）与 `--novel-list`
- 阅读页（`build_novel_html`）内置阅读器：字号 A−/A＋、行距密/疏、暗/米黄/白三档主题、
  目录抽屉、顶部进度条；偏好与滚动位置按书名存 localStorage，重开回到上次阅读处
  （仍为单文件、离线可读、正文全量转义，页内仅一段自有 JS）
- 测试 37 → 42 项：新增切分/编码导入/书架/阅读器组件断言

## 1.0.0

首个公开版本：把通用下载器内核从私有工程中抽离并重写为中性模板。

- 抓取：多级降级（requests → cloudscraper → curl → selenium）、Cloudflare 挑战页识别、
  重试与线性退避、缺失 charset 时的编码兜底（修中文标题乱码）
- 镜像池：同 kind 域名互换、状态优选评分、`canon_host()` 去掉 `www.`
  （修 `www.` 前缀导致 kind 退回 generic、解析器走错的问题）
- 解析：源标签 / data-* / 裸 m3u8 / JS 赋值 / og:video / LD+JSON / 播放器 JS；
  HLS master playlist 分档；统一的画质选择策略；
  广告目录过滤不误杀文件名含 `sample-` / `preview-` 的媒体
- 下载：ffmpeg 直出（带 Referer/UA/Cookie）、下载前探测真实大小、
  输出偏小时判失败并丢弃残档、原生 Range 断点续传降级
- Web UI：Token + Host 校验 + 速率限制、任务轮询、Range 文件服务、缩略图代理、
  收藏 / 历史 / 媒体库、内嵌前端（含 `node --check` 语法校验测试）
- 小说：选择器与编码配置化，产出单文件深色主题阅读页
- 测试：37 项离线用例，覆盖上述所有行为
