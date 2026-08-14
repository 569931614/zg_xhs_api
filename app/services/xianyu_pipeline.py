from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import requests

from app.services.xhs_pipeline import product_text


ARK_CHAT_COMPLETIONS_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
DEFAULT_XIANYU_COPY_MODEL = "doubao-seed-2-0-pro-260215"
XIANYU_CLOSING_LINE = "可预定，货期90-150天，国内运费到付，喜欢私聊"
XIANYU_BODY_MIN_CHARS = 120
XIANYU_BODY_MAX_CHARS = 200
COUNTRY_TRANSLATIONS = {
    "aland islands": "",
    "austria": "奥地利",
    "austrian": "奥地利",
    "australia": "澳大利亚",
    "australian": "澳大利亚",
    "belgium": "比利时",
    "belgian": "比利时",
    "belgie": "比利时",
    "belgië": "比利时",
    "belgique": "比利时",
    "belgisch": "比利时",
    "belgo": "比利时",
    "brazil": "巴西",
    "brazilian": "巴西",
    "canada": "加拿大",
    "canadian": "加拿大",
    "catalan": "西班牙",
    "china": "中国",
    "chinese": "中国",
    "czech": "捷克",
    "czechoslovakia": "捷克",
    "denmark": "丹麦",
    "danish": "丹麦",
    "danemark": "丹麦",
    "finland": "芬兰",
    "finnish": "芬兰",
    "france": "法国",
    "french": "法国",
    "francais": "法国",
    "francaise": "法国",
    "français": "法国",
    "française": "法国",
    "francia": "法国",
    "frances": "法国",
    "francés": "法国",
    "frankrijk": "法国",
    "germany": "德国",
    "german": "德国",
    "allemagne": "德国",
    "ireland": "爱尔兰",
    "irish": "爱尔兰",
    "italy": "意大利",
    "italian": "意大利",
    "italia": "意大利",
    "italie": "意大利",
    "japan": "日本",
    "japanese": "日本",
    "korea": "韩国",
    "korean": "韩国",
    "netherlands": "荷兰",
    "dutch": "荷兰",
    "holland": "荷兰",
    "hollandsk": "荷兰",
    "holländsk": "荷兰",
    "holländska": "荷兰",
    "nederland": "荷兰",
    "nederlandse": "荷兰",
    "norway": "挪威",
    "norwegian": "挪威",
    "poland": "波兰",
    "polish": "波兰",
    "portugal": "葡萄牙",
    "portuguese": "葡萄牙",
    "scotland": "苏格兰",
    "scottish": "苏格兰",
    "spain": "西班牙",
    "spanish": "西班牙",
    "sweden": "瑞典",
    "swedish": "瑞典",
    "suede": "瑞典",
    "suedoises": "瑞典",
    "suédoises": "瑞典",
    "svensk": "瑞典",
    "svenska": "瑞典",
    "switzerland": "瑞士",
    "swiss": "瑞士",
    "turkey": "土耳其",
    "turkish": "土耳其",
    "united kingdom": "英国",
    "uk": "英国",
    "british": "英国",
    "united states": "美国",
    "usa": "美国",
    "american": "美国",
}
PLACE_TRANSLATIONS = {
    "london": "伦敦",
    "hackney": "伦敦",
    "paris": "巴黎",
    "milan": "米兰",
    "milano": "米兰",
    "rome": "罗马",
    "roma": "罗马",
    "naples": "那不勒斯",
    "napoli": "那不勒斯",
    "new york": "纽约",
    "los angeles": "洛杉矶",
    "copenhagen": "哥本哈根",
    "stockholm": "斯德哥尔摩",
    "amsterdam": "阿姆斯特丹",
    "brussels": "布鲁塞尔",
    "berlin": "柏林",
    "vienna": "维也纳",
    "zurich": "苏黎世",
}
logger = logging.getLogger("uvicorn.error")


class XianyuPipelineError(RuntimeError):
    pass


class XianyuCopyValidationError(ValueError):
    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


@dataclass
class XianyuCopyResult:
    job_id: str
    title: str
    content: str
    xianyu_copy: str
    product_type: str
    price: str
    source_price: str
    source_currency: str
    image_links: list[str]


