# CHANGELOG

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
