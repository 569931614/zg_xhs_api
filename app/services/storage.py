from __future__ import annotations

import hashlib
import base64
import logging
import mimetypes
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import requests
from PIL import Image, ImageOps

from app.services.cloudflare import (
    CloudflareBypasser,
    build_chromium_options,
    cloudflare_semaphore,
    resolve_browser_path,
)


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_SUPERBED_UPLOAD_URL = "https://api.superbed.cc/upload"
DEFAULT_MAX_IMAGE_BYTES = 30 * 1024 * 1024
DEFAULT_OPTIMIZE_MIN_BYTES = 1500 * 1024
DEFAULT_OPTIMIZE_MAX_DIMENSION = 1600
DEFAULT_IMAGE_UPLOAD_CONCURRENCY = 6
DEFAULT_IMAGE_UPLOAD_TOTAL_CONCURRENCY = 12
logger = logging.getLogger("uvicorn.error")

_upload_semaphore: threading.BoundedSemaphore | None = None
_upload_semaphore_limit = 0
_upload_semaphore_lock = threading.Lock()


class ImageHostingError(RuntimeError):
    pass


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return max(minimum, value)


def env_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name, "").strip().lower()
    if not raw_value:
        return default
    return raw_value in {"1", "true", "yes", "on"}


def extension_from_response(url: str, content_type: str) -> str:
    content_type = content_type.split(";", 1)[0].strip().lower()
    guessed = mimetypes.guess_extension(content_type) if content_type else ""
    if guessed == ".jpe":
        guessed = ".jpg"
    if guessed in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}:
        return guessed
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}:
        return suffix
    return ".jpg"


def superbed_upload_url() -> str:
    base_url = os.getenv("SUPERBED_UPLOAD_URL", DEFAULT_SUPERBED_UPLOAD_URL).strip()
    token = os.getenv("SUPERBED_TOKEN", "").strip()
    if not token:
        raise ImageHostingError("SUPERBED_TOKEN is not configured")

    parsed = urlparse(base_url)
    query_params = [param for param in parsed.query.split("&") if param]
    if "token=" not in parsed.query:
        query_params.append(urlencode({"token": token}))
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            "&".join(query_params),
            parsed.fragment,
        )
    )


def extract_superbed_url(payload: Any) -> str:
    if isinstance(payload, str) and payload.startswith(("http://", "https://")):
        return payload
    if not isinstance(payload, dict):
        return ""

    for key in ("url", "src", "image", "link"):
        value = payload.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value

    data = payload.get("data")
    if isinstance(data, str) and data.startswith(("http://", "https://")):
        return data
    if isinstance(data, dict):
        nested = extract_superbed_url(data)
        if nested:
            return nested
        links = data.get("links")
        if isinstance(links, dict):
            nested = extract_superbed_url(links)
            if nested:
                return nested

    images = payload.get("images")
    if isinstance(images, list):
        for image in images:
            nested = extract_superbed_url(image)
            if nested:
                return nested
    return ""


def download_image(url: str, target_dir: Path, session: requests.Session) -> tuple[Path, int]:
    target_dir.mkdir(parents=True, exist_ok=True)
    response = session.get(url, timeout=45, stream=True)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        response.close()
        status_code = exc.response.status_code if exc.response is not None else 0
        if status_code in {401, 403, 429} and env_bool("IMAGE_DOWNLOAD_BROWSER_FALLBACK", True):
            logger.warning(
                "image_upload stage=download_browser_fallback_start source_host=%s status=%s",
                urlparse(url).netloc,
                status_code,
            )
            return download_image_with_browser(url, target_dir)
        raise

    max_bytes = int(os.getenv("IMAGE_DOWNLOAD_MAX_BYTES", str(DEFAULT_MAX_IMAGE_BYTES)))
    content_type = response.headers.get("content-type", "")
    ext = extension_from_response(url, content_type)
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    target_path = target_dir / f"{digest}{ext}"

    total = 0
    with target_path.open("wb") as file:
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ImageHostingError(f"Image exceeds max download size: {max_bytes} bytes")
            file.write(chunk)
    return target_path, total


