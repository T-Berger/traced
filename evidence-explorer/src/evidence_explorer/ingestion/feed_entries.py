"""Shared bounded feed traversal and content collection for source adapters.

The iterator owns link normalization, duplicate tracking and attempt budgets.
Callers retain their error, final-record deduplication and empty-result policies.
Content collection preserves the distinction between fetching linked articles
and inspecting text already supplied by a feed.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin

from .feed_content import parse_feed_content
from .fetching import FetchedResource, PublicFetcher
from .models import CollectedArticle, CollectionError, ContentMode, EntryError
from .parsing import clean_metadata, publication_date
from .url_policy import normalize_url, public_addresses


class ArticleCollector(Protocol):
    fetcher: PublicFetcher

    def collect_article(
        self, url: str, topic: str = "General", fallback_metadata: dict | None = None
    ) -> CollectedArticle: ...


@dataclass(frozen=True)
class FeedEntry:
    """One traversal event; duplicates consume no additional attempt budget."""

    data: dict[str, Any]
    raw_link: str
    title: str | None
    attempt: int
    link: str | None = None
    error: EntryError | None = None
    duplicate: bool = False


def iter_feed_entries(
    entries: Iterable[dict[str, Any]], *, final_url: str, limit: int
) -> Iterator[FeedEntry]:
    """Yield valid entries, failures and duplicates in their original order.

    Missing or invalid links consume an attempt. Repeated normalized links do
    not, including links whose earlier content fetch failed. No network I/O is
    performed here; each caller validates its supported limit before traversal.
    """
    seen: set[str] = set()
    attempted = 0
    for entry in entries:
        if attempted >= limit:
            break
        title = clean_metadata(entry.get("title"), 300) or None
        raw_link = entry.get("link") or ""
        if not isinstance(raw_link, str) or not raw_link.strip():
            attempted += 1
            yield FeedEntry(
                entry,
                "",
                title,
                attempted,
                error=EntryError(
                    url="",
                    title=title,
                    code="missing_link",
                    message="Feed entry has no article link.",
                ),
            )
            continue
        try:
            link = normalize_url(urljoin(final_url, raw_link))
        except CollectionError as exc:
            attempted += 1
            yield FeedEntry(
                entry,
                raw_link,
                title,
                attempted,
                error=EntryError(
                    url=exc.url or raw_link, title=title, code=exc.code, message=str(exc)
                ),
            )
            continue
        if link in seen:
            yield FeedEntry(entry, raw_link, title, attempted, link=link, duplicate=True)
            continue
        seen.add(link)
        attempted += 1
        yield FeedEntry(entry, raw_link, title, attempted, link=link)


def collect_feed_entry(
    collector: ArticleCollector,
    entry: FeedEntry,
    resource: FetchedResource,
    feed_metadata: dict[str, Any],
    *,
    topic: str,
    content_mode: ContentMode,
    empty_title: str | None = None,
) -> CollectedArticle:
    """Collect one valid entry, leaving exception policy to the calling domain.

    Feed-content mode validates the permalink without fetching it. The title
    fallback remains caller-selectable for the two existing article adapters.
    """
    if entry.link is None or entry.error is not None or entry.duplicate:
        raise ValueError("Content collection requires a valid, nonduplicate feed entry.")
    publisher = clean_metadata(feed_metadata.get("title"), 150)
    language = clean_metadata(feed_metadata.get("language"), 30)
    if content_mode == "feed_content":
        public_addresses(entry.link, collector.fetcher.resolver)
        return parse_feed_content(
            entry.data,
            permalink=entry.link,
            resource=resource,
            topic=topic,
            publisher=publisher,
            language=language,
            feed_author=feed_metadata.get("author") or "",
        )
    return collector.collect_article(
        entry.link,
        topic=topic,
        fallback_metadata={
            "title": entry.title if entry.title is not None else empty_title,
            "publisher": publisher,
            "language": language,
            "published_at": publication_date(
                entry.data.get("published"), entry.data.get("published_parsed")
            ),
            "author": entry.data.get("author"),
            "feed_url": resource.final_url,
        },
    )
