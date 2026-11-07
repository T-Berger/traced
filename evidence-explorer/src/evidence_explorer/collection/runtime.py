"""Schedule and execute direct-collection jobs inside the shared worker lifecycle.

Provider calls run between repository operations, outside SQLite transactions.
This runtime owns collection queue processing but no thread, polling loop, or
worker lock; the integrated ``WorkspaceWorker`` supplies that lifecycle.
"""

from __future__ import annotations

from datetime import datetime

from .models import CollectionFailure, CollectionSource
from .ownership import find_feed_owner
from .persistence import CollectionRepository
from .registry import collect
from .service import CollectionService


class CollectionRuntime:
    """Initialize collection storage and process sources through the shared registry.

    Successful batches and failures are persisted through the supplied repository.
    This runtime does not import cases, analyze articles, or record human decisions.
    """

    def __init__(self, repository: CollectionRepository, *, collector=collect):
        self.repository = repository
        self.repository.initialize()
        self.collector = collector

    def recover(self) -> None:
        self.repository.recover()

    def run_once(
        self, *, now: datetime | None = None, schedule_before: datetime | None = None
    ) -> bool:
        """Schedule ready sources and handle at most one queued collection job.

        Check saved-feed ownership before fetching to avoid a duplicate request.
        Return True when a claimed job was handled, including a recorded failure;
        the return value is not a collection-success or coverage claim.
        """
        # Readiness checks keep missing credentials from producing repeated API requests.
        ready = [
            source["id"]
            for source in self.repository.sources()
            if source["enabled"]
            and CollectionService.readiness(source["platform"])["ready"]
            and not find_feed_owner(self.repository.db_path, source["platform"], source["locator"])
        ]
        self.repository.schedule(now=schedule_before or now, limit=20, source_ids=ready)
        job = self.repository.claim(now=now)
        if job is None:
            return False
        try:
            source = CollectionSource.model_validate_json(job["config"])
            if find_feed_owner(self.repository.db_path, source.platform, source.locator):
                raise CollectionFailure(
                    "This source is managed in Saved feeds; no duplicate request was made."
                )
            readiness = CollectionService.readiness(source.platform)
            if not readiness["ready"]:
                raise CollectionFailure(readiness["message"])
            batch = self.collector(source)
            self.repository.complete(job, batch, now=now)
        except CollectionFailure as exc:
            self.repository.fail(
                job, str(exc), retry_after_seconds=exc.retry_after_seconds, now=now
            )
        except Exception:
            # HTTP exceptions can include authorization headers or remote response bodies.
            self.repository.fail(
                job,
                "Collection failed. Check the source and retry; no evidence was replaced.",
                now=now,
            )
        return True
