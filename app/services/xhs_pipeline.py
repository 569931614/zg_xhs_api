from __future__ import annotations

import json
import mimetypes
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

from app.services.storage import USER_AGENT, upload_to_superbed


TARGET_RATIO = 3.0 / 4.0
DUOMI_CREATE_URL = "https://duomiapi.com/v1/images/generations?async=true"
DUOMI_TASK_URL_TEMPLATE = "https://duomiapi.com/v1/tasks/{task_id}"
DEFAULT_XHS_POST_API_BASE = "https://xhspost.aivip1.top"
DEFAULT_XHS_POST_API_KEY = "xhs_post"
TERMINAL_SUCCESS = {"succeeded", "success", "completed", "complete"}
TERMINAL_FAILURE = {"failed", "failure", "cancelled", "canceled", "error"}


class XHSPipelineError(RuntimeError):
    pass


@dataclass
class XHSPipelineResult:
    job_id: str
    title: str
    content: str
    share_link: str
    qrcode_link: str
    processed_image_paths: list[Path]


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return max(minimum, value)


def session() -> requests.Session:
    client = requests.Session()
    client.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    return client


def open_image_bytes(content: bytes) -> Image.Image:
    image = Image.open(BytesIO(content))
    image = ImageOps.exif_transpose(image)
    return to_rgb(image)


def to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "P":
        image = image.convert("RGBA") if image.info.get("transparency") else image.convert("RGB")
    if image.mode == "RGBA":
        return image.convert("RGB")
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def download_image(url: str) -> Image.Image:
    response = session().get(url, timeout=60)
    response.raise_for_status()
    return open_image_bytes(response.content)


def save_jpeg(image: Image.Image, path: Path, quality: int = 95) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    to_rgb(image).save(path, "JPEG", quality=quality, optimize=True)


def aspect_ratio(image: Image.Image) -> float:
    width, height = image.size
    return width / height


