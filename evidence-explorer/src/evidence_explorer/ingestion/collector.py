"""Compose public fetching and pure extraction, returning successes and failures."""

from collections.abc import Callable

import httpx

from .feed_entries import collect_feed_entry, iter_feed_entries
from .fetching import PublicFetcher
from .models import CollectedArticle, CollectionError, ContentMode, EntryError, FeedCollection
from .parsing import parse_article, parse_feed
from .url_policy import Resolver


class NewsCollector:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver | None = None,
        timeout: float = 20.0,
        max_bytes: int = 3_000_000,
        max_redirects: int = 5,
    ):
        self.fetcher = PublicFetcher(
            transport=transport,
            resolver=resolver,
            timeout=timeout,
            max_bytes=max_bytes,
            max_redirects=max_redirects,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        self.fetcher.close()

    def collect_article(
        self,
        url: str,
        topic: str = "General",
        fallback_metadata: dict | None = None,
    ) -> CollectedArticle:
        return parse_article(self.fetcher.fetch(url), topic=topic, fallback=fallback_metadata)

    def collect_feed(
        self,
        url: str,
        limit: int = 5,
        topic: str = "General",
        progress: Callable[[str, float], None] | None = None,
        content_mode: ContentMode = "article_links",
    ) -> FeedCollection:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise CollectionError(
                "Feed limit must be between 1 and 20.", code="invalid_limit", url=url
            )
        if content_mode not in {"article_links", "feed_content"}:
            raise CollectionError(
                "Feed content mode must be article_links or feed_content.",
                code="invalid_content_mode",
                url=url,
            )
        resource = self.fetcher.fetch(url)
        feed = parse_feed(resource)
        result = FeedCollection(
            requested_url=resource.requested_url,
            final_url=resource.final_url,
            retrieved_at=resource.retrieved_at,
            entries_seen=len(feed.entries),
        )
        imported: set[str] = set()
        for entry in iter_feed_entries(feed.entries, final_url=resource.final_url, limit=limit):
            if entry.error is not None:
                result.errors.append(entry.error)
                continue
            if entry.duplicate:
                result.duplicates_skipped += 1
                continue
            try:
                if progress:
                    progress(
                        f"Collecting article {entry.attempt}/{limit}",
                        (entry.attempt - 1) / limit,
                    )
                collected = collect_feed_entry(
                    self, entry, resource, feed.feed, topic=topic, content_mode=content_mode
                )
                if collected.article.url in imported:
                    result.duplicates_skipped += 1
                else:
                    imported.add(collected.article.url)
                    result.articles.append(collected)
            except CollectionError as exc:
                result.errors.append(
                    EntryError(
                        url=exc.url or entry.raw_link,
                        title=entry.title,
                        code=exc.code,
                        message=str(exc),
                    )
                )
        if progress:
            progress("Feed collection complete", 1.0)
        return result
