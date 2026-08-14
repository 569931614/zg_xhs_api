from __future__ import annotations

import json
import logging
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
from urllib.parse import quote, urlparse

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

from app.services.storage import USER_AGENT, upload_to_superbed


TARGET_RATIO = 3.0 / 4.0
DUOMI_CREATE_URL = "https://duomiapi.com/v1/images/generations?async=true"
DUOMI_TASK_URL_TEMPLATE = "https://duomiapi.com/v1/tasks/{task_id}"
ARK_IMAGE_GENERATION_URL = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
ARK_RESPONSES_URL = "https://ark.cn-beijing.volces.com/api/v3/responses"
DEFAULT_ARK_IMAGE_MODEL = "doubao-seedream-5-0-260128"
DEFAULT_ARK_IMAGE_SIZE = "1728x2304"
DEFAULT_ARK_REFERENCE_MAX_PIXELS = 36_000_000
DEFAULT_XHS_COPY_MODEL = "deepseek-v4-flash-260425"
DEFAULT_XHS_POST_API_BASE = "https://xhspost.aivip1.top"
DEFAULT_XHS_POST_API_KEY = "xhs_post"
TERMINAL_SUCCESS = {"succeeded", "success", "completed", "complete"}
TERMINAL_FAILURE = {"failed", "failure", "cancelled", "canceled", "error"}
COVER_CROP_ONLY_DOMAINS = {"eliaselias.dk"}
logger = logging.getLogger("uvicorn.error")


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


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = float(raw_value)
    except ValueError:
        return default
    return max(minimum, value)


