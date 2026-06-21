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
import mimetypes
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.request import url2pathname

import requests
from bs4 import BeautifulSoup


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

NOISE_RE = re.compile(
    r"(logo|icon|sprite|avatar|payment|paypal|visa|mastercard|amex|klarna|"
    r"afterpay|trust|badge|seal|banner|hero|background|bg-|newsletter|"
    r"social|facebook|instagram|tiktok|youtube|pinterest|review|star|"
    r"flag|placeholder|loading|spinner|favicon)",
    re.I,
)

PRODUCT_RE = re.compile(
    r"(product|products|prod|pdp|catalog|cdn\.shopify|woocommerce|media|"
    r"image|images|photo|gallery|zoom|large|main)",
    re.I,
)

SMALL_SIZE_RE = re.compile(r"(^|[_-])(\d{1,2})x(\d{1,2})([_\.-]|$)", re.I)


@dataclass
class ImageCandidate:
    url: str
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    width: int | None = None
    height: int | None = None
    alt: str = ""
    source: str = ""

    def add(self, points: int, reason: str) -> None:
        self.score += points
        if reason not in self.reasons:
            self.reasons.append(reason)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text


def compact_url(url: str) -> str:
    parsed = urlparse(url)
    keep_params = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in {"v", "width", "height", "format", "quality"}:
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
    path = parsed.path
    if "static.wixstatic.com" in parsed.netloc and "/media/" in path:
        media_id = path.split("/media/", 1)[1].split("/", 1)[0]
        if media_id:
            return f"wix:{media_id}"
    return f"{parsed.netloc}{path}".lower()


def dedupe_image_candidates(candidates: list[ImageCandidate]) -> list[ImageCandidate]:
    best_by_key: dict[str, ImageCandidate] = {}
    for item in candidates:
        key = image_identity_key(item.url)
        previous = best_by_key.get(key)
        if previous is None or item.score > previous.score:
            best_by_key[key] = item
    return sorted(best_by_key.values(), key=lambda c: c.score, reverse=True)


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
    return gallery if len(gallery) >= 3 else candidates


def normalize_url(raw: str, base_url: str) -> str:
    if not raw:
        return ""
    raw = raw.strip().strip("'\"")
    if raw.startswith("//"):
        raw = "https:" + raw
    return compact_url(urljoin(base_url, raw))


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
        if not merged.get(key):
            merged[key] = value
    return merged


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
            current = {
                "name": clean_text(item.get("name")),
                "description": clean_text(item.get("description")),
                "sku": clean_text(item.get("sku") or item.get("mpn")),
                "brand": clean_text(brand),
                "price": clean_text(offer0.get("price")),
                "currency": clean_text(offer0.get("priceCurrency")),
                "availability": clean_text(offer0.get("availability")),
                "url": normalize_url(clean_text(item.get("url") or offer0.get("url")), base_url),
                "rating": rating,
                "source": "json-ld",
            }
            product_info = merge_product_info(product_info, current)
            product_images.extend(extract_image_urls(item.get("image"), base_url))
    return product_info, list(dict.fromkeys(product_images))


