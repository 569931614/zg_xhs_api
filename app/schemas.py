from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator


class ScrapeRequest(BaseModel):
    url: HttpUrl | None = None
    urls: list[HttpUrl] = Field(default_factory=list, max_length=10)
    render: Literal["auto", "always", "never"] = "auto"
    max_images: int = Field(default=12, ge=1, le=12)
    min_score: int = Field(default=25, ge=-100, le=200)
    download_images: bool = False

    @model_validator(mode="after")
    def validate_urls(self) -> "ScrapeRequest":
        if self.url is None and not self.urls:
            raise ValueError("Either url or urls is required")
        return self

    def product_urls(self) -> list[str]:
        values = []
        if self.url is not None:
            values.append(str(self.url))
        values.extend(str(url) for url in self.urls)
        return list(dict.fromkeys(values))

    def is_batch(self) -> bool:
        return bool(self.urls)


class ScrapeResponse(BaseModel):
    name: str = ""
    image_links: list[str] = Field(default_factory=list)
    dimensions: str = ""
    product_details: dict[str, Any] = Field(default_factory=dict)


class ScrapeBatchItem(BaseModel):
    url: str
    success: bool
    result: ScrapeResponse | None = None
    error: str | None = None


class ScrapeBatchResponse(BaseModel):
    results: list[ScrapeBatchItem] = Field(default_factory=list)


class XHSCreateRequest(ScrapeRequest):
    pass


class XHSCreateResponse(BaseModel):
    job_id: str
    qrcode_image_link: str
    share_link: str
    title: str = ""
    content: str = ""
    result_path: str = ""


class XHSCreateBatchItem(BaseModel):
    url: str
    success: bool
    result: XHSCreateResponse | None = None
    error: str | None = None


class XHSCreateBatchResponse(BaseModel):
    results: list[XHSCreateBatchItem] = Field(default_factory=list)