def env_bool(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


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


def resize_to_max_pixels(image: Image.Image, max_pixels: int) -> tuple[Image.Image, bool]:
    image = to_rgb(image)
    width, height = image.size
    if width * height <= max_pixels:
        return image, False

    scale = (max_pixels / float(width * height)) ** 0.5
    resized_width = max(1, int(width * scale))
    resized_height = max(1, int(height * scale))
    while resized_width * resized_height > max_pixels:
        if resized_width >= resized_height:
            resized_width -= 1
        else:
            resized_height -= 1
    return image.resize((resized_width, resized_height), Image.Resampling.LANCZOS), True


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


def ark_api_key() -> str:
    value = os.getenv("ARK_API_KEY") or os.getenv("VOLCENGINE_ARK_API_KEY") or ""
    if not value:
        raise XHSPipelineError("ARK_API_KEY is not configured")
    return value


def expand_prompt(description: str) -> str:
    return (
        "Outpaint the provided image to a 3:4 product photograph by extending only the existing edge "
        "pixels, textures, perspective lines, wall, floor, and lighting already visible in the source. "
        "Preserve the original product exactly. Do not add, invent, complete, repair, redesign, or "
        "supplement anything. Do not add blocks, patches, panels, corners, trim, decorations, props, "
        "extra furniture, people, text, logos, watermarks, UI elements, shadows, highlights, color fields, "
        "or any object or background detail that is not directly implied by the source image edges. "
        "The new canvas area must look like a seamless continuation of the existing image only."
    )


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


def wait_for_duomi_task(task_id: str, request_id_value: str | None = None, attempt: int = 1) -> dict[str, Any]:
    interval = float(os.getenv("DUOMI_POLL_INTERVAL", "5"))
    timeout = float(os.getenv("DUOMI_TIMEOUT", "600"))
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    last_response: dict[str, Any] | None = None
    logger.info(
        "xhs stage=duomi_wait_start request_id=%s attempt=%d task_id=%s timeout=%.2fs interval=%.2fs",
        request_id_value,
        attempt,
        task_id,
        timeout,
        interval,
    )
    while time.monotonic() < deadline:
        last_response = duomi_request_json(
            "GET",
            DUOMI_TASK_URL_TEMPLATE.format(task_id=quote(task_id, safe="")),
        )
        state = str(last_response.get("state", "")).lower()
        if state in TERMINAL_SUCCESS:
            logger.info(
                "xhs stage=duomi_wait_done request_id=%s attempt=%d task_id=%s state=%s elapsed=%.2fs",
                request_id_value,
                attempt,
                task_id,
                state,
                time.monotonic() - started,
            )
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


def expand_cover_with_duomi(
    reference_url: str,
    description: str,
    request_id_value: str | None = None,
    attempt: int = 1,
) -> Image.Image:
    started = time.monotonic()
    logger.info(
        "xhs stage=duomi_expand_start request_id=%s attempt=%d",
        request_id_value,
        attempt,
    )
    submitted = duomi_request_json(
        "POST",
        DUOMI_CREATE_URL,
        {
            "model": "gpt-image-2",
            "prompt": expand_prompt(description),
            "size": "3:4",
            "image": [reference_url],
        },
    )
    task_id = submitted.get("id")
    if not task_id:
        raise XHSPipelineError(f"Duomi create response did not include task id: {submitted}")
    logger.info(
        "xhs stage=duomi_expand_submitted request_id=%s attempt=%d task_id=%s",
        request_id_value,
        attempt,
        task_id,
    )
    final = wait_for_duomi_task(str(task_id), request_id_value, attempt)
    generated_url = extract_duomi_image_url(final)
    image = download_image(generated_url)
    logger.info(
        "xhs stage=duomi_expand_done request_id=%s attempt=%d task_id=%s elapsed=%.2fs",
        request_id_value,
        attempt,
        task_id,
        time.monotonic() - started,
    )
    return image


def extract_ark_image_url(payload: dict[str, Any]) -> str:
    data = payload.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("url"):
                return str(item["url"])
    if isinstance(data, dict):
        images = data.get("images")
        if isinstance(images, list):
            for item in images:
                if isinstance(item, dict) and item.get("url"):
                    return str(item["url"])
        if data.get("url"):
            return str(data["url"])
    raise XHSPipelineError(f"Ark response did not include an image URL: {payload}")


def expand_cover_with_ark(
    reference_url: str,
    description: str,
    request_id_value: str | None = None,
    attempt: int = 1,
) -> Image.Image:
    started = time.monotonic()
    timeout = float(os.getenv("ARK_IMAGE_TIMEOUT", "600"))
    logger.info(
        "xhs stage=ark_expand_start request_id=%s attempt=%d model=%s size=%s timeout=%.2fs",
        request_id_value,
        attempt,
        os.getenv("ARK_IMAGE_MODEL", DEFAULT_ARK_IMAGE_MODEL),
        os.getenv("ARK_IMAGE_SIZE", DEFAULT_ARK_IMAGE_SIZE),
        timeout,
    )
    response = requests.post(
        os.getenv("ARK_IMAGE_GENERATION_URL", ARK_IMAGE_GENERATION_URL),
        headers={
            "Authorization": f"Bearer {ark_api_key()}",
            "Content-Type": "application/json",
        },
        json={
            "model": os.getenv("ARK_IMAGE_MODEL", DEFAULT_ARK_IMAGE_MODEL),
            "prompt": expand_prompt(description),
            "image": reference_url,
            "sequential_image_generation": "disabled",
            "response_format": "url",
            "size": os.getenv("ARK_IMAGE_SIZE", DEFAULT_ARK_IMAGE_SIZE),
            "stream": False,
            "watermark": False,
        },
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise XHSPipelineError(f"Ark HTTP {response.status_code}: {response.text[:500]}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise XHSPipelineError(f"Ark returned non-JSON response: {response.text[:500]}") from exc
    if not isinstance(payload, dict):
        raise XHSPipelineError(f"Ark returned unexpected response: {payload!r}")
    generated_url = extract_ark_image_url(payload)
    image = download_image(generated_url)
    logger.info(
        "xhs stage=ark_expand_done request_id=%s attempt=%d elapsed=%.2fs",
        request_id_value,
        attempt,
        time.monotonic() - started,
    )
    return image


def expand_provider() -> str:
    provider = os.getenv("XHS_EXPAND_PROVIDER", "ark").strip().lower()
    return provider or "ark"


def expand_enabled() -> bool:
    return env_bool("XHS_USE_EXPAND", env_bool("XHS_USE_DUOMI_EXPAND", True))


def ark_reference_max_pixels() -> int:
    return env_int("ARK_REFERENCE_MAX_PIXELS", DEFAULT_ARK_REFERENCE_MAX_PIXELS)


def normalized_hostname(url: str) -> str:
    hostname = (urlparse(url).hostname or "").lower()
    return hostname.removeprefix("www.")


def host_matches_domain(hostname: str, domain: str) -> bool:
    hostname = hostname.removeprefix("www.")
    domain = domain.lower().removeprefix("www.")
    return hostname == domain or hostname.endswith(f".{domain}")


def cover_crop_only_reason(product: dict[str, Any], image_url: str) -> str:
    urls = [
        str(product.get("source_url") or ""),
        str(product.get("page_url") or ""),
        str(product.get("url") or ""),
        image_url,
    ]
    for url in urls:
        hostname = normalized_hostname(url)
        if not hostname:
            continue
        for domain in COVER_CROP_ONLY_DOMAINS:
            if host_matches_domain(hostname, domain):
                return f"source domain {domain} uses direct 3:4 crop"
    return ""


def expand_cover(
    reference_url: str,
    description: str,
    request_id_value: str | None = None,
    attempt: int = 1,
) -> tuple[str, Image.Image]:
    provider = expand_provider()
    if provider == "ark":
        return provider, expand_cover_with_ark(reference_url, description, request_id_value, attempt)
    if provider == "duomi":
        return provider, expand_cover_with_duomi(reference_url, description, request_id_value, attempt)
    raise XHSPipelineError(f"Unsupported XHS_EXPAND_PROVIDER: {provider}")


def expand_max_attempts() -> int:
    return env_int("XHS_EXPAND_MAX_ATTEMPTS", 3)


def expand_retry_delay() -> float:
    return env_float("XHS_EXPAND_RETRY_DELAY", 5.0)


def expand_cover_with_retries(
    original_path: Path,
    product: dict[str, Any],
    request_id_value: str | None = None,
    job_id: str | None = None,
) -> tuple[str, Image.Image, str, int]:
    attempts = expand_max_attempts()
    delay = expand_retry_delay()
    last_error: Exception | None = None
    text = product_text(product)
    for attempt in range(1, attempts + 1):
        attempt_started = time.monotonic()
        try:
            logger.info(
                "xhs stage=cover_expand_attempt_start request_id=%s job_id=%s attempt=%d max_attempts=%d provider=%s",
                request_id_value,
                job_id,
                attempt,
                attempts,
                expand_provider(),
            )
            reference_session = session()
            reference_url = upload_to_superbed(original_path, reference_session)
            provider, image = expand_cover(reference_url, text, request_id_value, attempt)
            logger.info(
                "xhs stage=cover_expand_attempt_done request_id=%s job_id=%s attempt=%d provider=%s elapsed=%.2fs",
                request_id_value,
                job_id,
                attempt,
                provider,
                time.monotonic() - attempt_started,
            )
            return provider, image, reference_url, attempt
        except Exception as exc:
            last_error = exc
            logger.exception(
                "xhs stage=cover_expand_attempt_failed request_id=%s job_id=%s attempt=%d max_attempts=%d provider=%s elapsed=%.2fs",
                request_id_value,
                job_id,
                attempt,
                attempts,
                expand_provider(),
                time.monotonic() - attempt_started,
            )
            if attempt < attempts and delay > 0:
                time.sleep(delay)
    raise XHSPipelineError(f"Cover expansion failed after {attempts} attempts: {last_error}") from last_error


def xhs_copy_max_attempts() -> int:
    raw_value = os.getenv("XHS_COPY_MAX_ATTEMPTS", "3").strip()
    try:
        return max(1, min(5, int(raw_value)))
    except ValueError:
        return 3


def xhs_copy_retry_delay() -> float:
    return env_float("XHS_COPY_RETRY_DELAY", 1.0)


def xhs_publish_max_attempts() -> int:
    raw_value = os.getenv("XHS_PUBLISH_MAX_ATTEMPTS", "3").strip()
    try:
        return max(1, min(5, int(raw_value)))
    except ValueError:
        return 3


def xhs_publish_retry_delay() -> float:
    return env_float("XHS_PUBLISH_RETRY_DELAY", 2.0)


def process_cover(
    image_url: str,
    job_dir: Path,
    product: dict[str, Any],
    request_id_value: str | None = None,
    job_id: str | None = None,
) -> tuple[int, Path, dict[str, Any]]:
    started = time.monotonic()
    original_dir = job_dir / "xhs_originals"
    processed_dir = job_dir / "xhs_processed"
    original_path = original_dir / "original_cover.jpg"
    logger.info(
        "xhs stage=cover_process_start request_id=%s job_id=%s source_host=%s",
        request_id_value,
        job_id,
        urlparse(image_url).netloc,
    )
    image = download_image(image_url)
    save_jpeg(image, original_path)
    meta: dict[str, Any] = {"source_url": image_url, "original_path": str(original_path)}
    expand_reference_path = original_path
    ratio = aspect_ratio(image)
    logger.info(
        "xhs stage=cover_downloaded request_id=%s job_id=%s size=%sx%s ratio=%.4f",
        request_id_value,
        job_id,
        image.size[0],
        image.size[1],
        ratio,
    )

    if abs(ratio - TARGET_RATIO) > 0.01:
        crop_only_reason = cover_crop_only_reason(product, image_url)
        if crop_only_reason:
            meta["expanded"] = False
            meta["expand_skipped"] = crop_only_reason
            image = crop_to_3_4(image)
            logger.info(
                "xhs stage=cover_expand_skipped request_id=%s job_id=%s reason=%s",
                request_id_value,
                job_id,
                crop_only_reason,
            )
        elif expand_enabled():
            provider = expand_provider()
            if provider == "ark":
                max_pixels = ark_reference_max_pixels()
                reference_image, resized = resize_to_max_pixels(image, max_pixels)
                if resized:
                    original_width, original_height = image.size
                    reference_width, reference_height = reference_image.size
                    expand_reference_path = original_dir / "ark_reference_cover.jpg"
                    save_jpeg(reference_image, expand_reference_path)
                    meta["ark_reference_path"] = str(expand_reference_path)
                    meta["ark_reference_original_size"] = f"{original_width}x{original_height}"
                    meta["ark_reference_size"] = f"{reference_width}x{reference_height}"
                    meta["ark_reference_max_pixels"] = max_pixels
                    logger.info(
                        "xhs stage=cover_ark_reference_resized request_id=%s job_id=%s original_size=%sx%s reference_size=%sx%s max_pixels=%d",
                        request_id_value,
                        job_id,
                        original_width,
                        original_height,
                        reference_width,
                        reference_height,
                        max_pixels,
                    )
            provider, image, reference_url, attempts_used = expand_cover_with_retries(
                expand_reference_path,
                product,
                request_id_value,
                job_id,
            )
            meta["expand_provider"] = provider
            meta["expand_reference_url"] = reference_url
            meta["expand_attempts"] = attempts_used
            meta["expanded"] = True
        else:
            meta["expanded"] = False
            meta["expand_skipped"] = "XHS_USE_EXPAND is disabled"
            image = crop_to_3_4(image)
    else:
        meta["expanded"] = False

    image = crop_to_3_4(image)
    image = add_logo(image, choose_logo_style_by_background(image))
    output_path = processed_dir / "cover_3x4_logo.jpg"
    save_jpeg(image, output_path)
    logger.info(
        "xhs stage=cover_process_done request_id=%s job_id=%s expanded=%s elapsed=%.2fs",
        request_id_value,
        job_id,
        meta["expanded"],
        time.monotonic() - started,
    )
    return 0, output_path, meta


def process_gallery_image(
    index: int,
    image_url: str,
    job_dir: Path,
    request_id_value: str | None = None,
    job_id: str | None = None,
) -> tuple[int, Path, dict[str, Any]]:
    started = time.monotonic()
    original_dir = job_dir / "xhs_originals"
    processed_dir = job_dir / "xhs_processed"
    original_path = original_dir / f"original_{index + 1:02d}.jpg"
    logger.info(
        "xhs stage=gallery_process_start request_id=%s job_id=%s index=%d source_host=%s",
        request_id_value,
        job_id,
        index,
        urlparse(image_url).netloc,
    )
    image = download_image(image_url)
    save_jpeg(image, original_path)
    image = crop_to_3_4(image)
    image = add_logo(image, choose_logo_style_by_background(image))
    output_path = processed_dir / f"image_{index + 1:02d}_3x4_logo.jpg"
    save_jpeg(image, output_path)
    logger.info(
        "xhs stage=gallery_process_done request_id=%s job_id=%s index=%d elapsed=%.2fs",
        request_id_value,
        job_id,
        index,
        time.monotonic() - started,
    )
    return index, output_path, {"source_url": image_url, "original_path": str(original_path)}


def process_images_for_xhs(
    image_urls: list[str],
    product: dict[str, Any],
    job_dir: Path,
    request_id_value: str | None = None,
    job_id: str | None = None,
) -> tuple[list[Path], list[dict[str, Any]]]:
    started = time.monotonic()
    image_urls = [url for url in image_urls if url][:12]
    if not image_urls:
        raise XHSPipelineError("No product images were found for XHS note creation")

    workers = min(env_int("XHS_IMAGE_PROCESS_CONCURRENCY", 20), len(image_urls))
    results: dict[int, Path] = {}
    metadata: dict[int, dict[str, Any]] = {}
    logger.info(
        "xhs stage=process_images_start request_id=%s job_id=%s images=%d workers=%d expand_enabled=%s provider=%s",
        request_id_value,
        job_id,
        len(image_urls),
        workers,
        expand_enabled(),
        expand_provider(),
    )
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(process_cover, image_urls[0], job_dir, product, request_id_value, job_id): 0
        }
        for index, image_url in enumerate(image_urls[1:], start=1):
            futures[executor.submit(process_gallery_image, index, image_url, job_dir, request_id_value, job_id)] = index
        for future in as_completed(futures):
            index, path, meta = future.result()
            results[index] = path
            metadata[index] = meta
    logger.info(
        "xhs stage=process_images_done request_id=%s job_id=%s images=%d workers=%d elapsed=%.2fs",
        request_id_value,
        job_id,
        len(image_urls),
        workers,
        time.monotonic() - started,
    )
    return [results[index] for index in sorted(results)], [metadata[index] for index in sorted(metadata)]


def extract_response_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    texts: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    text = part.get("text")
                    if isinstance(text, str) and text.strip():
                        texts.append(text.strip())
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())

    if texts:
        return "\n".join(texts).strip()
    raise XHSPipelineError(f"Ark responses output did not include text: {payload}")


def generate_xhs_copy(product: dict[str, Any], request_id_value: str | None = None, job_id: str | None = None) -> tuple[str, str]:
    started = time.monotonic()
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
    model = os.getenv("XHS_COPY_MODEL") or os.getenv("DEEPSEEK_MODEL") or DEFAULT_XHS_COPY_MODEL
    url = os.getenv("ARK_RESPONSES_URL", ARK_RESPONSES_URL)
    attempts = xhs_copy_max_attempts()
    retry_delay = xhs_copy_retry_delay()
    prompt_base = f"{system_prompt}\n\n{user_prompt}"
    headers = {
        "Authorization": f"Bearer {ark_api_key()}",
        "Content-Type": "application/json",
    }
    for attempt in range(1, attempts + 1):
        attempt_started = time.monotonic()
        logger.info(
            "xhs stage=generate_copy_attempt_start request_id=%s job_id=%s attempt=%d max_attempts=%d provider=ark model=%s",
            request_id_value,
            job_id,
            attempt,
            attempts,
            model,
        )
        try:
            response = requests.post(
                url,
                headers=headers,
                json={
                    "model": model,
                    "stream": False,
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": prompt_base,
                                }
                            ],
                        }
                    ],
                },
                timeout=120,
            )
            if response.status_code >= 400:
                raise XHSPipelineError(f"Ark responses HTTP {response.status_code}: {response.text[:500]}")
            payload = response.json()
            content = extract_response_text(payload)
            parts = [part.strip() for part in content.splitlines() if part.strip()]
            if not parts:
                raise XHSPipelineError("Ark responses returned empty content")
            title = parts[0]
            body = content[len(title) :].strip()
            if not body:
                raise XHSPipelineError("Ark responses returned incomplete XHS copy")
            logger.info(
                "xhs stage=generate_copy_attempt_done request_id=%s job_id=%s attempt=%d title=%r elapsed=%.2fs",
                request_id_value,
                job_id,
                attempt,
                title,
                time.monotonic() - attempt_started,
            )
            logger.info(
                "xhs stage=generate_copy_done request_id=%s job_id=%s title=%r attempts=%d elapsed=%.2fs",
                request_id_value,
                job_id,
                title,
                attempt,
                time.monotonic() - started,
            )
            return title, body
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError, XHSPipelineError) as exc:
            logger.warning(
                "xhs stage=generate_copy_attempt_failed request_id=%s job_id=%s attempt=%d max_attempts=%d error=%r elapsed=%.2fs",
                request_id_value,
                job_id,
                attempt,
                attempts,
                str(exc),
                time.monotonic() - attempt_started,
            )
            if attempt < attempts and retry_delay > 0:
                time.sleep(retry_delay)

    raise XHSPipelineError(f"Ark responses returned invalid XHS copy after {attempts} attempts")


