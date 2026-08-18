# Product Scraper Service

通过商品页链接提取商品名、价格、图片链接、尺寸和产品详情，过滤推荐商品图、联系图标、logo、社媒图、支付图标等干扰资产。

## 技术选型

- FastAPI：对外提供 HTTP API，部署简单。
- Playwright：处理 React / Next.js 等 JS 渲染商品页。
- DrissionPage + Chrome/Chromium：在授权场景下处理 Cloudflare 真人验证页。
- BeautifulSoup + JSON-LD：优先读取结构化商品数据，速度快。
- PM2：用于线上进程守护和日志查看。

## 本地运行

```bash
cd /www/wwwroot/zg_xhs_api
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

如果需要处理 Cloudflare 真人验证页，本机或服务器还需要安装 Chrome/Chromium。无法自动识别浏览器路径时，在 `.env` 中设置 `CF_BROWSER_PATH`。

打开：

```text
http://localhost:8000
```

## API

接口接受公开可访问的 `http/https` 商品页链接。服务会阻止 localhost、内网 IP、链路本地地址等 SSRF 风险地址。

```http
POST /api/scrape
Content-Type: application/json
```

请求：

```json
{
  "url": "https://www.monumentgallery.co.uk/product/garbo-fringe-lamps-by-mariyo-yagi",
  "render": "auto",
  "max_images": 12,
  "download_images": false
}
```

也可以一次提交多条商品链接，`urls` 最多 10 条：

```json
{
  "urls": [
    "https://www.monumentgallery.co.uk/product/garbo-fringe-lamps-by-mariyo-yagi",
    "https://www.sauceldn.com/seating#/early-20th-century-antler-chairs/"
  ],
  "render": "auto",
  "max_images": 12
}
```

传 `url` 时保持原来的单条响应；传 `urls` 时返回：

```json
{
  "results": [
    {
      "url": "https://...",
      "success": true,
      "result": {
        "name": "...",
        "price": "",
        "currency": "",
        "image_links": [],
        "dimensions": "",
        "product_details": {}
      },
      "error": null
    }
  ]
}
```

`render` 可选：

- `auto`：先静态抓取，不够好再浏览器渲染。
- `always`：总是浏览器渲染。
- `never`：只静态抓取。

`max_images` 可选，范围 `1-12`，服务最多返回 12 张主商品图。

响应只包含：

- `name`：商品名
- `price`：商品价格
- `currency`：币种，能从结构化数据识别时返回
- `image_links`：图片链接，优先返回上传后的图床链接
- `dimensions`：尺寸
- `product_details`：产品详情，包含描述和其他可识别详情字段

服务会在 `render=auto` 时先静态抓取；如果商品信息、图片或尺寸信息不足，会自动再用浏览器渲染抓取一次。遇到 Cloudflare 真人验证页时，会在授权配置下尝试使用 DrissionPage + Chrome/Chromium fallback 获取通过验证后的 HTML。尺寸缺失不会阻断返回。

### 获取拍卖商品列表

```http
POST /api/auction/list
Content-Type: application/json
```

请求：

```json
{
  "url": "https://drouot.com/zh/c/199/jia-ju",
  "render": "auto",
  "max_items": 50,
  "max_pages": 0,
  "save_to_db": true
}
```

拍卖网站数据直接通过 Drouot API 获取，例如：

```text
https://api.drouot.com/drouot/gingolem/neoGingo/lot/search?lang=zh&cat=199&page=1&facet=false
```

接口支持传 Drouot 分类页 URL，也支持直接传上面的 API URL。服务会从 `page=1` 开始递增请求，每页预期 100 条；当某页 `lots` 数量小于 100 时，认为已经到最后一页并停止。

参数说明：

- `max_items`：接口响应里返回的预览条数，范围 `1-100`；完整数据写入 SQLite。
- `max_pages`：最多抓取页数，`0` 表示不限页数，一直抓到最后一页。
- `save_to_db`：是否保存到 SQLite，默认 `true`。
- `render`：为兼容旧调用保留，Drouot API 抓取不需要浏览器渲染。

SQLite 默认保存到 `SCRAPER_DATA_DIR/auction_lots.sqlite3`，可用 `AUCTION_DB_PATH` 指定路径。

响应：

```json
{
  "source_url": "https://drouot.com/zh/c/199/jia-ju",
  "fetched_url": "https://api.drouot.com/drouot/gingolem/neoGingo/lot/search?lang=zh&cat=199&page=1&facet=false",
  "title": "Drouot category 199",
  "total_count": 5666,
  "pages_fetched": 57,
  "saved_count": 5666,
  "database_path": "/www/wwwroot/zg_xhs_api/data/auction_lots.sqlite3",
  "items": [
    {
      "lot_id": "34599005",
      "lot_number": "221",
      "title": "一款1960年代的柚木贴面斯堪的纳维亚风格餐桌...",
      "url": "https://drouot.com/zh/l/34599005-kuan-1960nian-dai-de-you-mu-tie-mian-si-kan-de-na-wei-ya",
      "image_link": "https://cdn.drouot.com/d/image/lot?size=ftall&path=...",
      "sale_status": "live",
      "sale_time": "8月14日 | 下午02:00",
      "estimate": "€400 - 600",
      "currency": "EUR",
      "low_estimate": 400,
      "high_estimate": 600,
      "sale_timestamp": 1786708800,
      "sale_type": "LIVE",
      "sale_id": 184022,
      "auctioneer_id": 970,
      "page": 1,
      "position": 1,
      "raw_text": "live 8月14日 | 下午02:00 ..."
    }
  ]
}
```

### 创建小红书笔记

```http
POST /api/xhs/create
Content-Type: application/json
```

请求参数与 `/api/scrape` 一致：

```json
{
  "url": "https://moltocollectibles.it/en/collectibles/credenza-in-legno-effetto-bambu-anni-80/",
  "render": "auto",
  "max_images": 12
}
```

`/api/xhs/create` 同样支持 `urls` 数组。批量模式会逐条创建小红书笔记，单条失败不会中断其他链接。

流程：

- 先抓取商品名、详情、尺寸和最多 12 张主商品图。
- 下载商品图并处理为 3:4，小红书封面图默认使用火山 Ark Seedream 扩图，也可切回多米 GPT Image 2，所有成图底部添加 `ZIQU` 品牌文字。
- 使用 DeepSeek 生成小红书标题和正文。
- 将本地处理后的图片作为小红书图文笔记发布。
- 接口最终返回小红书笔记二维码图片链接。

响应：

```json
{
  "job_id": "1783607518-7b810176bd",
  "qrcode_image_link": "https://xhspost.aivip1.top/api/html-render/qrcode?size=320&data=...",
  "share_link": "https://note.aivip1.top/#/xhs-auto-api?id=...",
  "xhs_link": "https://note.aivip1.top/#/xhs-auto-api?id=...",
  "title": "旨丘｜...",
  "content": "旨丘在售的这...",
  "result_path": "/data/1783607518-7b810176bd/xhs_result.json"
}
```

### 生成闲鱼文案

```http
POST /api/xianyu/copy
Content-Type: application/json
```

请求参数与 `/api/scrape` 一致：

```json
{
  "url": "https://moltocollectibles.it/en/collectibles/credenza-in-legno-effetto-bambu-anni-80/",
  "render": "auto",
  "max_images": 12
}
```

`/api/xianyu/copy` 同样支持 `urls` 数组。流程会先抓取商品标题、价格、尺寸、详情和主商品图，上传图片后传给火山方舟 Doubao 视觉模型生成闲鱼标题和正文，不发布到闲鱼。

闲鱼正文中的尺寸统一使用 `cm`；如果源站尺寸为英寸，会按 `1 inch = 2.54 cm` 换算后再生成正文。
闲鱼正文会尽量包含抓取到的国家/产地；除尺寸行和固定收尾句外，中间正文主段控制在 120-200 字。固定收尾句会单独一行。

价格字段由服务端按固定公式计算：

```text
人民币售价 = 网站标价 * 8 * 2 + 5000
```

如果网站没有公开价格或无法解析出数字，`price` 返回 `99999`。

响应：

```json
{
  "job_id": "1783607518-7b810176bd",
  "title": "【中古预定】...",
  "content": "尺寸：...\n...",
  "xianyu_copy": "【中古预定】...\n\n尺寸：...\n...",
  "product_type": "灯具",
  "price": "53000",
  "source_price": "3000",
  "source_currency": "EUR",
  "image_links": ["https://..."],
  "result_path": "/data/1783607518-7b810176bd/xianyu_result.json"
}
```

`xianyu_copy` 为合并后的闲鱼文案，格式是标题、空行、正文。
`product_type` 为产品类型，例如 `灯具`、`椅子`、`长凳`。
`image_links` 为获取到的产品图链接，保持原顺序返回。

遇到 Cloudflare / bot verification 等页面时，服务会自动尝试 Cloudflare fallback。fallback 未安装依赖、缺少浏览器、验证未通过或被禁用时，接口会返回 `502`，错误信息类似：

```json
{
  "detail": "Failed to scrape page: Blocked by Cloudflare/security verification page"
}
```

Cloudflare fallback 只在 `render=auto` 或 `render=always` 时启用；`render=never` 会保持只静态抓取。

## 图片链接

服务会先下载抓取到的主商品图到 job 临时目录，再上传到 SuperBed 图床。上传完成后会删除本地临时图片，并在 `image_links` 返回 SuperBed 图片链接。

单张图片下载或上传失败时，接口不会中断整条商品结果，会回退使用原始图片链接。

需要配置：

```env
CORS_ALLOW_ORIGINS=*
SCRAPER_DATA_DIR=./data
AUCTION_DB_PATH=
DROUOT_API_TIMEOUT=60
DROUOT_API_RETRIES=4
DROUOT_API_RETRY_DELAY=2
LOG_LEVEL=INFO
SUPERBED_UPLOAD_URL=https://api.superbed.cc/upload
SUPERBED_TOKEN=你的SuperBed token
SUPERBED_CATEGORIES=product-scraper
IMAGE_DOWNLOAD_MAX_BYTES=31457280
IMAGE_UPLOAD_CONCURRENCY=6
IMAGE_UPLOAD_TOTAL_CONCURRENCY=12
SCRAPE_CONCURRENCY=3
BATCH_CONCURRENCY=2
RENDER_CONCURRENCY=2
CF_BYPASS_ENABLED=true
CF_BYPASS_CONCURRENCY=1
CF_BYPASS_RETRIES=8
CF_BYPASS_WAIT=8
CF_BYPASS_PAGE_TIMEOUT=60
CF_BYPASS_HEADLESS=false
CF_BROWSER_PATH=
CF_CHROMIUM_ARGS=
CF_TURNSTILE_EXTENSION_PATH=
CF_UA_EXTENSION_PATH=
XHS_IMAGE_PROCESS_CONCURRENCY=20
XHS_USE_EXPAND=true
XHS_EXPAND_PROVIDER=ark
ARK_API_KEY=你的火山Ark API key
ARK_IMAGE_MODEL=doubao-seedream-5-0-260128
ARK_IMAGE_SIZE=1728x2304
ARK_IMAGE_TIMEOUT=600
ARK_REFERENCE_MAX_PIXELS=36000000
ARK_CHAT_COMPLETIONS_URL=https://ark.cn-beijing.volces.com/api/v3/chat/completions
XIANYU_COPY_MODEL=doubao-seed-2-0-pro-260215
XIANYU_COPY_MAX_IMAGES=4
XIANYU_COPY_MAX_ATTEMPTS=5
XIANYU_COPY_RETRY_DELAY=1
XHS_EXPAND_MAX_ATTEMPTS=3
XHS_EXPAND_RETRY_DELAY=5
DUOMI_API_KEY=你的多米API key
XHS_USE_DUOMI_EXPAND=true
DUOMI_POLL_INTERVAL=2
DUOMI_TIMEOUT=600
DEEPSEEK_API_BASE=https://api.deepseek.com
DEEPSEEK_API_KEY=你的DeepSeek API key
DEEPSEEK_MODEL=deepseek-v4-pro
XHS_POST_API_BASE=https://xhspost.aivip1.top
XHS_POST_API_KEY=xhs_post
```

```json
{
  "url": "https://...",
  "hosted_url": "https://..."
}
```

## 并发控制

接口仍是同步请求-响应模式，没有引入异步队列。服务通过全局并发阀门控制资源使用：

- `SCRAPE_CONCURRENCY`：同时执行完整抓取流程的请求数，默认 `3`
- `BATCH_CONCURRENCY`：单个批量请求内同时处理的商品链接数，默认 `2`
- `RENDER_CONCURRENCY`：同时启动 Playwright 浏览器渲染的请求数，默认 `2`
- `AUCTION_DB_PATH`：拍卖列表 SQLite 数据库路径，默认 `SCRAPER_DATA_DIR/auction_lots.sqlite3`
- `DROUOT_API_TIMEOUT`：Drouot API 单页请求超时秒数，默认 `60`
- `DROUOT_API_RETRIES`：Drouot API 单页失败重试次数，默认 `4`
- `DROUOT_API_RETRY_DELAY`：Drouot API 重试递增等待秒数，默认 `2`
- `CF_BYPASS_ENABLED`：是否在 Cloudflare 验证页启用授权浏览器 fallback，默认 `true`
- `CF_BYPASS_CONCURRENCY`：同时运行 Cloudflare fallback 浏览器的数量，默认 `1`
- `CF_BYPASS_RETRIES`：验证按钮点击/等待次数，默认 `8`
- `CF_BYPASS_WAIT`：验证后额外等待秒数，默认 `8`
- `CF_BYPASS_PAGE_TIMEOUT`：Cloudflare fallback 单次页面加载超时秒数，默认 `60`
- `CF_BYPASS_HEADLESS`：Cloudflare fallback 是否无头运行，默认 `false`
- `CF_BROWSER_PATH`：Chrome/Chromium/Edge 可执行文件路径，自动识别失败时填写
- `CF_CHROMIUM_ARGS`：Cloudflare fallback 额外 Chromium 启动参数。Linux 下会自动追加 `--disable-dev-shm-usage`，root 用户会自动追加 `--no-sandbox`
- `IMAGE_UPLOAD_CONCURRENCY`：单个请求内并发上传图片数，默认 `6`
- `IMAGE_UPLOAD_TOTAL_CONCURRENCY`：全服务同时上传图片数，默认 `12`
- `XHS_IMAGE_PROCESS_CONCURRENCY`：小红书图片下载/裁剪/加 logo 的并发数，默认 `20`
- `ARK_REFERENCE_MAX_PIXELS`：Ark 封面扩图参考图最大像素数，超过时会先等比缩小再上传 SuperBed，默认 `36000000`
- `XHS_COPY_MAX_ATTEMPTS`：小红书标题和正文生成失败后的最大尝试次数，默认 `3`
- `XHS_COPY_RETRY_DELAY`：小红书标题和正文重试前等待秒数，默认 `1`
- `XHS_PUBLISH_MAX_ATTEMPTS`：小红书发布失败后的最大尝试次数，默认 `3`
- `XHS_PUBLISH_RETRY_DELAY`：小红书发布重试前等待秒数，默认 `2`
- `ARK_CHAT_COMPLETIONS_URL`：火山方舟对话补全接口地址，闲鱼文案使用。
- `XIANYU_COPY_MODEL`：闲鱼文案使用的 Doubao 2.0 Pro 模型，默认 `doubao-seed-2-0-pro-260215`
- `XIANYU_COPY_MAX_IMAGES`：传给闲鱼文案视觉模型的图片数量，默认 `4`，最大 `8`
- `XIANYU_COPY_MAX_ATTEMPTS`：闲鱼文案生成失败后的最大尝试次数，默认 `5`，最大 `5`
- `XIANYU_COPY_RETRY_DELAY`：闲鱼文案重试前等待秒数，默认 `1`
- `XHS_EXPAND_MAX_ATTEMPTS`：封面扩图最大尝试次数，默认 `3`
- `XHS_EXPAND_RETRY_DELAY`：封面扩图失败后的重试间隔秒数，默认 `5`

批量 `urls` 会并行处理，并保持响应结果顺序与输入链接顺序一致。超过并发上限的请求会同步等待空位。

## PM2 部署

先复制环境变量模板并填入密钥：

```bash
cp .env.example .env
```

安装依赖：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

如需 Cloudflare fallback，在服务器安装 Chrome/Chromium，并按需配置：

```bash
google-chrome --version || chromium --version
```

无桌面环境时建议使用 Xvfb 启动服务，或将 `CF_BYPASS_HEADLESS=true` 后测试目标站点是否能通过验证。部分 Cloudflare 验证在无头模式下可能仍会失败。

用 PM2 启动：

```bash
pm2 start .venv/bin/uvicorn --name zg-xhs-api --interpreter none -- app.main:app --host 0.0.0.0 --port 8000
```

如果目标站点的 Cloudflare 验证无法在 headless 模式通过，用 Xvfb 启动：

```bash
pm2 start /usr/bin/xvfb-run --name zg-xhs-api --interpreter none -- -a .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

修改 `.env` 后重启：

```bash
pm2 restart zg-xhs-api --update-env
```

服务地址：

```text
http://服务器IP:8000
```

查看日志：

```bash
pm2 logs zg-xhs-api --lines 200
```

排查单条链接时，先在日志里找到 `api event=received` 的 `request_id`，再按同一个 `request_id` 过滤后续阶段。常见阶段包括：

- `scrape stage=extract_start/extract_done`：商品页抓取和图片候选筛选。
- `extractor stage=fetch_static_* / fetch_rendered_*`：静态请求或 Playwright 渲染。
- `xhs stage=cover_expand_attempt_*`：封面扩图每一次尝试。
- `xhs stage=generate_copy_*`：DeepSeek 文案生成。
- `xhs stage=publish_*`：小红书发布接口。

封面需要扩图且扩图连续失败时，任务会失败返回错误，不再裁剪降级发布。

## 扩展其他网站

核心逻辑在：

```text
app/services/extractor.py
```

新增站点时优先补通用规则：

- JSON-LD / Shopify / WooCommerce 数据提取
- DOM 文本中的商品字段识别
- 图片候选打分规则
- 推荐商品、footer、联系图标、社媒图过滤规则

只有通用规则无法覆盖时，再加少量站点特定 fallback。
