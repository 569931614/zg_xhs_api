const form = document.querySelector("#scrape-form");
const statusEl = document.querySelector("#status");
const resultEl = document.querySelector("#result");
const nameEl = document.querySelector("#product-name");
const priceEl = document.querySelector("#product-price");
const descriptionEl = document.querySelector("#product-description");
const jsonLink = document.querySelector("#json-link");
const imagesEl = document.querySelector("#images");

function setStatus(message) {
  statusEl.textContent = message;
}

function renderResult(data) {
  const product = data.product || {};
  nameEl.textContent = product.name || "未识别商品名";
  priceEl.textContent = product.price || product.currency || "";
  descriptionEl.textContent = product.description || "";
  jsonLink.href = data.result_url;
  imagesEl.innerHTML = "";

  for (const image of data.images || []) {
    const tile = document.createElement("article");
    tile.className = "tile";

    const img = document.createElement("img");
    img.src = image.hosted_url || image.local_url || image.url;
    img.alt = image.alt || product.name || "Product image";
    tile.appendChild(img);

    const link = document.createElement("a");
    link.href = image.hosted_url || image.local_url || image.url;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = image.storage_provider ? `${image.storage_provider}: ${image.hosted_url}` : (image.filename || image.url);
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
    max_images: Number(form.max_images.value || 40),
    download_images: true
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
    setStatus(`完成：${data.rendered ? "已使用浏览器渲染" : "静态抓取"}，获取 ${data.images.length} 张图片。`);
  } catch (error) {
    setStatus(error.message);
  } finally {
    button.disabled = false;
  }
});
