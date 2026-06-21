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

```http
POST /api/scrape
Content-Type: application/json
```

请求：

```json
{
  "url": "https://www.monumentgallery.co.uk/product/garbo-fringe-lamps-by-mariyo-yagi",
  "render": "auto",
  "max_images": 7,
  "download_images": true
}
```

`render` 可选：

- `auto`：先静态抓取，不够好再浏览器渲染。
- `always`：总是浏览器渲染。
- `never`：只静态抓取。

响应包含：

- `product.name`
- `product.price`
- `product.description`
- `product.details`
- `images[].url`
- `images[].hosted_url`
- `images[].storage_provider`
- `images[].local_url`
- `result_url`

## 图片上传

服务会先把筛选后的产品图上传到 Super 图床；如果 Super 图床上传失败，会自动上传到阿里云 OSS。

Super 图床配置：

```bash
SUPERBED_UPLOAD_URL=https://api.superbed.cc/upload
SUPERBED_TOKEN=你的super图床token
SUPERBED_CATEGORIES=product-scraper
```

阿里云 OSS 兜底配置：

```bash
ALI_OSS_ACCESS_KEY_ID=你的AccessKeyId
ALI_OSS_ACCESS_KEY_SECRET=你的AccessKeySecret
ALI_OSS_ENDPOINT=oss-cn-hangzhou.aliyuncs.com
ALI_OSS_BUCKET=你的bucket名称
ALI_OSS_PUBLIC_BASE_URL=https://你的访问域名
ALI_OSS_PREFIX=product-scraper
```

如果两个上传渠道都不可用，接口仍会返回本地 `local_url`，并在对应图片里写入 `upload_error`，方便排查环境变量或权限。

上传成功时，图片字段会包含：

```json
{
  "hosted_url": "https://...",
  "storage_provider": "superbed"
}
```

如果 Super 图床失败但 OSS 成功，`storage_provider` 会是 `aliyun-oss`。

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
