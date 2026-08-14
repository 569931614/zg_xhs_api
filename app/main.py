from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.schemas import (
    AuctionListRequest,
    AuctionListResponse,
    ScrapeBatchItem,
    ScrapeBatchResponse,
    ScrapeRequest,
    ScrapeResponse,
    XHSCreateBatchItem,
    XHSCreateBatchResponse,
    XHSCreateRequest,
    XHSCreateResponse,
    XianyuCopyBatchItem,
    XianyuCopyBatchResponse,
    XianyuCopyRequest,
    XianyuCopyResponse,
)
from app.services.auction_list import AuctionListError, extract_auction_list
from app.services.extractor import extract
from app.services.logging_config import configure_logging
from app.services.storage import upload_images_to_superbed
from app.services.xianyu_pipeline import XianyuPipelineError, create_xianyu_copy
from app.services.xhs_pipeline import XHSPipelineError, create_xhs_note


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
configure_logging()
DATA_DIR = Path(os.getenv("SCRAPER_DATA_DIR", BASE_DIR / "data")).resolve()
STATIC_DIR = BASE_DIR / "static"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_SCRAPE_CONCURRENCY = 3
DEFAULT_BATCH_CONCURRENCY = 2
_scrape_semaphore: threading.BoundedSemaphore | None = None
_scrape_semaphore_limit = 0
_scrape_semaphore_lock = threading.Lock()
logger = logging.getLogger("uvicorn.error")

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


