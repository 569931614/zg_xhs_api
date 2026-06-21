from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import os
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.schemas import ScrapeRequest, ScrapeResponse
from app.services.extractor import USER_AGENT, extract
from app.services.storage import upload_image_with_fallback


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
DATA_DIR = Path(os.getenv("SCRAPER_DATA_DIR", BASE_DIR / "data")).resolve()
STATIC_DIR = BASE_DIR / "static"
DATA_DIR.mkdir(parents=True, exist_ok=True)

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


def product_is_weak(result: dict) -> bool:
    product = result.get("product") or {}
    name = str(product.get("name") or "").strip().lower()
    description = str(product.get("description") or "").strip()
    images = result.get("images") or []
    weak_names = {"", "shop", "store", "monument", "home"}
    return name in weak_names or len(description) < 20 or not images


def image_extension(content_type: str, url: str) -> str:
    ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) if content_type else ""
    if ext == ".jpe":
        return ".jpg"
    if ext:
        return ext
    path_ext = Path(urlparse(url).path).suffix.lower()
    return path_ext if path_ext in {".jpg", ".jpeg", ".png", ".webp", ".avif"} else ".jpg"


def download_selected_images(job_dir: Path, job_id: str, images: list[dict]) -> list[dict]:
    image_dir = job_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    enriched = []

    for index, image in enumerate(images, 1):
        item = dict(image)
        url = item["url"]
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            ext = image_extension(content_type, url)
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
            filename = f"{index:02d}_{digest}{ext}"
            path = image_dir / filename
            path.write_bytes(response.content)
            item["filename"] = filename
            item["bytes"] = path.stat().st_size
            item["local_url"] = f"/data/{job_id}/images/{filename}"
            try:
                upload = upload_image_with_fallback(path, filename, content_type)
                item["hosted_url"] = upload.url
                item["storage_provider"] = upload.provider
            except Exception as exc:
                item["upload_error"] = str(exc)
        except Exception as exc:
            item["download_error"] = str(exc)
        enriched.append(item)
    return enriched


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/scrape", response_model=ScrapeResponse)
def scrape_product(payload: ScrapeRequest) -> ScrapeResponse:
    url = str(payload.url)
    validate_public_url(url)

    rendered = payload.render == "always"
    try:
        result = extract(url, rendered, payload.max_images, payload.min_score)
        if payload.render == "auto" and product_is_weak(result):
            rendered = True
            result = extract(url, True, payload.max_images, payload.min_score)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to scrape page: {exc}") from exc

    job_id = f"{int(time.time())}-{uuid.uuid4().hex[:10]}"
    job_dir = DATA_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    images = result.get("images") or []
    if payload.download_images:
        images = download_selected_images(job_dir, job_id, images)
        result["images"] = images

    result_path = job_dir / "product_extract.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    return ScrapeResponse(
        job_id=job_id,
        input_url=result.get("input_url", url),
        fetched_url=result.get("fetched_url", url),
        rendered=rendered,
        product=result.get("product") or {},
        images=images,
        rejected_preview=result.get("rejected_preview") or [],
        result_url=f"/data/{job_id}/product_extract.json",
    )
