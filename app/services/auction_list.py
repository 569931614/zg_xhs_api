from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo


logger = logging.getLogger("uvicorn.error")

DROUOT_API_BASE = "https://api.drouot.com/drouot/gingolem/neoGingo/lot/search"
DROUOT_SITE_BASE = "https://drouot.com"
DROUOT_CDN_IMAGE_BASE = "https://cdn.drouot.com/d/image/lot"
DROUOT_PAGE_SIZE = 100
DROUOT_DEFAULT_LANG = "zh"
CURRENCY_SYMBOLS = {
    "EUR": "€",
    "GBP": "£",
    "USD": "$",
    "CNY": "¥",
    "JPY": "¥",
}


class AuctionListError(RuntimeError):
    pass


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return max(minimum, value)


def auction_db_path() -> Path:
    configured = os.getenv("AUCTION_DB_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    base_dir = Path(__file__).resolve().parents[2]
    data_dir = Path(os.getenv("SCRAPER_DATA_DIR", base_dir / "data")).resolve()
    return data_dir / "auction_lots.sqlite3"


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS drouot_lots (
                lot_id INTEGER PRIMARY KEY,
                category_id INTEGER NOT NULL,
                lang TEXT NOT NULL,
                page INTEGER NOT NULL,
                position INTEGER NOT NULL,
                lot_number TEXT,
                title TEXT,
                lot_url TEXT,
                image_link TEXT,
                currency TEXT,
                low_estimate REAL,
                high_estimate REAL,
                estimate TEXT,
                sale_timestamp INTEGER,
                sale_time TEXT,
                sale_type TEXT,
                sale_status TEXT,
                sale_id INTEGER,
                auctioneer_id INTEGER,
                raw_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS drouot_fetch_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_url TEXT NOT NULL,
                api_url TEXT NOT NULL,
                category_id INTEGER NOT NULL,
                lang TEXT NOT NULL,
                pages_fetched INTEGER NOT NULL,
                saved_count INTEGER NOT NULL,
                total_count INTEGER,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_drouot_lots_category ON drouot_lots(category_id, lang)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_drouot_lots_sale ON drouot_lots(sale_id)")


def extract_lang_from_path(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    if parts and re.fullmatch(r"[a-z]{2}", parts[0], re.I):
        return parts[0].lower()
    return DROUOT_DEFAULT_LANG


def parse_source(source_url: str) -> tuple[str, int, int]:
    parsed = urlparse(source_url)
    query = parse_qs(parsed.query)
    lang = (query.get("lang") or [extract_lang_from_path(parsed.path)])[0] or DROUOT_DEFAULT_LANG

    category = ""
    if query.get("cat"):
        category = query["cat"][0]
    else:
        match = re.search(r"/c/(\d+)(?:/|$)", parsed.path)
        if match:
            category = match.group(1)
    if not category or not category.isdigit():
        raise AuctionListError("Drouot category id is required. Use a /c/{cat}/ page or an API URL with cat=.")

    page = 1
    if query.get("page") and str(query["page"][0]).isdigit():
        page = max(1, int(query["page"][0]))
    return lang, int(category), page


def build_api_url(lang: str, category_id: int, page: int) -> str:
    params = {"lang": lang, "cat": category_id, "page": page, "facet": "false"}
    return f"{DROUOT_API_BASE}?{urlencode(params)}"


def fetch_api_page(lang: str, category_id: int, page: int) -> dict[str, Any]:
    url = build_api_url(lang, category_id, page)
    headers = {
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": DROUOT_SITE_BASE,
        "Referer": f"{DROUOT_SITE_BASE}/{lang}/c/{category_id}",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
    }
    timeout = env_int("DROUOT_API_TIMEOUT", 60)
    retries = env_int("DROUOT_API_RETRIES", 4)
    retry_delay = env_int("DROUOT_API_RETRY_DELAY", 2, minimum=0)
    last_error: Exception | None = None
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        import requests

        for attempt in range(1, retries + 1):
            try:
                response = requests.get(url, headers=headers, timeout=timeout)
                return parse_api_response(response, page)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "auction_list stage=api_requests_attempt_failed page=%d category=%d attempt=%d/%d reason=%r",
                    page,
                    category_id,
                    attempt,
                    retries,
                    str(exc),
                )
                if attempt < retries and retry_delay:
                    time.sleep(retry_delay * attempt)
    else:
        for attempt in range(1, retries + 1):
            try:
                response = curl_requests.get(url, headers=headers, impersonate="chrome120", timeout=timeout)
                return parse_api_response(response, page)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "auction_list stage=api_curl_attempt_failed page=%d category=%d attempt=%d/%d reason=%r",
                    page,
                    category_id,
                    attempt,
                    retries,
                    str(exc),
                )
                if attempt < retries and retry_delay:
                    time.sleep(retry_delay * attempt)

    raise AuctionListError(f"Drouot API request failed for page {page}: {last_error}")


def parse_api_response(response: Any, page: int) -> dict[str, Any]:
    if response.status_code >= 400:
        raise AuctionListError(f"Drouot API returned HTTP {response.status_code} for page {page}")
    try:
        data = response.json()
    except ValueError as exc:
        raise AuctionListError(f"Drouot API returned invalid JSON for page {page}") from exc
    if not isinstance(data, dict):
        raise AuctionListError(f"Drouot API returned unexpected payload for page {page}")
    return data


def drouot_lot_url(lot: dict[str, Any], lang: str) -> str:
    lot_id = lot.get("id")
    slug = str(lot.get("slug") or "").strip("/")
    path = f"/{lang}/l/{lot_id}"
    if slug:
        path += f"-{slug}"
    return urlunparse(("https", "drouot.com", path, "", "", ""))


def drouot_image_link(lot: dict[str, Any]) -> str:
    photo = lot.get("photo")
    if not isinstance(photo, dict):
        return ""
    path = str(photo.get("path") or "").strip()
    if not path:
        return ""
    return f"{DROUOT_CDN_IMAGE_BASE}?size=ftall&path={path}"


def currency_symbol(currency: str) -> str:
    return CURRENCY_SYMBOLS.get(currency.upper(), currency.upper())


def format_amount(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def format_estimate(lot: dict[str, Any]) -> str:
    low = lot.get("lowEstim")
    high = lot.get("highEstim")
    currency = str(lot.get("currencyId") or "").strip()
    symbol = currency_symbol(currency) if currency else ""
    low_text = format_amount(low)
    high_text = format_amount(high)
    if low_text and high_text:
        return f"{symbol}{low_text} - {high_text}"
    if low_text:
        return f"{symbol}{low_text}"
    if high_text:
        return f"{symbol}{high_text}"
    return ""


def format_sale_time(lot: dict[str, Any]) -> str:
    timestamp = lot.get("date")
    if not timestamp:
        return ""
    timezone = str(lot.get("timezone") or "Europe/Paris")
    try:
        dt = datetime.fromtimestamp(int(timestamp), ZoneInfo(timezone))
    except Exception:
        dt = datetime.fromtimestamp(int(timestamp))
    period = "上午" if dt.hour < 12 else "下午"
    hour = dt.hour % 12 or 12
    return f"{dt.month}月{dt.day}日 | {period}{hour:02d}:{dt.minute:02d}"


def normalize_lot(lot: dict[str, Any], lang: str, page: int, position: int) -> dict[str, Any]:
    title = str(lot.get("description") or "").strip()
    estimate = format_estimate(lot)
    sale_status = str(lot.get("saleType") or lot.get("saleStatus") or "").strip().lower()
    sale_time = format_sale_time(lot)
    raw_parts = [part for part in (sale_status, sale_time, title, f"估价 {estimate}" if estimate else "") if part]
    return {
        "lot_id": str(lot.get("id") or ""),
        "lot_number": str(lot.get("num") or ""),
        "title": title,
        "url": drouot_lot_url(lot, lang),
        "image_link": drouot_image_link(lot),
        "sale_status": sale_status,
        "sale_time": sale_time,
        "estimate": estimate,
        "currency": str(lot.get("currencyId") or ""),
        "low_estimate": lot.get("lowEstim"),
        "high_estimate": lot.get("highEstim"),
        "sale_timestamp": lot.get("date") or 0,
        "sale_type": str(lot.get("saleType") or ""),
        "sale_id": lot.get("saleId"),
        "auctioneer_id": lot.get("auctioneerId"),
        "page": page,
        "position": position,
        "raw_text": " ".join(raw_parts),
        "raw_json": lot,
    }


def upsert_lots(
    db_path: Path,
    category_id: int,
    lang: str,
    items: list[dict[str, Any]],
) -> int:
    init_db(db_path)
    fetched_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    with sqlite3.connect(db_path) as conn:
        for item in items:
            conn.execute(
                """
                INSERT INTO drouot_lots (
                    lot_id, category_id, lang, page, position, lot_number, title, lot_url,
                    image_link, currency, low_estimate, high_estimate, estimate, sale_timestamp,
                    sale_time, sale_type, sale_status, sale_id, auctioneer_id, raw_json, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lot_id) DO UPDATE SET
                    category_id=excluded.category_id,
                    lang=excluded.lang,
                    page=excluded.page,
                    position=excluded.position,
                    lot_number=excluded.lot_number,
                    title=excluded.title,
                    lot_url=excluded.lot_url,
                    image_link=excluded.image_link,
                    currency=excluded.currency,
                    low_estimate=excluded.low_estimate,
                    high_estimate=excluded.high_estimate,
                    estimate=excluded.estimate,
                    sale_timestamp=excluded.sale_timestamp,
                    sale_time=excluded.sale_time,
                    sale_type=excluded.sale_type,
                    sale_status=excluded.sale_status,
                    sale_id=excluded.sale_id,
                    auctioneer_id=excluded.auctioneer_id,
                    raw_json=excluded.raw_json,
                    fetched_at=excluded.fetched_at
                """,
                (
                    int(item["lot_id"]),
                    category_id,
                    lang,
                    item["page"],
                    item["position"],
                    item["lot_number"],
                    item["title"],
                    item["url"],
                    item["image_link"],
                    item["currency"],
                    item["low_estimate"],
                    item["high_estimate"],
                    item["estimate"],
                    item["sale_timestamp"],
                    item["sale_time"],
                    item["sale_type"],
                    item["sale_status"],
                    item["sale_id"],
                    item["auctioneer_id"],
                    json.dumps(item["raw_json"], ensure_ascii=False, separators=(",", ":")),
                    fetched_at,
                ),
            )
    return len(items)


def save_fetch_run(
    db_path: Path,
    source_url: str,
    api_url: str,
    category_id: int,
    lang: str,
    pages_fetched: int,
    saved_count: int,
    total_count: int | None,
    started_at: str,
) -> None:
    init_db(db_path)
    finished_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO drouot_fetch_runs (
                source_url, api_url, category_id, lang, pages_fetched, saved_count,
                total_count, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_url,
                api_url,
                category_id,
                lang,
                pages_fetched,
                saved_count,
                total_count,
                started_at,
                finished_at,
            ),
        )


def public_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key != "raw_json"}


