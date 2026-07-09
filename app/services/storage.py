from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import requests


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_SUPERBED_UPLOAD_URL = "https://api.superbed.cc/upload"
DEFAULT_MAX_IMAGE_BYTES = 30 * 1024 * 1024
DEFAULT_IMAGE_UPLOAD_CONCURRENCY = 6
DEFAULT_IMAGE_UPLOAD_TOTAL_CONCURRENCY = 12

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
    response.raise_for_status()

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


def upload_one_image(index: int, image: dict[str, Any], temp_dir: Path) -> tuple[int, dict[str, Any]]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    item = dict(image)
    source_url = str(item.get("url") or "")
    if not source_url:
        item["upload_error"] = "Missing image URL"
        return index, item

    try:
        with upload_semaphore():
            local_path, byte_count = download_image(source_url, temp_dir, session)
            hosted_url = upload_to_superbed(local_path, session)
        item["hosted_url"] = hosted_url
        item["filename"] = local_path.name
        item["bytes"] = byte_count
    except Exception as exc:
        item["hosted_url"] = item.get("hosted_url") or source_url
        item["upload_error"] = str(exc)
    return index, item


def upload_images_to_superbed(images: list[dict[str, Any]], temp_dir: Path) -> list[dict[str, Any]]:
    if not images:
        return []

    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        results: list[dict[str, Any] | None] = [None] * len(images)
        max_workers = min(image_upload_concurrency(), len(images))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(upload_one_image, index, image, temp_dir)
                for index, image in enumerate(images)
            ]
            for future in as_completed(futures):
                index, item = future.result()
                results[index] = item
        return [item for item in results if item is not None]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