def download_image_with_browser(url: str, target_dir: Path) -> tuple[Path, int]:
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        from DrissionPage import ChromiumPage
    except ImportError as exc:
        raise ImageHostingError("DrissionPage is not installed for browser image download fallback") from exc

    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ImageHostingError(f"Cannot browser-download invalid image URL: {url}")

    browser_path = resolve_browser_path()
    headless = env_bool("CF_BYPASS_HEADLESS", False)
    retries = env_int("CF_BYPASS_RETRIES", 8)
    page_timeout = env_int("CF_BYPASS_PAGE_TIMEOUT", 60)
    max_bytes = int(os.getenv("IMAGE_DOWNLOAD_MAX_BYTES", str(DEFAULT_MAX_IMAGE_BYTES)))
    started = time.monotonic()

    with cloudflare_semaphore():
        driver = ChromiumPage(addr_or_opts=build_chromium_options(browser_path, headless))
        try:
            ok = driver.get(url, timeout=page_timeout)
            if ok is False:
                raise ImageHostingError(f"Browser image load timed out after {page_timeout}s: {url}")
            CloudflareBypasser(driver, max_retries=retries).bypass()

            payload = driver.run_cdp(
                "Runtime.evaluate",
                expression="""
                    (async () => {
                        try {
                            const image = document.images && document.images[0];
                            const target = image ? (image.currentSrc || image.src) : location.href;
                            const response = await fetch(target, {credentials: 'include', cache: 'reload'});
                            const contentType = response.headers.get('content-type') || '';
                            if (!response.ok) {
                                return {ok: false, status: response.status, statusText: response.statusText, contentType, target};
                            }
                            const buffer = await response.arrayBuffer();
                            const bytes = new Uint8Array(buffer);
                            let binary = '';
                            const chunkSize = 0x8000;
                            for (let i = 0; i < bytes.length; i += chunkSize) {
                                binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
                            }
                            return {ok: true, status: response.status, contentType, data: btoa(binary), bytes: bytes.length, target};
                        } catch (error) {
                            return {ok: false, error: String(error), name: error && error.name, target: location.href};
                        }
                    })()
                """,
                awaitPromise=True,
                returnByValue=True,
            )
        finally:
            driver.quit()

    result = payload.get("result", {}).get("value") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise ImageHostingError(f"Browser image download returned unexpected payload: {payload}")
    if not result.get("ok"):
        raise ImageHostingError(f"Browser image download failed: {result}")

    data = base64.b64decode(str(result.get("data") or ""))
    if len(data) > max_bytes:
        raise ImageHostingError(f"Image exceeds max download size: {max_bytes} bytes")

    content_type = str(result.get("contentType") or "")
    ext = extension_from_response(url, content_type)
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    target_path = target_dir / f"{digest}{ext}"
    target_path.write_bytes(data)
    logger.warning(
        "image_upload stage=download_browser_fallback_done source_host=%s bytes=%d elapsed=%.2fs",
        parsed.netloc,
        len(data),
        time.monotonic() - started,
    )
    return target_path, len(data)


def optimize_image_for_upload(path: Path, original_bytes: int) -> tuple[Path, int]:
    min_bytes = env_int("IMAGE_UPLOAD_OPTIMIZE_MIN_BYTES", DEFAULT_OPTIMIZE_MIN_BYTES)
    max_dimension = env_int("IMAGE_UPLOAD_MAX_DIMENSION", DEFAULT_OPTIMIZE_MAX_DIMENSION)
    if original_bytes < min_bytes:
        return path, original_bytes

    try:
        image = Image.open(path)
        image = ImageOps.exif_transpose(image)
        if image.mode in {"RGBA", "LA"}:
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.getchannel("A"))
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")

        width, height = image.size
        if max(width, height) > max_dimension:
            image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)

        optimized_path = path.with_suffix(".optimized.jpg")
        image.save(optimized_path, "JPEG", quality=88, optimize=True, progressive=True)
        optimized_bytes = optimized_path.stat().st_size
        if optimized_bytes < original_bytes:
            return optimized_path, optimized_bytes
    except Exception:
        logger.exception("image_upload event=optimize_failed path=%s", path)
    return path, original_bytes