def request_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def url_log_value(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    return f"{parsed.netloc}{path}"


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
    source_url = str(product.get("page_url") or result.get("fetched_url") or result.get("input_url") or "").strip()
    if description:
        product_details = {"description": description, **product_details}
    product_details.pop("dimensions", None)

    return {
        "name": str(product.get("name") or "").strip(),
        "price": str(product.get("price") or "").strip(),
        "currency": str(product.get("currency") or "").strip(),
        "source_url": source_url,
        "image_links": [link for link in (image_link(image) for image in images) if link],
        "dimensions": dimensions,
        "product_details": product_details,
    }


def extracted_image_links(result: dict) -> list[str]:
    return [
        link
        for link in (
            str(image.get("url") or "").strip()
            for image in result.get("images") or []
            if isinstance(image, dict)
        )
        if link
    ]


def run_scrape_url(
    url_value: str,
    payload: ScrapeRequest,
    upload_images: bool = True,
    request_id_value: str | None = None,
) -> tuple[dict, str, Path]:
    request_id_value = request_id_value or request_id("scrape")
    total_started = time.monotonic()
    semaphore = scrape_semaphore()
    wait_started = time.monotonic()
    logger.info(
        "scrape event=queued request_id=%s upload_images=%s render=%s max_images=%s url=%s",
        request_id_value,
        upload_images,
        payload.render,
        payload.max_images,
        url_log_value(url_value),
    )
    semaphore.acquire()
    try:
        logger.info(
            "scrape event=started request_id=%s queue_wait=%.2fs",
            request_id_value,
            time.monotonic() - wait_started,
        )
        url = strip_tracking_query(url_value)
        validate_public_url(url)

        rendered = payload.render == "always"
        try:
            extract_started = time.monotonic()
            try:
                logger.info(
                    "scrape stage=extract_start request_id=%s rendered=%s url=%s",
                    request_id_value,
                    rendered,
                    url_log_value(url),
                )
                result = extract(
                    url,
                    rendered,
                    payload.max_images,
                    allow_cloudflare_bypass=payload.render != "never",
                )
            except Exception as exc:
                if payload.render == "auto" and not rendered and should_retry_render(exc):
                    rendered = True
                    logger.warning(
                        "scrape stage=extract_retry_render request_id=%s reason=%r url=%s",
                        request_id_value,
                        str(exc),
                        url_log_value(url),
                    )
                    result = extract(
                        url,
                        True,
                        payload.max_images,
                        allow_cloudflare_bypass=payload.render != "never",
                    )
                else:
                    raise
            if payload.render == "auto" and (product_is_weak(result) or not product_has_dimensions(result)):
                rendered = True
                logger.info(
                    "scrape stage=extract_retry_render request_id=%s reason=weak_result images=%d has_dimensions=%s url=%s",
                    request_id_value,
                    len(result.get("images") or []),
                    product_has_dimensions(result),
                    url_log_value(url),
                )
                result = extract(
                    url,
                    True,
                    payload.max_images,
                    allow_cloudflare_bypass=payload.render != "never",
                )
            logger.info(
                "scrape stage=extract_done request_id=%s url_host=%s rendered=%s images=%d name=%r dimensions=%s elapsed=%.2fs",
                request_id_value,
                urlparse(url).netloc,
                rendered,
                len(result.get("images") or []),
                (result.get("product") or {}).get("name", ""),
                bool((result.get("product") or {}).get("dimensions")),
                time.monotonic() - extract_started,
            )
        except Exception as exc:
            logger.exception(
                "scrape event=failed request_id=%s stage=extract url=%s elapsed=%.2fs",
                request_id_value,
                url_log_value(url),
                time.monotonic() - total_started,
            )
            raise HTTPException(status_code=502, detail=f"Failed to scrape page: {exc}") from exc

        job_id = f"{int(time.time())}-{uuid.uuid4().hex[:10]}"
        job_dir = DATA_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "scrape stage=job_created request_id=%s job_id=%s job_dir=%s",
            request_id_value,
            job_id,
            job_dir,
        )

        if upload_images:
            upload_started = time.monotonic()
            logger.info(
                "scrape stage=upload_images_start request_id=%s job_id=%s images=%d",
                request_id_value,
                job_id,
                len(result.get("images") or []),
            )
            images = upload_images_to_superbed(
                result.get("images") or [],
                job_dir / "_image_upload_tmp",
                request_id_value=request_id_value,
            )
            logger.info(
                "scrape stage=upload_images_done request_id=%s job_id=%s images=%d elapsed=%.2fs",
                request_id_value,
                job_id,
                len(images),
                time.monotonic() - upload_started,
            )
            public_result = compact_scrape_result(result, images)
        else:
            public_result = compact_scrape_result(result, [])
            public_result["image_links"] = extracted_image_links(result)

        result_path = job_dir / "product_extract.json"
        result_path.write_text(json.dumps(public_result, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(
            "scrape event=done request_id=%s job_id=%s upload_images=%s image_links=%d elapsed=%.2fs",
            request_id_value,
            job_id,
            upload_images,
            len(public_result.get("image_links") or []),
            time.monotonic() - total_started,
        )

        return public_result, job_id, job_dir
    finally:
        semaphore.release()


def run_scrape(
    payload: ScrapeRequest,
    upload_images: bool = True,
    request_id_value: str | None = None,
) -> tuple[dict, str, Path]:
    return run_scrape_url(payload.product_urls()[0], payload, upload_images, request_id_value)


def run_batch(urls: list[str], worker: Callable[[int, str], Any], batch_id: str, endpoint: str) -> list[Any]:
    max_workers = min(env_int("BATCH_CONCURRENCY", DEFAULT_BATCH_CONCURRENCY), len(urls))
    results: list[Any] = [None] * len(urls)
    started = time.monotonic()
    logger.info(
        "batch event=started batch_id=%s endpoint=%s urls=%d workers=%d",
        batch_id,
        endpoint,
        len(urls),
        max_workers,
    )
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {executor.submit(worker, index, url): index for index, url in enumerate(urls)}
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            item_started = time.monotonic()
            try:
                results[index] = future.result()
            except Exception:
                logger.exception(
                    "batch event=item_crashed batch_id=%s endpoint=%s index=%d",
                    batch_id,
                    endpoint,
                    index,
                )
                raise
            logger.info(
                "batch event=item_collected batch_id=%s endpoint=%s index=%d collect_elapsed=%.2fs",
                batch_id,
                endpoint,
                index,
                time.monotonic() - item_started,
            )
    logger.info(
        "batch event=done batch_id=%s endpoint=%s urls=%d elapsed=%.2fs",
        batch_id,
        endpoint,
        len(urls),
        time.monotonic() - started,
    )
    return results


def http_error_text(exc: HTTPException) -> str:
    return str(exc.detail)


def create_xhs_response(result: Any, job_id: str) -> XHSCreateResponse:
    return XHSCreateResponse(
        job_id=result.job_id,
        qrcode_image_link=result.qrcode_link,
        share_link=result.share_link,
        xhs_link=result.share_link,
        title=result.title,
        content=result.content,
        result_path=f"/data/{job_id}/xhs_result.json",
    )


def create_xianyu_response(result: Any, job_id: str) -> XianyuCopyResponse:
    return XianyuCopyResponse(
        job_id=result.job_id,
        title=result.title,
        content=result.content,
        xianyu_copy=result.xianyu_copy,
        product_type=result.product_type,
        price=result.price,
        source_price=result.source_price,
        source_currency=result.source_currency,
        image_links=result.image_links,
        result_path=f"/data/{job_id}/xianyu_result.json",
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/scrape", response_model=ScrapeResponse | ScrapeBatchResponse)
def scrape_product(payload: ScrapeRequest) -> ScrapeResponse | ScrapeBatchResponse:
    api_request_id = request_id("api-scrape")
    logger.info(
        "api event=received request_id=%s endpoint=/api/scrape batch=%s urls=%d",
        api_request_id,
        payload.is_batch(),
        len(payload.product_urls()),
    )
    if not payload.is_batch():
        public_result, job_id, _ = run_scrape(payload, request_id_value=api_request_id)
        logger.info("api event=done request_id=%s endpoint=/api/scrape job_id=%s", api_request_id, job_id)
        return ScrapeResponse(**public_result)

    def scrape_one(index: int, url: str) -> ScrapeBatchItem:
        item_request_id = f"{api_request_id}-{index + 1}"
        item_started = time.monotonic()
        logger.info(
            "batch event=item_started batch_id=%s request_id=%s endpoint=/api/scrape index=%d url=%s",
            api_request_id,
            item_request_id,
            index,
            url_log_value(url),
        )
        try:
            public_result, job_id, _ = run_scrape_url(url, payload, request_id_value=item_request_id)
            logger.info(
                "batch event=item_done batch_id=%s request_id=%s endpoint=/api/scrape index=%d job_id=%s elapsed=%.2fs",
                api_request_id,
                item_request_id,
                index,
                job_id,
                time.monotonic() - item_started,
            )
            return ScrapeBatchItem(url=url, success=True, result=ScrapeResponse(**public_result))
        except HTTPException as exc:
            logger.warning(
                "batch event=item_failed batch_id=%s request_id=%s endpoint=/api/scrape index=%d error=%r elapsed=%.2fs",
                api_request_id,
                item_request_id,
                index,
                http_error_text(exc),
                time.monotonic() - item_started,
            )
            return ScrapeBatchItem(url=url, success=False, error=http_error_text(exc))
        except Exception as exc:
            logger.exception(
                "batch event=item_failed batch_id=%s request_id=%s endpoint=/api/scrape index=%d elapsed=%.2fs",
                api_request_id,
                item_request_id,
                index,
                time.monotonic() - item_started,
            )
            return ScrapeBatchItem(url=url, success=False, error=str(exc))

    results = run_batch(payload.product_urls(), scrape_one, api_request_id, "/api/scrape")
    return ScrapeBatchResponse(results=results)


@app.post("/api/auction/list", response_model=AuctionListResponse)
def scrape_auction_list(payload: AuctionListRequest) -> AuctionListResponse:
    api_request_id = request_id("api-auction-list")
    started = time.monotonic()
    url = strip_tracking_query(str(payload.url))
    logger.info(
        "api event=received request_id=%s endpoint=/api/auction/list render=%s max_items=%d max_pages=%d save_to_db=%s url=%s",
        api_request_id,
        payload.render,
        payload.max_items,
        payload.max_pages,
        payload.save_to_db,
        url_log_value(url),
    )
    validate_public_url(url)
    try:
        result = extract_auction_list(
            url,
            payload.render,
            payload.max_items,
            max_pages=payload.max_pages,
            save_to_db=payload.save_to_db,
        )
    except AuctionListError as exc:
        logger.exception(
            "api event=failed request_id=%s endpoint=/api/auction/list url=%s elapsed=%.2fs",
            api_request_id,
            url_log_value(url),
            time.monotonic() - started,
        )
        raise HTTPException(status_code=502, detail=f"Failed to scrape auction list: {exc}") from exc
    except Exception as exc:
        logger.exception(
            "api event=failed request_id=%s endpoint=/api/auction/list url=%s elapsed=%.2fs",
            api_request_id,
            url_log_value(url),
            time.monotonic() - started,
        )
        raise HTTPException(status_code=502, detail=f"Failed to scrape auction list: {exc}") from exc

    logger.info(
        "api event=done request_id=%s endpoint=/api/auction/list items=%d pages=%d saved=%d elapsed=%.2fs",
        api_request_id,
        len(result.get("items") or []),
        result.get("pages_fetched", 0),
        result.get("saved_count", 0),
        time.monotonic() - started,
    )
    return AuctionListResponse(**result)


@app.post("/api/xhs/create", response_model=XHSCreateResponse | XHSCreateBatchResponse)
def create_xhs_product_note(payload: XHSCreateRequest) -> XHSCreateResponse | XHSCreateBatchResponse:
    api_request_id = request_id("api-xhs")
    logger.info(
        "api event=received request_id=%s endpoint=/api/xhs/create batch=%s urls=%d",
        api_request_id,
        payload.is_batch(),
        len(payload.product_urls()),
    )
    if payload.is_batch():
        def create_one(index: int, url: str) -> XHSCreateBatchItem:
            item_request_id = f"{api_request_id}-{index + 1}"
            started = time.monotonic()
            logger.info(
                "batch event=item_started batch_id=%s request_id=%s endpoint=/api/xhs/create index=%d url=%s",
                api_request_id,
                item_request_id,
                index,
                url_log_value(url),
            )
            try:
                public_result, job_id, job_dir = run_scrape_url(
                    url,
                    payload,
                    upload_images=False,
                    request_id_value=item_request_id,
                )
                result = create_xhs_note(public_result, job_id, job_dir, request_id_value=item_request_id)
                logger.info(
                    "batch event=item_done batch_id=%s request_id=%s endpoint=/api/xhs/create index=%d job_id=%s elapsed=%.2fs",
                    api_request_id,
                    item_request_id,
                    index,
                    result.job_id,
                    time.monotonic() - started,
                )
                return XHSCreateBatchItem(
                    url=url,
                    success=True,
                    result=create_xhs_response(result, job_id),
                )
            except HTTPException as exc:
                logger.warning(
                    "batch event=item_failed batch_id=%s request_id=%s endpoint=/api/xhs/create index=%d error=%r elapsed=%.2fs",
                    api_request_id,
                    item_request_id,
                    index,
                    http_error_text(exc),
                    time.monotonic() - started,
                )
                return XHSCreateBatchItem(url=url, success=False, error=http_error_text(exc))
            except XHSPipelineError as exc:
                logger.exception(
                    "batch event=item_failed batch_id=%s request_id=%s endpoint=/api/xhs/create index=%d elapsed=%.2fs",
                    api_request_id,
                    item_request_id,
                    index,
                    time.monotonic() - started,
                )
                return XHSCreateBatchItem(url=url, success=False, error=f"Failed to create XHS note: {exc}")
            except Exception as exc:
                logger.exception(
                    "batch event=item_failed batch_id=%s request_id=%s endpoint=/api/xhs/create index=%d elapsed=%.2fs",
                    api_request_id,
                    item_request_id,
                    index,
                    time.monotonic() - started,
                )
                return XHSCreateBatchItem(url=url, success=False, error=f"Failed to create XHS note: {exc}")

        results = run_batch(payload.product_urls(), create_one, api_request_id, "/api/xhs/create")
        return XHSCreateBatchResponse(results=results)

    started = time.monotonic()
    public_result, job_id, job_dir = run_scrape(payload, upload_images=False, request_id_value=api_request_id)
    try:
        result = create_xhs_note(public_result, job_id, job_dir, request_id_value=api_request_id)
    except XHSPipelineError as exc:
        logger.exception(
            "api event=failed request_id=%s endpoint=/api/xhs/create job_id=%s elapsed=%.2fs",
            api_request_id,
            job_id,
            time.monotonic() - started,
        )
        raise HTTPException(status_code=502, detail=f"Failed to create XHS note: {exc}") from exc
    except Exception as exc:
        logger.exception(
            "api event=failed request_id=%s endpoint=/api/xhs/create job_id=%s elapsed=%.2fs",
            api_request_id,
            job_id,
            time.monotonic() - started,
        )
        raise HTTPException(status_code=502, detail=f"Failed to create XHS note: {exc}") from exc

    logger.info(
        "api event=done request_id=%s endpoint=/api/xhs/create job_id=%s elapsed=%.2fs",
        api_request_id,
        result.job_id,
        time.monotonic() - started,
    )
    return create_xhs_response(result, job_id)


@app.post("/api/xianyu/copy", response_model=XianyuCopyResponse | XianyuCopyBatchResponse)
def create_xianyu_product_copy(payload: XianyuCopyRequest) -> XianyuCopyResponse | XianyuCopyBatchResponse:
    api_request_id = request_id("api-xianyu")
    logger.info(
        "api event=received request_id=%s endpoint=/api/xianyu/copy batch=%s urls=%d",
        api_request_id,
        payload.is_batch(),
        len(payload.product_urls()),
    )
    if payload.is_batch():
        def create_one(index: int, url: str) -> XianyuCopyBatchItem:
            item_request_id = f"{api_request_id}-{index + 1}"
            started = time.monotonic()
            logger.info(
                "batch event=item_started batch_id=%s request_id=%s endpoint=/api/xianyu/copy index=%d url=%s",
                api_request_id,
                item_request_id,
                index,
                url_log_value(url),
            )
            try:
                public_result, job_id, job_dir = run_scrape_url(
                    url,
                    payload,
                    upload_images=True,
                    request_id_value=item_request_id,
                )
                result = create_xianyu_copy(public_result, job_id, job_dir, request_id_value=item_request_id)
                logger.info(
                    "batch event=item_done batch_id=%s request_id=%s endpoint=/api/xianyu/copy index=%d job_id=%s elapsed=%.2fs",
                    api_request_id,
                    item_request_id,
                    index,
                    result.job_id,
                    time.monotonic() - started,
                )
                return XianyuCopyBatchItem(
                    url=url,
                    success=True,
                    result=create_xianyu_response(result, job_id),
                )
            except HTTPException as exc:
                logger.warning(
                    "batch event=item_failed batch_id=%s request_id=%s endpoint=/api/xianyu/copy index=%d error=%r elapsed=%.2fs",
                    api_request_id,
                    item_request_id,
                    index,
                    http_error_text(exc),
                    time.monotonic() - started,
                )
                return XianyuCopyBatchItem(url=url, success=False, error=http_error_text(exc))
            except XianyuPipelineError as exc:
                logger.exception(
                    "batch event=item_failed batch_id=%s request_id=%s endpoint=/api/xianyu/copy index=%d elapsed=%.2fs",
                    api_request_id,
                    item_request_id,
                    index,
                    time.monotonic() - started,
                )
                return XianyuCopyBatchItem(url=url, success=False, error=f"Failed to create Xianyu copy: {exc}")
            except Exception as exc:
                logger.exception(
                    "batch event=item_failed batch_id=%s request_id=%s endpoint=/api/xianyu/copy index=%d elapsed=%.2fs",
                    api_request_id,
                    item_request_id,
                    index,
                    time.monotonic() - started,
                )
                return XianyuCopyBatchItem(url=url, success=False, error=f"Failed to create Xianyu copy: {exc}")

        results = run_batch(payload.product_urls(), create_one, api_request_id, "/api/xianyu/copy")
        return XianyuCopyBatchResponse(results=results)

    started = time.monotonic()
    public_result, job_id, job_dir = run_scrape(payload, upload_images=True, request_id_value=api_request_id)
    try:
        result = create_xianyu_copy(public_result, job_id, job_dir, request_id_value=api_request_id)
    except XianyuPipelineError as exc:
        logger.exception(
            "api event=failed request_id=%s endpoint=/api/xianyu/copy job_id=%s elapsed=%.2fs",
            api_request_id,
            job_id,
            time.monotonic() - started,
        )
        raise HTTPException(status_code=502, detail=f"Failed to create Xianyu copy: {exc}") from exc
    except Exception as exc:
        logger.exception(
            "api event=failed request_id=%s endpoint=/api/xianyu/copy job_id=%s elapsed=%.2fs",
            api_request_id,
            job_id,
            time.monotonic() - started,
        )
        raise HTTPException(status_code=502, detail=f"Failed to create Xianyu copy: {exc}") from exc

    logger.info(
        "api event=done request_id=%s endpoint=/api/xianyu/copy job_id=%s elapsed=%.2fs",
        api_request_id,
        result.job_id,
        time.monotonic() - started,
    )
    return create_xianyu_response(result, job_id)