def ark_api_key() -> str:
    value = os.getenv("ARK_API_KEY", "").strip() or os.getenv("VOLCENGINE_ARK_API_KEY", "").strip()
    if not value:
        raise XianyuPipelineError("ARK_API_KEY is not configured")
    return value


def normalize_price_number(value: str) -> Decimal | None:
    text = str(value or "").replace("\xa0", " ")
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
        return Decimal(number)
    except InvalidOperation:
        return None


def calculate_xianyu_price(source_price: str) -> str:
    text = str(source_price or "").strip().lower()
    if re.search(r"\b(?:login|request|estimate|estimated|sold|on request|upon request|contact|enquire|inquire)\b", text):
        return "99999"
    amount = normalize_price_number(source_price)
    if amount is None or amount <= 0:
        return "99999"
    price = (amount * Decimal("8") * Decimal("2") + Decimal("5000")).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )
    return str(int(price))


def format_cm(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    text = format(rounded.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def inch_text_to_cm(value: str) -> str:
    amount = normalize_price_number(value)
    if amount is None:
        return ""
    return format_cm(amount * Decimal("2.54"))


def normalize_dimensions_to_cm(value: Any) -> str:
    text = " ".join(str(value or "").replace("\xa0", " ").split())
    if not text:
        return ""

    has_inches = bool(re.search(r'(?:\binches?\b|\bin\b|["″”])', text, re.I))
    if not has_inches:
        return text

    has_metric = bool(re.search(r"(?<![a-z])(?:cm|centimeters?|centimetres?|mm|m)\b", text, re.I))
    if not has_metric:
        number_count = len(re.findall(r"\d+(?:[.,]\d+)?", text))
        explicit_unit_count = len(re.findall(r'\d+(?:[.,]\d+)?\s*(?:inches?\b|in\b|["″”])', text, re.I))
        if re.search(r"[x×]", text) and explicit_unit_count < number_count:
            converted = re.sub(
                r"\d+(?:[.,]\d+)?",
                lambda match: inch_text_to_cm(match.group(0)),
                text,
            )
            converted = re.sub(r'\s*(?:inches?\b|in\b|["″”])', "", converted, flags=re.I)
            converted = re.sub(r"\s+", " ", converted).strip()
            return converted if re.search(r"\bcm\b", converted, re.I) else f"{converted} cm"

        converted = re.sub(
            r'\d+(?:[.,]\d+)?\s*(?:inches?\b|in\b|["″”])',
            lambda match: f"{inch_text_to_cm(match.group(0))} cm",
            text,
            flags=re.I,
        )
        converted = re.sub(r"\s+", " ", converted).strip()
        return converted

    # Mixed strings such as "10 in (25 cm)" keep the existing centimeter value.
    converted = re.sub(
        r"\d+(?:[.,]\d+)?\s*(?:inches?\b|in\b|[\"″”])\s*\(\s*(\d+(?:[.,]\d+)?)\s*cm\s*\)",
        lambda match: f"{match.group(1).replace(',', '.')} cm",
        text,
        flags=re.I,
    )
    converted = re.sub(
        r"(\d+(?:[.,]\d+)?)\s*cm\s*\(\s*\d+(?:[.,]\d+)?\s*(?:inches?\b|in\b|[\"″”])\s*\)",
        lambda match: f"{match.group(1).replace(',', '.')} cm",
        converted,
        flags=re.I,
    )
    converted = re.sub(
        r"\d+(?:[.,]\d+)?\s*(?:inches?\b|in\b|[\"″”])",
        lambda match: f"{inch_text_to_cm(match.group(0))} cm",
        converted,
        flags=re.I,
    )
    return re.sub(r"\s+", " ", converted).strip()


def product_with_cm_dimensions(product: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(product)
    dimensions = normalize_dimensions_to_cm(product.get("dimensions"))
    if dimensions:
        normalized["dimensions"] = dimensions
    details = product.get("product_details")
    if isinstance(details, dict):
        normalized_details = dict(details)
        if normalized_details.get("dimensions"):
            normalized_details["dimensions"] = normalize_dimensions_to_cm(normalized_details["dimensions"])
        normalized["product_details"] = normalized_details
    return normalized


def known_country_from_text(value: Any) -> str:
    text = " ".join(str(value or "").replace("\xa0", " ").split())
    if not text:
        return ""
    contextual = contextual_country_from_text(text)
    if contextual:
        return contextual
    text = strip_non_product_country_context(text)
    lower = text.lower()
    for country in set(COUNTRY_TRANSLATIONS.values()):
        if country and country in text:
            return country
    for key, country in sorted(COUNTRY_TRANSLATIONS.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", lower):
            return country
    return ""


def country_key_patterns() -> list[tuple[str, str]]:
    return sorted(
        ((key, country) for key, country in COUNTRY_TRANSLATIONS.items() if country),
        key=lambda item: len(item[0]),
        reverse=True,
    )


def place_key_patterns() -> list[tuple[str, str]]:
    return sorted(
        ((key, place) for key, place in PLACE_TRANSLATIONS.items() if place),
        key=lambda item: len(item[0]),
        reverse=True,
    )


def known_place_from_text(value: Any) -> str:
    text = " ".join(str(value or "").replace("\xa0", " ").split())
    if not text:
        return ""
    lower = text.lower()
    for place in set(PLACE_TRANSLATIONS.values()):
        if place and place in text:
            return place
    for key, place in place_key_patterns():
        if re.search(rf"\b{re.escape(key)}\b", lower):
            return place
    return ""


def place_aliases(origin: str) -> set[str]:
    aliases = {origin}
    for key, place in PLACE_TRANSLATIONS.items():
        if place == origin:
            aliases.add(key)
    for key, country in COUNTRY_TRANSLATIONS.items():
        if country == origin:
            aliases.add(key)
    return aliases


def text_contains_origin(text: str, origin: str) -> bool:
    lower = text.lower()
    for alias in place_aliases(origin):
        if re.search(r"[\u4e00-\u9fff]", alias):
            if alias in text:
                return True
        elif re.search(rf"\b{re.escape(alias.lower())}\b", lower):
            return True
    return False


def contextual_country_from_text(text: str) -> str:
    lower = text.lower()
    contexts = (
        "sent from",
        "ships from",
        "ship from",
        "made in",
        "produced in",
        "origin",
        "place of origin",
        "country of origin",
    )
    for key, country in country_key_patterns():
        key_pattern = re.escape(key)
        for context in contexts:
            if re.search(rf"\b{re.escape(context)}\b\s*:?\s*(?:the\s+)?{key_pattern}\b", lower):
                return country
    return ""


def strip_non_product_country_context(text: str) -> str:
    stripped = text
    stripped = re.sub(r"hong kong sar\s*\([^)]*\).*?update country", "", stripped, flags=re.I)
    for key, _country in country_key_patterns():
        key_pattern = re.escape(key)
        stripped = re.sub(rf"\bdelivery\s+to\s+(?:the\s+)?{key_pattern}\b", "", stripped, flags=re.I)
        stripped = re.sub(rf"\byour\s+location\s+is\s+(?:the\s+)?{key_pattern}\b", "", stripped, flags=re.I)
        stripped = re.sub(rf"\bdestination\s+is\s+(?:the\s+)?{key_pattern}\b", "", stripped, flags=re.I)
    stripped = re.sub(r"\bwe speak\b[^.。]*", "", stripped, flags=re.I)
    return stripped


def normalize_country_text(value: Any, allow_freeform: bool = False) -> str:
    country = known_country_from_text(value)
    if country:
        return country
    place = known_place_from_text(value)
    if place:
        return place
    if not allow_freeform:
        return ""
    text = clean_country_source(value)
    if not looks_like_country_value(text):
        return ""
    return text


def clean_country_source(value: Any) -> str:
    text = " ".join(str(value or "").replace("\xa0", " ").split())
    text = re.split(r"[,;/|]", text, maxsplit=1)[0].strip()
    return text.strip(" .:-")


def looks_like_country_value(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    if lower in {"of", "from", "unknown", "xxx", "n/a", "na", "default title"}:
        return False
    if " sar" in lower or lower.endswith("sar"):
        return False
    if re.search(r"[$€£¥]|\b(?:eur|usd|gbp|shipping|delivery|price|login|request|newsletter)\b", lower):
        return False
    if len(text) > 40:
        return False
    if len(text.split()) > 4:
        return False
    return bool(re.search(r"[A-Za-zÀ-ÿ\u4e00-\u9fff]", text))


def extract_product_country(product: dict[str, Any]) -> str:
    details = product.get("product_details") if isinstance(product.get("product_details"), dict) else {}
    country_keys = ("Place of Origin", "Origin", "Country")
    for key in country_keys:
        country = normalize_country_text(details.get(key), allow_freeform=True)
        if country:
            return country

    name_country = known_country_from_text(product.get("name"))
    if name_country:
        return name_country

    for key in ("Origin / period", "Origin / Period"):
        country = normalize_country_text(details.get(key), allow_freeform=True)
        if country:
            return country

    haystack = " ".join(
        str(value or "")
        for value in [product.get("description"), details.get("description"), *details.values()]
    )
    return contextual_country_from_text(strip_non_product_country_context(haystack))


def xianyu_display_dimensions(product: dict[str, Any]) -> str:
    dimensions = normalize_dimensions_to_cm(product.get("dimensions"))
    return dimensions or "详询"


def clean_collection_punctuation(body: str) -> str:
    body = re.sub(r"值得收藏\s*[。.!！]*$", "值得收藏", body.strip())
    return re.sub(r"[，,。；;]+\s*值得收藏$", "，值得收藏", body)


def normalize_xianyu_content_dimensions(content: str, product: dict[str, Any], product_type: str = "") -> str:
    dimensions = xianyu_display_dimensions(product)
    content = content.strip()
    lines = content.splitlines()
    body_lines: list[str] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if re.match(r"^\s*尺寸\s*[:：]", line):
            body_lines = lines[index + 1 :]
            break
        body_lines = lines[index:]
        break

    body = " ".join(line.strip() for line in body_lines if line.strip())
    body = re.sub(r"\s+", " ", body).strip()
    body = body.replace(f"“{XIANYU_CLOSING_LINE}”", XIANYU_CLOSING_LINE)
    body = body.replace(f"\"{XIANYU_CLOSING_LINE}\"", XIANYU_CLOSING_LINE)
    if XIANYU_CLOSING_LINE in body:
        body = body.replace(XIANYU_CLOSING_LINE, "").strip(" ，,。；;")
    body = clean_collection_punctuation(body)
    lines_out = [f"尺寸：{dimensions}"]
    if body:
        lines_out.append(body)
    lines_out.append(XIANYU_CLOSING_LINE)
    return "\n".join(lines_out).strip()


def xianyu_content_body(content: str) -> str:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if len(lines) < 3:
        return ""
    return lines[1]


def validate_xianyu_copy(title: str, content: str, product: dict[str, Any], product_type: str) -> None:
    reasons: list[str] = []
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    body = xianyu_content_body(content)
    country = extract_product_country(product)

    if not title:
        reasons.append("标题为空")
    elif not title.startswith("【中古预定】"):
        reasons.append("标题未按【中古预定】开头")

    if len(lines) != 3:
        reasons.append("正文不是尺寸行、主段、预定行三行格式")
    else:
        if not lines[0].startswith("尺寸："):
            reasons.append("正文第一行不是尺寸行")
        if lines[2] != XIANYU_CLOSING_LINE:
            reasons.append("固定预定句没有单独作为最后一行")

    if not body:
        reasons.append("正文主段为空")
    else:
        body_len = len(body)
        if body_len < XIANYU_BODY_MIN_CHARS or body_len > XIANYU_BODY_MAX_CHARS:
            reasons.append(f"正文主段字数为{body_len}，不在{XIANYU_BODY_MIN_CHARS}-{XIANYU_BODY_MAX_CHARS}字范围内")
        if country and not text_contains_origin(body, country):
            reasons.append(f"正文主段未包含国家/产地：{country}")
        if re.match(r"^(?:这是|本款为|本件为|本品为|这款|这件)", body):
            reasons.append("正文主段应直接以产品信息开头，不应以“这是”“本款为”“本件为”等套话开头")
        if not body.endswith("值得收藏"):
            reasons.append("正文主段未以“值得收藏”结尾")

    if re.search(r'(?:\binches?\b|\bin\b|["″”]|英寸)', content, re.I):
        reasons.append("正文仍包含英寸单位")

    if not product_type:
        reasons.append("产品类型为空")

    if reasons:
        raise XianyuCopyValidationError(reasons)


def xianyu_product_text(product: dict[str, Any], calculated_price: str) -> str:
    product = product_with_cm_dimensions(product)
    image_links = [str(url) for url in product.get("image_links") or [] if url]
    lines = [product_text(product)]
    country = extract_product_country(product)
    if country:
        lines.append(f"国家/产地：{country}")
    if product.get("source_url"):
        lines.append(f"商品链接：{product['source_url']}")
    if product.get("price"):
        currency = str(product.get("currency") or "").strip()
        lines.append(f"网站标价：{product['price']}{f' {currency}' if currency else ''}")
    if calculated_price:
        lines.append(f"闲鱼人民币售价：{calculated_price}")
    if image_links:
        lines.append("产品图片链接：")
        lines.extend(image_links[:6])
    return "\n".join(line for line in lines if line)


def xianyu_image_links(product: dict[str, Any]) -> list[str]:
    max_images_raw = os.getenv("XIANYU_COPY_MAX_IMAGES", "4").strip()
    try:
        max_images = max(1, min(8, int(max_images_raw)))
    except ValueError:
        max_images = 4
    return [
        str(url).strip()
        for url in product.get("image_links") or []
        if str(url).strip().startswith(("http://", "https://"))
    ][:max_images]


def xianyu_copy_max_attempts() -> int:
    raw_value = os.getenv("XIANYU_COPY_MAX_ATTEMPTS", "5").strip()
    try:
        return max(1, min(5, int(raw_value)))
    except ValueError:
        return 5


def xianyu_copy_retry_delay() -> float:
    raw_value = os.getenv("XIANYU_COPY_RETRY_DELAY", "1").strip()
    try:
        return max(0.0, float(raw_value))
    except ValueError:
        return 1.0


def xianyu_retry_instruction(error: Exception) -> str:
    text = str(error)
    match = re.search(r"正文主段字数为(\d+)", text)
    if match:
        body_len = int(match.group(1))
        if body_len > XIANYU_BODY_MAX_CHARS:
            return "上一次正文主段偏长，请删减修饰和重复描述，把主段压缩到120-200字。"
        if body_len < XIANYU_BODY_MIN_CHARS:
            return "上一次正文主段偏短，请补充基本信息、图片特征和画廊收藏价值，把主段扩展到120-200字。"
    return "请严格按格式和字段要求重新生成。"


def parse_copy_payload(content: str) -> dict[str, str]:
    text = content.strip()
    json_match = re.search(r"\{.*\}", text, flags=re.S)
    if json_match:
        try:
            payload = json.loads(json_match.group(0))
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            return {
                "title": str(payload.get("title") or "").strip(),
                "content": str(payload.get("content") or "").strip(),
                "product_type": str(payload.get("product_type") or "").strip(),
                "price": str(payload.get("price") or "").strip(),
            }

    parts = [part.strip() for part in text.splitlines() if part.strip()]
    if not parts:
        return {"title": "", "content": "", "product_type": "", "price": ""}
    return {"title": parts[0], "content": text[len(parts[0]) :].strip(), "product_type": "", "price": ""}


def infer_product_type(product: dict[str, Any], title: str = "", content: str = "") -> str:
    details = product.get("product_details") if isinstance(product.get("product_details"), dict) else {}
    haystack = " ".join(
        str(value or "")
        for value in [
            product.get("name"),
            title,
            content,
            details.get("description"),
            *details.values(),
        ]
    ).lower()
    type_keywords = [
        ("灯具", ("lamp", "lamps", "lighting", "light", "pendant", "chandelier", "sconce", "lantern", "floor lamp", "table lamp", "壁灯", "台灯", "吊灯", "落地灯", "灯具")),
        ("长凳", ("bench", "长凳")),
        ("椅子", ("chair", "chairs", "armchair", "stool", "椅", "凳")),
        ("桌子", ("table", "desk", "console", "coffee table", "dining table", "桌", "几")),
        ("柜子", ("cabinet", "sideboard", "credenza", "commode", "dresser", "chest", "柜")),
        ("沙发", ("sofa", "settee", "loveseat", "沙发")),
        ("镜子", ("mirror", "镜")),
        ("床", ("bed", "daybed", "床")),
        ("屏风", ("screen", "屏风")),
    ]
    for product_type, keywords in type_keywords:
        if any(keyword in haystack for keyword in keywords):
            return product_type
    return ""


def generate_xianyu_copy(
    product: dict[str, Any],
    request_id_value: str | None = None,
    job_id: str | None = None,
) -> tuple[str, str, str, str]:
    started = time.monotonic()
    calculated_price = calculate_xianyu_price(str(product.get("price") or ""))
    image_links = xianyu_image_links(product)
    if not image_links:
        raise XianyuPipelineError("No product images were found for Xianyu copy creation")
    system_prompt = """假如你是旨丘画廊的CMO，请为上图这件作品写一段文案用于闲鱼，生成标题、正文、价格。

输出必须是严格 JSON，不要使用 Markdown，不要加解释：
{
  "title": "标题",
  "content": "正文",
  "product_type": "产品类型",
  "price": "人民币售价"
}

要求如下：
1、标题：先获取网站商品标题；若商品标题不是英文，必须先准确翻译成英文。英文标题后请根据商品链接文字内容写中文描述，中文描述格式为：什么国家什么年代的什么特征的什么品类家具；上述某个信息如没有出现就直接忽略，不要在标题中文描述里写设计师或制造商。
标题按照以下格式：【中古预定】+英文商品标题+中文描述。英文商品标题部分不要包含价格、库存编号、Item 编号或网站名。
2、正文第一句必须是尺寸行：抓取网站尺寸；若无，写“尺寸：详询”。所有尺寸必须统一使用 cm；若源站尺寸为英寸，使用已换算后的 cm 尺寸，不要在正文保留 inch、in、英寸或双引号英寸符号。
3、正文必须包含国家/产地；若产品信息里有国家/产地，正文主段必须明确写出该国家。
4、正文除第一句尺寸和最后一句预定信息外，中间正文主段必须写120-200字，合并成一个连续段落，不要空行，不要分段；正文主段必须直接以产品信息开头，例如“瑞典1940年代Bjerkås Armatur生产的做旧铁艺吊灯…”，不要以“这是”“本款为”“本件为”“本品为”“这款”“这件”等套话开头。
5、中间正文主段只写两句：第一句写图片文字上能获取的所有基本信息，包括什么国家什么年代由什么设计师设计、什么制造商生产的什么品类及特征的家具。
6、中间正文主段第二句结合产品照片及其文字描写其独特性和收藏性展开，需要以画廊专业角度去写，并以“值得收藏”这几个字结尾。
7、不要额外增加日常使用场景或实用性句子。
8、正文最后一句必须单独一行，并严格写为：“可预定，货期90-150天，国内运费到付，喜欢私聊”
9、产品类型：根据商品标题、页面文字和图片判断产品品类，用简短中文名返回，例如“灯具”“椅子”“长凳”“桌子”“柜子”“沙发”“镜子”。
10、价格：使用用户提供的人民币售价，不要自行换算，不要添加货币符号、逗号或其他文字。
11、不要编造产品信息；没有出现的国家、年代、设计师、制造商、材质信息直接忽略。"""
    user_prompt = (
        f"产品详细信息如下：\n{xianyu_product_text(product, calculated_price)}\n\n"
        f"请根据以上信息生成闲鱼标题、正文、价格。若产品标题不是英文，先翻译成英文再用于标题。价格字段必须填：{calculated_price or '空字符串'}"
    )
    model = os.getenv("XIANYU_COPY_MODEL") or os.getenv("ARK_VISION_MODEL") or DEFAULT_XIANYU_COPY_MODEL
    url = os.getenv("ARK_CHAT_COMPLETIONS_URL", ARK_CHAT_COMPLETIONS_URL)
    logger.info(
        "xianyu stage=generate_copy_start request_id=%s job_id=%s provider=ark model=%s images=%d source_price=%r calculated_price=%r",
        request_id_value,
        job_id,
        model,
        len(image_links),
        product.get("price", ""),
        calculated_price,
    )
    headers = {
        "Authorization": f"Bearer {ark_api_key()}",
        "Content-Type": "application/json",
    }
    user_content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
    user_content.extend(
        {"type": "image_url", "image_url": {"url": image_url}}
        for image_url in image_links
    )
    attempts = xianyu_copy_max_attempts()
    retry_delay = xianyu_copy_retry_delay()
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        attempt_started = time.monotonic()
        attempt_user_content = list(user_content)
        if last_error is not None:
            attempt_user_content.append(
                {
                    "type": "text",
                    "text": (
                        f"上一次输出不合格，失败原因：{last_error}。\n"
                        f"{xianyu_retry_instruction(last_error)}"
                        "不要复用不合格结果；尤其确保正文主段为120-200字、只写两句、包含国家/产地、直接以产品信息开头、不要以“这是”“本款为”“本件为”等套话开头、以“值得收藏”结尾，"
                        "并让“可预定，货期90-150天，国内运费到付，喜欢私聊”单独作为最后一行。"
                    ),
                }
            )
        request_body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": attempt_user_content},
            ],
            "temperature": 0.6,
            "response_format": {"type": "json_object"},
        }
        logger.info(
            "xianyu stage=generate_copy_attempt_start request_id=%s job_id=%s attempt=%d max_attempts=%d",
            request_id_value,
            job_id,
            attempt,
            attempts,
        )
        try:
            response = requests.post(
                url,
                headers=headers,
                json=request_body,
                timeout=120,
            )
            if response.status_code == 400 and "response_format" in response.text:
                request_body.pop("response_format", None)
                response = requests.post(
                    url,
                    headers=headers,
                    json=request_body,
                    timeout=120,
                )
            if response.status_code >= 400:
                raise XianyuPipelineError(f"Ark HTTP {response.status_code}: {response.text[:500]}")
            payload = response.json()
            raw_content = str(payload["choices"][0]["message"]["content"]).strip()
            parsed = parse_copy_payload(raw_content)
            title = parsed["title"]
            product_type = parsed["product_type"] or infer_product_type(product, title, parsed["content"])
            body = normalize_xianyu_content_dimensions(parsed["content"], product, product_type)
            product_type = product_type or infer_product_type(product, title, body)
            if not title or not body:
                raise XianyuCopyValidationError(["Ark 返回的闲鱼文案不完整"])
            validate_xianyu_copy(title, body, product, product_type)
            logger.info(
                "xianyu stage=generate_copy_attempt_done request_id=%s job_id=%s attempt=%d title=%r elapsed=%.2fs",
                request_id_value,
                job_id,
                attempt,
                title,
                time.monotonic() - attempt_started,
            )
            logger.info(
                "xianyu stage=generate_copy_done request_id=%s job_id=%s title=%r attempts=%d elapsed=%.2fs",
                request_id_value,
                job_id,
                title,
                attempt,
                time.monotonic() - started,
            )
            return title, body, product_type, calculated_price
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError, XianyuPipelineError) as exc:
            last_error = exc
            logger.warning(
                "xianyu stage=generate_copy_attempt_failed request_id=%s job_id=%s attempt=%d max_attempts=%d error=%r elapsed=%.2fs",
                request_id_value,
                job_id,
                attempt,
                attempts,
                str(exc),
                time.monotonic() - attempt_started,
            )
            if attempt < attempts and retry_delay > 0:
                time.sleep(retry_delay)

    raise XianyuPipelineError(f"Ark returned invalid Xianyu copy after {attempts} attempts: {last_error}") from last_error


def create_xianyu_copy(
    product: dict[str, Any],
    job_id: str,
    job_dir: Path,
    request_id_value: str | None = None,
) -> XianyuCopyResult:
    started = time.monotonic()
    title, content, product_type, price = generate_xianyu_copy(product, request_id_value, job_id)
    xianyu_copy = f"{title}\n{content}".strip()
    image_links = [str(url) for url in product.get("image_links") or [] if url]
    result = {
        "job_id": job_id,
        "title": title,
        "content": content,
        "xianyu_copy": xianyu_copy,
        "product_type": product_type,
        "price": price,
        "source_price": str(product.get("price") or ""),
        "source_currency": str(product.get("currency") or ""),
        "image_links": image_links,
    }
    (job_dir / "xianyu_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "xianyu event=done request_id=%s job_id=%s images=%d elapsed=%.2fs",
        request_id_value,
        job_id,
        len(image_links),
        time.monotonic() - started,
    )
    return XianyuCopyResult(
        job_id=job_id,
        title=title,
        content=content,
        xianyu_copy=xianyu_copy,
        product_type=product_type,
        price=price,
        source_price=result["source_price"],
        source_currency=result["source_currency"],
        image_links=image_links,
    )
