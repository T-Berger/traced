"""Saved-feed operations for application facades; never perform network access."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .contracts import FeedSubscriptionInput, FeedSubscriptionRecord


def source_options() -> list[dict]:
    from .adapters import source_readiness

    descriptions = (
        ("rss", "RSS / Atom", "News articles or full social posts from a public RSS/Atom URL."),
        (
            "telegram",
            "Telegram",
            "Recent text from a public channel preview; no private channels or full archive.",
        ),
        (
            "youtube",
            "YouTube",
            "Use a UC channel ID or channel URL. Titles and descriptions only; no transcripts.",
        ),
        (
            "reddit",
            "Reddit",
            "Public community posts through the approved Reddit API; "
            "server credentials and access approval required.",
        ),
        (
            "x",
            "X",
            "Account posts through the official API; "
            "server bearer token and endpoint access required.",
        ),
    )
    return [
        {
            "platform": platform,
            "label": label,
            "help": help_text,
            "ready": (state := source_readiness(platform)).ready,
            "reason": state.message,
        }
        for platform, label, help_text in descriptions
    ]


def require_ready(platform: str) -> None:
    from .adapters import source_readiness

    state = source_readiness(platform)
    if not state.ready:
        raise ValueError(state.message)


if TYPE_CHECKING:
    from ..db import Repository


class FeedService:
    def __init__(self, repo: Repository):
        self.repo = repo

    def list_feeds(self, limit: int = 30, offset: int = 0) -> list[FeedSubscriptionRecord]:
        return self.repo.feeds.list_page(limit=limit, offset=offset)

    def save_feed(self, request: FeedSubscriptionInput) -> FeedSubscriptionRecord:
        require_ready(request.platform)
        return self.repo.feeds.save(request)

    def enqueue_feed_refresh(self, feed_id: int) -> int:
        feed = self.repo.feeds.get(feed_id)
        if feed is None:
            raise ValueError(f"Saved feed {feed_id} does not exist.")
        require_ready(feed.platform)
        return self.repo.feeds.queue_one(feed_id)

    def set_feed_enabled(self, feed_id: int, enabled: bool) -> FeedSubscriptionRecord:
        if enabled:
            feed = self.repo.feeds.get(feed_id)
            if feed is None:
                raise ValueError(f"Saved feed {feed_id} does not exist.")
            require_ready(feed.platform)
        return self.repo.feeds.set_enabled(feed_id, enabled)
