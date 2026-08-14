from __future__ import annotations

import logging
import re
import time
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.cloudflare import (
    CloudflareBypassError,
    fetch_cloudflare_bypassed,
    is_cloudflare_blocked,
)
from app.services.extractor import (
    clean_text,
    fetch_rendered,
    fetch_static,
    should_try_cloudflare_bypass_from_fetch_error,
)


logger = logging.getLogger("uvicorn.error")

LOT_LINK_RE = re.compile(r"^/(?:[a-z]{2}/)?l/(\d+)(?:[-/].*)?$", re.I)
RESULT_COUNT_RE = re.compile(r"(\d[\d\s,.]*)\s*个结果")
SALE_TIME_RE = re.compile(
    r"(?:(?P<status>live|online|room|closed|已结束|即将开始)\s*)?"
    r"(?P<time>\d{1,2}月\d{1,2}日\s*\|\s*(?:上午|下午)?\s*\d{1,2}:\d{2})",
    re.I,
)
ESTIMATE_RE = re.compile(
    r"估价\s*(?P<estimate>[€$£¥]?\s*[\d\s,.]+(?:\s*-\s*[€$£¥]?\s*[\d\s,.]+)?)",
    re.I,
)


class AuctionListError(RuntimeError):
    pass


def parse_total_count(body_text: str) -> int | None:
    match = RESULT_COUNT_RE.search(body_text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def clean_title_from_card_text(raw_text: str, image_alt: str = "") -> str:
    title = raw_text
    title = SALE_TIME_RE.sub("", title, count=1)
    title = ESTIMATE_RE.sub("", title)
    title = re.sub(r"^\s*[-|:：]+\s*", "", title)
    title = clean_text(title).strip(" -|:：")
    if title:
        return title
    return clean_text(image_alt).strip(" -|:：")


def first_image_url(element: Any, base_url: str) -> str:
    for img in element.find_all("img"):
        raw = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-original")
            or img.get("data-lazy-src")
            or ""
        )
        if not raw and img.get("srcset"):
            raw = str(img.get("srcset")).split(",")[-1].strip().split(" ")[0]
        raw = clean_text(raw)
        if not raw or raw.startswith("data:"):
            continue
        alt = clean_text(img.get("alt"))
        haystack = f"{raw} {alt}".lower()
        if any(marker in haystack for marker in ("logo", "favicon", "icon", "avatar", "sprite")):
            continue
        return urljoin(base_url, raw)
    return ""


def parse_lot_anchor(anchor: Any, base_url: str) -> dict[str, Any] | None:
    href = clean_text(anchor.get("href"))
    match = LOT_LINK_RE.match(href)
    if not match:
        return None

    lot_id = match.group(1)
    raw_text = clean_text(anchor.get_text(" ", strip=True))
    if not raw_text:
        raw_text = clean_text(anchor.get("aria-label") or anchor.get("title"))

    image = first_image_url(anchor, base_url)
    image_alt = ""
    img = anchor.find("img")
    if img:
        image_alt = clean_text(img.get("alt"))

    sale_status = ""
    sale_time = ""
    sale_match = SALE_TIME_RE.search(raw_text)
    if sale_match:
        sale_status = clean_text(sale_match.group("status") or "")
        sale_time = clean_text(sale_match.group("time") or "")

    estimate = ""
    estimate_match = ESTIMATE_RE.search(raw_text)
    if estimate_match:
        estimate = clean_text(estimate_match.group("estimate"))

    title = clean_title_from_card_text(raw_text, image_alt)
    if not title and not image:
        return None

    return {
        "lot_id": lot_id,
        "title": title,
        "url": urljoin(base_url, href),
        "image_link": image,
        "sale_status": sale_status,
        "sale_time": sale_time,
        "estimate": estimate,
        "raw_text": raw_text,
    }


def parse_auction_list_html(html: str, final_url: str, max_items: int) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    page_title = clean_text(soup.title.string if soup.title else "")
    body_text = clean_text(soup.body.get_text(" ") if soup.body else "")
    heading = clean_text(soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "")

    items: list[dict[str, Any]] = []
    seen_lot_ids: set[str] = set()
    seen_urls: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        item = parse_lot_anchor(anchor, final_url)
        if not item:
            continue
        key = item["lot_id"] or item["url"]
        if key in seen_lot_ids or item["url"] in seen_urls:
            continue
        seen_lot_ids.add(key)
        seen_urls.add(item["url"])
        items.append(item)
        if len(items) >= max_items:
            break

    return {
        "fetched_url": final_url,
        "title": heading or page_title,
        "total_count": parse_total_count(body_text),
        "items": items,
    }


def fetch_auction_list_html(url: str, render: Literal["auto", "always", "never"]) -> tuple[str, str]:
    allow_cloudflare_bypass = render != "never"
    rendered = render == "always"
    parsed = urlparse(url)

    try:
        html, final_url = fetch_rendered(url) if rendered else fetch_static(url)
    except Exception as exc:
        if allow_cloudflare_bypass and should_try_browser_fallback(exc):
            logger.warning(
                "auction_list event=fetch_blocked_try_cloudflare host=%s render=%s reason=%r",
                parsed.netloc,
                render,
                str(exc),
            )
            try:
                return fetch_cloudflare_bypassed(url)
            except CloudflareBypassError as cf_exc:
                raise AuctionListError(f"Blocked by Cloudflare/security verification page: {cf_exc}") from cf_exc
        raise

    soup = BeautifulSoup(html, "lxml")
    page_title = clean_text(soup.title.string if soup.title else "")
    body_text = clean_text(soup.body.get_text(" ") if soup.body else "")
    if is_cloudflare_blocked(page_title, body_text, html):
        if not allow_cloudflare_bypass:
            raise AuctionListError("Blocked by Cloudflare/security verification page")
        logger.warning(
            "auction_list event=blocked_try_cloudflare host=%s final_url=%s title=%r",
            parsed.netloc,
            final_url,
            page_title,
        )
        try:
            return fetch_cloudflare_bypassed(final_url)
        except CloudflareBypassError as cf_exc:
            raise AuctionListError(f"Blocked by Cloudflare/security verification page: {cf_exc}") from cf_exc

    return html, final_url


def should_try_browser_fallback(exc: Exception) -> bool:
    if should_try_cloudflare_bypass_from_fetch_error(exc):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "read timed out",
            "connect timeout",
            "connection timed out",
            "tls",
            "ssl",
            "handshake",
        )
    )


def extract_auction_list(
    url: str,
    render: Literal["auto", "always", "never"],
    max_items: int,
) -> dict[str, Any]:
    started = time.monotonic()
    parsed = urlparse(url)
    logger.info(
        "auction_list event=started host=%s render=%s max_items=%d",
        parsed.netloc,
        render,
        max_items,
    )
    html, final_url = fetch_auction_list_html(url, render)
    result = parse_auction_list_html(html, final_url, max_items)

    if render == "auto" and not result["items"]:
        logger.info("auction_list event=retry_render reason=no_items host=%s", parsed.netloc)
        html, final_url = fetch_auction_list_html(url, "always")
        result = parse_auction_list_html(html, final_url, max_items)

    if not result["items"]:
        raise AuctionListError("No auction lots found on this page")

    result["source_url"] = url
    logger.info(
        "auction_list event=done host=%s final_host=%s items=%d total_count=%s elapsed=%.2fs",
        parsed.netloc,
        urlparse(result["fetched_url"]).netloc,
        len(result["items"]),
        result["total_count"],
        time.monotonic() - started,
    )
    return result
