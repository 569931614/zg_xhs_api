from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator


class ScrapeRequest(BaseModel):
    url: HttpUrl | None = None
    urls: list[HttpUrl] = Field(default_factory=list, max_length=10)
    render: Literal["auto", "always", "never"] = "auto"
    max_images: int = Field(default=12, ge=1, le=12)
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
    price: str = ""
    currency: str = ""
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


class AuctionListRequest(BaseModel):
    url: HttpUrl
    render: Literal["auto", "always", "never"] = "auto"
    max_items: int = Field(default=50, ge=1, le=100)
    max_pages: int = Field(default=0, ge=0, le=500)
    save_to_db: bool = True


class AuctionLotItem(BaseModel):
    lot_id: str = ""
    lot_number: str = ""
    title: str = ""
    url: str
    image_link: str = ""
    sale_status: str = ""
    sale_time: str = ""
    estimate: str = ""
    currency: str = ""
    low_estimate: float | int | None = None
    high_estimate: float | int | None = None
    sale_timestamp: int = 0
    sale_type: str = ""
    sale_id: int | None = None
    auctioneer_id: int | None = None
    page: int = 0
    position: int = 0
    raw_text: str = ""


class AuctionListResponse(BaseModel):
    source_url: str
    fetched_url: str
    title: str = ""
    total_count: int | None = None
    pages_fetched: int = 0
    saved_count: int = 0
    database_path: str = ""
    items: list[AuctionLotItem] = Field(default_factory=list)


class XHSCreateRequest(ScrapeRequest):
    pass


class XHSCreateResponse(BaseModel):
    job_id: str
    qrcode_image_link: str
    share_link: str
    xhs_link: str
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


class XianyuCopyRequest(ScrapeRequest):
    pass


class XianyuCopyResponse(BaseModel):
    job_id: str
    title: str = ""
    content: str = ""
    xianyu_copy: str = ""
    product_type: str = ""
    price: str = ""
    source_price: str = ""
    source_currency: str = ""
    image_links: list[str] = Field(default_factory=list)
    result_path: str = ""


class XianyuCopyBatchItem(BaseModel):
    url: str
    success: bool
    result: XianyuCopyResponse | None = None
    error: str | None = None


class XianyuCopyBatchResponse(BaseModel):
    results: list[XianyuCopyBatchItem] = Field(default_factory=list)
