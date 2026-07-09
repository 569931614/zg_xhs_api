from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.schemas import ScrapeRequest, ScrapeResponse
from app.services.extractor import extract
from app.services.storage import upload_images_to_superbed


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
DATA_DIR = Path(os.getenv("SCRAPER_DATA_DIR", BASE_DIR / "data")).resolve()
STATIC_DIR = BASE_DIR / "static"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_SCRAPE_CONCURRENCY = 3
_scrape_semaphore: threading.BoundedSemaphore | None = None
_scrape_semaphore_limit = 0
_scrape_semaphore_lock = threading.Lock()

app = FastAPI(
    title="Independent Product Scraper",
    description="Extract product information and clean main product images from independent store URLs.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/data", StaticFiles(directory=DATA_DIR), name="data")


PRIVATE_HOSTS = {"localhost", "localhost.localdomain"}
TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_PARAMS = {
    "fbclid",
    "gclid",
    "gbraid",
    "wbraid",
    "msclkid",
    "mc_cid",
    "mc_eid",
}


def is_private_address(hostname: str) -> bool:
    if hostname.lower() in PRIVATE_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False

    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
            return True
    return False


def validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Only http and https URLs are supported.")
    if not parsed.hostname:
        raise HTTPException(status_code=400, detail="URL must include a hostname.")
    if is_private_address(parsed.hostname):
        raise HTTPException(status_code=400, detail="Private, localhost, and internal network URLs are blocked.")


def strip_tracking_query(url: str) -> str:
    parsed = urlparse(url)
    kept_params = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        key_lower = key.lower()
        if key_lower in TRACKING_QUERY_PARAMS or key_lower.startswith(TRACKING_QUERY_PREFIXES):
            continue
        kept_params.append((key, value))
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(kept_params),
            parsed.fragment,
        )
    )


def product_is_weak(result: dict) -> bool:
    product = result.get("product") or {}
    name = str(product.get("name") or "").strip().lower()
    description = str(product.get("description") or "").strip()
    images = result.get("images") or []
    weak_names = {"", "shop", "store", "monument", "home"}
    return name in weak_names or len(description) < 20 or not images


def product_has_dimensions(result: dict) -> bool:
    product = result.get("product") or {}
    dimensions = str(product.get("dimensions") or "").strip()
    if dimensions:
        return True
    details = product.get("details") or {}
    if isinstance(details, dict):
        return bool(str(details.get("dimensions") or "").strip())
    return False


def should_retry_render(exc: Exception) -> bool:
    text = str(exc)
    return any(marker in text for marker in ("403", "Forbidden", "401", "Unauthorized", "429"))


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return max(minimum, value)


def scrape_semaphore() -> threading.BoundedSemaphore:
    global _scrape_semaphore, _scrape_semaphore_limit
    limit = env_int("SCRAPE_CONCURRENCY", DEFAULT_SCRAPE_CONCURRENCY)
    with _scrape_semaphore_lock:
        if _scrape_semaphore is None or _scrape_semaphore_limit != limit:
            _scrape_semaphore = threading.BoundedSemaphore(limit)
            _scrape_semaphore_limit = limit
        return _scrape_semaphore


def image_link(image: dict) -> str:
    return str(image.get("hosted_url") or image.get("local_url") or image.get("url") or "").strip()


def compact_scrape_result(result: dict, images: list[dict]) -> dict:
    product = result.get("product") or {}
    details = product.get("details") if isinstance(product.get("details"), dict) else {}
    product_details = dict(details)
    dimensions = str(product.get("dimensions") or product_details.pop("dimensions", "") or "").strip()
    description = str(product.get("description") or "").strip()
    if description:
        product_details = {"description": description, **product_details}
    product_details.pop("dimensions", None)

    return {
        "name": str(product.get("name") or "").strip(),
        "image_links": [link for link in (image_link(image) for image in images) if link],
        "dimensions": dimensions,
        "product_details": product_details,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/scrape", response_model=ScrapeResponse)
def scrape_product(payload: ScrapeRequest) -> ScrapeResponse:
    with scrape_semaphore():
        url = strip_tracking_query(str(payload.url))
        validate_public_url(url)

        rendered = payload.render == "always"
        try:
            try:
                result = extract(url, rendered, payload.max_images, payload.min_score)
            except Exception as exc:
                if payload.render == "auto" and not rendered and should_retry_render(exc):
                    rendered = True
                    result = extract(url, True, payload.max_images, payload.min_score)
                else:
                    raise
            if payload.render == "auto" and (product_is_weak(result) or not product_has_dimensions(result)):
                rendered = True
                result = extract(url, True, payload.max_images, payload.min_score)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to scrape page: {exc}") from exc

        job_id = f"{int(time.time())}-{uuid.uuid4().hex[:10]}"
        job_dir = DATA_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        images = upload_images_to_superbed(
            result.get("images") or [],
            job_dir / "_image_upload_tmp",
        )
        public_result = compact_scrape_result(result, images)

        result_path = job_dir / "product_extract.json"
        result_path.write_text(json.dumps(public_result, ensure_ascii=False, indent=2), encoding="utf-8")

        return ScrapeResponse(**public_result)
