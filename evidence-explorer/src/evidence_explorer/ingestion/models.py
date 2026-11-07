"""Validated collector results; persistence and model analysis live elsewhere."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..models import ArticleInput

ContentMode = Literal["article_links", "feed_content"]


class CollectionError(ValueError):
    """An actionable collection failure with a stable machine-readable category."""

    def __init__(self, message: str, *, code: str = "collection_error", url: str = ""):
        super().__init__(message)
        self.code = code
        self.url = url


class SourceProvenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    requested_url: str
    final_url: str
    feed_url: str | None = None
    retrieved_at: str
    author: str | None = None
    published_at: str | None = None
    publication_source: Literal["article", "feed", "platform", "unknown"] = "unknown"
    content_source: Literal["article", "feed_content", "feed_summary", "platform_post"] = "article"


class CollectedArticle(BaseModel):
    model_config = ConfigDict(frozen=True)

    article: ArticleInput
    provenance: SourceProvenance


class EntryError(BaseModel):
    url: str
    message: str
    code: str
    title: str | None = None


class FeedCollection(BaseModel):
    articles: list[CollectedArticle] = Field(default_factory=list)
    errors: list[EntryError] = Field(default_factory=list)
    requested_url: str
    final_url: str
    retrieved_at: str
    entries_seen: int = 0
    duplicates_skipped: int = 0
    warnings: list[str] = Field(default_factory=list)
    cursor: str | None = None
