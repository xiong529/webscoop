# 网站资源爬取工具 (resources-reptile)

基于 Scrapy + Python 的网站资源爬取程序：图片、视频、音频、文档、软件安装包等均可下载，内置完整反爬策略，并提供图形化操作界面。

## 快速开始（图形界面，推荐）

双击运行 **`start.bat`** —— 自动创建虚拟环境、安装依赖并弹出图形界面：

1. 在输入框内输入网址，点击 **「发现资源」**
2. 左侧列表展示可爬取的内容：
   - **图片** → 显示缩略图 + 文件名
   - **视频** → 显示视频封面 + 文件名
   - **文件** → 显示文件名（含类型/大小）
3. 打开网址后可使用 **「全选」/「反选」/「全不选」**，也可按 **展示**（图片/视频/文件）或 **类型** 过滤
4. 勾选需要的资源：**点击整行即勾选**，**Shift+点击** 范围复选，**Ctrl+点击** 单独反转，或右键菜单
   「按类型全选图片/视频/文件」，最后点击 **「下载勾选」**

**抓取体验（流式 + 可打断）**：
- 发现是**流式**的——探测出一个就立即显示一个（不必等全站抓完），资源多的站能边抓边看
- 数字分页并发抓取，加快多页站的速度
- 想提前结束按 **「停止」**：已上屏的资源全部保留并正常显示，可直接勾选下载

下载的文件默认按类型分类保存到 **`information/`** 目录（可用「文件名模板」自定义子目录与命名）：

```
information/
├── images/     图片（.jpg/.png/.gif/.webp…）
├── videos/     视频（.mp4/.mkv/.avi…）
├── audios/     音频（.mp3/.wav/.flac…）
├── docs/       文档（.pdf/.docx/.xlsx…）
├── software/   软件安装包（.exe/.msi/.apk…）
├── archives/   压缩包（.zip/.rar/.7z…）
└── others/     其他
```

> 已有同名文件会自动跳过，不会重复下载。GUI 入口文件：`gui.py`，核心逻辑：`gui_crawler.py`，统一 HTTP 层：`gui_fetch.py`（配置入口：`config.py`）。

### 文件名模板（下载命名自由度）

「抓取设置」下方一行 **文件名模板** 控制每个文件的保存路径与文件名（`/` 分隔子目录，默认
`{category}/{name}` 与旧行为等价）。可用 token：

| token | 含义 |
| --- | --- |
| `{category}` | 分类目录：images / videos / audios / docs / software / archives / others |
| `{kind}` | 类型：image / video / file |
| `{name}` | 资源原名（含扩展名） |
| `{stem}` | 原名去扩展名的主体 |
| `{ext}` | 扩展名（含点，如 `.jpg`；原名无扩展名则为空） |
| `{site}` | 站点域名（去 www.） |
| `{title}` | 页面标题（无则回退站点域名） |
| `{size}` | 文件大小（字节） |
| `{width}x{height}` | 分辨率（如 `1920x1080`；未知为空） |

例如 `{site}/{title}/{stem}_{width}x{height}{ext}` 会把 pexels 的图存成
`example.com/页面标题/photo-123_1920x1080.jpg`。渲染结果会自动净化，无法穿越目录、
含非法字符；若改名后没有扩展名会自动补回原名扩展名（避免下载后类型无法识别）。
可用环境变量 `RESOURCES_FILENAME_TEMPLATE` 覆盖默认值。

### 高清规则表（highres_rules.json）

图片/视频的「缩略图 → 高清原图」变换由 `highres_rules.json` 规则表驱动（参考 Fatkun 的
rules.json 设计）：新增网站只需追加一条规则（匹配 URL 正则 + 变换器名称），**无需改代码**。
内置规则：WordPress 尺寸后缀剥离、pexels 原图直链、pixabay 高清变体、通用 w/h 参数升级。

### LLM 规则生成器（llm_rules.py）

