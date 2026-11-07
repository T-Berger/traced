"""A cheap worker tick: reconcile durable outcomes and queue due feeds once."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from .contracts import FeedTickResult

if TYPE_CHECKING:
    from ..db import Repository


def tick_feeds(
    repo: Repository,
    *,
    now: datetime | None = None,
    limit: int = 20,
) -> FeedTickResult:
    if repo.read_only:
        return FeedTickResult()
    return repo.feeds.schedule_due(now=now, limit=limit)
