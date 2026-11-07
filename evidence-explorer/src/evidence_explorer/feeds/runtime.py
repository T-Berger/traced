"""Small scheduler adapter shared by both worker entry points."""

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from ..db import Repository

logger = logging.getLogger(__name__)


class FeedScheduler:
    def __init__(
        self,
        repo: Repository,
        *,
        interval_seconds: float = 15,
        clock: Callable[[], float] | None = None,
    ):
        if interval_seconds <= 0:
            raise ValueError("The feed scheduler interval must be positive.")
        self.repo = repo
        self.interval_seconds = interval_seconds
        self._clock = clock or time.monotonic
        self._next_tick = 0.0

    def tick(self, *, force: bool = False, now: datetime | None = None, limit: int = 20):
        """Queue due feeds without HTTP; a scheduler error does not stop other jobs."""
        if self.repo.read_only:
            return None
        current = self._clock()
        if not force and current < self._next_tick:
            return None
        self._next_tick = current + self.interval_seconds
        from .scheduler import tick_feeds

        try:
            result = tick_feeds(self.repo, now=now, limit=limit)
            self.repo.set_meta("feed_scheduler_last_tick", datetime.now(UTC).isoformat())
            if self.repo.get_meta("feed_scheduler_error") is not None:
                self.repo.set_meta("feed_scheduler_error", None)
            return result
        except Exception as exc:
            logger.exception("Saved-feed scheduler failed; other queued work will continue")
            try:
                self.repo.set_meta("feed_scheduler_error", str(exc)[:1000])
            except Exception:
                logger.exception("Could not record the feed scheduler error")
            return None
