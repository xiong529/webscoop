# Scrapy settings for resources_reptile project
#
# 说明：这是一个网站资源爬虫项目，主要用于爬取图片、视频、文档、
# 软件安装包等资源。配置了完整的反爬策略（随机UA、随机代理、
# 浏览器请求头、随机延迟、Cookie会话、自动限速）。

BOT_NAME = "resources_reptile"

SPIDER_MODULES = ["resources_reptile.spiders"]
NEWSPIDER_MODULE = "resources_reptile.spiders"

# -----------------------------------------------
# robots.txt 策略（按站可配置）
# -----------------------------------------------
# 说明：内置 RobotsTxtMiddleware 需要全局 ROBOTSTXT_OBEY=True 才会工作，
# 是否真正遵守由 RobotsPolicyMiddleware 按域名决定：
#   遵守  = config.ROBOTS_POLICY 中该域为 True（或未配置时取 ROBOTS_OBEY_DEFAULT=True）
#   豁免  = 其余情况（默认全部豁免，即 meta dont_obey_robotstxt=True）
# 单次抓取可用 spider 参数 robots=1 把起始域名临时加入遵守名单。
ROBOTSTXT_OBEY = True

# 默认请求头（会被中间件进一步补充）
DEFAULT_REQUEST_HEADERS = {
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# -----------------------------------------------
# 下载 / 限速
# -----------------------------------------------
# 每个域名并发请求数（调低更安全，调高更快）
CONCURRENT_REQUESTS_PER_DOMAIN = 6
# 基础下载延迟（秒）。Scrapy 默认 RANDOMIZE_DOWNLOAD_DELAY=True，
# 实际延迟为 0.5~1.5 倍随机化。AutoThrottle 开启时由其动态接管。
DOWNLOAD_DELAY = 1.0

# -----------------------------------------------
# 限速方式二选一（二重限速语义重叠，容易造成困惑）：
# - AutoThrottle（推荐，默认开启）：按响应时间动态调节延迟，生效时
#   RandomDelayMiddleware 自动跳过（见 middlewares.py）；
# - RANDOM_DELAY_MIN/MAX：仅当 AUTOTHROTTLE_ENABLED=False 时提供
#   固定范围的随机延迟。
# 两者同时开启不会叠加，但不要同时依赖两套参数。
# -----------------------------------------------
RANDOM_DELAY_MIN = 1.0
RANDOM_DELAY_MAX = 4.0

# 开启自动限速（AutoThrottle），动态调整请求速度
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 2.0
AUTOTHROTTLE_MAX_DELAY = 20.0
# 目标并发 2.0：比默认 1.0 吞吐高一倍，仍留有较大的削峰余量
AUTOTHROTTLE_TARGET_CONCURRENCY = 2.0

# 下载器设置：支持大文件（视频/安装包）
DOWNLOAD_TIMEOUT = 60
DOWNLOAD_SIZELIMIT = 0
DOWNLOAD_FAIL_ON_DATALOSS = False

# -----------------------------------------------
# Cookie 会话
# -----------------------------------------------
COOKIES_ENABLED = True

# -----------------------------------------------
# 反爬开关
# -----------------------------------------------
# 是否启用本机代理（默认 127.0.0.1:65534，用于访问国外站点）。
# 代理来源见 config.py DEFAULT_PROXY，可写死/环境变量覆盖。
PROXY_ENABLED = True

# -----------------------------------------------
# 页面自动渲染
# -----------------------------------------------
# 渲染中间件总开关（false 时 render=1 仍由下载处理器兜底渲染起始页，
# 适配器平台/自动域名不渲染）。
RENDER_MIDDLEWARE_ENABLED = False
# 命中即渲染的域名名单（JS 动态加载站，如 example.com）
RENDER_AUTO_DOMAINS = []
# TLS 指纹模拟目标浏览器（Scrapling/curl_cffi 支持，如 chrome / firefox / safari）
SCRAPLING_IMPERSONATE = "chrome"
# 是否在 UA 池中包含爬虫工具 UA（默认仅浏览器 UA）
USER_AGENT_INCLUDE_TOOL = False

# -----------------------------------------------
# 重试策略
# -----------------------------------------------
RETRY_ENABLED = True
RETRY_TIMES = 5
RETRY_HTTP_CODES = [500, 502, 503, 504, 522, 524, 408, 429]

# -----------------------------------------------
# 下载器中间件
# -----------------------------------------------
DOWNLOADER_MIDDLEWARES = {
    # 反爬中间件（数字越小优先级越高）
    "resources_reptile.middlewares.RandomUserAgentMiddleware": 100,
    "resources_reptile.middlewares.BrowserHeadersMiddleware": 150,
    "resources_reptile.middlewares.RandomProxyMiddleware": 200,
    # 页面自动渲染（render=1 / 适配器平台 / RENDER_AUTO_DOMAINS 命中时）
    # 在代理挑选（200）之后、延迟中间件（275）之前执行
    "resources_reptile.middlewares.RenderPageMiddleware": 250,
    "resources_reptile.middlewares.RandomDelayMiddleware": 275,
    # robots 策略：按域名设置 dont_obey_robotstxt（必须先于内置中间件执行）
    "resources_reptile.middlewares.RobotsPolicyMiddleware": 900,
    # 内置中间件
    "scrapy.downloadermiddlewares.retry.RetryMiddleware": 500,
    "scrapy.downloadermiddlewares.defaultheaders.DefaultHeadersMiddleware": 400,
    "scrapy.downloadermiddlewares.cookies.CookiesMiddleware": 700,
    "scrapy.downloadermiddlewares.httpcompression.HttpCompressionMiddleware": 800,
    "scrapy.downloadermiddlewares.downloadtimeout.DownloadTimeoutMiddleware": 350,
    "scrapy.downloadermiddlewares.robotstxt.RobotsTxtMiddleware": 1000,
    "scrapy.downloadermiddlewares.useragent.UserAgentMiddleware": None,
}

# -----------------------------------------------
# 下载处理器：TLS 指纹模拟（Scrapling/curl_cffi）
# -----------------------------------------------
DOWNLOAD_HANDLERS = {
    "http": "resources_reptile.download_handlers.ImpersonatedDownloadHandler",
    "https": "resources_reptile.download_handlers.ImpersonatedDownloadHandler",
}

# -----------------------------------------------
# Item 管道
# -----------------------------------------------
ITEM_PIPELINES = {
    # 文件下载管道：按类型分类存入 downloads/ 目录
    "resources_reptile.pipelines.ResourceFilesPipeline": 300,
}

FILES_STORE = "downloads"
MEDIA_ALLOW_REDIRECTS = True

# -----------------------------------------------
# 日志
# -----------------------------------------------
LOG_LEVEL = "INFO"
LOG_ENCODING = "utf-8"

# -----------------------------------------------
# 去重
# -----------------------------------------------
# 规范化去重：去重前剔除 utm_*、fbclid、gclid 等追踪参数，避免
# 翻页/分享链接带不同追踪参数造成重复抓取。
# 想恢复纯请求指纹去重，改回 "scrapy.dupefilters.RFPDupeFilter" 即可。
DUPEFILTER_CLASS = "resources_reptile.dupefilters.NormalizedRFPDupeFilter"
# 需要剔除的追踪参数名前缀/完整名（完整名精确匹配，前缀以 *_ 结尾）
DUPEFILTER_STRIP_PARAMS = (
    "utm_", "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid",
    "yclid", "gbraid", "wbraid", "_ga", "_gl",
)

FEED_EXPORT_ENCODING = "utf-8"