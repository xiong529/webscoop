# 网站资源爬取工具 (resources-reptile)

基于 Python + Scrapy 的网站资源批量爬取与下载工具：把网页里的 **图片、视频、音频、文档、
软件安装包** 等资源一键抓到本地。提供图形化界面（tkinter）与 Scrapy 命令行两种使用方式，
内置完整反爬应对（浏览器 TLS 指纹模拟、代理池、Playwright 渲染、登录态 Cookie 注入、
接口 JSON 捕获），并对抖音 / 快手 / 小红书等「页面空壳 + 签名接口」平台做了专项适配。

## 1. 项目解决什么问题

网站在资源批量获取上常见的痛点：

- **资源分散**：图片/视频散落在多个页面，手工右键另存为效率极低；
- **缩略图陷阱**：页面只给缩略图/封面，真实高清原图藏在详情页或 CDN；
- **JS 空壳页面**：数据全靠前端 JS 动态渲染，静态抓取只能拿到推荐位等无关内容；
- **签名接口站**：抖音等平台作品列表走带签名的 JSON 接口（如 `/aweme/v1/web/aweme/post/`），
  且无登录态时可能静默返回空数据；
- **反爬门槛**：UA 检测、TLS 指纹识别、IP 限流、需要登录态；
- **流媒体**：部分站点只提供 m3u8 分片流，直链下载不可用。

本工具把这些场景统一成一条流水线：**输入网址 → 自动发现全部可下载资源 → 预览勾选 → 批量下载**，
GUI 下全程鼠标操作，不用写代码。

## 2. 主要功能

### 资源发现
- 自动提取 `img / video / audio / a / source / meta(og:)` 等标签中的资源直链，探测类型与大小；
- **流式上屏**：探测出一个显示一个，资源多的站点边抓边看；支持中途停止，已上屏资源全部保留；
- **分页跟随**：自动抓取 `?page=N` 等后续页并合并（`PAGE_FOLLOW_LIMIT`）；
- **详情页跟进**：按路径特征（`/photo/xxx`、`/video/xxx` 等）批量跟进内容页，提取
  og:image / 真实视频直链（`DETAIL_PAGE_LIMIT`）；
- **平台适配器**（抖音/快手/小红书）：页面命中适配器后自动走「渲染 + 捕获接口 JSON + 提取」
  路径——即使未勾选渲染模式，也会强制渲染并捕获页面发出的接口数据，从
  `aweme_list` 等字段提取真实作品（含视频直链、封面、标题）。支持抖音分享短链
  `v.douyin.com/xxx` 与 `iesdouyin.com/share/user/...` 主页。

### 下载与保存
- 类型自动识别（MIME 嗅探 + 扩展名），按 `images / videos / audios / docs / software /
  archives / others` 分类落盘到 `information/`（GUI）或 `downloads/`（Scrapy）；
- **文件名模板**：`{category} {kind} {name} {stem} {ext} {site} {title} {size} {width}x{height}`
  自由组合目录与命名（如 `{site}/{title}/{stem}_{width}x{height}{ext}`），路径自动净化防穿越；
- **断点续载**：已存在文件自动跳过；下载失败自动重试（3 次指数退避），失败原因分桶记录；
- **HLS（m3u8）分片下载**：自动选择最高带宽变体，8 线程并发合并为 `.ts` 文件（免 ffmpeg），
  支持 BYTERANGE 偏移分片；**不支持 AES-128 加密流与直播流**，报可读原因后跳过；
- **高清原图规则表**：`highres_rules.json` 驱动「缩略图 → 高清原图」变换，新增站点只需追加
  规则无需改代码；规律不明时可用 LLM 分析一页直链样式自动生成规则（`llm_rules.py`）。

### 反爬与登录态
| 反爬手段 | 应对方式 |
|---|---|
| UA 检测 | 随机浏览器 UA 池 |
| TLS/JA3 指纹识别 | curl_cffi 模拟 chrome/firefox/safari 等真实指纹（可会话级随机换指纹+IP 绑定） |
| IP 限流/封禁 | 本机代理（默认 `127.0.0.1:65534`）+ 代理池逐行配置（`proxies.txt`），失败自动吊销换下一个 |
| JS 动态渲染 | Playwright 无头渲染（GUI 勾选渲染模式；渲染回退链：本机中转 → 代理池按站 → 直连） |
| 签名接口 | 渲染时捕获匹配特征的接口 JSON（`/aweme/v1/web/` 等）并提取资源 |
| 需要登录态 | Cookie 注入（见下） |
| 严格 TLS 风控 | Scrapling Fetcher 第三层兜底（常规链失败/非 2xx 时再试一次） |