def upload_to_superbed(path: Path, session: requests.Session) -> str:
    upload_url = superbed_upload_url()
    categories = os.getenv("SUPERBED_CATEGORIES", "").strip()
    data = {"categories": categories} if categories else None
    with path.open("rb") as file:
        response = session.post(
            upload_url,
            data=data,
            files={"file": (path.name, file, mimetypes.guess_type(path.name)[0] or "image/jpeg")},
            timeout=60,
        )
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as exc:
        raise ImageHostingError(f"SuperBed returned non-JSON response: {response.text[:200]}") from exc

    hosted_url = extract_superbed_url(payload)
    if not hosted_url:
        raise ImageHostingError(f"SuperBed response did not include an image URL: {payload}")
    return hosted_url


def image_upload_concurrency() -> int:
    return env_int("IMAGE_UPLOAD_CONCURRENCY", DEFAULT_IMAGE_UPLOAD_CONCURRENCY)


def image_upload_total_concurrency() -> int:
    return env_int("IMAGE_UPLOAD_TOTAL_CONCURRENCY", DEFAULT_IMAGE_UPLOAD_TOTAL_CONCURRENCY)


def upload_semaphore() -> threading.BoundedSemaphore:
    global _upload_semaphore, _upload_semaphore_limit
    limit = image_upload_total_concurrency()
    with _upload_semaphore_lock:
        if _upload_semaphore is None or _upload_semaphore_limit != limit:
            _upload_semaphore = threading.BoundedSemaphore(limit)
            _upload_semaphore_limit = limit
        return _upload_semaphore


def upload_one_image(
    index: int,
    image: dict[str, Any],
    temp_dir: Path,
    request_id_value: str | None = None,
) -> tuple[int, dict[str, Any]]:
    started = time.monotonic()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    item = dict(image)
    source_url = str(item.get("url") or "")
    if not source_url:
        item["upload_error"] = "Missing image URL"
        logger.warning(
            "image_upload event=failed request_id=%s index=%d reason=missing_url elapsed=%.2fs",
            request_id_value,
            index,
            time.monotonic() - started,
        )
        return index, item

    try:
        logger.info(
            "image_upload event=started request_id=%s index=%d source_host=%s",
            request_id_value,
            index,
            urlparse(source_url).netloc,
        )
        with upload_semaphore():
            local_path, byte_count = download_image(source_url, temp_dir, session)
            upload_path, upload_byte_count = optimize_image_for_upload(local_path, byte_count)
            hosted_url = upload_to_superbed(upload_path, session)
        item["hosted_url"] = hosted_url
        item["filename"] = upload_path.name
        item["bytes"] = byte_count
        item["upload_bytes"] = upload_byte_count
        logger.info(
            "image_upload event=done request_id=%s index=%d bytes=%d upload_bytes=%d elapsed=%.2fs",
            request_id_value,
            index,
            byte_count,
            upload_byte_count,
            time.monotonic() - started,
        )
    except Exception as exc:
        item["hosted_url"] = item.get("hosted_url") or source_url
        item["upload_error"] = str(exc)
        logger.exception(
            "image_upload event=failed request_id=%s index=%d source_host=%s elapsed=%.2fs",
            request_id_value,
            index,
            urlparse(source_url).netloc,
            time.monotonic() - started,
        )
    return index, item


def upload_images_to_superbed(
    images: list[dict[str, Any]],
    temp_dir: Path,
    request_id_value: str | None = None,
) -> list[dict[str, Any]]:
    if not images:
        return []

    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        results: list[dict[str, Any] | None] = [None] * len(images)
        max_workers = min(image_upload_concurrency(), len(images))
        logger.info(
            "image_upload_batch event=started request_id=%s images=%d workers=%d",
            request_id_value,
            len(images),
            max_workers,
        )
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(upload_one_image, index, image, temp_dir, request_id_value)
                for index, image in enumerate(images)
            ]
            for future in as_completed(futures):
                index, item = future.result()
                results[index] = item
        logger.info(
            "image_upload_batch event=done request_id=%s images=%d elapsed=%.2fs",
            request_id_value,
            len(images),
            time.monotonic() - started,
        )
        return [item for item in results if item is not None]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
