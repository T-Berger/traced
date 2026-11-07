"""Typed dashboard values; display filters never alter saved evidence or decisions."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SentimentLabel = Literal["positive", "negative", "neutral", "mixed", "unclear", "unknown"]


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DashboardFilters(Frozen):
    text_query: str = Field(default="", max_length=500)
    platform: str | None = None
    channel_key: str | None = None
    author_key: str | None = None
    time_basis: Literal["publication", "acquisition"] = "publication"
    bucket: Literal["day", "week", "month"] = "day"
    start: str | None = None
    end: str | None = None
    sentiment_run_id: str | None = None

    @field_validator("start", "end")
    @classmethod
    def iso_date(cls, value: str | None) -> str | None:
        if value is not None and date.fromisoformat(value).isoformat() != value:
            raise ValueError("Dates must use YYYY-MM-DD.")
        return value

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.start and self.end and self.start > self.end:
            raise ValueError("Start date must be on or before end date.")
        return self


class DashboardRow(Frozen):
    id: str
    kind: str
    title: str
    source_url: str | None
    preview: str = Field(max_length=240)
    published_at: str | None
    acquired_at: str | None
    time_status: Literal["known", "date_only", "missing", "invalid", "naive", "unsupported"]
    platform: str
    channel_key: str | None
    channel_label: str
    author_key: str | None
    author_label: str
    synthetic: bool | None
    sentiment: SentimentLabel = "unknown"
    sentiment_target: str | None = None
    sentiment_finding_id: str | None = None
    sentiment_fragment_ids: tuple[str, ...] = ()
    sentiment_model: str | None = None
    sentiment_prompt_version: str | None = None
    sentiment_run_id: str | None = None


class DashboardSummary(Frozen):
    key: str | None
    label: str
    count: int = Field(ge=0)
    record_ids: tuple[str, ...]


class SentimentCounts(Frozen):
    positive: int = Field(default=0, ge=0)
    negative: int = Field(default=0, ge=0)
    neutral: int = Field(default=0, ge=0)
    mixed: int = Field(default=0, ge=0)
    unclear: int = Field(default=0, ge=0)
    unknown: int = Field(default=0, ge=0)


class DashboardTimeBucket(Frozen):
    start: str
    end: str
    label: str
    count: int = Field(ge=0)
    record_ids: tuple[str, ...]
    sentiment_counts: SentimentCounts


class DashboardSentimentTarget(Frozen):
    target: str
    count: int = Field(ge=0)
    record_ids: tuple[str, ...]
    sentiment_counts: SentimentCounts


class DashboardEdge(Frozen):
    id: str
    source_record_id: str
    target_record_id: str
    relation: Literal["documented_repost", "same_content", "shared_url"]
    basis: str
    link_ids: tuple[str, ...] = ()


class DashboardOption(Frozen):
    value: str
    label: str
    count: int = Field(ge=0)


class DashboardOptions(Frozen):
    platforms: tuple[DashboardOption, ...]
    channels: tuple[DashboardOption, ...]
    authors: tuple[DashboardOption, ...]
    sentiment_runs: tuple[DashboardOption, ...]


class DashboardCoverage(Frozen):
    total_records: int = Field(ge=0)
    excluded_records: int = Field(ge=0)
    eligible_records: int = Field(ge=0)
    matching_records: int = Field(ge=0)
    unknown_timestamp_records: int = Field(ge=0)
    date_only_timestamp_records: int = Field(ge=0)
    missing_timestamp_records: int = Field(ge=0)
    invalid_timestamp_records: int = Field(ge=0)
    naive_timestamp_records: int = Field(ge=0)
    unsupported_timestamp_records: int = Field(ge=0)
    outside_date_records: int = Field(ge=0)
    unknown_channel_records: int = Field(ge=0)
    unknown_author_records: int = Field(ge=0)
    unknown_sentiment_records: int = Field(ge=0)
    invalid_annotation_records: int = Field(ge=0)
    relationship_total: int = Field(ge=0)
    relationship_shown: int = Field(ge=0)
    sentiment_run_available: bool


class DashboardProjection(Frozen):
    case_id: str
    case_fingerprint: str
    filters: DashboardFilters
    count_unit: Literal["saved_record_snapshots"] = "saved_record_snapshots"
    rows: tuple[DashboardRow, ...]
    time_buckets: tuple[DashboardTimeBucket, ...]
    platforms: tuple[DashboardSummary, ...]
    channels: tuple[DashboardSummary, ...]
    authors: tuple[DashboardSummary, ...]
    sentiment_counts: SentimentCounts
    sentiment_targets: tuple[DashboardSentimentTarget, ...]
    sentiment_comparable: bool
    edges: tuple[DashboardEdge, ...]
    options: DashboardOptions
    coverage: DashboardCoverage
    limitations: tuple[str, ...]
