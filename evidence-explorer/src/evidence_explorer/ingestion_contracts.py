"""Typed ingestion requests and job results, independent of HTTP and persistence."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator


class IngestionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = Field(min_length=1, max_length=2000)
    source_type: Literal["article", "feed"] = "article"
    limit: Annotated[int, Field(strict=True, ge=1, le=20)] = 5
    topic: str = Field(default="General", min_length=1, max_length=150)
    analyze: StrictBool = True
    content_mode: Literal["article_links", "feed_content"] = "article_links"
    feed_id: Annotated[int, Field(strict=True, gt=0)] | None = None
    scheduled: StrictBool = False
    platform: Literal["rss", "telegram", "reddit", "youtube", "x"] = "rss"
    locator: str | None = Field(default=None, max_length=2000)
    cursor: str | None = Field(default=None, max_length=256)
    collection_source_id: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("url", "topic", mode="before")
    @classmethod
    def strip_values(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_feed_options(self):
        if self.source_type != "feed" and (
            self.feed_id is not None or self.content_mode != "article_links"
        ):
            raise ValueError("Saved feed options require a feed import.")
        if self.scheduled and self.feed_id is None:
            raise ValueError("A scheduled import requires a saved feed ID.")
        if self.platform != "rss" and self.source_type != "feed":
            raise ValueError("Platform sources require a feed import.")
        if self.collection_source_id is not None and (
            self.source_type != "feed" or self.feed_id is not None or self.scheduled
        ):
            raise ValueError("Saved collection projection cannot create a second feed schedule.")
        return self


class ImportFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    error: str
    code: str | None = None


class IngestionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: Literal["article", "feed"]
    url: str
    imported: int = 0
    updated: int = 0
    unchanged: int = 0
    article_ids: list[int] = Field(default_factory=list)
    new_ids: list[int] = Field(default_factory=list)
    updated_ids: list[int] = Field(default_factory=list)
    unchanged_ids: list[int] = Field(default_factory=list)
    errors: list[ImportFailure] = Field(default_factory=list)
    analysis_job_id: int | None = None
    duplicates_skipped: int = 0
    entries_seen: int = 0
    skipped: bool = False
    skip_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    retry_after_seconds: int | None = Field(default=None, ge=0, le=86400)
