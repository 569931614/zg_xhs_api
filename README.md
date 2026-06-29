# Product Scraper Service

通过商品页链接提取商品名、价格、描述和主商品图，过滤推荐商品图、联系图标、logo、社媒图、支付图标等干扰资产。

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

`render` 可选：

- `auto`：先静态抓取，不够好再浏览器渲染。
- `always`：总是浏览器渲染。
- `never`：只静态抓取。

`max_images` 可选，范围 `1-12`，服务最多返回 12 张主商品图。

响应包含：

- `skipped`：保留兼容字段，当前默认不因尺寸缺失跳过
- `skip_reason`
- `product.name`
- `product.price`
- `product.dimensions`
- `product.description`
- `product.details`
- `images[].url`
- `images[].hosted_url`
- `result_url`

服务会在 `render=auto` 时先静态抓取；如果商品信息、图片或尺寸信息不足，会自动再用浏览器渲染抓取一次。尺寸缺失不会阻断返回。

遇到 Cloudflare / bot verification 等服务端无法通过的反爬页面时，接口会返回 `502`，错误信息类似：

```json
{
  "detail": "Failed to scrape page: Blocked by Cloudflare/security verification page"
}
```

## 图片链接

服务会直接返回抓取到的原图链接，`images[].hosted_url` 与 `images[].url` 一致。

服务不再下载图片到本地缓存；`download_images` 参数保留用于兼容旧请求，但不会触发下载。

```json
{
  "url": "https://...",
  "hosted_url": "https://..."
}
```

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