def xhs_api_base() -> str:
    return os.getenv("XHS_POST_API_BASE", DEFAULT_XHS_POST_API_BASE).rstrip("/")


def publish_xhs_note(
    title: str,
    content: str,
    image_paths: list[Path],
    request_id_value: str | None = None,
    job_id: str | None = None,
) -> tuple[dict[str, Any], str]:
    started = time.monotonic()
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
    logger.info(
        "xhs stage=publish_start request_id=%s job_id=%s images=%d title=%r",
        request_id_value,
        job_id,
        len(image_paths),
        title,
    )
    ordered_paths: list[Path] = []
    for index, path in enumerate(image_paths, start=1):
        ordered_path = path.with_name(f"{index:02d}_{path.name}")
        if ordered_path != path:
            shutil.copy2(path, ordered_path)
        ordered_paths.append(ordered_path)
    logger.info(
        "xhs stage=publish_image_order request_id=%s job_id=%s files=%s",
        request_id_value,
        job_id,
        [path.name for path in ordered_paths],
    )
    attempts = xhs_publish_max_attempts()
    retry_delay = xhs_publish_retry_delay()
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        attempt_started = time.monotonic()
        logger.info(
            "xhs stage=publish_attempt_start request_id=%s job_id=%s attempt=%d max_attempts=%d images=%d title=%r",
            request_id_value,
            job_id,
            attempt,
            attempts,
            len(image_paths),
            title,
        )
        try:
            with ExitStack() as stack:
                files = []
                for path in ordered_paths:
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
            logger.info(
                "xhs stage=publish_attempt_done request_id=%s job_id=%s attempt=%d images=%d elapsed=%.2fs",
                request_id_value,
                job_id,
                attempt,
                len(image_paths),
                time.monotonic() - attempt_started,
            )
            logger.info(
                "xhs stage=publish_done request_id=%s job_id=%s images=%d attempts=%d elapsed=%.2fs",
                request_id_value,
                job_id,
                len(image_paths),
                attempt,
                time.monotonic() - started,
            )
            return payload, share_link
        except (requests.RequestException, ValueError, KeyError, TypeError, XHSPipelineError) as exc:
            last_error = exc
            logger.warning(
                "xhs stage=publish_attempt_failed request_id=%s job_id=%s attempt=%d max_attempts=%d error=%r elapsed=%.2fs",
                request_id_value,
                job_id,
                attempt,
                attempts,
                str(exc),
                time.monotonic() - attempt_started,
            )
            if attempt < attempts and retry_delay > 0:
                time.sleep(retry_delay)

    raise XHSPipelineError(f"XHS publisher failed after {attempts} attempts: {last_error}") from last_error