def crop_to_3_4(image: Image.Image) -> Image.Image:
    image = to_rgb(image)
    width, height = image.size
    current_ratio = width / height
    if abs(current_ratio - TARGET_RATIO) <= 0.01:
        return image
    if current_ratio > TARGET_RATIO:
        new_width = int(height * TARGET_RATIO)
        left = max(0, (width - new_width) // 2)
        return image.crop((left, 0, left + new_width, height))
    new_height = int(width / TARGET_RATIO)
    top = max(0, (height - new_height) // 2)
    return image.crop((0, top, width, top + new_height))


def detect_bottom_color(image: Image.Image, sample_height_ratio: float = 0.2) -> tuple[int, int, int]:
    image = to_rgb(image)
    width, height = image.size
    sample_height = max(1, int(height * sample_height_ratio))
    bottom_region = image.crop((0, height - sample_height, width, height))
    small = bottom_region.resize((50, 50), Image.Resampling.LANCZOS)
    pixels = list(small.getdata())
    avg_r = sum(pixel[0] for pixel in pixels) // len(pixels)
    avg_g = sum(pixel[1] for pixel in pixels) // len(pixels)
    avg_b = sum(pixel[2] for pixel in pixels) // len(pixels)
    return avg_r, avg_g, avg_b


def choose_logo_style_by_background(image: Image.Image) -> int:
    avg_color = detect_bottom_color(image, sample_height_ratio=0.07)
    brightness = 0.299 * avg_color[0] + 0.587 * avg_color[1] + 0.114 * avg_color[2]
    return 2 if brightness > 180 else 1


def load_logo_font(font_size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    app_dir = Path(__file__).resolve().parents[1]
    candidates = [
        app_dir / "assets" / "LiberationSans-Regular.ttf",
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), font_size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=font_size)
    except TypeError:
        return ImageFont.load_default()


def add_logo(image: Image.Image, logo_style: int | None = None) -> Image.Image:
    image = to_rgb(image)
    width, height = image.size
    font_size = max(12, int(33.0 * (width / 1242.0)))
    font = load_logo_font(font_size)
    draw = ImageDraw.Draw(image)
    text_color = (255, 255, 255) if (logo_style or choose_logo_style_by_background(image)) == 1 else (0, 0, 0)
    groups = ["COLLECTABLE DESIGN", "ZIQU", "VINTAGE FURNITURE"]
    text_widths = []
    for text in groups:
        bbox = draw.textbbox((0, 0), text, font=font)
        text_widths.append(bbox[2] - bbox[0])
    total_text_width = sum(text_widths)
    gap = max(8, (width - total_text_width) / (len(groups) + 1))
    text_top_y = int(height * (1571.0 / 1656.0))
    cursor_x = gap
    for index, text in enumerate(groups):
        draw.text((int(cursor_x), text_top_y), text, fill=text_color, font=font)
        cursor_x += text_widths[index] + gap
    return image


def product_text(product: dict[str, Any]) -> str:
    details = product.get("product_details") if isinstance(product.get("product_details"), dict) else {}
    lines = []
    if product.get("name"):
        lines.append(f"产品名称：{product['name']}")
    description = clean_product_detail_text(details.get("description", ""))
    if description:
        lines.append(f"描述：{description}")
    if product.get("dimensions"):
        lines.append(f"尺寸：{product['dimensions']}")
    for key, value in details.items():
        if key == "description" or value in (None, "", [], {}):
            continue
        key_text = str(key).strip()
        value_text = clean_product_detail_text(str(value))
        if is_noise_detail(key_text) or not value_text:
            continue
        lines.append(f"{key_text}：{value_text}")
    return "\n".join(lines)


def is_noise_detail(value: str) -> bool:
    normalized = value.lower()
    return any(
        marker in normalized
        for marker in (
            "email",
            "newsletter",
            "privacy",
            "cookie",
            "piva",
            "terms",
            "conditions",
            "job opportunities",
            "viale parioli",
        )
    )


def clean_product_detail_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    cut_markers = [
        "(+39)",
        "Email:",
        "©",
        "Newsletter",
        "Privacy policy",
        "Cookie Policy",
        "Job Opportunities",
        "Leave this field empty",
    ]
    for marker in cut_markers:
        index = text.lower().find(marker.lower())
        if index >= 0:
            text = text[:index].strip()
    if is_noise_detail(text) and len(text) < 160:
        return ""
    return text


def duomi_api_key() -> str:
    value = os.getenv("DUOMI_API_KEY") or os.getenv("DUOMI_API_TOKEN") or ""
    if not value:
        raise XHSPipelineError("DUOMI_API_KEY is not configured")
    return value


def duomi_request_json(method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"Authorization": duomi_api_key()}
    kwargs: dict[str, Any] = {"timeout": 60, "headers": headers}
    if body is not None:
        kwargs["json"] = body
    response = requests.request(method, url, **kwargs)
    if response.status_code >= 400:
        raise XHSPipelineError(f"Duomi HTTP {response.status_code}: {response.text[:500]}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise XHSPipelineError(f"Duomi returned non-JSON response: {response.text[:500]}") from exc
    if not isinstance(payload, dict):
        raise XHSPipelineError(f"Duomi returned unexpected response: {payload!r}")
    return payload


def wait_for_duomi_task(task_id: str) -> dict[str, Any]:
    interval = float(os.getenv("DUOMI_POLL_INTERVAL", "5"))
    timeout = float(os.getenv("DUOMI_TIMEOUT", "600"))
    deadline = time.monotonic() + timeout
    last_response: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last_response = duomi_request_json(
            "GET",
            DUOMI_TASK_URL_TEMPLATE.format(task_id=quote(task_id, safe="")),
        )
        state = str(last_response.get("state", "")).lower()
        if state in TERMINAL_SUCCESS:
            return last_response
        if state in TERMINAL_FAILURE:
            raise XHSPipelineError(f"Duomi task failed with state={state}: {last_response}")
        time.sleep(interval)
    raise XHSPipelineError(f"Timed out waiting for Duomi task {task_id}: {last_response}")


def extract_duomi_image_url(payload: dict[str, Any]) -> str:
    data = payload.get("data")
    images = data.get("images") if isinstance(data, dict) else None
    if isinstance(images, list):
        for item in images:
            if isinstance(item, dict) and item.get("url"):
                return str(item["url"])
    raise XHSPipelineError(f"Duomi response did not include an image URL: {payload}")


def expand_cover_with_duomi(reference_url: str, description: str) -> Image.Image:
    base_rule = (
        "Only expand the edges to make a clean 3:4 product photograph. Keep the original furniture "
        "unchanged. Extend the wall, floor, lighting, and background naturally. Do not add text, people, "
        "extra furniture, UI elements, logos, or unrelated objects."
    )
    prompt = f"{description}\n\n{base_rule}" if description else base_rule
    submitted = duomi_request_json(
        "POST",
        DUOMI_CREATE_URL,
        {
            "model": "gpt-image-2",
            "prompt": prompt,
            "size": "3:4",
            "image": [reference_url],
        },
    )
    task_id = submitted.get("id")
    if not task_id:
        raise XHSPipelineError(f"Duomi create response did not include task id: {submitted}")
    final = wait_for_duomi_task(str(task_id))
    generated_url = extract_duomi_image_url(final)
    return download_image(generated_url)


def process_cover(image_url: str, job_dir: Path, product: dict[str, Any]) -> tuple[int, Path, dict[str, Any]]:
    original_dir = job_dir / "xhs_originals"
    processed_dir = job_dir / "xhs_processed"
    original_path = original_dir / "original_cover.jpg"
    image = download_image(image_url)
    save_jpeg(image, original_path)
    meta: dict[str, Any] = {"source_url": image_url, "original_path": str(original_path)}

    if abs(aspect_ratio(image) - TARGET_RATIO) > 0.01:
        try:
            reference_session = session()
            reference_url = upload_to_superbed(original_path, reference_session)
            meta["duomi_reference_url"] = reference_url
            image = expand_cover_with_duomi(reference_url, product_text(product))
            meta["duomi_expanded"] = True
        except Exception as exc:
            meta["duomi_expanded"] = False
            meta["duomi_error"] = str(exc)
            image = crop_to_3_4(image)
    else:
        meta["duomi_expanded"] = False

    image = crop_to_3_4(image)
    image = add_logo(image, choose_logo_style_by_background(image))
    output_path = processed_dir / "cover_3x4_logo.jpg"
    save_jpeg(image, output_path)
    return 0, output_path, meta


def process_gallery_image(index: int, image_url: str, job_dir: Path) -> tuple[int, Path, dict[str, Any]]:
    original_dir = job_dir / "xhs_originals"
    processed_dir = job_dir / "xhs_processed"
    original_path = original_dir / f"original_{index + 1:02d}.jpg"
    image = download_image(image_url)
    save_jpeg(image, original_path)
    image = crop_to_3_4(image)
    image = add_logo(image, choose_logo_style_by_background(image))
    output_path = processed_dir / f"image_{index + 1:02d}_3x4_logo.jpg"
    save_jpeg(image, output_path)
    return index, output_path, {"source_url": image_url, "original_path": str(original_path)}


def process_images_for_xhs(image_urls: list[str], product: dict[str, Any], job_dir: Path) -> tuple[list[Path], list[dict[str, Any]]]:
    image_urls = [url for url in image_urls if url][:12]
    if not image_urls:
        raise XHSPipelineError("No product images were found for XHS note creation")

    workers = min(env_int("XHS_IMAGE_PROCESS_CONCURRENCY", 20), len(image_urls))
    results: dict[int, Path] = {}
    metadata: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_cover, image_urls[0], job_dir, product): 0}
        for index, image_url in enumerate(image_urls[1:], start=1):
            futures[executor.submit(process_gallery_image, index, image_url, job_dir)] = index
        for future in as_completed(futures):
            index, path, meta = future.result()
            results[index] = path
            metadata[index] = meta
    return [results[index] for index in sorted(results)], [metadata[index] for index in sorted(metadata)]


def deepseek_api_key() -> str:
    value = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not value:
        raise XHSPipelineError("DEEPSEEK_API_KEY is not configured")
    return value


def generate_xhs_copy(product: dict[str, Any]) -> tuple[str, str]:
    system_prompt = """你是旨丘画廊的CMO，请为这件作品写一段文案用于小红书（标题及内容）要求如下：

输出格式要求：
- 标题单独占第一行
- 空一行后，再开始正文内容
- 正文段落之间都要空一行

1、标题共20个字，前三个字为"旨丘｜"，后17个字请参考作品特色去描写，同时带出其品类。最特色卖点与品类之间以"·"隔开。标题不要出现"法式"这类国家+式的说法，不要出现"复古"，可用年代或中古代替。
2、内容第一段写能获取到的基本信息，包括国家、年代、设计师、制造商、品类、材质与特征；没有的信息不要硬写。段首以"旨丘在售的这…"开始。
3、内容第二段描写造型特点、使用场景和实用性，语言优美流畅，直击最突出的特点。
4、内容第三段结合产品信息描写独特性和收藏性，以画廊专业角度去写。
5、最后一段需严格按照以下格式：
【藏品信息Information 】
*产品英文名
*尺寸：xxx
✅Available/在售
欢迎详询@旨丘，
解锁🔓更多精彩选品！
——————————————————
国内展厅：
📍上海｜上海市黄浦陕西南路陕南邨169号4F
📍广州｜广州市越秀区东山街道3号橄榄山
6、整体文字必须参考所有产品信息来修改润色并扩充，不要遗漏信息，不要列序号。正文前三段共约250字，最后不要加无关文字。
7、标题参考时尚编辑叙事角度，不要出现"收藏级"三字，可用"值得收藏"替代。
8、在正文【藏品信息Information 】结束之后，再空一行，生成3-6个小红书话题标签。标签格式：#标签名[话题]#。必须包含：中古家具、空间美学、上海中古家具。根据品类生成"中古+品类名"标签，再生成1-2个特色标签。所有标签用空格隔开放在同一行。
9、文案中只允许出现"旨丘"作为店铺/品牌名称，禁止出现其他任何店铺名、网站名、出品方名称。
10、正文部分除产品名称外禁止出现不必要英文单词。"""
    user_prompt = f"这是产品的详细信息：\n{product_text(product)}\n\n请根据以上信息生成小红书标题和正文。"
    base_url = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com").rstrip("/")
    response = requests.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {deepseek_api_key()}",
            "Content-Type": "application/json",
        },
        json={
            "model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
        },
        timeout=120,
    )
    if response.status_code >= 400:
        raise XHSPipelineError(f"DeepSeek HTTP {response.status_code}: {response.text[:500]}")
    payload = response.json()
    content = str(payload["choices"][0]["message"]["content"]).strip()
    parts = [part.strip() for part in content.splitlines() if part.strip()]
    if not parts:
        raise XHSPipelineError("DeepSeek returned empty content")
    title = parts[0]
    body = content[len(title) :].strip()
    return title, body or content