新站的高清变换规律不确定时，可用 `llm_rules.py` 让 LLM 分析一张网页的图片/视频直链样式，
自动产出一条 `highres_rules.json` 规则并合并入库——**一次分析，永久零成本生效**：

```bash
python llm_rules.py https://example.com/gallery        # 只展示建议规则
python llm_rules.py https://example.com/gallery --apply # 合并进 highres_rules.json
python llm_rules.py --file saved.html --apply           # JS 页先手动存 HTML 再分析
```

调用 OpenAI 兼容接口（DeepSeek / Ollama / OpenRouter / LiteLLM 等均可），配置见
`config.py` 的「LLM 规则分析」段，也可临时用 CLI 参数覆盖：

```bash
RESOURCES_LLM_KEY=sk-xxx RESOURCES_LLM_MODEL=deepseek-chat python llm_rules.py URL --apply
python llm_rules.py URL --base https://api.example.com/v1 --key sk-xxx --model gpt-4o-mini --apply
```

**GUI 配置**：主界面「**LLM 模型…**」按钮打开配置弹窗——填 **接口地址 / API Key / 模型名**，
点「**测试连接**」实时验证（无 Token 消耗），「保存配置」写入本地 `llm_config.json`
（已 gitignore）。保存后 `llm_rules.py` 与 GUI 均直接使用该配置。

安全设计：LLM **只能从变换器白名单选 transform**（含通用正则 `regex_sub`），
不执行任何 LLM 生成的代码；校验失败（非法正则/未知变换器）则拒绝入库。

### API 抓取（Pexels 官方接口）

主界面「**API 抓取…**」按钮打开独立弹窗（不与爬虫主界面混排）：填入 **接口地址 + API Key**，
点击「**API 获取**」即可从 Pexels 官方 API（`api.pexels.com/v1`）直接抓取图片/视频资源，
结果进入同一列表，继续用现有的勾选 / 下载 / 预览功能：

- **预设接口**：搜索图片 `/search`、精选图片 `/curated`、搜索视频 `/videos/search`、热门视频 `/videos/popular`（选下拉后自动填好接口地址）
- **自定义 URL**：任意 `/v1/` 接口，如 `https://api.pexels.com/v1/search?query=mountain&orientation=landscape`、`/photos/:id`、`/videos/:id`、`/collections/:id`
- 直接粘贴 pexels **网站搜索页**地址也行（`www.pexels.com/search/...` 自动转 API）
- **自动翻页**：跟随响应 `next_page` 连续抓取（上限「翻页」数，默认 `config.API_PAGE_LIMIT`=3；每页 `per_page` ≤ 80）
- API Key 自动保存到本地 `pexels_api_key.txt`（已加入 .gitignore；也可用环境变量 `RESOURCES_API_KEY` 提供），界面实时显示剩余配额

照片下载取 `src.original` 原图直链，视频取最高清 mp4 直链（自动排除 HLS 流）。

### 备用下载（gallery-dl 兜底）

内置发现器认不出的网站（JS 渲染、特殊结构），点击「备用下载(gallery-dl)」把当前网址交给
gallery-dl 批量抓取——它内置 1400+ 网站的支持（Instagram、Pinterest、Reddit、壁纸站等）。

> 注意：pexels 作者主页/收藏页（无限滚动，如 `pexels.com/@user/featured-uploads/`）只有首屏
> 资源能被内置发现器静态抓取；**「发现资源」会自动用 gallery-dl 补充并去重合并**（状态栏会显示
> "gallery-dl 自动补充 N 个"）。已适配其 WAF（`gallery_backup.py` CLI 包装模式去掉触发 520 的
> 空 `X-Forwarded-*` 请求头，下载走本地代理网关可获得完整速度）；带 `/zh-cn/` 等语言前缀的
> 网址会自动去掉后再交给解析器（对任意网站生效）。