def extract_auction_list(
    url: str,
    render: str,
    max_items: int,
    max_pages: int = 0,
    save_to_db: bool = True,
) -> dict[str, Any]:
    del render
    started = time.monotonic()
    started_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    lang, category_id, first_page = parse_source(url)
    total_count: int | None = None
    pages_fetched = 0
    preview_items: list[dict[str, Any]] = []
    total_items = 0
    saved_count = 0
    db_path = auction_db_path()
    if save_to_db:
        init_db(db_path)

    logger.info(
        "auction_list event=started source=api category=%d lang=%s first_page=%d max_pages=%d",
        category_id,
        lang,
        first_page,
        max_pages,
    )
    page = first_page
    while True:
        if max_pages and pages_fetched >= max_pages:
            break
        data = fetch_api_page(lang, category_id, page)
        lots = data.get("lots") or []
        if not isinstance(lots, list):
            raise AuctionListError(f"Drouot API page {page} does not contain a lots list")
        if total_count is None and isinstance(data.get("numFound"), int):
            total_count = int(data["numFound"])

        page_items: list[dict[str, Any]] = []
        for index, lot in enumerate(lots, 1):
            if isinstance(lot, dict) and lot.get("id"):
                item = normalize_lot(lot, lang, page, index)
                page_items.append(item)
                if len(preview_items) < max_items:
                    preview_items.append(item)
        total_items += len(page_items)
        if save_to_db and page_items:
            saved_count += upsert_lots(db_path, category_id, lang, page_items)
        pages_fetched += 1
        logger.info(
            "auction_list stage=page_done category=%d lang=%s page=%d lots=%d total_items=%d saved=%d",
            category_id,
            lang,
            page,
            len(lots),
            total_items,
            saved_count,
        )
        if len(lots) < DROUOT_PAGE_SIZE:
            break
        page += 1

    if not total_items:
        raise AuctionListError("No auction lots returned by Drouot API")

    api_url = build_api_url(lang, category_id, first_page)
    if save_to_db:
        save_fetch_run(db_path, url, api_url, category_id, lang, pages_fetched, saved_count, total_count, started_at)

    result = {
        "source_url": url,
        "fetched_url": api_url,
        "title": f"Drouot category {category_id}",
        "total_count": total_count,
        "pages_fetched": pages_fetched,
        "saved_count": saved_count,
        "database_path": str(db_path) if save_to_db else "",
        "items": [public_item(item) for item in preview_items],
    }
    logger.info(
        "auction_list event=done source=api category=%d lang=%s pages=%d items=%d saved=%d elapsed=%.2fs",
        category_id,
        lang,
        pages_fetched,
        total_items,
        saved_count,
        time.monotonic() - started,
    )
    return result