def xhs_api_base() -> str:
    return os.getenv("XHS_POST_API_BASE", DEFAULT_XHS_POST_API_BASE).rstrip("/")


def publish_xhs_note(title: str, content: str, image_paths: list[Path]) -> tuple[dict[str, Any], str]:
    api_base = xhs_api_base()
    identifier = f"product-scraper-xhs-{int(time.time())}"
    data = {
        "title": title,
        "content": content,
        "type": "normal",
        "identifier": identifier,
        "activityId": identifier,
        "userId": "1",
    }
    with ExitStack() as stack:
        files = []
        for path in image_paths:
            handle = stack.enter_context(path.open("rb"))
            files.append(
                (
                    "images",
                    (path.name, handle, mimetypes.guess_type(path.name)[0] or "image/jpeg"),
                )
            )
        response = requests.post(
            f"{api_base}/api/xhs-auto/notes",
            headers={"x-api-key": os.getenv("XHS_POST_API_KEY", DEFAULT_XHS_POST_API_KEY)},
            data=data,
            files=files,
            timeout=240,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise XHSPipelineError(f"XHS publisher returned non-JSON response: {response.text[:500]}") from exc
    if response.status_code >= 400 or payload.get("code") != 200:
        raise XHSPipelineError(f"XHS publisher failed: HTTP {response.status_code}: {payload}")
    note_data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    share_link = str(note_data.get("shareLink") or "")
    if not share_link and note_data.get("id"):
        share_link = f"{api_base}/#/xhs-auto-api?id={note_data['id']}"
    if not share_link:
        raise XHSPipelineError(f"XHS publisher response did not include shareLink: {payload}")
    return payload, share_link


def create_xhs_note(product: dict[str, Any], job_id: str, job_dir: Path) -> XHSPipelineResult:
    image_urls = list(product.get("image_links") or [])
    processed_paths, image_metadata = process_images_for_xhs(image_urls, product, job_dir)
    title, content = generate_xhs_copy(product)
    publish_response, share_link = publish_xhs_note(title, content, processed_paths)
    qrcode_link = f"{xhs_api_base()}/api/html-render/qrcode?size=320&data={quote(share_link, safe='')}"

    result = {
        "job_id": job_id,
        "title": title,
        "content": content,
        "share_link": share_link,
        "qrcode_link": qrcode_link,
        "processed_image_paths": [str(path) for path in processed_paths],
        "image_metadata": image_metadata,
        "publish_response": publish_response,
    }
    (job_dir / "xhs_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(job_dir / "xhs_originals", ignore_errors=True)
    return XHSPipelineResult(
        job_id=job_id,
        title=title,
        content=content,
        share_link=share_link,
        qrcode_link=qrcode_link,
        processed_image_paths=processed_paths,
    )
