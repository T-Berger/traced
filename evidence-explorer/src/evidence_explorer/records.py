"""Validated internal records; JSON dictionaries are produced only at wire boundaries."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from .ingestion_contracts import IngestionRequest as IngestionRequest
from .ingestion_contracts import IngestionResult as IngestionResult
from .models import AnalysisMode, ClaimInput


class StoredRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class PageRequest(StoredRecord):
    limit: int = Field(default=200, strict=True, ge=1, le=500)
    offset: int = Field(default=0, strict=True, ge=0)


class SourcePassage(BaseModel):
    """Minimal grounding input, also accepted from established model-client callers."""

    model_config = ConfigDict(frozen=True, extra="ignore")
    id: int
    text: str


class ArticleSummary(StoredRecord):
    id: int
    title: str
    publisher: str
    url: str
    published_at: str
    language: str
    topic: str
    is_sample: bool
    content_hash: str
    created_at: str
    updated_at: str
    requested_url: str | None = None
    final_url: str | None = None
    feed_url: str | None = None
    retrieved_at: str | None = None
    author: str | None = None
    publication_source: Literal["article", "feed", "platform", "unknown"] | None = None
    content_source: Literal["article", "feed_content", "feed_summary", "platform_post"] | None = (
        None
    )
    source_platform: str | None = None
    source_channel_id: str | None = None
    source_native_id: str | None = None
    source_author_id: str | None = None
    acquisition_method: str | None = None
    content_scope: str | None = None


class ArticleRecord(ArticleSummary):
    text: str
    source_metadata_json: str | None = None


class ArticleScope(StoredRecord):
    upper_id: int = Field(ge=0)
    count: int = Field(ge=0)


class ChunkRecord(StoredRecord):
    id: int
    article_id: int
    ordinal: int
    text: str
    start_char: int
    end_char: int
    title: str
    article_title: str
    publisher: str
    url: str
    published_at: str
    language: str
    topic: str
    is_sample: bool
    content_hash: str


class KeywordMatch(ChunkRecord):
    score: float


class SearchMatch(ChunkRecord):
    score: float
    semantic_score: float | None = None
    keyword_score: float | None = None
    model_key: str


class EmbeddingRecord(StoredRecord):
    chunk_id: int
    vector: tuple[float, ...] = Field(min_length=1)


class ClaimRecord(ClaimInput):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    article_id: int
    model_key: str
    article_title: str
    publisher: str
    url: str
    is_sample: bool
    published_at: str
    topic: str


class VerifiedClaimRecord(ClaimRecord):
    source_verified: Literal[True] = True


class JobRecord(StoredRecord):
    id: int
    # A persisted envelope can outlive its dispatcher; unsupported kinds fail visibly.
    kind: str
    payload: dict[str, JsonValue]
    dedupe_key: str | None
    status: Literal["queued", "running", "succeeded", "failed"]
    stage: str
    progress: float = Field(ge=0, le=1)
    result: dict[str, JsonValue] | None
    error: str | None
    attempts: int = Field(ge=0)
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None


ArticleId = Annotated[int, Field(strict=True, gt=0)]


class AnalysisRequest(StoredRecord):
    mode: AnalysisMode = "demo"
    article_ids: list[ArticleId] | None = None


class SearchRequest(AnalysisRequest):
    query: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=10, strict=True, ge=1, le=30)

    @field_validator("query", mode="before")
    @classmethod
    def strip_query(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class AnalysisResult(StoredRecord):
    articles: int
    embeddings_created: int = 0
    claims_created: int = 0
    claims_rejected: int = Field(default=0, ge=0)
    cached: int = 0
    mode: AnalysisMode
    model_key: str


class SearchResult(StoredRecord):
    matches: list[SearchMatch]
    mode: AnalysisMode
    query: str
    model_key: str


class CollectionTotals(StoredRecord):
    articles: int
    sources: int
    languages: int
    claims: int


class CollectionView(StoredRecord):
    articles: list[ArticleSummary]
    claims: list[VerifiedClaimRecord]
    totals: CollectionTotals
    article_limit: int
    claim_limit: int