def create_xhs_note(
    product: dict[str, Any],
    job_id: str,
    job_dir: Path,
    request_id_value: str | None = None,
) -> XHSPipelineResult:
    started = time.monotonic()
    image_urls = list(product.get("image_links") or [])
    logger.info(
        "xhs event=started request_id=%s job_id=%s images=%d product_name=%r",
        request_id_value,
        job_id,
        len(image_urls),
        product.get("name", ""),
    )
    parallel_started = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as executor:
        image_future = executor.submit(process_images_for_xhs, image_urls, product, job_dir, request_id_value, job_id)
        copy_future = executor.submit(generate_xhs_copy, product, request_id_value, job_id)
        processed_paths, image_metadata = image_future.result()
        title, content = copy_future.result()
    logger.info(
        "xhs stage=parallel_prepare_done request_id=%s job_id=%s elapsed=%.2fs",
        request_id_value,
        job_id,
        time.monotonic() - parallel_started,
    )
    publish_response, share_link = publish_xhs_note(title, content, processed_paths, request_id_value, job_id)
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
    logger.info(
        "xhs event=done request_id=%s job_id=%s images=%d elapsed=%.2fs",
        request_id_value,
        job_id,
        len(image_urls),
        time.monotonic() - started,
    )
    return XHSPipelineResult(
        job_id=job_id,
        title=title,
        content=content,
        share_link=share_link,
        qrcode_link=qrcode_link,
        processed_image_paths=processed_paths,
    )