流程：**设置弹窗**（最多下载数量 0~500、最小文件大小 KB、文件类型 全部/仅图片/仅视频）
→ 解析出候选文件列表（文件名 + 精确大小）→ **预览勾选弹窗**（单击行勾选，Ctrl/Shift 多选，
显示已勾选数量与总大小）→ 下载勾选项到同一 `information/` 目录。解析或下载中均可点
「取消备用下载」终止；下载结束统计按文件数计算（含已在目录中的排查）。

## 命令行方式（Scrapy，深度全站爬取）

```bash
pip install -r requirements.txt

# 爬取整个站点并下载所有资源
scrapy crawl resource -a start_urls="https://example.com"

# 只下载视频 + 压缩包
scrapy crawl resource -a start_urls="https://example.com/videos" -a download_extensions="mp4,mkv,zip"

# 限制扫描深度
scrapy crawl resource -a start_urls="https://example.com" -a max_depth=5
```

命令行模式通过 Scrapy 的管道下载，保存位置由 `FILES_STORE` 控制（默认 `downloads/`）。

## 浏览器指纹模拟（TLS/JA3，借鉴 Scrapling Fetcher）

GUI 与 Scrapy 双模式统一采用 **curl_cffi**（Scrapling 的底层依赖）运行真实浏览器
TLS/JA3 指纹与 HTTP/2，可有效绕过基于指纹的机器识别：

- 普通页面/API 请求 → TLS 模拟 + 本机代理
- 媒体/大文件请求 → 委托 Scrapy 内置 HTTP11 处理器流式落盘（内存友好）
- 指纹目标由 `config.py` 的 `IMPERSONATE` 调整（如 `chrome` / `firefox` / `safari`）

## 本机代理（默认 127.0.0.1:65534）

默认启用本机代理用于访问国外站点，入口集中在 `config.py`：

```python
DEFAULT_PROXY = "http://127.0.0.1:65534"   # 可写死/环境变量 RESOURCES_PROXY 覆盖
PROXY_ENABLED = True                       # 环境变量 RESOURCES_PROXY_ENABLED="0" 临时关闭
```

GUI、Scrapy 下载处理器、Scrapy 代理中间件均从同一配置读取，保证行为一致。

## 断点续爬 / 并发控制（借鉴 MediaCrawler）

- `RESUME_EXISTING=True`：已存在文件自动跳过，中断后可继续
- 通过 `config.py` 统一管理代理开关、并发数、超时，GUI 与 CLI 共用

## 反爬策略

针对真实网站的常见反爬手段，本项目内置：

