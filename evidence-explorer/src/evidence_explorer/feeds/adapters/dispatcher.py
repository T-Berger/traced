"""Bridge saved-feed requests to existing RSS and platform collection adapters."""

import hashlib
from collections.abc import Callable

from pydantic import ValidationError

from ...collection.models import CollectionSource
from ...ingestion import NewsCollector
from ...ingestion.models import FeedCollection
from .base import SourceAdapterError
from .contracts import Collector, PlatformAdapter
from .conversion import batch_to_feed
from .locators import normalize_source
from .reddit import RedditAdapter
from .telegram import TelegramAdapter
from .x import XAdapter
from .youtube import YouTubeAdapter

_ADAPTERS: dict[str, type[PlatformAdapter]] = {
    "telegram": TelegramAdapter,
    "youtube": YouTubeAdapter,
    "reddit": RedditAdapter,
    "x": XAdapter,
}


def collect_platform_feed(
    platform: str,
    locator: str,
    *,
    name: str,
    topic: str = "General",
    limit: int = 5,
    source_id: str | int | None = None,
    interval_minutes: int = 60,
    cursor: str | None = None,
    content_mode: str = "article_links",
    progress: Callable[[str, float], None] | None = None,
    collector: Collector | None = None,
    news_collector_factory: Callable[[], NewsCollector] | None = None,
) -> FeedCollection:
    normalized = normalize_source(platform, locator)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise SourceAdapterError(
            "The source item limit must be between 1 and 20.", code="invalid_source"
        )
    if normalized.platform == "rss":
        factory = news_collector_factory or NewsCollector
        with factory() as news:
            return news.collect_feed(
                normalized.url,
                limit=limit,
                topic=topic,
                content_mode=content_mode,
                progress=progress,
            )
    identity = (
        "saved-"
        + hashlib.sha256(f"{normalized.platform}:{normalized.locator}".encode()).hexdigest()[:24]
    )
    if source_id is not None:
        identity = f"saved-feed-{source_id}" if type(source_id) is int else source_id
    try:
        source = CollectionSource(
            id=identity,
            platform=normalized.platform,
            label=name,
            locator=normalized.locator,
            interval_minutes=interval_minutes,
            item_limit=limit,
            content_mode="feed_content",
            cursor=cursor,
        )
    except ValidationError:
        raise SourceAdapterError(
            "Check the source name, interval, and item limit.", code="invalid_source"
        ) from None
    if progress:
        progress("Collecting platform text", 0.05)
    adapter = _ADAPTERS[normalized.platform](collector=collector)
    batch = adapter.collect(source)
    result = batch_to_feed(batch, normalized, name=name, topic=topic, limit=limit)
    if progress:
        progress("Platform collection complete", 1.0)
    return result