def extract_meta_info(soup: BeautifulSoup, base_url: str) -> tuple[dict[str, Any], list[str]]:
    def meta(*names: str) -> str:
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return clean_text(tag["content"])
        return ""

    title = meta("og:title", "twitter:title") or clean_text(soup.title.string if soup.title else "")
    description = meta("og:description", "description", "twitter:description")
    canonical = soup.find("link", attrs={"rel": re.compile("canonical", re.I)})
    info = {
        "name": title,
        "description": description,
        "url": normalize_url(canonical.get("href"), base_url) if canonical else base_url,
        "source": "meta",
    }
    images = [
        normalize_url(meta("og:image", "twitter:image", "twitter:image:src"), base_url),
    ]
    return info, [u for u in images if u]


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
        "SUBSCRIBE",
        "T&CS",
    }

    price_index = None
    price_re = re.compile(r"^(POA|PRICE ON APPLICATION)$|^[£$€]\s?[\d,.]+|^[A-Z]{3}\s?[\d,.]+", re.I)
    for index, line in enumerate(lines):
        if price_re.search(line):
            price_index = index
            break

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
        if not name:
            for candidate in reversed(lines[:price_index]):
                if candidate.upper() not in nav_words and len(candidate) > 3:
                    name = candidate
                    break
    if not name:
        h1 = soup.find("h1")
        if h1:
            name = clean_text(h1.get_text(" "))

    price = lines[price_index] if price_index is not None else ""
    labels = ["Designer", "Manufacturer", "Material", "Period", "Dimensions", "Condition"]
    details: dict[str, str] = {}
    for label in labels:
        pattern = re.compile(rf"^{re.escape(label)}\s*:\s*(.+)$", re.I)
        for line in lines:
            match = pattern.match(line)
            if match:
                details[label] = clean_text(match.group(1))
                break

    description = ""
    if price_index is not None:
        start = price_index + 1
        while start < len(lines) and (
            lines[start].upper() in {"ENQUIRE", "SOLD", "ADD TO CART"}
            or lines[start].lower() == name.lower()
            or lines[start] == price
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
        for marker in labels + ["RECOMMENDED PRODUCTS", "RELATED PRODUCTS"]:
            for index in range(start, len(lines)):
                if lines[index].upper().startswith(marker.upper()):
                    stop = min(stop, index)
                    break
        description = clean_text(" ".join(lines[start:stop]))

    info: dict[str, Any] = {"source": "dom-text", "url": base_url}
    if name and (not existing_name or existing_name.lower() in {"monument", "shop", "store"}):
        info["name"] = name
    if price:
        info["price"] = price
    if description:
        info["description"] = description
    if details:
        info["details"] = details
    return info


def try_shopify_json(url: str, session: requests.Session) -> tuple[dict[str, Any], list[str]]:
    parsed = urlparse(url)
    match = re.search(r"(/products/[^/?#]+)", parsed.path)
    if not match:
        return {}, []
    endpoint = urlunparse((parsed.scheme, parsed.netloc, match.group(1) + ".js", "", "", ""))
    try:
        response = session.get(endpoint, timeout=20)
        if response.status_code != 200 or "json" not in response.headers.get("content-type", ""):
            return {}, []
        product = response.json()
    except Exception:
        return {}, []

    variants = product.get("variants") or []
    first_variant = variants[0] if variants else {}
    price = first_variant.get("price")
    if isinstance(price, int):
        price = f"{price / 100:.2f}"
    images = []
    for image in product.get("images") or []:
        if isinstance(image, str):
            images.append(normalize_url(image, endpoint))
        elif isinstance(image, dict):
            images.append(normalize_url(image.get("src") or image.get("url") or "", endpoint))
    info = {
        "name": clean_text(product.get("title")),
        "description": clean_text(BeautifulSoup(product.get("description") or "", "lxml").get_text(" ")),
        "sku": clean_text(first_variant.get("sku")),
        "price": clean_text(price),
        "url": endpoint.removesuffix(".js"),
        "source": "shopify-product-json",
    }
    return info, [u for u in images if u]


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
        if re.search(r"(product|pdp|gallery|carousel|media|image|photo|slider|zoom)", attrs, re.I):
            score += 18 - depth * 2
            reasons.append("inside product/media block")
        if re.search(r"(header|footer|nav|menu|newsletter|social|recommend|related)", attrs, re.I):
            score -= 20
            reasons.append("inside likely non-product block")
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
) -> None:
    url = normalize_url(raw_url, base_url)
    if not url or url.startswith("data:"):
        return
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return
    item = candidates.setdefault(url, ImageCandidate(url=url, source=source))
    item.add(points, reason)
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
    for url in structured_images:
        add_candidate(candidates, url, base_url, 90, "structured product image", "structured")
    for url in meta_images:
        add_candidate(candidates, url, base_url, 45, "open graph image", "meta")

    for img in soup.find_all(["img", "source"]):
        attrs = []
        for attr in ("src", "data-src", "data-original", "data-zoom-image", "data-large_image"):
            if img.get(attr):
                attrs.append(str(img[attr]))
        for attr in ("srcset", "data-srcset", "imagesrcset"):
            if img.get(attr):
                attrs.append(pick_srcset_best(str(img[attr])))
        for raw in attrs:
            add_candidate(candidates, raw, base_url, 12, "page image candidate", "dom", img)

    for link in soup.find_all("a"):
        href = link.get("href", "")
        if re.search(r"\.(jpe?g|png|webp|avif)(\?|$)", href, re.I):
            add_candidate(candidates, href, base_url, 8, "linked image file", "link", link)

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
            item.add(-25, "low-value image format")
    ranked = sorted(candidates.values(), key=lambda c: c.score, reverse=True)
    return ranked


def fetch_static(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    if parsed.scheme == "file":
        path = Path(url2pathname(parsed.path))
        if parsed.netloc and not re.match(r"^[A-Za-z]:", str(path)):
            path = Path(f"//{parsed.netloc}") / str(path).lstrip("\\/")
        return path.read_text(encoding="utf-8"), url

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return response.text, response.url


def fetch_rendered(url: str) -> tuple[str, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed. Run without --render or install playwright.") from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT, viewport={"width": 1440, "height": 1600})
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        html = page.content()
        final_url = page.url
        browser.close()
    return html, final_url


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


def extract(url: str, render: bool, max_images: int, min_score: int) -> dict[str, Any]:
    html, final_url = fetch_rendered(url) if render else fetch_static(url)
    soup = BeautifulSoup(html, "lxml")
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})

    jsonld_info, jsonld_images = extract_jsonld_products(soup, final_url)
    meta_info, meta_images = extract_meta_info(soup, final_url)
    shopify_info, shopify_images = try_shopify_json(final_url, session)

    product_info = {}
    for info in (jsonld_info, shopify_info, meta_info):
        product_info = merge_product_info(product_info, info)
    dom_info = extract_dom_product_info(soup, final_url, product_info.get("name", ""))
    product_info = merge_product_info(dom_info, product_info)
    product_info["page_url"] = final_url

    structured_images = list(dict.fromkeys(jsonld_images + shopify_images))
    candidates = gather_image_candidates(
        soup,
        final_url,
        structured_images,
        meta_images,
        product_info.get("name", ""),
    )
    candidates = dedupe_image_candidates(candidates)
    candidates = prefer_numbered_gallery(candidates)
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
        if c.score >= min_score
    ][:max_images]

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
    parser.add_argument("--min-score", type=int, default=25, help="Minimum image confidence score")
    parser.add_argument(
        "--out",
        default="product_extract",
        help="Output directory for result JSON and optional images",
    )
    args = parser.parse_args()

    try:
        result = extract(args.url, args.render, args.max_images, args.min_score)
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
