const form = document.querySelector("#scrape-form");
const statusEl = document.querySelector("#status");
const resultEl = document.querySelector("#result");
const nameEl = document.querySelector("#product-name");
const dimensionsEl = document.querySelector("#product-dimensions");
const descriptionEl = document.querySelector("#product-description");
const detailsEl = document.querySelector("#product-details");
const imagesEl = document.querySelector("#images");

function setStatus(message) {
  statusEl.textContent = message;
}

function renderResult(data) {
  const productDetails = data.product_details || {};
  nameEl.textContent = data.name || "未识别商品名";
  dimensionsEl.textContent = data.dimensions ? `尺寸：${data.dimensions}` : "";
  descriptionEl.textContent = productDetails.description || "";
  imagesEl.innerHTML = "";
  detailsEl.innerHTML = "";

  for (const [key, value] of Object.entries(productDetails)) {
    if (key === "description") {
      continue;
    }
    const term = document.createElement("dt");
    term.textContent = key;

    const description = document.createElement("dd");
    description.textContent = typeof value === "object" ? JSON.stringify(value) : String(value);

    detailsEl.appendChild(term);
    detailsEl.appendChild(description);
  }

  for (const linkUrl of data.image_links || []) {
    const tile = document.createElement("article");
    tile.className = "tile";

    const img = document.createElement("img");
    img.src = linkUrl;
    img.alt = data.name || "Product image";
    tile.appendChild(img);

    const link = document.createElement("a");
    link.href = linkUrl;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = linkUrl;
    tile.appendChild(link);

    imagesEl.appendChild(tile);
  }

  resultEl.classList.remove("hidden");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = form.querySelector("button");
  button.disabled = true;
  resultEl.classList.add("hidden");
  setStatus("正在抓取，JS 页面可能需要十几秒...");

  const payload = {
    url: form.url.value,
    render: form.render.value,
    max_images: Number(form.max_images.value || 12),
    download_images: false
  };

  try {
    const response = await fetch("/api/scrape", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "抓取失败");
    }
    renderResult(data);
    setStatus(`完成：获取 ${data.image_links.length} 张图片。`);
  } catch (error) {
    setStatus(error.message);
  } finally {
    button.disabled = false;
  }
});
