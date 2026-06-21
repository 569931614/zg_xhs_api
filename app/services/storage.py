from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import requests


@dataclass
class UploadResult:
    provider: str
    url: str


class UploadError(RuntimeError):
    pass


def upload_to_superbed(path: Path, filename: str) -> UploadResult:
    token = os.getenv("SUPERBED_TOKEN", "").strip()
    if not token:
        raise UploadError("SUPERBED_TOKEN is not configured")

    endpoint = os.getenv("SUPERBED_UPLOAD_URL", "https://api.superbed.cc/upload").strip()
    categories = os.getenv("SUPERBED_CATEGORIES", "").strip()
    data = {"filename": filename}
    if categories:
        data["categories"] = categories

    with path.open("rb") as file:
        response = requests.post(
            endpoint,
            params={"token": token},
            data=data,
            files={"file": (filename, file)},
            timeout=45,
        )
    response.raise_for_status()
    payload = response.json()
    if payload.get("err") == 0 and payload.get("url"):
        return UploadResult(provider="superbed", url=str(payload["url"]))
    raise UploadError(str(payload.get("msg") or payload))


def upload_to_aliyun_oss(path: Path, filename: str, content_type: str | None = None) -> UploadResult:
    access_key_id = os.getenv("ALI_OSS_ACCESS_KEY_ID", "").strip()
    access_key_secret = os.getenv("ALI_OSS_ACCESS_KEY_SECRET", "").strip()
    endpoint = os.getenv("ALI_OSS_ENDPOINT", "").strip()
    bucket_name = os.getenv("ALI_OSS_BUCKET", "").strip()

    missing = [
        name
        for name, value in {
            "ALI_OSS_ACCESS_KEY_ID": access_key_id,
            "ALI_OSS_ACCESS_KEY_SECRET": access_key_secret,
            "ALI_OSS_ENDPOINT": endpoint,
            "ALI_OSS_BUCKET": bucket_name,
        }.items()
        if not value
    ]
    if missing:
        raise UploadError(f"Aliyun OSS config missing: {', '.join(missing)}")

    try:
        import oss2
    except ImportError as exc:
        raise UploadError("oss2 is not installed") from exc

    prefix = os.getenv("ALI_OSS_PREFIX", "product-scraper").strip().strip("/")
    object_name = f"{prefix}/{filename}" if prefix else filename
    headers = {"Content-Type": content_type} if content_type else None

    auth = oss2.Auth(access_key_id, access_key_secret)
    bucket = oss2.Bucket(auth, endpoint, bucket_name)
    bucket.put_object_from_file(object_name, str(path), headers=headers)

    public_base_url = os.getenv("ALI_OSS_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if public_base_url:
        url = f"{public_base_url}/{quote(object_name)}"
    else:
        endpoint_host = endpoint.replace("https://", "").replace("http://", "").rstrip("/")
        url = f"https://{bucket_name}.{endpoint_host}/{quote(object_name)}"
    return UploadResult(provider="aliyun-oss", url=url)


def upload_image_with_fallback(path: Path, filename: str, content_type: str | None = None) -> UploadResult:
    errors: list[str] = []
    try:
        return upload_to_superbed(path, filename)
    except Exception as exc:
        errors.append(f"superbed: {exc}")

    try:
        return upload_to_aliyun_oss(path, filename, content_type)
    except Exception as exc:
        errors.append(f"aliyun-oss: {exc}")

    raise UploadError("; ".join(errors))
