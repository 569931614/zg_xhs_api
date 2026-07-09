# Product Scraper Service

通过商品页链接提取商品名、图片链接、尺寸和产品详情，过滤推荐商品图、联系图标、logo、社媒图、支付图标等干扰资产。

## 技术选型

- FastAPI：对外提供 HTTP API，部署简单。
- Playwright：处理 React / Next.js 等 JS 渲染商品页。
- BeautifulSoup + JSON-LD：优先读取结构化商品数据，速度快。
- Docker：便于部署到云服务器。

## 本地运行

```powershell
cd "C:\Users\56993\Documents\Codex\2026-06-15\skill\outputs\product-scraper-service"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

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
- `image_links`：图片链接，优先返回上传后的图床链接
- `dimensions`：尺寸
- `product_details`：产品详情，包含描述和其他可识别详情字段

服务会在 `render=auto` 时先静态抓取；如果商品信息、图片或尺寸信息不足，会自动再用浏览器渲染抓取一次。尺寸缺失不会阻断返回。

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
  "max_images": 12,
  "min_score": 25
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
  "title": "旨丘｜...",
  "content": "旨丘在售的这...",
  "result_path": "/data/1783607518-7b810176bd/xhs_result.json"
}
```

遇到 Cloudflare / bot verification 等服务端无法通过的反爬页面时，接口会返回 `502`，错误信息类似：

```json
{
  "detail": "Failed to scrape page: Blocked by Cloudflare/security verification page"
}
```

## 图片链接

服务会先下载抓取到的主商品图到 job 临时目录，再上传到 SuperBed 图床。上传完成后会删除本地临时图片，并在 `image_links` 返回 SuperBed 图片链接。

单张图片下载或上传失败时，接口不会中断整条商品结果，会回退使用原始图片链接。

需要配置：

```env
SUPERBED_UPLOAD_URL=https://api.superbed.cc/upload
SUPERBED_TOKEN=你的SuperBed token
SUPERBED_CATEGORIES=product-scraper
IMAGE_DOWNLOAD_MAX_BYTES=31457280
IMAGE_UPLOAD_CONCURRENCY=6
IMAGE_UPLOAD_TOTAL_CONCURRENCY=12
SCRAPE_CONCURRENCY=3
BATCH_CONCURRENCY=2
RENDER_CONCURRENCY=2
XHS_IMAGE_PROCESS_CONCURRENCY=20
XHS_USE_EXPAND=true
XHS_EXPAND_PROVIDER=ark
ARK_API_KEY=你的火山Ark API key
ARK_IMAGE_MODEL=doubao-seedream-5-0-260128
ARK_IMAGE_SIZE=1728x2304
ARK_IMAGE_TIMEOUT=600
DUOMI_API_KEY=你的多米API key
XHS_USE_DUOMI_EXPAND=true
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
- `IMAGE_UPLOAD_CONCURRENCY`：单个请求内并发上传图片数，默认 `6`
- `IMAGE_UPLOAD_TOTAL_CONCURRENCY`：全服务同时上传图片数，默认 `12`
- `XHS_IMAGE_PROCESS_CONCURRENCY`：小红书图片下载/裁剪/加 logo 的并发数，默认 `20`

批量 `urls` 会并行处理，并保持响应结果顺序与输入链接顺序一致。超过并发上限的请求会同步等待空位。

## Docker 部署

先复制环境变量模板并填入密钥：

```bash
cp .env.example .env
```

```bash
docker compose up -d --build
```

服务地址：

```text
http://服务器IP:8000
```

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
