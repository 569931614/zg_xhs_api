from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


class ScrapeRequest(BaseModel):
    url: HttpUrl
    render: Literal["auto", "always", "never"] = "auto"
    max_images: int = Field(default=40, ge=1, le=80)
    min_score: int = Field(default=25, ge=-100, le=200)
    download_images: bool = False


class ImageResult(BaseModel):
    url: str
    score: int
    source: str = ""
    alt: str = ""
    width: int | None = None
    height: int | None = None
    reasons: list[str] = Field(default_factory=list)
    local_url: str | None = None
    hosted_url: str | None = None
    filename: str | None = None
    bytes: int | None = None
    download_error: str | None = None


class ScrapeResponse(BaseModel):
    job_id: str
    input_url: str
    fetched_url: str
    rendered: bool
    skipped: bool = False
    skip_reason: str | None = None
    product: dict[str, Any]
    images: list[ImageResult]
    rejected_preview: list[dict[str, Any]] = Field(default_factory=list)
    result_url: str
