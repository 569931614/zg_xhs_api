#!/usr/bin/env python3
"""
Extract product information and likely product images from an independent store
product page.

Usage:
  python product_page_extractor.py "https://example.com/products/item"
  python product_page_extractor.py "https://example.com/products/item" --render --download-images
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse
from urllib.request import url2pathname

import requests
from bs4 import BeautifulSoup

from app.services.cloudflare import (
    CloudflareBypassError,
    fetch_cloudflare_bypassed,
    is_cloudflare_blocked,
)


logger = logging.getLogger("uvicorn.error")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

MAX_PRODUCT_IMAGES = 12
DEFAULT_RENDER_CONCURRENCY = 2
SECOND_IMAGE_FILTER_DOMAINS = {"studio125.co.uk"}
EARLY_GALLERY_FILTER_DOMAINS = {"pauletteintstad.com"}
PRIMARY_GALLERY_ONLY_DOMAINS = {"ancien.co.uk", "pauletteintstad.com"}
HASH_PROJECT_ROUTE_DOMAINS = {"sauceldn.com"}
ATKRIS_PRODUCT_IMAGE_DOMAINS = {"atkris.com"}
FUNDAMENTE_DOMAINS = {"fundamente.nl"}
SITONVINTAGE_DOMAINS = {"sitonvintage.com"}
KNOWN_CURRENCY_CODES = {
    "AUD",
    "CAD",
    "CHF",
    "CNY",
    "DKK",
    "EUR",
    "GBP",
    "HKD",
    "JPY",
    "NOK",
    "SEK",
    "USD",
}

_render_semaphore: threading.BoundedSemaphore | None = None
_render_semaphore_limit = 0
_render_semaphore_lock = threading.Lock()

NOISE_RE = re.compile(
    r"(logo|icon|sprite|avatar|payment|paypal|visa|mastercard|amex|klarna|"
    r"afterpay|trust|badge|seal|banner|hero|background|bg-|newsletter|"
    r"social|facebook|instagram|tiktok|youtube|pinterest|review|star|"
    r"flag|placeholder|loading|spinner|favicon|screenshot|app[-_ ]?store|"
    r"mobile[-_ ]?app)",
    re.I,
)

PRODUCT_RE = re.compile(
    r"(product|products|prod|pdp|catalog|cdn\.shopify|woocommerce|media|"
    r"image|images|photo|gallery|zoom|large|main)",
    re.I,
)

SMALL_SIZE_RE = re.compile(r"(^|[_-])(\d{1,2})x(\d{1,2})([_\.-]|$)", re.I)


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return max(minimum, value)


def render_semaphore() -> threading.BoundedSemaphore:
    global _render_semaphore, _render_semaphore_limit
    limit = env_int("RENDER_CONCURRENCY", DEFAULT_RENDER_CONCURRENCY)
    with _render_semaphore_lock:
        if _render_semaphore is None or _render_semaphore_limit != limit:
            _render_semaphore = threading.BoundedSemaphore(limit)
            _render_semaphore_limit = limit
        return _render_semaphore


@dataclass
class ImageCandidate:
    url: str
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    width: int | None = None
    height: int | None = None
    alt: str = ""
    source: str = ""
    order: int = 10_000

    def add(self, points: int, reason: str) -> None:
        self.score += points
        if reason not in self.reasons:
            self.reasons.append(reason)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text


def clean_price_text(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    text = re.sub(r"\b(?:euros?|euro)\b", "EUR", text, flags=re.I)
    text = re.sub(r"(\d)\s*[,.-]+\s*(?=EUR\b)", r"\1 ", text, flags=re.I)
    text = re.sub(r"(\d)\s*[,.-]\s*-$", r"\1", text)
    text = re.sub(
        r"^(regular|sale|unit|public|trade|asking|retail|list|listed)\s+price\s*:?\s*",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"^(price|prix|preis|precio|prezzo)\s*:?\s*", "", text, flags=re.I)
    listed_price = re.match(
        r"^((?:[£$€]\s*\d[\d\s,.]*)|(?:[A-Z]{3}\s*\d[\d\s,.]*))\s*(?:[|·•/].*)?$",
        text,
    )
    if listed_price:
        return clean_text(listed_price.group(1))
    return text


def price_amount(value: Any) -> float | None:
    text = clean_price_text(value)
    match = re.search(r"\d[\d\s.,]*", text)
    if not match:
        return None
    number = re.sub(r"\s+", "", match.group(0))
    if "," in number and "." in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        parts = number.split(",")
        number = "".join(parts) if len(parts[-1]) == 3 else number.replace(",", ".")
    elif "." in number:
        parts = number.split(".")
        if len(parts) > 2 or len(parts[-1]) == 3:
            number = "".join(parts)
    try:
        return float(number)
    except ValueError:
        return None


def price_is_valid(value: Any) -> bool:
    text = clean_price_text(value).lower()
    if re.search(r"\b(?:login|request|estimate|estimated|sold|on request|upon request|contact|enquire|inquire)\b", text):
        return False
    amount = price_amount(text)
    return amount is not None and amount > 0


def format_shopify_price(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        cents = int(value)
    except (TypeError, ValueError):
        return clean_price_text(value)
    return f"{cents / 100:.2f}"


def clean_dimension_text(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    text = re.split(r"\bPrice\s*[:：]", text, maxsplit=1, flags=re.I)[0]
    return clean_text(text).strip(" .;-")


def is_next_image_proxy_path(path: str) -> bool:
    normalized = path.rstrip("/")
    return normalized.endswith("/_next/image") or normalized.endswith("/_vercel/image")


def compact_url(url: str) -> str:
    parsed = urlparse(url)
    keep_params = []
    if is_next_image_proxy_path(parsed.path):
        keep_names = {"url", "w", "q", "width", "height", "format", "quality"}
    else:
        keep_names = {"v", "width", "height", "format", "quality"}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in keep_names:
            keep_params.append((key, value))
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(keep_params),
            "",
        )
    )


def image_identity_key(url: str) -> str:
    parsed = urlparse(url)
    if is_next_image_proxy_path(parsed.path):
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        nested_url = params.get("url")
        if nested_url:
            return image_identity_key(urljoin(url, nested_url))
    path = parsed.path
    shopify_key = shopify_image_identity_key(parsed)
    if shopify_key:
        return shopify_key
    if "static.wixstatic.com" in parsed.netloc and "/media/" in path:
        media_id = path.split("/media/", 1)[1].split("/", 1)[0]
        if media_id:
            return f"wix:{media_id}"
    parts = path.rsplit("/", 1)
    dirname = parts[0] if len(parts) == 2 else ""
    filename = unquote(parts[-1] if parts else path).lower()
    stem = re.sub(r"\.(jpe?g|png|webp|avif)$", "", filename, flags=re.I)
    stem = re.sub(r"[_-](?:\d{2,5}x\d{2,5}|\d{2,5}x(?:-q\d{1,3})?|scaled)$", "", stem, flags=re.I)
    stem = re.sub(r"[_-](?:600|804|1026|1080x?|1281|1536|1800x?|1920|2000x?)$", "", stem, flags=re.I)
    return f"{parsed.netloc}{dirname}/{stem}".lower()


def normalize_wix_media_url(url: str) -> str:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if hostname != "static.wixstatic.com" or "/media/" not in parsed.path:
        return url

    media_id = parsed.path.split("/media/", 1)[1].split("/", 1)[0]
    if not media_id or not re.search(r"\.(jpe?g|png|webp|avif)$", media_id, re.I):
        return url

    return urlunparse((parsed.scheme, parsed.netloc, f"/media/{media_id}", "", "", ""))


def shopify_image_identity_key(parsed: Any) -> str:
    hostname = (parsed.hostname or "").lower()
    path = unquote(parsed.path).lower()
    match = re.search(r"/cdn/shop/(files|products|collections)/(.+)$", path)
    if not match and hostname == "cdn.shopify.com":
        match = re.search(r"/s/files/\d+/\d+/\d+/\d+/(files|products|collections)/(.+)$", path)
    if not match:
        return ""
    folder, filename = match.groups()
    filename = filename.rsplit("/", 1)[-1]
    stem = re.sub(r"\.(jpe?g|png|webp|avif)$", "", filename, flags=re.I)
    stem = re.sub(r"[_-](?:\d{2,5}x\d{2,5}|\d{2,5}x(?:-q\d{1,3})?|scaled)$", "", stem, flags=re.I)
    stem = re.sub(r"[_-](?:600|804|1026|1080x?|1281|1536|1800x?|1920|2000x?)$", "", stem, flags=re.I)
    return f"shopify:{folder}/{stem}"


def dedupe_image_candidates(candidates: list[ImageCandidate]) -> list[ImageCandidate]:
    best_by_key: dict[str, ImageCandidate] = {}
    for item in candidates:
        key = image_identity_key(item.url)
        previous = best_by_key.get(key)
        if previous is None or item.score > previous.score:
            best_by_key[key] = item
    return sorted(best_by_key.values(), key=lambda c: c.score, reverse=True)


def normalized_hostname(url: str) -> str:
    hostname = (urlparse(url).hostname or "").lower()
    return hostname[4:] if hostname.startswith("www.") else hostname


def resolve_hash_project_url(url: str) -> str:
    parsed = urlparse(url)
    hostname = normalized_hostname(url)
    if hostname not in HASH_PROJECT_ROUTE_DOMAINS or not parsed.fragment.startswith("/"):
        return url
    fragment_path = parsed.fragment.split("?", 1)[0].strip()
    if not fragment_path or fragment_path == "/":
        return url
    if not fragment_path.endswith("/"):
        fragment_path += "/"
    return urlunparse((parsed.scheme, parsed.netloc, fragment_path, "", "", ""))


def filter_domain_image_candidates(candidates: list[ImageCandidate], page_url: str) -> list[ImageCandidate]:
    hostname = normalized_hostname(page_url)
    if hostname in PRIMARY_GALLERY_ONLY_DOMAINS:
        candidates = prefer_primary_product_gallery(candidates)
    if hostname in EARLY_GALLERY_FILTER_DOMAINS:
        candidates = filter_early_contiguous_gallery(candidates)
    if hostname in SECOND_IMAGE_FILTER_DOMAINS and len(candidates) >= 2:
        return candidates[:1] + candidates[2:]
    return candidates


def filter_early_contiguous_gallery(candidates: list[ImageCandidate]) -> list[ImageCandidate]:
    ordered = sorted(
        [
            item
            for item in candidates
            if item.order < 100_000
            and item.score >= 20
            and re.search(r"\.(jpe?g|png|webp|avif)(\?|$)", urlparse(item.url).path, re.I)
        ],
        key=lambda item: item.order,
    )
    if len(ordered) < 4:
        return candidates

    runs: list[list[ImageCandidate]] = []
    current = [ordered[0]]
    for item in ordered[1:]:
        if item.order - current[-1].order <= 2:
            current.append(item)
        else:
            runs.append(current)
            current = [item]
    runs.append(current)

    first_gallery = next((run for run in runs if len(run) >= 3), None)
    if not first_gallery:
        return candidates
    return sorted(first_gallery, key=lambda item: item.order)


def prefer_primary_product_gallery(candidates: list[ImageCandidate]) -> list[ImageCandidate]:
    gallery = [
        item
        for item in candidates
        if "inside primary product gallery" in item.reasons
    ]
    if len(gallery) < 2:
        return candidates
    return sorted(gallery, key=lambda item: (item.order, -item.score))


def prefer_authoritative_image_list(candidates: list[ImageCandidate], image_urls: list[str]) -> list[ImageCandidate]:
    keys = list(dict.fromkeys(image_identity_key(url) for url in image_urls if image_identity_key(url)))
    if len(keys) < 2:
        return candidates
    key_order = {key: index for index, key in enumerate(keys)}
    matched = [item for item in candidates if image_identity_key(item.url) in key_order]
    if len(matched) < 2:
        return candidates
    return sorted(matched, key=lambda item: (key_order[image_identity_key(item.url)], -item.score, item.order))


def prefer_page_path_images(candidates: list[ImageCandidate], page_url: str) -> list[ImageCandidate]:
    parsed = urlparse(page_url)
    path_parts = [part for part in parsed.path.strip("/").split("/") if part]
    if not path_parts:
        return candidates
    slug = path_parts[-1].lower()
    if len(slug) < 8 or not re.search(r"[a-z]", slug):
        return candidates
    matched = [
        item
        for item in candidates
        if slug in unquote(urlparse(url_for_series(item.url)).path).lower()
    ]
    if len(matched) < 3:
        return candidates
    matched_ids = {id(item) for item in matched}
    return matched + [item for item in candidates if id(item) not in matched_ids and item.source == "structured"]


def prefer_numbered_gallery(candidates: list[ImageCandidate]) -> list[ImageCandidate]:
    numbered: list[tuple[int, ImageCandidate]] = []
    for item in candidates:
        match = re.match(r"^view\s+(\d+)$", item.alt.strip(), re.I)
        if match:
            numbered.append((int(match.group(1)), item))
    if len(numbered) < 3:
        return candidates
    numbered.sort(key=lambda pair: pair[0])
    expected = 1
    gallery: list[ImageCandidate] = []
    seen_numbers: set[int] = set()
    for number, item in numbered:
        if number in seen_numbers:
            continue
        if number == expected:
            gallery.append(item)
            seen_numbers.add(number)
            expected += 1
            continue
        if gallery:
            break
    if len(gallery) >= 3:
        for item in gallery:
            item.add(30, "numbered gallery sequence")
        return gallery
    return candidates


def prefer_leading_filename_series(candidates: list[ImageCandidate]) -> list[ImageCandidate]:
    if len(candidates) < 8:
        return candidates
    series: list[ImageCandidate] = []
    first_prefix = ""
    for item in candidates:
        alt = item.alt.strip()
        match = re.match(r"^(.+?)(\d{3,5})\.(jpe?g|png|webp)$", alt, re.I)
        if not match:
            break
        prefix = re.sub(r"[-_\s]+$", "", match.group(1).lower())
        if not re.search(r"[a-z]", prefix):
            break
        if not first_prefix:
            first_prefix = prefix
        if prefix != first_prefix:
            break
        series.append(item)
    if len(series) >= 6:
        return series
    return candidates


def prefer_early_product_gallery(candidates: list[ImageCandidate]) -> list[ImageCandidate]:
    numbered = []
    for item in candidates:
        match = re.match(r"^view\s+(\d+)$", item.alt.strip(), re.I)
        if not match:
            break
        numbered.append(int(match.group(1)))
    if len(numbered) >= 3 and numbered == list(range(1, len(numbered) + 1)):
        return candidates

    useful = [
        item
        for item in candidates
        if item.score >= 25 and item.order < 100_000 and not re.search(r"\.(svg|gif)(\?|$)", urlparse(item.url).path, re.I)
    ]
    if len(useful) < 8:
        return candidates
    first_order = min(item.order for item in useful)
    early = [item for item in useful if item.order <= first_order + 12]
    if len(early) < 4:
        return candidates
    groups: dict[str, list[ImageCandidate]] = {}
    for item in early:
        parsed = urlparse(url_for_series(item.url))
        groups.setdefault(f"{parsed.netloc}{parsed.path.rsplit('/', 1)[0]}", []).append(item)
    best_group = max(groups.values(), key=lambda group: (len(group), -min(item.order for item in group)))
    if len(best_group) < 4:
        return candidates
    early_ids = {id(item) for item in best_group}
    early_sorted = sorted(best_group, key=lambda item: (item.order, -item.score))
    return early_sorted + [item for item in candidates if id(item) not in early_ids and item.source == "structured"]


def normalize_match_text(value: str) -> str:
    value = clean_product_title(value)
    value = re.sub(r"\.(jpe?g|png|webp|avif)$", "", value, flags=re.I)
    value = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return clean_text(value)


def name_tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]{4,}", normalize_match_text(value))}


def url_for_series(url: str) -> str:
    parsed = urlparse(url)
    if is_next_image_proxy_path(parsed.path):
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        nested_url = params.get("url")
        if nested_url:
            return urljoin(url, nested_url)
    return url


def strip_trailing_image_sequence_suffix(stem: str) -> str:
    while True:
        match = re.match(r"^(.+?)-(\d{1,4})$", stem)
        if not match:
            return stem
        suffix = int(match.group(2))
        if 1800 <= suffix <= 2099:
            return stem
        stem = match.group(1)


def image_series_key(item: ImageCandidate) -> str:
    parsed = urlparse(url_for_series(item.url))
    path = parsed.path
    filename = unquote(path.rsplit("/", 1)[-1]).lower()
    if not re.search(r"\.(jpe?g|png|webp|avif)$", filename, re.I):
        return ""
    stem = re.sub(r"\.(jpe?g|png|webp|avif)$", "", filename, flags=re.I)
    stem = re.sub(r"[_-](?:\d{2,5}x\d{2,5}|\d{2,5}x(?:-q\d{1,3})?|scaled)$", "", stem, flags=re.I)
    stem = re.sub(r"[_-](?:600|804|1026|1080x?|1281|1536|1800x?|1920|2000x?)$", "", stem, flags=re.I)
    if re.match(r"^(?:img|dsc|dscf|dscn|pict|photo)[_-]?\d{3,6}$", stem, re.I):
        return f"{parsed.netloc}{path.rsplit('/', 1)[0]}/camera-sequence"
    if re.search(r"(screenshot|skærmbillede|screen-shot|whatsapp|instagram)", stem, re.I):
        return ""
    compact_stem = stem.replace("_", "").replace("-", "")
    if stem.endswith("_n") or stem.endswith("_n_master") or (
        len(compact_stem) >= 16 and all(char in "0123456789abcdef" for char in compact_stem)
    ):
        return ""
    if stem in {"image", "photo", "main", "product", "0", ""}:
        return ""
    stem = re.sub(r"[_\s]+", "-", stem)
    match = re.match(r"^(.+?)-\d{1,4}-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", stem, re.I)
    if match:
        stem = match.group(1)
    stem = strip_trailing_image_sequence_suffix(stem)
    match = re.match(r"^(.+?)[_-][a-z]$", stem)
    if match:
        stem = match.group(1)
    stem = re.sub(r"[-_]+$", "", stem)
    if len(stem) < 4:
        return ""
    return stem


def alt_group_key(item: ImageCandidate, product_name: str) -> str:
    alt = normalize_match_text(item.alt)
    if not alt or re.search(r"^\d+$", alt):
        return ""
    if re.search(r"\.(jpe?g|png|webp|avif)$", item.alt, re.I):
        return ""
    product = normalize_match_text(product_name)
    if product and (alt == product or alt in product or product in alt):
        return alt
    product_tokens = name_tokens(product_name)
    alt_tokens = name_tokens(item.alt)
    if len(product_tokens & alt_tokens) >= 2:
        return alt
    return ""


def candidate_matches_product_name(item: ImageCandidate, product_name: str) -> bool:
    tokens = name_tokens(product_name)
    if not tokens:
        return False
    haystack = normalize_match_text(f"{item.url} {item.alt}")
    return len(tokens & set(haystack.split())) >= min(3, len(tokens))


def dominant_host(items: list[ImageCandidate]) -> str:
    counts: dict[str, int] = {}
    for item in items:
        host = urlparse(url_for_series(item.url)).netloc.lower()
        if host:
            counts[host] = counts.get(host, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda pair: pair[1])[0]


def prefer_current_product_group(
    candidates: list[ImageCandidate],
    product_name: str,
) -> list[ImageCandidate]:
    if len(candidates) < 4:
        return candidates

    useful = [
        item
        for item in candidates
        if item.score >= 20 and not re.search(r"\.(svg|gif|mp4|mov|webm)(\?|$)", urlparse(item.url).path, re.I)
    ]
    if len(useful) < 4:
        return candidates

    alt_groups: dict[str, list[ImageCandidate]] = {}
    for item in useful:
        key = alt_group_key(item, product_name)
        if key:
            alt_groups.setdefault(key, []).append(item)
    if alt_groups:
        best_alt = max(alt_groups.values(), key=lambda group: (len(group), sum(i.score for i in group)))
        if len(best_alt) >= 3:
            host = dominant_host(best_alt)
            keep_ids = {id(item) for item in best_alt}
            return [
                item
                for item in candidates
                if id(item) in keep_ids and (not host or urlparse(url_for_series(item.url)).netloc.lower() == host)
            ]

    series_groups: dict[str, list[ImageCandidate]] = {}
    for item in useful:
        key = image_series_key(item)
        if key:
            series_groups.setdefault(key, []).append(item)
    if series_groups:
        product_tokens = name_tokens(product_name)

        def group_rank(group: list[ImageCandidate]) -> tuple[int, int, int, int]:
            key = image_series_key(group[0])
            key_tokens = set(re.findall(r"[a-z0-9]{4,}", key))
            token_overlap = len(product_tokens & key_tokens)
            return (
                token_overlap,
                len(group),
                sum(item.score for item in group),
                -min(item.order for item in group),
            )

        best_series = max(series_groups.values(), key=group_rank)
        best_key = image_series_key(best_series[0])
        if len(best_series) >= 3:
            keep_ids = {id(item) for item in best_series}
            for item in useful:
                if id(item) in keep_ids:
                    continue
                if candidate_matches_product_name(item, product_name) and item.score >= 60 and not image_series_key(item):
                    keep_ids.add(id(item))
            return [item for item in candidates if id(item) in keep_ids or image_series_key(item) == best_key]

    top_score = max(item.score for item in useful)
    if top_score >= 180:
        high_confidence = [item for item in useful if item.score >= top_score * 0.6]
        if len(high_confidence) >= 3:
            keep_ids = {id(item) for item in high_confidence}
            return [item for item in candidates if id(item) in keep_ids]

    return candidates


def normalize_url(raw: str, base_url: str) -> str:
    if not raw:
        return ""
    raw = raw.strip().strip("'\"")
    if raw.startswith("//"):
        raw = "https:" + raw
    url = urljoin(base_url, raw)
    url = url_for_series(url)
    url = normalize_wix_media_url(url)
    return compact_url(url)


def pick_srcset_best(srcset: str) -> str:
    best_url = ""
    best_score = -1
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        url = bits[0]
        score = 1
        if len(bits) > 1:
            token = bits[1].lower()
            if token.endswith("w") and token[:-1].isdigit():
                score = int(token[:-1])
            elif token.endswith("x"):
                try:
                    score = int(float(token[:-1]) * 1000)
                except ValueError:
                    pass
        if score > best_score:
            best_url = url
            best_score = score
    return best_url


def pick_srcset_width(srcset: str, preferred_width: int) -> str:
    best_url = ""
    best_distance: int | None = None
    for part in srcset.split(","):
        bits = part.strip().split()
        if len(bits) < 2:
            continue
        token = bits[1].lower()
        if not token.endswith("w") or not token[:-1].isdigit():
            continue
        width = int(token[:-1])
        distance = abs(width - preferred_width)
        if best_distance is None or distance < best_distance or (
            distance == best_distance and width == preferred_width
        ):
            best_url = bits[0]
            best_distance = distance
    return best_url


def image_url_from_img_tag(img: Any, base_url: str, preferred_srcset_width: int | None = None) -> str:
    raw_url = ""
    if preferred_srcset_width is not None and img.get("srcset"):
        raw_url = pick_srcset_width(str(img["srcset"]), preferred_srcset_width)
    if not raw_url:
        raw_url = clean_text(img.get("src") or img.get("data-src") or "")
    return normalize_url(raw_url, base_url)


def extract_atkris_product_image_urls(soup: BeautifulSoup, base_url: str) -> list[str]:
    urls: list[str] = []

    featured = soup.select_one(".featured-image img")
    if featured is not None:
        featured_url = image_url_from_img_tag(featured, base_url, preferred_srcset_width=1600)
        if featured_url:
            urls.append(featured_url)

    gallery_images = soup.select(".product-slider-thumbs .slider img")
    if not gallery_images:
        gallery_images = soup.select(".product-slider-thumbs .slider-nav img")
    for img in gallery_images:
        gallery_url = image_url_from_img_tag(img, base_url)
        if gallery_url:
            urls.append(gallery_url)

    return list(dict.fromkeys(urls))


def extract_fundamente_product_image_urls(soup: BeautifulSoup, base_url: str) -> list[str]:
    urls: list[str] = []
    for img in soup.select(".gallery--collection img.gallery--grid__img"):
        image_url = image_url_from_img_tag(img, base_url)
        if image_url:
            urls.append(image_url)
    return list(dict.fromkeys(urls))


def extract_sitonvintage_product_image_urls(soup: BeautifulSoup, base_url: str) -> list[str]:
    urls: list[str] = []
    for link in soup.select(".elementor-gallery__container a.e-gallery-item[href]"):
        href = clean_text(link.get("href"))
        if re.search(r"\.(jpe?g|png|webp|avif)(\?|$)", href, re.I):
            urls.append(normalize_url(href, base_url))
    return list(dict.fromkeys(urls))


def extract_domain_product_image_urls(soup: BeautifulSoup, base_url: str) -> list[str]:
    hostname = normalized_hostname(base_url)
    if hostname in ATKRIS_PRODUCT_IMAGE_DOMAINS:
        return extract_atkris_product_image_urls(soup, base_url)
    if hostname in FUNDAMENTE_DOMAINS:
        return extract_fundamente_product_image_urls(soup, base_url)
    if hostname in SITONVINTAGE_DOMAINS:
        return extract_sitonvintage_product_image_urls(soup, base_url)
    return []


def flatten_jsonld(node: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(node, list):
        for item in node:
            items.extend(flatten_jsonld(item))
    elif isinstance(node, dict):
        items.append(node)
        for key in ("@graph", "itemListElement"):
            if key in node:
                items.extend(flatten_jsonld(node[key]))
    return items


def jsonld_type_matches(item: dict[str, Any], target: str) -> bool:
    value = item.get("@type") or item.get("type")
    if isinstance(value, list):
        return any(str(v).lower() == target.lower() for v in value)
    return str(value).lower() == target.lower()


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def extract_offer_price(offer: dict[str, Any]) -> tuple[str, str]:
    price = clean_price_text(offer.get("price"))
    currency = clean_text(offer.get("priceCurrency")).upper()
    if price and currency:
        return price, currency

    for spec in as_list(offer.get("priceSpecification")):
        if not isinstance(spec, dict):
            continue
        spec_price = clean_price_text(spec.get("price"))
        spec_currency = clean_text(spec.get("priceCurrency")).upper()
        if spec_price:
            return spec_price, spec_currency or currency
    return price, currency


def extract_image_urls(value: Any, base_url: str) -> list[str]:
    urls: list[str] = []
    for item in as_list(value):
        if isinstance(item, str):
            urls.append(normalize_url(item, base_url))
        elif isinstance(item, dict):
            for key in ("url", "contentUrl", "src"):
                if item.get(key):
                    urls.append(normalize_url(str(item[key]), base_url))
    return [u for u in urls if u]


def merge_product_info(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
    merged = dict(primary)
    for key, value in secondary.items():
        if value in (None, "", [], {}):
            continue
        if key == "details" and isinstance(value, dict) and isinstance(merged.get(key), dict):
            combined = dict(value)
            combined.update(merged[key])
            merged[key] = combined
            continue
        if key == "price":
            existing = merged.get(key)
            if not existing or (not price_is_valid(existing) and price_is_valid(value)):
                merged[key] = value
            continue
        if key == "currency" and not merged.get(key):
            merged[key] = value
            continue
        if not merged.get(key):
            merged[key] = value
    return merged


def clean_product_title(title: str) -> str:
    title = clean_text(title)
    if not title:
        return ""
    separators = [" — ", " – ", " - ", " | "]
    suffix_noise = re.compile(
        r"(gallery|galleria|galerie|shop|store|collectibles|design|mobilia|"
        r"mdrn|modern living|atkris|objekt|béton brut|beton brut|daddy deco|"
        r"paulette|approved|spazio leone|the oblist|envan rijn|ancien et jolie|sauce)",
        re.I,
    )
    for separator in separators:
        parts = [part.strip() for part in title.split(separator) if part.strip()]
        if len(parts) >= 2 and suffix_noise.search(parts[-1]):
            title = clean_text(separator.join(parts[:-1]))
            break
    title = re.sub(r"\s+[£$€]\s?[\d,.]+(?:\s*[A-Z]{3})?(?:\s*\|\s*Item\s*\(\d+\))?$", "", title, flags=re.I).strip()
    title = re.sub(r"\s*\|\s*Item\s*\(\d+\)\s*$", "", title, flags=re.I).strip()
    return title


def clean_detail_key(key: Any) -> str:
    return clean_text(str(key)).strip(":").strip()


def clean_detail_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, list):
        items = [clean_detail_value(item) for item in value]
        return [item for item in items if item not in (None, "", [], {})]
    if isinstance(value, dict):
        item: dict[str, Any] = {}
        for key, nested_value in value.items():
            detail_key = clean_detail_key(key)
            if not detail_key:
                continue
            cleaned = clean_detail_value(nested_value)
            if cleaned not in (None, "", [], {}):
                item[detail_key] = cleaned
        return item
    return clean_text(value)


def details_from_mapping(mapping: dict[str, Any], excluded_keys: set[str] | None = None) -> dict[str, Any]:
    excluded = {key.lower() for key in (excluded_keys or set())}
    details: dict[str, Any] = {}
    for key, value in mapping.items():
        detail_key = clean_detail_key(key)
        if not detail_key or detail_key.lower() in excluded:
            continue
        cleaned = clean_detail_value(value)
        if cleaned not in (None, "", [], {}):
            details[normalize_detail_label(detail_key)] = cleaned
    return details


def details_from_property_values(value: Any) -> dict[str, Any]:
    details: dict[str, Any] = {}
    for item in as_list(value):
        if not isinstance(item, dict):
            continue
        name = clean_detail_key(item.get("name") or item.get("propertyID"))
        if not name:
            continue
        detail_value = clean_detail_value(item.get("value") or item.get("description"))
        if detail_value not in (None, "", [], {}):
            details[normalize_detail_label(name)] = detail_value
    return details


def extract_labeled_detail_pairs(text: str, labels: list[str]) -> dict[str, str]:
    normalized_text = clean_text(text)
    if not normalized_text:
        return {}
    label_pattern = "|".join(re.escape(label) for label in sorted(labels, key=len, reverse=True))
    pattern = re.compile(rf"(?P<label>{label_pattern})\s*[:：]\s*", re.I)
    matches = list(pattern.finditer(normalized_text))
    details: dict[str, str] = {}
    for index, match in enumerate(matches):
        value_start = match.end()
        value_end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized_text)
        label = clean_detail_key(match.group("label"))
        value = clean_detail_value(normalized_text[value_start:value_end])
        if label and isinstance(value, str) and value:
            details[normalize_detail_label(label)] = value.strip(" .;-")
    return details


def is_weak_existing_name(name: str) -> bool:
    normalized = clean_text(name).lower()
    if normalized in {"", "shop", "store", "home", "monument", "spazio leone", "objekt"}:
        return True
    return bool(
        re.search(
            r"(tables|seating|storage|mirrors|lighting|objects|new arrivals|gallery|galerie|shop|store)\s*(—|–|-|\|)",
            normalized,
            re.I,
        )
    )


def extract_jsonld_products(soup: BeautifulSoup, base_url: str) -> tuple[dict[str, Any], list[str]]:
    product_info: dict[str, Any] = {}
    product_images: list[str] = []
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for item in flatten_jsonld(parsed):
            if not jsonld_type_matches(item, "Product"):
                continue
            offer = as_list(item.get("offers"))
            offer0 = offer[0] if offer and isinstance(offer[0], dict) else {}
            brand = item.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name")
            rating = item.get("aggregateRating")
            if isinstance(rating, dict):
                rating = {
                    "ratingValue": rating.get("ratingValue"),
                    "reviewCount": rating.get("reviewCount") or rating.get("ratingCount"),
                }
            price, currency = extract_offer_price(offer0)
            current = {
                "name": clean_product_title(clean_text(item.get("name"))),
                "description": clean_text(item.get("description")),
                "sku": clean_text(item.get("sku") or item.get("mpn")),
                "brand": clean_text(brand),
                "price": price,
                "currency": currency,
                "availability": clean_text(offer0.get("availability")),
                "url": normalize_url(clean_text(item.get("url") or offer0.get("url")), base_url),
                "rating": rating,
                "source": "json-ld",
            }
            details = details_from_mapping(
                item,
                {
                    "@context",
                    "@type",
                    "type",
                    "name",
                    "description",
                    "image",
                    "offers",
                    "url",
                    "aggregateRating",
                    "additionalProperty",
                },
            )
            details.update(details_from_property_values(item.get("additionalProperty")))
            if details:
                current["details"] = details
            add_synthesized_dimensions(current)
            product_info = merge_product_info(product_info, current)
            product_images.extend(extract_image_urls(item.get("image"), base_url))
    return product_info, list(dict.fromkeys(product_images))


def normalize_detail_label(label: str) -> str:
    normalized = clean_text(label).strip(":").strip()
    lower = normalized.lower()
    canonical_labels = {
        "place of origin": "Place of Origin",
        "country of origin": "Place of Origin",
        "country of manufacture": "Place of Origin",
        "land van herkomst": "Place of Origin",
        "origin country": "Place of Origin",
        "production country": "Place of Origin",
        "made in": "Place of Origin",
        "date of manufacture": "Date of Manufacture",
        "year of manufacture": "Date of Manufacture",
        "materials": "Material",
        "materials and techniques": "Materials and Techniques",
        "designed by": "Designer",
        "produced by": "Manufacturer",
        "producer": "Manufacturer",
        "maker": "Manufacturer",
        "reference number": "Reference Number",
        "ref": "Reference Number",
    }
    if lower in canonical_labels:
        return canonical_labels[lower]
    if lower in {
        "dimensions",
        "dimension",
        "measurements",
        "measurement",
        "size",
        "sizes",
    }:
        return "dimensions"
    if lower in {"sku", "stock keeping unit"}:
        return "sku"
    if lower in {"mpn", "manufacturer part number"}:
        return "mpn"
    return normalized


def extract_meta_info(soup: BeautifulSoup, base_url: str) -> tuple[dict[str, Any], list[str]]:
    def meta(*names: str) -> str:
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return clean_text(tag["content"])
        return ""

    title = clean_product_title(meta("og:title", "twitter:title") or clean_text(soup.title.string if soup.title else ""))
    fallback_title = clean_product_title(clean_text(soup.title.string if soup.title else ""))
    if is_weak_existing_name(title) and fallback_title:
        title = fallback_title
    description = meta("og:description", "description", "twitter:description")
    canonical = soup.find("link", attrs={"rel": re.compile("canonical", re.I)})
    info = {
        "name": title,
        "description": description,
        "url": normalize_url(canonical.get("href"), base_url) if canonical else base_url,
        "source": "meta",
    }
    price = meta("product:price:amount", "og:price:amount")
    currency = meta("product:price:currency", "og:price:currency")
    if price:
        info["price"] = clean_price_text(price)
    if currency:
        info["currency"] = clean_text(currency).upper()
    images = [
        normalize_url(meta("og:image", "twitter:image", "twitter:image:src"), base_url),
    ]
    return info, [u for u in images if u]


def extract_nextjs_product_info(soup: BeautifulSoup) -> dict[str, Any]:
    script_text = "\n".join(script.get_text() for script in soup.find_all("script"))
    if "dimensions" not in script_text and "heightCm" not in script_text:
        return {}

    quote = r'(?:\\"|")'

    def string_field(name: str) -> str:
        match = re.search(rf"{quote}{re.escape(name)}{quote}\s*:\s*{quote}([^\"\\]+){quote}", script_text)
        return clean_text(match.group(1)) if match else ""

    def number_field(name: str) -> str:
        match = re.search(rf"{quote}{re.escape(name)}{quote}\s*:\s*(-?\d+(?:\.\d+)?)", script_text)
        return clean_text(match.group(1)) if match else ""

    details: dict[str, Any] = {}
    dimensions = string_field("dimensions")
    if dimensions and dimensions != "$undefined":
        details["dimensions"] = dimensions
    for label, field_name in (
        ("Height cm", "heightCm"),
        ("Width cm", "widthCm"),
        ("Depth cm", "depthCm"),
    ):
        value = number_field(field_name)
        if value:
            details[label] = value

    info: dict[str, Any] = {"source": "nextjs-product-data"}
    if details:
        info["details"] = details
    if details.get("dimensions"):
        info["dimensions"] = str(details["dimensions"])
    return info


def line_looks_like_dimensions(line: str) -> bool:
    line = clean_dimension_text(line)
    if not line or len(line) > 240:
        return False
    if re.search(r"(http|copyright|cookie|newsletter|shipping|delivery|email|vat)", line, re.I):
        return False
    has_unit = bool(re.search(r"(?:cm|mm|in|inch|inches|m)\b|″|”", line, re.I))
    if not has_unit:
        return False
    return bool(
        re.search(
            r"\b(?:W|D|H|L|SH|AH|Dia|Diameter|Height|Width|Depth|Length|Seat\s+height)\.?\s*[:=\-]?\s*\d",
            line,
            re.I,
        )
        or re.search(
            r"\d+(?:[.,]\d+)?\s*(?:cm|mm|in|inch|inches|m)?\s*(?:x|×)\s*"
            r"\d+(?:[.,]\d+)?(?:\s*(?:cm|mm|in|inch|inches|m))?",
            line,
            re.I,
        )
        or re.search(r"\d+(?:[.,]\d+)?\s*(?:cm|mm|in|inch|inches|m)\s*\([hwdbl]\)", line, re.I)
    )


def labeled_value_looks_like_dimensions(value: str) -> bool:
    value = clean_dimension_text(value)
    if line_looks_like_dimensions(value):
        return True
    return bool(
        re.search(
            r"^\d+(?:[.,]\d+)?\s*(?:x|×)\s*\d+(?:[.,]\d+)?"
            r"(?:\s*(?:x|×)\s*\d+(?:[.,]\d+)?)?\s*$",
            value,
            re.I,
        )
    )


def extract_dimensions_from_text(text: str) -> str:
    text = clean_text(text)
    if not text:
        return ""
    measurement_in_cm = re.search(
        r"\b(?:Measurements?\s+)?in\s+cm\s+"
        r"\d+(?:[.,]\d+)?\s*(?:x|×)\s*\d+(?:[.,]\d+)?\s*(?:x|×)\s*\d+(?:[.,]\d+)?h?\b",
        text,
        re.I,
    )
    if measurement_in_cm:
        return clean_text(measurement_in_cm.group(0))
    labeled = re.search(
        r"(Dimensions?|Measurements?|Size)\s*[;:,-]?\s*"
        r"(.{0,180}?\d(?:[^.!?;]|[x×;,-]){0,180}?(?:cm|mm|in|inch|inches|″|”))",
        text,
        re.I,
    )
    if labeled:
        candidate = clean_dimension_text(labeled.group(2))
        if line_looks_like_dimensions(candidate):
            return candidate

    sentence_candidates = re.split(r"(?<=[.!?])\s+", text)
    for sentence in sentence_candidates:
        if line_looks_like_dimensions(sentence):
            return clean_dimension_text(sentence)
    return ""


def extract_dimensions_from_lines(lines: list[str]) -> str:
    groups: list[list[str]] = []
    current: list[str] = []
    previous_was_label = False
    for line in lines:
        normalized = clean_text(line)
        if not normalized:
            continue
        label_only = bool(re.match(r"^(Dimensions?|Measurements?|Measurement|Size|Sizes)\s*:?\s*$", normalized, re.I))
        same_line = re.match(r"^(Dimensions?|Measurements?|Measurement|Size|Sizes)\s*[;:]\s*(.+)$", normalized, re.I)
        value = clean_dimension_text(same_line.group(2)) if same_line else clean_dimension_text(normalized)
        looks_like = line_looks_like_dimensions(value)
        if same_line and looks_like:
            groups.append([value])
            current = []
            previous_was_label = False
            continue
        if looks_like and (
            previous_was_label
            or re.match(
                r"^[•\-–]?\s*(?:\d+\s+)?(?:H|W|D|L|SH|AH|Height|Width|Depth|Length|Seat|[A-Z][A-Za-z ]{1,40}\s+[–-])",
                value,
                re.I,
            )
            or re.search(r"\d\s*(?:x|×)\s*\d", value, re.I)
        ):
            current.append(value)
            previous_was_label = False
            continue
        if current:
            groups.append(current)
            current = []
        previous_was_label = label_only
    if current:
        groups.append(current)

    if not groups:
        return ""
    best = max(groups, key=lambda group: (len(group), sum(len(item) for item in group)))
    return clean_text("; ".join(best[:6]))


def add_synthesized_dimensions(info: dict[str, Any]) -> None:
    details = info.get("details") if isinstance(info.get("details"), dict) else {}
    if info.get("dimensions") or details.get("dimensions"):
        return

    dimensions = extract_dimensions_from_text(str(info.get("description") or ""))
    if not dimensions:
        ordered_parts = []
        for key in ("Height", "Width", "Depth", "Length", "Seat Height", "Seat height"):
            value = details.get(key)
            if value:
                ordered_parts.append(f"{key}: {value}")
        if len(ordered_parts) >= 2:
            dimensions = " x ".join(ordered_parts)
    if dimensions:
        details = dict(details)
        details["dimensions"] = dimensions
        info["details"] = details
        info["dimensions"] = dimensions


def line_looks_like_price(line: str) -> bool:
    line = clean_price_text(line)
    if not line or len(line) > 120:
        return False
    if re.search(r"(newsletter|receive|recieve|updated|shipping|delivery|transport|cookie|country|row\s+\d+)", line, re.I):
        return False
    if re.match(r"^(POA|PRICE ON APPLICATION|INQUIRE FOR PRICING(?: AND SHIPPING COSTS)?|LOGIN TO VIEW PRICE)$", line, re.I):
        return True
    if re.match(r"^[£$€]\s?[\d,.]+(?:\s*(?:No VAT|incl\.?\s*VAT|excl\.?\s*VAT|EUR|USD|GBP|SEK))?$", line, re.I):
        return True
    if re.match(r"^\d[\d\s,.]*\s*(?:EUR|USD|GBP|SEK)(?:\s*(?:No VAT|incl\.?\s*VAT|excl\.?\s*VAT))?$", line, re.I):
        return True
    code_match = re.match(r"^([A-Z]{3})\s?[\d,.]+(?:\s*(?:incl\.?\s*VAT|excl\.?\s*VAT|No VAT))?$", line)
    return bool(code_match and code_match.group(1) in KNOWN_CURRENCY_CODES)


def line_looks_like_labeled_price_value(line: str) -> bool:
    line = clean_price_text(line)
    if not line or len(line) > 40:
        return False
    if re.search(r"[A-Za-zÀ-ÿ]{3,}", line) and not re.search(r"\b(?:EUR|USD|GBP|SEK|VAT)\b", line, re.I):
        return False
    amount = price_amount(line)
    return amount is not None and amount > 0


def find_dom_price(lines: list[str], nav_words: set[str]) -> tuple[int | None, str]:
    currency_tokens = {"€", "$", "£", "EUR", "USD", "GBP"}
    amount_re = re.compile(r"^\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?$|^\d+(?:[.,]\d{2})?$|^\d[\d\s.,]*[,.-]\s*-$")
    for index, line in enumerate(lines):
        upper = line.upper()
        if upper.startswith(("RELATED PRODUCTS", "RECOMMENDED PRODUCTS")):
            break
        if re.match(r"^Price\s*:?\s*$", line, re.I):
            currency = ""
            for offset, value in enumerate(lines[index + 1 : index + 6], start=1):
                if value.upper() in nav_words:
                    continue
                if value in currency_tokens or value.upper() in currency_tokens:
                    currency = value
                    continue
                if amount_re.match(value):
                    return index + offset, clean_price_text(f"{currency} {value}".strip())
                candidate = clean_price_text(f"{currency} {value}".strip())
                if line_looks_like_labeled_price_value(candidate):
                    return index + offset, candidate
                if line_looks_like_price(value):
                    return index + offset, clean_price_text(value)
            continue
        if line in currency_tokens or upper in currency_tokens:
            for offset, value in enumerate(lines[index + 1 : index + 4], start=1):
                nearby = " ".join(lines[max(0, index - 5) : index + offset + 1])
                if re.search(
                    r"(currency|valuta|shipment costs|shipping|delivery time|transport prices|european union|united kingdom|united states|china)",
                    nearby,
                    re.I,
                ):
                    break
                candidate = clean_price_text(f"{line} {value}".strip())
                if line_looks_like_labeled_price_value(candidate):
                    return index + offset, candidate
        nearby = " ".join(lines[max(0, index - 5) : index + 1])
        if "RELATED PRODUCTS" in nearby.upper() or "RECOMMENDED PRODUCTS" in nearby.upper():
            break
        if line_looks_like_price(line) and not re.search(
            r"(shipment costs|shipping|delivery time|transport prices|european union|united kingdom|united states|china)",
            nearby,
            re.I,
        ):
            return index, clean_price_text(line)
    return None, ""


def extract_labeled_description(lines: list[str], nav_words: set[str], labels: list[str]) -> str:
    stop_markers = {
        "CONDITION REPORT",
        "SPECIFICATIONS",
        "REQUEST INFO",
        "REQUEST INFO ABOUT THIS PRODUCT",
        "RELATED PRODUCTS",
        "RECOMMENDED PRODUCTS",
    }
    label_markers = {label.upper() for label in labels}
    for index, line in enumerate(lines):
        if not re.match(r"^Description\s*:?\s*$", line, re.I):
            continue
        description_lines: list[str] = []
        for value in lines[index + 1 :]:
            upper = value.upper()
            if upper in nav_words or upper in stop_markers or upper in label_markers:
                break
            if re.match(r"^(Condition Report|Specifications|Request Info|Related Products|Recommended Products)\b", value, re.I):
                break
            description_lines.append(value)
        return clean_text(" ".join(description_lines))
    return ""


def extract_heading_lead_description(
    lines: list[str],
    product_name: str,
    nav_words: set[str],
    labels: list[str],
    price_labels: list[str],
) -> str:
    wanted = normalize_match_text(product_name)
    if not wanted:
        return ""

    heading_index = None
    wanted_tokens = {token for token in wanted.split() if len(token) >= 4}
    for index, line in enumerate(lines):
        candidate = normalize_match_text(line)
        if not candidate:
            continue
        candidate_tokens = {token for token in candidate.split() if len(token) >= 4}
        overlap = candidate_tokens & wanted_tokens
        is_heading_match = (
            candidate == wanted
            or (len(candidate) >= 20 and (candidate in wanted or wanted in candidate))
            or (
                len(candidate_tokens) >= 3
                and len(overlap) >= min(5, len(wanted_tokens))
                and len(overlap) / len(candidate_tokens) >= 0.6
            )
        )
        if is_heading_match:
            heading_index = index
            break
    if heading_index is None:
        return ""

    label_markers = tuple(label.upper() for label in labels + price_labels)
    description_lines: list[str] = []
    for line in lines[heading_index + 1 :]:
        upper = line.upper()
        if upper in nav_words:
            break
        if upper.startswith(label_markers):
            break
        if line_looks_like_price(line) or line_looks_like_dimensions(line):
            break
        if re.match(r"^(ENQUIRE|SOLD|ADD TO CART|BUY NOW|PREVIOUS|NEXT)$", line, re.I):
            break
        if len(line) < 20 and description_lines:
            break
        if len(line) >= 20:
            description_lines.append(line)

    return clean_text(" ".join(description_lines))


def extract_heading_text_block(
    soup: BeautifulSoup,
    product_name: str,
    nav_words: set[str],
) -> dict[str, Any]:
    if not product_name:
        return {}

    def normalize_heading(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", clean_text(value).lower()).strip()

    wanted = normalize_heading(product_name)
    heading = None
    for candidate in soup.find_all(["h1", "h2", "h3"]):
        text = clean_text(candidate.get_text(" "))
        if text and normalize_heading(text) == wanted:
            heading = candidate
            break
    if heading is None:
        return {}

    container = heading.find_parent(class_=re.compile(r"(sqs-html-content|product|detail|description)", re.I))
    if container is None:
        container = heading.parent
    if container is None:
        return {}

    block_lines: list[str] = []
    found_heading = False
    for element in container.find_all(["h1", "h2", "h3", "p", "li"], recursive=False):
        text = clean_text(element.get_text(" "))
        if not text:
            continue
        if not found_heading:
            if element == heading or normalize_heading(text) == wanted:
                found_heading = True
            continue
        if text.upper() in nav_words:
            break
        block_lines.append(text)

    if not block_lines:
        return {}

    price = ""
    content_lines: list[str] = []
    for line in block_lines:
        if line_looks_like_price(line):
            price = clean_price_text(line)
            break
        if re.search(r"^(ENQUIRE|SOLD|ADD TO CART|BUY NOW)$", line, re.I):
            break
        content_lines.append(line)

    details: dict[str, Any] = {}
    description_lines: list[str] = []
    for line in content_lines:
        if line_looks_like_dimensions(line):
            details.setdefault("dimensions", clean_dimension_text(line))
            continue
        seat_height = re.match(r"^(Seat\s+height)\s*:?\s*(.+)$", line, re.I)
        if seat_height:
            details.setdefault(clean_text(seat_height.group(1)).title(), clean_text(seat_height.group(2)))
            continue
        if re.search(r"\bc\.\s*\d{3,4}\b|\b\d{4}s?\b", line, re.I) and len(line) <= 140:
            details.setdefault("Origin / period", line)
            continue
        description_lines.append(line)

    info: dict[str, Any] = {}
    if price:
        info["price"] = price
    if description_lines:
        info["description"] = clean_text(" ".join(description_lines))
    if details:
        info["details"] = details
    if details.get("dimensions"):
        info["dimensions"] = str(details["dimensions"])
    add_synthesized_dimensions(info)
    return info


def extract_fundamente_product_info(soup: BeautifulSoup, base_url: str) -> dict[str, Any]:
    block = soup.select_one(".gallery--collection .gallery--text")
    if block is None:
        return {}

    heading = block.find(["h1", "h2", "h3"])
    name = clean_product_title(clean_text(heading.get_text(" ")) if heading else "")

    description_lines = []
    article = block.find("article")
    if article is not None:
        for element in article.find_all(["p", "li"], recursive=False):
            text = clean_text(element.get_text(" "))
            if text:
                description_lines.append(text)
    description = clean_text(" ".join(description_lines))

    details: dict[str, Any] = {}
    price = ""
    meta_block = block.select_one(".meta-block")
    meta_lines = [
        clean_text(line)
        for line in (meta_block.get_text("\n").splitlines() if meta_block else [])
        if clean_text(line)
    ]
    for index, line in enumerate(meta_lines):
        if re.match(r"^Dimensions?\s*:?\s*$", line, re.I) and index + 1 < len(meta_lines):
            dimensions = clean_dimension_text(meta_lines[index + 1])
            if dimensions:
                details["dimensions"] = dimensions
        elif re.match(r"^Dimensions?\s*[:：]\s*(.+)$", line, re.I):
            match = re.match(r"^Dimensions?\s*[:：]\s*(.+)$", line, re.I)
            if match:
                dimensions = clean_dimension_text(match.group(1))
                if dimensions:
                    details["dimensions"] = dimensions
        elif re.match(r"^Price\s*:?\s*$", line, re.I) and index + 1 < len(meta_lines):
            price = clean_price_text(meta_lines[index + 1])
        elif re.match(r"^Price\s*[:：]\s*(.+)$", line, re.I):
            match = re.match(r"^Price\s*[:：]\s*(.+)$", line, re.I)
            if match:
                price = clean_price_text(match.group(1))

    info: dict[str, Any] = {"source": "fundamente-product-page", "url": base_url}
    if name:
        info["name"] = name
    if description:
        info["description"] = description
    if price:
        info["price"] = price
    if details:
        info["details"] = details
    if details.get("dimensions"):
        info["dimensions"] = str(details["dimensions"])
    add_synthesized_dimensions(info)
    return info


def extract_sitonvintage_product_info(soup: BeautifulSoup, base_url: str) -> dict[str, Any]:
    heading = soup.select_one("h1.product_title") or soup.find("h1")
    name = clean_product_title(clean_text(heading.get_text(" ")) if heading else "")
    if not name:
        return {}

    info: dict[str, Any] = {
        "source": "sitonvintage-product-page",
        "url": base_url,
        "name": name,
    }
    lines = [clean_text(line) for line in (soup.body.get_text("\n").splitlines() if soup.body else [])]
    lines = [line for line in lines if line]
    for index, line in enumerate(lines):
        if re.match(r"^Price\s*:?\s*$", line, re.I) and index + 1 < len(lines):
            value = clean_text(lines[index + 1])
            if value:
                if re.search(r"\bsold\b", value, re.I):
                    info["availability"] = "Sold"
                elif line_looks_like_price(value):
                    info["price"] = clean_price_text(value)
            break
    return info


def extract_domain_product_info(soup: BeautifulSoup, base_url: str) -> dict[str, Any]:
    hostname = normalized_hostname(base_url)
    if hostname in FUNDAMENTE_DOMAINS:
        return extract_fundamente_product_info(soup, base_url)
    if hostname in SITONVINTAGE_DOMAINS:
        return extract_sitonvintage_product_info(soup, base_url)
    return {}


def extract_dom_product_info(soup: BeautifulSoup, base_url: str, existing_name: str = "") -> dict[str, Any]:
    body = soup.body
    if body is None:
        return {}
    lines = [clean_text(line) for line in body.get_text("\n").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return {}

    nav_words = {
        "PAST EXHIBITION",
        "COLLECTION",
        "TRADE",
        "HIRE",
        "ABOUT",
        "CONTACT",
        "HOME",
        "SEATING",
        "TABLES",
        "STORAGE",
        "MIRRORS",
        "LIGHTING",
        "OBJECTS",
        "DINING",
        "KIDS",
        "ARCHIVE",
        "LOOKBOOK",
        "CONTACT US",
        "SIGN UP",
        "BACK TO TABLES",
        "PREV / NEXT",
        "SUBSCRIBE",
        "T&CS",
    }

    price_index, price = find_dom_price(lines, nav_words)

    name = ""
    for heading in soup.find_all(["h1", "h2", "h3"]):
        heading_text = clean_text(heading.get_text(" "))
        if (
            heading_text
            and heading_text.upper() not in nav_words
            and heading_text.upper() not in {"RECOMMENDED PRODUCTS", "RELATED PRODUCTS"}
            and len(heading_text) > 3
        ):
            name = heading_text
            break
    if price_index is not None:
        line_candidates = [
            line
            for line in lines[:price_index]
            if line.upper() not in nav_words
            and len(line) > 3
            and not re.search(r"furniture|objects from|mostly|century", line, re.I)
        ]
        uppercase_candidates = [
            line
            for line in line_candidates
            if line.upper() == line and re.search(r"[A-Z]", line)
        ]
        if uppercase_candidates:
            name = uppercase_candidates[-1]
        elif not name:
            for candidate in reversed(lines[:price_index]):
                if candidate.upper() not in nav_words and len(candidate) > 3:
                    name = candidate
                    break
    if not name:
        h1 = soup.find("h1")
        if h1:
            name = clean_text(h1.get_text(" "))
    name = clean_product_title(name)
    title_name = clean_product_title(clean_text(soup.title.string if soup.title else ""))
    if existing_name and not is_weak_existing_name(existing_name) and is_weak_existing_name(name):
        name = clean_product_title(existing_name)
    if title_name and is_weak_existing_name(name):
        name = title_name

    labels = [
        "Designer",
        "Design",
        "Designed by",
        "Manufacturer",
        "Maker",
        "Produced by",
        "Producer",
        "Creator",
        "Place of Origin",
        "Place Of Origin",
        "Country of Origin",
        "Country Of Origin",
        "Country of Manufacture",
        "Country Of Manufacture",
        "Land van herkomst",
        "Origin Country",
        "Production Country",
        "Made in",
        "Made In",
        "Origin",
        "Country",
        "Date of Manufacture",
        "Date Of Manufacture",
        "Year of Manufacture",
        "Year Of Manufacture",
        "Year",
        "Material",
        "Materials",
        "Materials and Techniques",
        "Materials And Techniques",
        "Period",
        "Era",
        "Circa",
        "Reference Number",
        "Reference",
        "Ref",
        "Dimensions",
        "Dimension",
        "Measurements",
        "Measurement",
        "Size",
        "Sizes",
        "Height",
        "Width",
        "Depth",
        "Length",
        "Seat Height",
        "Condition",
    ]
    price_labels = [
        "Price",
        "Prix",
        "Preis",
        "Precio",
        "Prezzo",
        "Regular Price",
        "Sale Price",
        "Public Price",
        "Trade Price",
        "Asking Price",
        "Retail Price",
        "List Price",
        "Listed Price",
    ]
    details: dict[str, Any] = {}

    def add_detail(label: Any, value: Any, override: bool = False) -> None:
        detail_key = normalize_detail_label(clean_detail_key(label))
        detail_value = clean_detail_value(value)
        if detail_key == "dimensions":
            detail_value = clean_dimension_text(detail_value)
            if str(detail_value).lower() in {"soon", "coming soon", "n/a", "na", "none", "-", "stock", "in stock"}:
                return
            if not labeled_value_looks_like_dimensions(str(detail_value)):
                return
        if (
            not detail_key
            or detail_value in (None, "", [], {})
            or detail_key.upper() in nav_words
            or str(detail_value).upper() in nav_words
        ):
            return
        if len(detail_key) > 80 or len(str(detail_value)) > 1000:
            return
        if override:
            details[detail_key] = detail_value
        else:
            details.setdefault(detail_key, detail_value)

    def is_inside_non_product_detail_block(element: Any) -> bool:
        attrs = " ".join(
            clean_text(" ".join(parent.get("class", [])) + " " + str(parent.get("id") or ""))
            for parent in element.parents
        )
        return bool(re.search(r"\b(filter|sidebar|navigation|menu|footer|header|breadcrumbs?)\b", attrs, re.I))

    for row in soup.find_all("tr"):
        if is_inside_non_product_detail_block(row):
            continue
        cells = [clean_text(cell.get_text(" ")) for cell in row.find_all(["th", "td"])]
        cells = [cell for cell in cells if cell]
        if len(cells) >= 2:
            add_detail(cells[0], " ".join(cells[1:]))

    for definition_list in soup.find_all("dl"):
        if is_inside_non_product_detail_block(definition_list):
            continue
        terms = definition_list.find_all("dt")
        for term in terms:
            values = []
            sibling = term.find_next_sibling()
            while sibling and sibling.name != "dt":
                if sibling.name == "dd":
                    values.append(clean_text(sibling.get_text(" ")))
                sibling = sibling.find_next_sibling()
            if values:
                add_detail(term.get_text(" "), " ".join(values))

    for tab_container in soup.select(".product-tabs"):
        value_by_class: dict[str, str] = {}
        for value_node in tab_container.select(".infotext"):
            value_classes = [str(item) for item in value_node.get("class", [])]
            value = clean_text(value_node.get_text(" "))
            if not value:
                continue
            for class_name in value_classes:
                if class_name not in {"infotext", "hide", "active"}:
                    value_by_class[class_name] = value

        for label_node in tab_container.select("ul li"):
            label = clean_text(label_node.get_text(" "))
            if not label:
                continue
            for class_name in label_node.get("class", []):
                value = value_by_class.get(str(class_name))
                if value:
                    add_detail(label, value, override=True)
                    break

    for line in lines:
        if re.match(
            r"^[A-Z][A-Za-zÀ-ÿ-]+(?:\s+[A-Z][A-Za-zÀ-ÿ-]+){0,2},\s*(?:\d{4}s?|(?:\d{1,2}(?:st|nd|rd|th)\s+century))\b",
            line,
        ):
            add_detail("Origin / period", line)
        match = re.match(r"^([^:：]{2,80})\s*[:：]\s*(.{1,1000})$", line)
        if match:
            add_detail(match.group(1), match.group(2))
        for detail_key, detail_value in extract_labeled_detail_pairs(line, labels).items():
            add_detail(detail_key, detail_value)

    for label in labels:
        same_line_pattern = re.compile(rf"^{re.escape(label)}\s*:\s*(.+)$", re.I)
        label_only_pattern = re.compile(rf"^{re.escape(label)}\s*:?\s*$", re.I)
        for index, line in enumerate(lines):
            detail_key = normalize_detail_label(label)
            if detail_key in details:
                break
            match = same_line_pattern.match(line)
            if match:
                add_detail(detail_key, match.group(1))
                break
            if label_only_pattern.match(line):
                for value in lines[index + 1 : index + 4]:
                    if value.upper() in nav_words:
                        continue
                    if any(re.match(rf"^{re.escape(other)}\s*:?\s*$", value, re.I) for other in labels):
                        break
                    add_detail(detail_key, value)
                    break

    for label in price_labels:
        same_line_pattern = re.compile(rf"^{re.escape(label)}\s*[:：]\s*(.+)$", re.I)
        label_only_pattern = re.compile(rf"^{re.escape(label)}\s*[:：]?\s*$", re.I)
        for index, line in enumerate(lines):
            if price:
                break
            match = same_line_pattern.match(line)
            if match and line_looks_like_price(match.group(1)):
                price_index = index
                price = clean_price_text(match.group(1))
                break
            if label_only_pattern.match(line):
                currency = ""
                for offset, value in enumerate(lines[index + 1 : index + 6], start=1):
                    if value.upper() in nav_words:
                        continue
                    if value in {"€", "$", "£"} or value.upper() in KNOWN_CURRENCY_CODES:
                        currency = value
                        continue
                    candidate = clean_price_text(f"{currency} {value}".strip())
                    if line_looks_like_price(candidate) or line_looks_like_labeled_price_value(candidate):
                        price_index = index + offset
                        price = candidate
                        break

    dimensions_from_lines = extract_dimensions_from_lines(lines)
    existing_dimensions = str(details.get("dimensions") or "")
    if dimensions_from_lines and (
        not existing_dimensions or dimensions_from_lines.count(";") > existing_dimensions.count(";")
    ):
        details["dimensions"] = dimensions_from_lines

    heading_info = extract_heading_text_block(soup, name, nav_words)

    description = extract_labeled_description(lines, nav_words, labels)
    if not description:
        description = extract_heading_lead_description(lines, name, nav_words, labels, price_labels)
    if price_index is not None:
        start = price_index + 1
        while start < len(lines) and (
            lines[start].upper() in {"ENQUIRE", "SOLD", "ADD TO CART", "CONTACT US", "REQUEST INFO"}
            or lines[start].lower() == name.lower()
            or lines[start] == price
            or lines[start] == "0"
        ):
            start += 1
        if start < len(lines):
            combined = lines[start]
            if name and combined.lower().startswith(name.lower()):
                combined = combined[len(name) :].strip()
            if price and combined.startswith(price):
                combined = combined[len(price) :].strip()
            if combined.upper().startswith("ENQUIRE"):
                combined = combined[len("ENQUIRE") :].strip()
            if combined != lines[start]:
                lines[start] = combined
                if not lines[start]:
                    start += 1
        stop = len(lines)
        for marker in labels + [
            "RECOMMENDED PRODUCTS",
            "RELATED PRODUCTS",
            "PREVIOUS",
            "NEXT",
            "FACEBOOK",
            "INSTAGRAM",
            "CONTACT US",
            "OBJEKT B.V.",
        ]:
            for index in range(start, len(lines)):
                if lines[index].upper().startswith(marker.upper()):
                    stop = min(stop, index)
                    break
        if not description:
            description = clean_text(" ".join(lines[start:stop]))

    info: dict[str, Any] = {"source": "dom-text", "url": base_url}
    if name and is_weak_existing_name(existing_name):
        info["name"] = name
    if heading_info.get("price"):
        info["price"] = heading_info["price"]
    elif price:
        info["price"] = price
    if heading_info.get("description"):
        info["description"] = heading_info["description"]
    elif description:
        info["description"] = description
    if isinstance(heading_info.get("details"), dict):
        details.update(heading_info["details"])
    if details:
        info["details"] = details
    if details.get("dimensions"):
        info["dimensions"] = str(details["dimensions"])
    add_synthesized_dimensions(info)
    return info


def parse_shopify_product_payload(
    product: dict[str, Any],
    base_url: str,
    source: str,
) -> tuple[dict[str, Any], list[str]]:
    variants = product.get("variants") or []
    first_variant = variants[0] if variants else {}
    images = []
    for image in product.get("images") or []:
        if isinstance(image, str):
            images.append(normalize_url(image, base_url))
        elif isinstance(image, dict):
            images.append(normalize_url(image.get("src") or image.get("url") or "", base_url))
    for media in product.get("media") or []:
        if not isinstance(media, dict):
            continue
        media_url = media.get("src")
        preview = media.get("preview_image")
        if not media_url and isinstance(preview, dict):
            media_url = preview.get("src")
        if media_url:
            images.append(normalize_url(str(media_url), base_url))

    handle = clean_text(product.get("handle"))
    product_url = clean_text(product.get("url"))
    if not product_url and handle:
        product_url = f"/products/{handle}"
    info = {
        "name": clean_text(product.get("title")),
        "description": clean_text(BeautifulSoup(product.get("description") or "", "lxml").get_text(" ")),
        "sku": clean_text(first_variant.get("sku")),
        "price": format_shopify_price(product.get("price") or first_variant.get("price")),
        "currency": clean_text(product.get("currency") or first_variant.get("currency")).upper(),
        "url": normalize_url(product_url, base_url) if product_url else "",
        "source": source,
    }
    details = details_from_mapping(
        product,
        {"title", "description", "body_html", "image", "images", "featured_image"},
    )
    if details:
        info["details"] = details
    add_synthesized_dimensions(info)
    return info, list(dict.fromkeys(u for u in images if u))


def try_shopify_json(url: str, session: requests.Session) -> tuple[dict[str, Any], list[str]]:
    parsed = urlparse(url)
    match = re.search(r"(/products/[^/?#]+)", parsed.path)
    if not match:
        return {}, []
    endpoint = urlunparse((parsed.scheme, parsed.netloc, match.group(1) + ".js", "", "", ""))
    try:
        response = session.get(endpoint, timeout=20)
        if response.status_code in {401, 403, 429}:
            try:
                from curl_cffi import requests as curl_requests

                response = curl_requests.get(
                    endpoint,
                    headers={**dict(session.headers), "Accept": "application/json,text/javascript,*/*"},
                    impersonate="chrome120",
                    timeout=20,
                    allow_redirects=True,
                )
            except Exception:
                return {}, []
        content_type = response.headers.get("content-type", "")
        if response.status_code != 200 or not re.search(r"(json|javascript|text/plain)", content_type, re.I):
            return {}, []
        product = response.json()
    except Exception:
        return {}, []

    info, images = parse_shopify_product_payload(product, endpoint, "shopify-product-json")
    if not info.get("url"):
        info["url"] = endpoint.removesuffix(".js")
    return info, images


def extract_inline_shopify_product_json(soup: BeautifulSoup, base_url: str) -> tuple[dict[str, Any], list[str]]:
    parsed = urlparse(base_url)
    handle_match = re.search(r"/products/([^/?#]+)", parsed.path)
    page_handle = handle_match.group(1).lower() if handle_match else ""
    for script in soup.find_all("script"):
        script_id = clean_text(script.get("id"))
        script_type = clean_text(script.get("type"))
        raw = script.string or script.get_text()
        if not raw or '"images"' not in raw or '"variants"' not in raw:
            continue
        if script_type and not re.search(r"(json|javascript)", script_type, re.I):
            continue
        if script_id and not re.search(r"(product|shopify)", script_id, re.I):
            continue
        try:
            product = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(product, dict):
            continue
        handle = clean_text(product.get("handle")).lower()
        if page_handle and handle and handle != page_handle:
            continue
        info, images = parse_shopify_product_payload(product, base_url, "inline-shopify-product-json")
        if images:
            return info, images
    return {}, []


def extract_analytics_product_info(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.find_all("script"):
        raw = script.string or script.get_text()
        if not raw or "view_item" not in raw or "price" not in raw:
            continue
        marker = re.search(r"gtag\(\s*['\"]event['\"]\s*,\s*['\"]view_item['\"]\s*,", raw)
        if not marker:
            continue
        object_start = raw.find("{", marker.end())
        if object_start < 0:
            continue
        try:
            payload, _ = json.JSONDecoder().raw_decode(raw[object_start:])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        candidates = []
        items = payload.get("items")
        if isinstance(items, list):
            candidates.extend(item for item in items if isinstance(item, dict))
        candidates.append(payload)
        for item in candidates:
            price = clean_price_text(item.get("price"))
            currency = clean_text(item.get("currency") or payload.get("currency")).upper()
            if price_is_valid(price) and currency in KNOWN_CURRENCY_CODES:
                return {
                    "price": price,
                    "currency": currency,
                    "source": "analytics-view-item",
                }
    return {}


def try_monument_airtable_api(
    url: str,
    session: requests.Session,
    max_images: int,
) -> dict[str, Any] | None:
    parsed = urlparse(url)
    if parsed.hostname not in {"www.monumentgallery.co.uk", "monumentgallery.co.uk"}:
        return None
    match = re.search(r"^/product/([^/?#]+)/?$", parsed.path)
    if not match:
        return None

    slug = match.group(1)
    endpoint = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            "/api/airtable-items",
            "",
            urlencode({"slug": slug}),
            "",
        )
    )
    try:
        response = session.get(endpoint, timeout=12, headers={"Accept": "application/json"})
        response.raise_for_status()
        data = response.json()
    except Exception:
        return None

    item = data.get("item") if isinstance(data, dict) else None
    if not isinstance(item, dict):
        return None

    details: dict[str, Any] = {
        "Designer": clean_text(item.get("designer")),
        "Manufacturer": clean_text(item.get("manufacturer")),
        "Material": clean_text(item.get("material")),
        "Period": clean_text(item.get("period")),
        "dimensions": clean_text(item.get("dimensions")),
        "Condition": clean_text(item.get("condition")),
    }
    details = {key: value for key, value in details.items() if value}
    extra_details = details_from_mapping(
        item,
        {
            "images",
            "image",
            "displayName",
            "description",
            "availability",
            "designer",
            "manufacturer",
            "material",
            "period",
            "dimensions",
            "condition",
        },
    )
    details.update(extra_details)
    images = []
    for image in as_list(item.get("images")):
        if isinstance(image, str):
            images.append(normalize_url(image, endpoint))
    if not images and item.get("image"):
        images.append(normalize_url(str(item["image"]), endpoint))
    images = list(dict.fromkeys(u for u in images if u))

    product_info: dict[str, Any] = {
        "name": clean_text(item.get("displayName")),
        "description": clean_text(item.get("description")),
        "availability": clean_text(item.get("availability")),
        "source": "monument-airtable-api",
        "url": url,
        "page_url": url,
    }
    if details:
        product_info["details"] = details
    if details.get("dimensions"):
        product_info["dimensions"] = details["dimensions"]
    product_info = {
        key: value for key, value in product_info.items() if value not in (None, "", [], {})
    }

    selected_images = [
        {
            "url": image,
            "score": 100,
            "source": "monument-airtable-api",
            "alt": product_info.get("name", ""),
            "width": None,
            "height": None,
            "reasons": ["monument airtable product image"],
        }
        for image in images[:max_images]
    ]

    return {
        "input_url": url,
        "fetched_url": url,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "product": product_info,
        "images": selected_images,
        "rejected_preview": [],
    }


def extract_auctionet_page_data(soup: BeautifulSoup) -> dict[str, Any]:
    script_text = "\n".join(script.get_text() for script in soup.find_all("script"))
    marker = "window.vipDataAtPageLoad"
    marker_index = script_text.find(marker)
    if marker_index < 0:
        return {}
    assignment_index = script_text.find("=", marker_index)
    if assignment_index < 0:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(script_text[assignment_index + 1 :].lstrip())
    except json.JSONDecodeError:
        return {}
    item = data.get("item") if isinstance(data, dict) else None
    if not isinstance(item, dict):
        return {}

    info: dict[str, Any] = {"source": "auctionet-page-data"}
    h1 = soup.find("h1")
    if h1:
        info["name"] = clean_product_title(clean_text(h1.get_text(" ")))
    currency = clean_text(item.get("currency")).upper()
    if currency:
        info["currency"] = currency
    if item.get("estimate") not in (None, ""):
        info["price"] = f"Estimate {item['estimate']}"

    details = details_from_mapping(
        item,
        {
            "id",
            "auction_id",
            "currency",
            "reserve_met",
            "publicly_visible",
            "license_weapon",
            "you_have_license_weapon_bidding_enabled",
            "reason_for_not_being_able_to_bid",
        },
    )
    description = ""
    meta_description = soup.find("meta", attrs={"property": "og:description"}) or soup.find(
        "meta", attrs={"name": "description"}
    )
    if meta_description and meta_description.get("content"):
        description = clean_text(meta_description["content"])
        info["description"] = description
    dimensions = extract_dimensions_from_text(description)
    if dimensions:
        details["dimensions"] = dimensions
        info["dimensions"] = dimensions
    if details:
        info["details"] = details
    return info


def infer_context_score(element: Any) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    cursor = element
    for depth in range(5):
        if not cursor:
            break
        attrs = " ".join(
            clean_text(cursor.get(attr, "")) for attr in ("class", "id", "role", "aria-label")
        )
        if re.search(r"\bproduct-gallery\b|gallery-lightbox", attrs, re.I):
            score += max(28, 70 - depth * 8)
            reasons.append("inside primary product gallery")
        elif re.search(r"\bproduct-list\b|product-list-item|grid-item", attrs, re.I):
            score -= max(24, 64 - depth * 8)
            reasons.append("inside product listing block")
        elif re.search(r"(header|footer|nav|menu|newsletter|social|recommend|related)", attrs, re.I):
            score -= max(30, 60 - depth * 6)
            reasons.append("inside likely non-product block")
        elif re.search(r"(product|pdp|gallery|carousel|media|image|photo|slider|zoom)", attrs, re.I):
            score += 18 - depth * 2
            reasons.append("inside product/media block")
        cursor = cursor.parent
    return score, reasons


def parse_size(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else None


def add_candidate(
    candidates: dict[str, ImageCandidate],
    raw_url: str,
    base_url: str,
    points: int,
    reason: str,
    source: str,
    element: Any | None = None,
    order: int = 10_000,
) -> None:
    url = normalize_url(raw_url, base_url)
    if not url or url.startswith("data:"):
        return
    if "{" in url or "}" in url:
        return
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return
    path = parsed.path.lower()
    if path.rstrip("/").endswith("/0"):
        return
    if re.search(r"\.(mp4|mov|webm|m4v|avi|pdf|zip)(\?|$)", path, re.I):
        return
    item = candidates.setdefault(url, ImageCandidate(url=url, source=source))
    item.add(points, reason)
    item.order = min(item.order, order)
    if element is not None:
        item.alt = clean_text(element.get("alt") or item.alt)
        item.width = parse_size(element.get("width")) or item.width
        item.height = parse_size(element.get("height")) or item.height
        context_score, context_reasons = infer_context_score(element)
        if context_score:
            item.score += context_score
            for context_reason in context_reasons:
                if context_reason not in item.reasons:
                    item.reasons.append(context_reason)


def gather_image_candidates(
    soup: BeautifulSoup,
    base_url: str,
    structured_images: list[str],
    meta_images: list[str],
    product_name: str,
) -> list[ImageCandidate]:
    candidates: dict[str, ImageCandidate] = {}
    for order, url in enumerate(structured_images):
        add_candidate(candidates, url, base_url, 90, "structured product image", "structured", order=order)
    for order, url in enumerate(meta_images, len(structured_images)):
        add_candidate(candidates, url, base_url, 45, "open graph image", "meta", order=order)

    for order, img in enumerate(soup.find_all(["img", "source"]), len(structured_images) + len(meta_images)):
        attrs = []
        for attr in ("src", "data-src", "data-original", "data-zoom-image", "data-large_image"):
            if img.get(attr):
                attrs.append(str(img[attr]))
        for attr in ("srcset", "data-srcset", "imagesrcset"):
            if img.get(attr):
                attrs.append(pick_srcset_best(str(img[attr])))
        for raw in attrs:
            add_candidate(candidates, raw, base_url, 12, "page image candidate", "dom", img, order=order)

    for order, link in enumerate(soup.find_all("a"), 100_000):
        href = link.get("href", "")
        if re.search(r"\.(jpe?g|png|webp|avif)(\?|$)", href, re.I):
            add_candidate(candidates, href, base_url, 8, "linked image file", "link", link, order=order)

    name_tokens = [t.lower() for t in re.findall(r"[a-zA-Z0-9]{4,}", product_name or "")][:8]
    for item in candidates.values():
        haystack = f"{item.url} {item.alt}".lower()
        path = urlparse(item.url).path.lower()
        if PRODUCT_RE.search(item.url):
            item.add(14, "product-like URL")
        if NOISE_RE.search(item.url) or NOISE_RE.search(item.alt):
            item.add(-45, "noise keyword")
        if SMALL_SIZE_RE.search(path):
            item.add(-30, "very small filename size")
        if any(token in haystack for token in name_tokens):
            item.add(16, "matches product name")
        if re.match(r"^view\s+\d+$", item.alt.strip(), re.I):
            item.add(30, "generic product gallery alt")
        if re.search(r"/assets/(opengraph|logo|social|icons?)[\w.-]*\.(png|jpe?g|webp|avif)$", path, re.I):
            item.add(-50, "site-level asset")
        if item.width and item.height:
            if item.width < 180 or item.height < 180:
                item.add(-35, "small declared dimensions")
            elif item.width >= 500 and item.height >= 500:
                item.add(20, "large declared dimensions")
        if re.search(r"\.(svg|gif)(\?|$)", path, re.I):
            item.add(-80, "low-value image format")
        if re.search(r"(?:^|\.)icons8\.com$", urlparse(item.url).hostname or "", re.I):
            item.add(-120, "third-party icon asset")
    ranked = sorted(candidates.values(), key=lambda c: c.score, reverse=True)
    return ranked


def fetch_static(url: str) -> tuple[str, str]:
    started = time.monotonic()
    parsed = urlparse(url)
    if parsed.scheme == "file":
        path = Path(url2pathname(parsed.path))
        if parsed.netloc and not re.match(r"^[A-Za-z]:", str(path)):
            path = Path(f"//{parsed.netloc}") / str(path).lstrip("\\/")
        return path.read_text(encoding="utf-8"), url

    logger.info("extractor stage=fetch_static_start host=%s path=%s", parsed.netloc, parsed.path or "/")
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Referer": f"{parsed.scheme}://{parsed.netloc}/",
        }
    )
    try:
        response = session.get(url, timeout=30)
    except requests.exceptions.SSLError:
        if parsed.hostname and parsed.hostname.startswith("www."):
            retry_url = urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc.removeprefix("www."),
                    parsed.path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                )
            )
            logger.warning(
                "extractor stage=fetch_static_retry_bare_domain host=%s retry_url=%s",
                parsed.netloc,
                retry_url,
            )
            response = session.get(retry_url, timeout=30)
        else:
            raise
    logger.info(
        "extractor stage=fetch_static_response host=%s status=%d elapsed=%.2fs",
        parsed.netloc,
        response.status_code,
        time.monotonic() - started,
    )
    if response.status_code in {401, 403, 429}:
        try:
            from curl_cffi import requests as curl_requests

            logger.warning(
                "extractor stage=fetch_static_retry_curl host=%s status=%d",
                parsed.netloc,
                response.status_code,
            )
            curl_response = curl_requests.get(
                url,
                headers=dict(session.headers),
                impersonate="chrome120",
                timeout=30,
                allow_redirects=True,
            )
            if curl_response.status_code < 400:
                logger.info(
                    "extractor stage=fetch_static_curl_done host=%s status=%d elapsed=%.2fs",
                    parsed.netloc,
                    curl_response.status_code,
                    time.monotonic() - started,
                )
                return curl_response.text, curl_response.url
        except Exception:
            logger.exception("extractor stage=fetch_static_curl_failed host=%s", parsed.netloc)
            pass
    response.raise_for_status()
    logger.info(
        "extractor stage=fetch_static_done host=%s bytes=%d elapsed=%.2fs",
        parsed.netloc,
        len(response.text),
        time.monotonic() - started,
    )
    return response.text, response.url


def fetch_rendered(url: str) -> tuple[str, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed. Run without --render or install playwright.") from exc

    parsed = urlparse(url)
    wait_started = time.monotonic()
    logger.info("extractor stage=fetch_rendered_queued host=%s path=%s", parsed.netloc, parsed.path or "/")
    with render_semaphore():
        started = time.monotonic()
        logger.info(
            "extractor stage=fetch_rendered_start host=%s queue_wait=%.2fs",
            parsed.netloc,
            started - wait_started,
        )
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    user_agent=USER_AGENT,
                    viewport={"width": 1440, "height": 1600},
                    ignore_https_errors=True,
                )
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)
                html = page.content()
                final_url = page.url
            finally:
                browser.close()
    logger.info(
        "extractor stage=fetch_rendered_done host=%s bytes=%d elapsed=%.2fs",
        parsed.netloc,
        len(html),
        time.monotonic() - started,
    )
    return html, final_url


def should_try_cloudflare_bypass_from_fetch_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code in {401, 403, 429}:
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "403",
            "forbidden",
            "401",
            "unauthorized",
            "429",
            "cloudflare",
            "security verification",
        )
    )


def download_images(images: list[dict[str, Any]], out_dir: Path, session: requests.Session) -> None:
    image_dir = out_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(images, 1):
        url = item["url"]
        try:
            response = session.get(url, timeout=30, stream=True)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";")[0].strip()
            ext = mimetypes.guess_extension(content_type) or Path(urlparse(url).path).suffix or ".jpg"
            if ext == ".jpe":
                ext = ".jpg"
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
            filename = f"{index:02d}_{digest}{ext}"
            path = image_dir / filename
            with path.open("wb") as file:
                for chunk in response.iter_content(chunk_size=65536):
                    if chunk:
                        file.write(chunk)
            item["local_path"] = str(path)
        except Exception as exc:
            item["download_error"] = str(exc)


def extract(
    url: str,
    render: bool,
    max_images: int,
    allow_cloudflare_bypass: bool = True,
) -> dict[str, Any]:
    started = time.monotonic()
    max_images = max(1, min(max_images, MAX_PRODUCT_IMAGES))
    url = resolve_hash_project_url(url)
    parsed = urlparse(url)
    logger.info(
        "extractor event=started host=%s render=%s max_images=%d",
        parsed.netloc,
        render,
        max_images,
    )
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    fast_result = try_monument_airtable_api(url, session, max_images)
    if fast_result and fast_result.get("product", {}).get("dimensions"):
        logger.info(
            "extractor event=done host=%s source=monument_airtable images=%d elapsed=%.2fs",
            parsed.netloc,
            len(fast_result.get("images") or []),
            time.monotonic() - started,
        )
        return fast_result

    try:
        html, final_url = fetch_rendered(url) if render else fetch_static(url)
    except Exception as exc:
        if allow_cloudflare_bypass and should_try_cloudflare_bypass_from_fetch_error(exc):
            logger.warning(
                "extractor event=fetch_blocked_try_cloudflare host=%s render=%s reason=%r",
                parsed.netloc,
                render,
                str(exc),
            )
            try:
                html, final_url = fetch_cloudflare_bypassed(url)
            except CloudflareBypassError as cf_exc:
                raise RuntimeError(f"Blocked by Cloudflare/security verification page: {cf_exc}") from cf_exc
        else:
            raise
    soup = BeautifulSoup(html, "lxml")
    page_title = clean_text(soup.title.string if soup.title else "")
    body_text = clean_text(soup.body.get_text(" ") if soup.body else "")
    if is_cloudflare_blocked(page_title, body_text, html):
        logger.warning(
            "extractor event=blocked host=%s final_url=%s title=%r allow_cloudflare_bypass=%s",
            parsed.netloc,
            final_url,
            page_title,
            allow_cloudflare_bypass,
        )
        if allow_cloudflare_bypass:
            try:
                html, final_url = fetch_cloudflare_bypassed(final_url)
                soup = BeautifulSoup(html, "lxml")
                page_title = clean_text(soup.title.string if soup.title else "")
                body_text = clean_text(soup.body.get_text(" ") if soup.body else "")
            except CloudflareBypassError as exc:
                raise RuntimeError(f"Blocked by Cloudflare/security verification page: {exc}") from exc
        if is_cloudflare_blocked(page_title, body_text, html):
            logger.warning("extractor event=blocked_after_cloudflare host=%s final_url=%s title=%r", parsed.netloc, final_url, page_title)
            raise RuntimeError("Blocked by Cloudflare/security verification page")

    jsonld_info, jsonld_images = extract_jsonld_products(soup, final_url)
    meta_info, meta_images = extract_meta_info(soup, final_url)
    shopify_info, shopify_images = try_shopify_json(final_url, session)
    inline_shopify_info, inline_shopify_images = extract_inline_shopify_product_json(soup, final_url)
    analytics_info = extract_analytics_product_info(soup)
    nextjs_info = extract_nextjs_product_info(soup)
    auctionet_info = extract_auctionet_page_data(soup)
    domain_info = extract_domain_product_info(soup, final_url)
    domain_product_images = extract_domain_product_image_urls(soup, final_url)

    product_info = {}
    for info in (jsonld_info, shopify_info, inline_shopify_info, meta_info):
        product_info = merge_product_info(product_info, info)
    dom_info = extract_dom_product_info(soup, final_url, product_info.get("name", ""))
    product_info = merge_product_info(dom_info, product_info)
    product_info = merge_product_info(domain_info, product_info)
    product_info = merge_product_info(analytics_info, product_info)
    product_info = merge_product_info(nextjs_info, product_info)
    product_info = merge_product_info(auctionet_info, product_info)
    add_synthesized_dimensions(product_info)
    product_info["page_url"] = final_url

    authoritative_images = domain_product_images or shopify_images or inline_shopify_images or jsonld_images
    authoritative_keys = {
        key
        for key in (image_identity_key(url) for url in authoritative_images)
        if key
    }
    has_authoritative_gallery = len(authoritative_keys) >= 3
    structured_images = list(dict.fromkeys(domain_product_images + jsonld_images + shopify_images + inline_shopify_images))
    candidates = gather_image_candidates(
        soup,
        final_url,
        structured_images,
        meta_images,
        product_info.get("name", ""),
    )
    candidates = dedupe_image_candidates(candidates)
    candidates = prefer_authoritative_image_list(candidates, authoritative_images)
    if not has_authoritative_gallery:
        candidates = prefer_numbered_gallery(candidates)
        candidates = prefer_leading_filename_series(candidates)
        candidates = prefer_early_product_gallery(candidates)
        candidates = prefer_current_product_group(candidates, product_info.get("name", ""))
        candidates = prefer_page_path_images(candidates, final_url)
        candidates = filter_domain_image_candidates(candidates, final_url)
    selected = [
        {
            "url": c.url,
            "score": c.score,
            "source": c.source,
            "alt": c.alt,
            "width": c.width,
            "height": c.height,
            "reasons": c.reasons,
        }
        for c in candidates
    ][:max_images]

    logger.info(
        "extractor event=done host=%s final_host=%s render=%s candidates=%d selected=%d name=%r elapsed=%.2fs",
        parsed.netloc,
        urlparse(final_url).netloc,
        render,
        len(candidates),
        len(selected),
        product_info.get("name", ""),
        time.monotonic() - started,
    )
    return {
        "input_url": url,
        "fetched_url": final_url,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "product": product_info,
        "images": selected,
        "rejected_preview": [
            {"url": c.url, "score": c.score, "reasons": c.reasons}
            for c in candidates[len(selected) : len(selected) + 10]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract clean product images and product info.")
    parser.add_argument("url", help="Product detail page URL")
    parser.add_argument("--render", action="store_true", help="Render JavaScript with Playwright")
    parser.add_argument("--download-images", action="store_true", help="Download selected images")
    parser.add_argument("--max-images", type=int, default=12, help="Maximum selected images")
    parser.add_argument(
        "--out",
        default="product_extract",
        help="Output directory for result JSON and optional images",
    )
    args = parser.parse_args()

    try:
        result = extract(args.url, args.render, args.max_images)
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        if args.download_images:
            session = requests.Session()
            session.headers.update({"User-Agent": USER_AGENT})
            download_images(result["images"], out_dir, session)
        output_file = out_dir / "product_extract.json"
        output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"\nSaved: {output_file}", file=sys.stderr)
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
