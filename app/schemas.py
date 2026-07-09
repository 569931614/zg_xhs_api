from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


class ScrapeRequest(BaseModel):
    url: HttpUrl
    render: Literal["auto", "always", "never"] = "auto"
    max_images: int = Field(default=12, ge=1, le=12)
    min_score: int = Field(default=25, ge=-100, le=200)
    download_images: bool = False


class ScrapeResponse(BaseModel):
    name: str = ""
    image_links: list[str] = Field(default_factory=list)
    dimensions: str = ""
    product_details: dict[str, Any] = Field(default_factory=dict)


class XHSCreateRequest(ScrapeRequest):
    pass


class XHSCreateResponse(BaseModel):
    job_id: str
    qrcode_image_link: str
    share_link: str
    title: str = ""
    content: str = ""
    result_path: str = ""
