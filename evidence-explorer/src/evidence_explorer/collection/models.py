"""Validated collector settings and results shared by platform adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..source_contracts import Platform, SourceItem


class CollectionFailure(ValueError):
    """A user-safe collection problem, optionally including a retry delay."""

    def __init__(self, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class CollectionSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=100)
    platform: Platform
    label: str = Field(min_length=1, max_length=150)
    locator: str = Field(min_length=1, max_length=2000)
    enabled: bool = True
    interval_minutes: int = Field(default=15, ge=5, le=10080)
    item_limit: int = Field(default=5, ge=1, le=20)
    content_mode: Literal["article_links", "feed_content"] = "feed_content"
    cursor: str | None = Field(default=None, max_length=256)

    @field_validator("id", "label", "locator")
    @classmethod
    def visible_text(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("Enter a source name and locator without control characters.")
        return value.strip()


@dataclass(frozen=True)
class CollectedBatch:
    items: tuple[SourceItem, ...]
    warnings: tuple[str, ...] = ()
    cursor: str | None = None
