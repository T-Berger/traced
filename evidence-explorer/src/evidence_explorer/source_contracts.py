"""Validated saved-source records and stable identity helpers shared by collectors.

These contracts perform no storage, network, review workflow, or UI operations.
Unknown timestamps remain unknown and supplied public locators remain unchanged.
Bundle construction belongs to source_bundles; browsing belongs to the review UI.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

Platform = Literal["telegram", "x", "reddit", "youtube", "news", "other", "maltego"]


PLATFORM_LABELS = {
    "telegram": "Telegram",
    "x": "X",
    "reddit": "Reddit",
    "youtube": "YouTube",
    "news": "News",
    "other": "Other",
    # Frozen Maltego Data MCP lookup results; never a scheduled channel.
    "maltego": "Maltego",
}


MAX_SOURCE_ITEMS = 2000


_HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        raise ValueError("Timestamp must be ISO 8601 with an explicit timezone.") from None


def safe_source_url(value: str) -> str:
    """Validate a supplied HTTP(S) locator without resolving or fetching it.

    Keep the supplied URL intact: changing queries or fragments would silently
    change evidence and can invalidate signed source links.
    """
    message = "URL must be an absolute HTTP(S) URL with a valid public-looking hostname."
    try:
        if (
            not value
            or len(value) > 2000
            or "\\" in value
            or any(ord(char) < 33 or ord(char) == 127 for char in value)
        ):
            raise ValueError
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or not host
            or len(host) > 253
            or "%" in host
        ):
            raise ValueError
        # Accessing port validates its syntax and range as well.
        if parsed.port == 0:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if (
                not _HOST.fullmatch(host)
                or host.endswith(
                    (".localhost", ".local", ".localdomain", ".internal", ".lan", ".home")
                )
                or host.endswith(".home.arpa")
                or host.rsplit(".", 1)[-1].isdigit()
            ):
                raise ValueError from None
        else:
            if not address.is_global or address.is_multicast or address.is_reserved:
                raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise ValueError(message) from None


class SourceContract(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class SourceIdentity(SourceContract):
    platform: Platform
    channel_id: str = Field(min_length=1, max_length=512)
    native_id: str = Field(min_length=1, max_length=512)

    @field_validator("channel_id", "native_id")
    @classmethod
    def meaningful_identifier(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("Identifier must contain visible text without control characters.")
        return value


class SourceAcquisition(SourceContract):
    method: Literal[
        "saved_import",
        "telegram_public",
        "youtube_feed",
        "rss_article",
        "rss_content",
        "x_api",
        "reddit_api",
        "maltego_datamcp",
    ]
    content_scope: Literal[
        "post_text",
        "title_description",
        "article_text",
        "feed_content",
        "feed_summary",
        "lookup_result",
    ]
    retrieved_url: str | None = None
    feed_url: str | None = None
    published_at_raw: str | None = Field(default=None, max_length=100)
    author_label: str | None = Field(default=None, max_length=300)

    @field_validator("retrieved_url", "feed_url")
    @classmethod
    def safe_locator(cls, value: str | None) -> str | None:
        return safe_source_url(value) if value is not None else None


class SourceItem(SourceIdentity):
    channel_name: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1, max_length=50_000)
    source_url: str | None = Field(default=None, max_length=2000)
    author_id: str | None = Field(default=None, min_length=1, max_length=512)
    title: str | None = Field(default=None, max_length=1000)
    published_at: str | None = Field(default=None, max_length=80)
    acquired_at: str = Field(max_length=80)
    shared_urls: list[str] = Field(default_factory=list, max_length=100)
    repost_of: SourceIdentity | None = None
    provenance: SourceAcquisition | None = None

    @field_validator("channel_name", "text", "author_id")
    @classmethod
    def meaningful_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Text must contain at least one visible character.")
        return value

    @field_validator("published_at", "acquired_at")
    @classmethod
    def aware_timestamp(cls, value: str | None) -> str | None:
        return _timestamp(value) if value is not None else None

    @field_validator("source_url")
    @classmethod
    def valid_source_url(cls, value: str | None) -> str | None:
        return safe_source_url(value) if value is not None else None

    @field_validator("shared_urls")
    @classmethod
    def valid_shared_urls(cls, values: list[str]) -> list[str]:
        return sorted({safe_source_url(value) for value in values})


class SourceCase(SourceContract):
    title: str = Field(min_length=1, max_length=500)
    question: str = Field(min_length=1, max_length=5000)
    synthetic: bool = False

    @field_validator("title", "question")
    @classmethod
    def meaningful_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Text must contain at least one visible character.")
        return value


class SourceImport(SourceContract):
    source_schema: Literal["1.0"]
    case: SourceCase
    items: list[SourceItem] = Field(min_length=1, max_length=MAX_SOURCE_ITEMS)


def _identity(item: SourceIdentity) -> tuple[str, str, str]:
    return item.platform, item.channel_id, item.native_id


def _saved_item(item: SourceItem) -> dict:
    saved = item.model_dump(mode="json")
    # Preserve the identity of source imports created before live collection existed.
    if item.provenance is None:
        saved.pop("provenance")
    return saved


def _channel_key(platform: str, channel_id: str) -> str:
    return "source:channel:" + _digest([platform, channel_id])
