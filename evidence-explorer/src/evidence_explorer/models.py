"""Validated data exchanged between collection, storage, models, and the UI."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

AnalysisMode = Literal["demo", "ollama"]


class ArticleInput(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    publisher: str = Field(min_length=1, max_length=150)
    url: str = Field(min_length=1, max_length=2000)
    published_at: str
    text: str = Field(min_length=1, max_length=50_000)
    language: str = "en"
    topic: str = "General"
    is_sample: bool = False
    requested_url: str | None = Field(default=None, max_length=2000)
    final_url: str | None = Field(default=None, max_length=2000)
    feed_url: str | None = Field(default=None, max_length=2000)
    retrieved_at: str | None = None
    author: str | None = Field(default=None, max_length=300)
    publication_source: Literal["article", "feed", "platform", "unknown"] | None = None
    content_source: Literal["article", "feed_content", "feed_summary", "platform_post"] | None = (
        None
    )
    source_platform: str | None = Field(default=None, max_length=30)
    source_channel_id: str | None = Field(default=None, max_length=512)
    source_native_id: str | None = Field(default=None, max_length=512)
    source_author_id: str | None = Field(default=None, max_length=512)
    acquisition_method: str | None = Field(default=None, max_length=100)
    content_scope: str | None = Field(default=None, max_length=100)
    source_metadata_json: str | None = Field(default=None, max_length=300_000)

    @model_validator(mode="after")
    def source_text_length(self):
        if self.content_source != "platform_post" and len(self.text) < 30:
            raise ValueError("Article text must contain at least 30 characters.")
        return self

    @field_validator("title", "publisher", "text", mode="before")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class ChunkInput(BaseModel):
    ordinal: int
    text: str
    start_char: int
    end_char: int


class ClaimInput(BaseModel):
    claim_text: str = Field(min_length=5, max_length=800)
    quote: str = Field(min_length=10, max_length=1500)
    chunk_id: int
    subject: str = Field(default="", max_length=200)
    attribution: str = Field(default="", max_length=300)
    event_time: str | None = None
    location: str | None = None


class ClaimBatch(BaseModel):
    claims: list[ClaimInput] = Field(max_length=5)