- **Cookie 登录态注入**：GUI 工具栏「登录抓 Cookie…」弹出独立临时浏览上下文，手动登录后
  「保存到 cookies.txt」；也可直接在文本框中粘贴日常浏览器复制出的 Cookie 头（F12 →
  Network → 任一请求 → Request Headers → Cookie）。按域名行限定
  （`douyin.com:  sessionid=xxx; ...`），支持**同注册域家族匹配**——短链
  `v.douyin.com/xxx` 命中 `www.douyin.com` 的规则，登录态可送达真实接口所在域。
  渲染浏览器按域名作用域注入（`add_cookies`），不把已登录域 Cookie 泄漏给跨域资源。
- **验证码类风控不做通用求解**（滑块对未知站点不可靠）：走失败归桶（429/网络）+ 换代理
  重试的现有链路。

### 扩展能力
- **Pexels API 抓取**：独立弹窗填入接口地址 + API Key，从官方 API 抓图/视频（预设搜索、
  精选、热门视频接口，自动翻页跟随 `next_page`，Key 本地保存并 gitignore）；
- **gallery-dl 备用下载**：内置发现器认不出的站点一键交给 gallery-dl（内置 1400+ 站点
  支持），发现流程也会自动调用它补充无限滚动页面并去重合并；
- **在线播放**：VLC 组件在线预览视频（小于 60MB 先缓存后播，更大直接流播）。

### 统计
GUI 任务结束写 `information/stats.json`，Scrapy 爬完写 `downloads/stats.json`：页面数、
发现数、下载成功/失败数、失败原因分桶（403/429/timeout/other）、失败站点分布、耗时、
字节数；下载失败明细落 `failures.json`。

## 3. 安装方法

要求：**Python 3.10+**（Windows 推荐；macOS/Linux 亦可）。

安装只有一条路：**先把代码放到本地，再启动**。一键脚本 `start.bat` 位于仓库内，
没有本地副本就无法使用，所以两步顺序不能颠倒：

### 第 1 步：获取代码

```bash
git clone <你的仓库地址> resources-reptile
cd resources-reptile
```

也可以直接下载 GitHub 页面上的 ZIP 压缩包并解压到本地。

### 第 2 步：启动

**Windows：双击 `start.bat`** —— 自动创建虚拟环境、安装依赖并启动图形界面（推荐）。

**手动方式（任何系统通用）：**

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # 渲染模式需要（JS 站/接口捕获）
python gui.py                            # 启动图形界面
```

- 可选：安装 [VLC](https://www.videolan.org/)（在线播放功能）；
- 可选：`llm_rules.py` 用到的 LLM 需 OpenAI 兼容接口（DeepSeek / Ollama / OpenRouter /
  LiteLLM），填入 API Key 即可，未配置时相关功能自动停用。

### 常用配置（环境变量，全部可覆盖）

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `RESOURCES_INFO_DIR` | `information/` | GUI 下载目录 |
| `RESOURCES_SCRAPY_DIR` | `downloads/` | Scrapy 下载目录 |
| `RESOURCES_PROXY` | `http://127.0.0.1:65534` | 本机代理 |
| `RESOURCES_PROXY_ENABLED` | `1` | 是否启用代理 |
| `RESOURCES_RETRY_TIMES` | `3` | 下载失败重试次数 |
| `RESOURCES_RESUME` | `1` | 断点续载（已有文件跳过） |
| `RESOURCES_FILENAME_TEMPLATE` | `{category}/{name}` | 文件名模板 |
| `RESOURCES_PAGE_FOLLOW` | `20` | 分页跟随上限（0=关闭） |
| `RESOURCES_RENDER` | `0` | 默认开启渲染模式 |
| `RESOURCES_COOKIE_FILE` / `RESOURCES_COOKIE` | `cookies.txt` / 空 | Cookie 文件名 / 全域名 Cookie 头 |
| `RESOURCES_HLS_WORKERS` / `RESOURCES_HLS_MAX_SEGMENTS` | `8` / `15000` | HLS 并发分片数 / 上限 |
| `RESOURCES_SCRAPLING_FALLBACK` | `1` | Scrapling 第三层兜底开关 |
| `RESOURCES_ROBOTS` | `0` | 默认是否遵守 robots.txt（可对指定域名覆盖） |
| `RESOURCES_LLM_BASE/KEY/MODEL` | DeepSeek 默认 | LLM 规则生成器配置 |
| `RESOURCES_API_KEY` | 空 | Pexels API Key（或存 `pexels_api_key.txt`） |

## 4. 使用方法

### 图形界面（推荐）

```bash
python gui.py        # 或双击 start.bat
```

1. 输入网址，点 **「发现资源」**；
2. 左侧列表流式显示资源（缩略图/封面 + 文件名），可按 **展示**（图片/视频/文件）或
   **类型** 过滤，点整行勾选，Shift/Ctrl 多选，右键菜单「按类型全选」；
3. 勾选后点 **「下载勾选」**，文件按分类保存到 `information/`；
4. 可选操作：
   - **渲染模式**：勾选后 JS 动态站点也能抓（不勾也行——平台适配器命中会自动渲染）；
   - **登录抓 Cookie…**：弹窗登录保存登录态（或直接手动粘贴 Cookie）；
   - **API 抓取…**：Pexels 官方 API；
   - **备用下载(gallery-dl)**：内置发现器不认识的站；
   - **LLM 模型…**：配置高清规则生成器；
   - **文件名模板**：自定义保存路径与命名。

