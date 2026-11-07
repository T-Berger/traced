"""Saved-feed settings and scheduler results, independent of HTTP collection."""

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from ..ingestion.url_policy import normalize_url


class FeedSubscriptionInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=150)
    url: str = Field(min_length=1, max_length=2000)
    platform: Literal["rss", "telegram", "reddit", "youtube", "x"] = "rss"
    locator: str | None = Field(default=None, max_length=2000)
    canonical_key: str = Field(default="", max_length=2200)
    topic: str = Field(default="General", min_length=1, max_length=150)
    interval_minutes: StrictInt = Field(default=60, ge=5, le=10080)
    item_limit: StrictInt = Field(default=5, ge=1, le=20)
    content_mode: Literal["article_links", "feed_content"] = "article_links"
    analyze: StrictBool = True

    @model_validator(mode="before")
    @classmethod
    def normalize_source(cls, value):
        if not isinstance(value, dict):
            return value
        from ..source_identity import canonical_source_key
        from .adapters import normalize_source

        platform = value.get("platform", "rss")
        locator = value.get("locator") or value.get("url") or ""
        source = normalize_source(platform, locator)
        return {
            **value,
            "url": source.url,
            "platform": source.platform,
            "locator": source.locator,
            "canonical_key": canonical_source_key(source.platform, source.locator),
            "content_mode": value.get("content_mode", "article_links")
            if source.platform == "rss"
            else "feed_content",
        }

    @field_validator("name", "topic", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("url")
    @classmethod
    def public_url(cls, value: str) -> str:
        return normalize_url(value)


class FeedSubscriptionRecord(FeedSubscriptionInput):
    id: int
    enabled: bool
    next_refresh_at: str
    last_queued_at: str | None = None
    last_finished_at: str | None = None
    last_refreshed_at: str | None = None
    last_job_id: int | None = None
    last_result: dict[str, JsonValue] | None = None
    last_analysis_job_id: int | None = None
    last_analysis_status: Literal["queued", "running", "succeeded", "failed"] | None = None
    last_analysis_error: str | None = None
    last_error: str | None = None
    consecutive_failures: int = Field(default=0, ge=0)
    cursor: str | None = None
    created_at: str
    updated_at: str


class FeedTickResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    reconciled: int = 0
    queued_job_ids: list[int] = Field(default_factory=list)