| 反爬手段 | 应对方式 |
|---|---|
| User-Agent 检测 | 随机浏览器 UA 池（`utils/user_agents.py`） |
| 请求头校验（一致性检查） | 补全 Accept / Accept-Language / Sec-Fetch 等浏览器头（参考 GitHub [advanced-web-scraping-tutorial](https://github.com/sangaline/advanced-web-scraping-tutorial)） |
| IP 限流 / 封禁 | 随机代理池，失效自动切换（`proxies.txt` 或 `RESOURCES_PROXY_URLS`） |
| 高频访问检测 | 随机延迟 + Scrapy AutoThrottle 自动限速 |
| 登录态 / 会话 | Cookie 会话保持（`COOKIES_ENABLED=True`） |

### 浏览器渲染（JS 站点）

- GUI：「渲染模式」勾选后入口页走 Playwright 无头渲染；失败自动回退链：本机中转 → **代理池按站** → 直连（与普通请求同一套代理回退语义，池代理渲染失败会吊销换下一个候选）。
- Scrapy：**默认关闭**，属保守设计。需要自动渲染 JS 站时在 `resources_reptile/settings.py` 打开：

  ```python
  RENDER_MIDDLEWARE_ENABLED = True        # 渲染中间件开关
  # RENDER_AUTO_DOMAINS = ["example.com"] # 只对指定域名自动渲染（空列表 = 仅显式 render/meta 触发）
  ```

  - 命中平台适配器（抖音/快手/小红书等）的请求无论开关与否都会走「渲染 + 捕获接口 + 提取」路径（GUI 与 Scrapy 一致：适配器命中即强制渲染，避免静态页只扒到推荐位等无关内容）。
- 抖音主页/短链（`v.douyin.com/xxx` 分享短链、`iesdouyin.com/share/user/...`）：主页作品列表接口 `/aweme/v1/web/aweme/post/` 无登录态时可能静默空返回——此时到 GUI「登录抓 Cookie…」登录抖音并保存到 `cookies.txt` 后重新抓取即可完整提取（无需勾选渲染模式）。
- 三个开关行为对比：GUI 渲染模式（入口页生效）＞ `RENDER_AUTO_DOMAINS` 白名单域名＞适配器命中。`renderer.py` 为单例浏览器 + 每请求独立上下文，代理由候选链逐级尝试。

### Cookie 登录态注入（`cookies.txt`）

需要登录态的站点（如小红书非登录不可出接口）：GUI 工具栏「登录抓 Cookie…」按钮 → 弹出**独立临时上下文**的真实浏览器（不导入日常浏览器数据）→ 手动登录 → 「读取 Cookie」预览 → 「保存到 cookies.txt」，格式与手粘一致（`域名:  完整Cookie头`）。也可跳过按钮直接手粘。

- 注入时机与语义：FetchSession 普通请求、渲染浏览器、Scrapy 渲染中间件（经 renderer）三层自动带。渲染浏览器按**域名作用域**注入（`context.add_cookies`，cookie 只对本域及子域生效，避免把已登录域的 Cookie 泄漏给页面里的跨域第三方资源）；无域名前缀的裸 Cookie 视为全域名兜底（只能走 `extra_http_headers` 头注入）。
- Cookie 仅写本项目根目录 `cookies.txt`，不上传任何服务；抓取统计的失败原因/站点分布自动落盘 `stats.json`——GUI 任务结束写 `information/stats.json`，Scrapy CLI 爬完写 `FILES_STORE`（默认 downloads/）目录，两处都含发现/下载/失败分布与耗时。
| 超时重连 | 5 次重试，含 429 限流状态码 |

启用代理：默认已启用本机代理 `127.0.0.1:65534`（`config.py` `DEFAULT_PROXY`）。如需额外代理池，在 `settings.py` 设置 `PROXY_ENABLED = False` 并关闭指纹处理器代理后，在项目根目录 `proxies.txt` 中逐行填写代理地址。

## HLS（m3u8）流媒体分片下载

部分站点（点播/回放平台）只提供 m3u8 分片流，直链下载不适用。发现到 `.m3u8`
链接或 `Content-Type` 为 mpegurl 的资源时，自动走 `hls_downloader.py` 的纯
Python 合并下载（免 ffmpeg）：

- 主列表自动选择**最高带宽**变体；支持相对/绝对分片 URI、`#EXT-X-BYTERANGE` 偏移分片
- 分片并发下载（默认 8 线程，`HLS_WORKERS` 调整），单分片重试 3 次，失败即
  整体失败、回退常规下载；输出与源同名的 `.ts` 文件（mp4 播放器可直接打开，
  或 `ffmpeg -i out.ts out.mp4` 转封装）
- 明确边界：**不支持 AES-128 加密流**与**直播流**（无 `#EXT-X-ENDLIST` 且分片
  超 `HLS_MAX_SEGMENTS`），报可读原因后跳过，不卡死
- 验证码类风控**不做通用求解**（滑块对未知站点不可靠）：仍走失败归桶
  （429/网络）+ 换代理重试的现有链路，README 如实标注边界

## Scrapling 第三层兜底链路

常规请求链（curl_cffi TLS 指纹 → 指纹轮换 → 撤代理直连）全部失败或返回非 2xx
时，自动用 **Scrapling Fetcher**（curl-cffi 封装，补充 HTTP/3 协商与 stealthy
头）再试一次，进一步提高对严格 TLS 风控站点的通过率：

- 仅引入轻量 `Fetcher`（同 requests 语义），不引入 Playwright/Camoufox 等重型
  依赖；开关 `SCRAPLING_FALLBACK`（默认开）
- 失败路径无额外开销：只在常规链失败/非 2xx 时触发一次

## 项目结构

```
resources-reptile/
├── start.bat               # 一键启动脚本
├── config.py               # 统一配置（默认代理 / 并发 / 超时 / 指纹 / API）
├── discover_common.py      # 双引擎共享分类/高清/MIME/文件名模板/下载端点逻辑
├── gui.py                  # 图形界面（tkinter，API 抓取为独立弹窗）
├── gui_crawler.py          # GUI 资源发现与下载核心
├── gui_fetch.py            # 统一 HTTP 层（curl_cffi，回退 requests）
├── api_discoverer.py       # Pexels 官方 API 抓取器（接口地址 + API Key）
├── renderer.py             # Playwright 无头渲染（JS 站点抓取）
├── gallery_backup.py       # gallery-dl 备用下载（1400+ 网站兜底）
├── player_vlc.py           # VLC 在线播放器
├── highres_rules.json      # 缩略图 → 高清原图规则表
├── llm_rules.py            # LLM 规则生成器（主线 A：分析一页直链 → 建议规则入库）
├── tests/                  # 全量回归（见下「测试」）
│   ├── run_all.py          # 统一入口
│   ├── unit_common.py      # 发现核心逻辑单测
│   ├── unit_features.py    # 去重/MIME/robots/重试/failures 单测
│   ├── unit_llm.py         # LLM 规则生成器单测（假 LLM 注入全流程）
│   ├── e2e_stream.py       # Discoverer 流式回调 + 可打断 端到端
│   ├── e2e_mime.py         # MIME 嗅探管道端到端
│   ├── e2e_retry.py        # 下载重试 + failures.json 端到端
│   └── e2e_render.py       # 渲染模式端到端（需 chromium）
├── pexels_api_key.txt      # API Key 本地保存（自动生成，已 gitignore）
├── requirements.txt
├── proxies.txt             # 代理池模板
├── information/            # GUI 下载默认保存目录
├── resources_reptile/      # Scrapy 工程
│   ├── settings.py         # 全局配置（限速/反爬/管道/指纹处理器）
│   ├── download_handlers.py# TLS 指纹模拟下载处理器（curl_cffi）
│   ├── dupefilters.py      # 规范化去重（剔除 utm_ 等追踪参数）
│   ├── middlewares.py      # 反爬下载中间件 + 按域 robots 策略
│   ├── pipelines.py        # 资源分类下载管道（MIME 嗅探）
│   ├── items.py            # 数据模型
│   ├── spiders/resource_spider.py
│   └── utils/              # UA 池 / 代理池
└── downloads/              # Scrapy 命令行模式下载目录
```

## 测试（一条命令跑全量回归）

```bash
python tests/run_all.py        # 全部：单元 + 端到端
python tests/run_all.py unit   # 仅单元（不需浏览器/网络，秒级）
python tests/run_all.py e2e    # 仅端到端（本地 HTTP 服务）
```

- 每个套件在**独立子进程**运行，互不污染全局配置；任一失败即非零退出码。
- 端到端用例起本地 HTTP 服务验证真实爬取链路，不依赖外网；
  `e2e_render.py` 需要已安装 Playwright chromium（`python -m playwright install chromium`）。
- tests/ 下测试文件均为自包含脚本，也可单独运行，如 `python tests/unit_features.py`。

## 合规声明

本工具仅用于合法用途。请确保目标网站允许抓取，遵守其 robots.txt 与服务条款，不用于任何侵权或非法用途。