### 命令行（Scrapy，全站深度爬取）

```bash
scrapy crawl resource -a start_urls="https://example.com"                  # 整站所有资源
scrapy crawl resource -a start_urls="https://example.com/videos" -a download_extensions="mp4,mkv,zip"   # 只下指定类型
scrapy crawl resource -a start_urls="https://example.com" -a max_depth=5   # 限制深度
scrapy crawl resource -a start_urls="https://example.com" -a render=1      # 入口页渲染
```

### 登录态（cookies.txt）格式

每行一条，支持按域名限定（推荐，避免把登录态注入到无关站点）：

```
# 注释行以 # 开头
douyin.com:  sessionid=xxx; ttwid=yyy; sid_guard=zzz; ...
example.com:  sid=abc
```

- 子域规则互通：填 `www.douyin.com` 的 Cookie，`v.douyin.com` 短链同样命中；
- 不带域名前缀的行视为全域名兜底，谨慎使用；
- 保存后立即生效（无需重启程序）。

## 5. 输入输出示例

### 示例 1：普通网页

```
输入：https://example.com/gallery（一个含大量图片/视频的列表页）
过程：发现资源（自动分页跟随 + 详情页跟进 + 高清规则变换）→ 勾选 → 下载
输出：
information/
├── images/   example-photo-1.jpg  example-photo-2.jpg ...
├── videos/   example-clip-1.mp4   example-clip-2.mp4 ...
└── stats.json   # 页面数/发现数/成功失败/耗时/失败原因分布
```

### 示例 2：抖音博主主页（短链）

```
输入：https://v.douyin.com/xxxx/（分享短链，自动重定向到博主主页）
配置：先「登录抓 Cookie…」登录抖音并保存（无登录态时作品接口静默空返回）
输出：该博主主页全部视频直链（douyinvod.com CDN）+ 封面图，按分类落盘
```

### 示例 3：Pexels 官方 API

```
输入：API 地址 https://api.pexels.com/v1/search?query=mountain + API Key
输出：每张照片取 src.original 原图直链、视频取最高清 mp4 直链，自动翻页合并
```

### 示例 4：m3u8 流媒体

```
输入：发现到 .m3u8 链接（或 Content-Type 为 mpegurl）
输出：自动选择最高带宽变体 → 并发下载分片 → 合并为同名的 .ts 文件
     （ffmpeg -i out.ts out.mp4 可转封装）
```

## 6. 测试与项目结构

### 测试（一条命令全量回归）

```bash
python tests/run_all.py          # 全部：单元 + 端到端
python tests/run_all.py unit     # 仅单元（无需浏览器/网络）
python tests/run_all.py e2e      # 仅端到端（本地 HTTP 服务，不依赖外网）
```

- 13 个套件在独立子进程运行，互不污染全局配置；任一失败即非零退出码；
- `e2e_render.py`、`e2e_douyin_api.py` 需要已安装 Playwright chromium
  （`python -m playwright install chromium`）；
- 测试文件均可单独运行，如 `python tests/unit_netsuite.py`。

### 项目结构

```
resources-reptile/
├── start.bat               # Windows 一键启动
├── config.py               # 统一配置（目录/代理/并发/超时/指纹/API）
├── gui.py                  # 图形界面（tkinter）
├── gui_crawler.py          # GUI 资源发现与下载核心
├── gui_fetch.py            # 统一 HTTP 层（curl_cffi TLS 指纹 + Scrapling 兜底）
├── discover_common.py      # 分类 / MIME / 文件名模板 / 高清变换 / 下载端点
├── api_discoverer.py       # Pexels 官方 API 抓取器
├── renderer.py             # Playwright 无头渲染 + 接口 JSON 捕获
├── cookie_capture.py       # 登录抓 Cookie 弹窗 / 手动粘贴保存
├── gallery_backup.py       # gallery-dl 备用下载
├── player_vlc.py           # VLC 在线播放器
├── hls_downloader.py       # m3u8 分片下载合并
├── llm_rules.py            # LLM 高清规则生成器
├── platform_adapters.py    # 抖音/快手/小红书适配器（渲染 + 接口提取）
├── highres_rules.json      # 缩略图 → 高清原图规则表
├── resources_reptile/      # Scrapy 工程（爬虫/中间件/管道/去重/下载处理器）
├── tests/                  # 13 套回归测试
├── information/            # GUI 下载目录
└── downloads/              # Scrapy 下载目录
```

## 许可证

本项目采用 [MIT License](LICENSE) 开源，可自由使用、修改与分发，详见 LICENSE 文件。

## 合规声明

本工具仅用于合法用途。请确认目标网站允许抓取，遵守其 robots.txt 与服务条款，
不用于任何侵权或非法用途。登录态 Cookie 仅保存在本地 `cookies.txt`，不会上传任何服务。
