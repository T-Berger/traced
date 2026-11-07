"""Application operations shared by CLI and Dash; no inference in web requests."""

import hashlib
import json
import time
from copy import deepcopy
from datetime import UTC, datetime

from .config import Settings
from .db import Repository
from .models_client import OllamaClient
from .records import AnalysisRequest, CollectionView, IngestionRequest, SearchRequest


class ExplorerService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.repo = Repository(self.settings.db_path, read_only=self.settings.read_only)
        self.repo.initialize()
        self._status_at = 0.0
        self._ollama_status: dict = {}

    def _writable(self):
        if self.settings.read_only:
            raise PermissionError("This presentation snapshot cannot run analysis or queue jobs.")

    def search_keyword(self, query: str, limit: int = 10) -> list[dict]:
        if len(query) > 300:
            raise ValueError("Search queries can contain at most 300 characters.")
        return self.repo.keyword_search(query, limit=limit)

    def collection(self) -> CollectionView:
        return self.repo.collection()

    def enqueue_analysis(self, mode: str = "demo", article_ids: list[int] | None = None) -> int:
        self._writable()
        self._validate_mode(mode)
        payload = AnalysisRequest(mode=mode, article_ids=article_ids).model_dump(exclude_none=True)
        key = "analyze:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return self.repo.enqueue_job("analyze", payload, dedupe_key=key)

    def enqueue_search(self, query: str, mode: str = "demo", limit: int = 10) -> int:
        self._writable()
        self._validate_mode(mode)
        query = query.strip()
        if not query or len(query) > 300:
            raise ValueError("Enter a search query between 1 and 300 characters.")
        payload = SearchRequest(query=query, mode=mode, limit=min(max(limit, 1), 30)).model_dump(
            exclude_none=True
        )
        key = "search:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return self.repo.enqueue_job("search", payload, dedupe_key=key)

    def enqueue_ingestion(
        self,
        url: str,
        source_type: str = "article",
        limit: int = 5,
        topic: str = "General",
        analyze: bool = True,
        content_mode: str = "article_links",
    ) -> int:
        """Validate and queue collection; no source HTTP or DNS work happens here."""
        self._writable()
        from .ingestion import normalize_url

        request = IngestionRequest(
            url=url,
            source_type=source_type,
            limit=limit,
            topic=topic,
            analyze=analyze,
            content_mode=content_mode,
        )
        normalized = normalize_url(request.url)
        payload = request.model_dump(mode="json")
        identity = {**payload, "url": normalized}
        key = "ingest:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return self.repo.jobs.enqueue_job("ingest", payload, dedupe_key=key)

    def _feed_service(self):
        from .feeds.service import FeedService

        return FeedService(self.repo)

    def list_feeds(self, limit: int = 30, offset: int = 0) -> list[dict]:
        from .feeds.adapters import source_readiness

        return [
            {
                **feed.model_dump(mode="json"),
                "ready": (state := source_readiness(feed.platform)).ready,
                "readiness_reason": state.message,
            }
            for feed in self._feed_service().list_feeds(limit=limit, offset=offset)
        ]

    def feed_source_options(self) -> list[dict]:
        from .feeds.service import source_options

        return source_options()

    def enqueue_collected_source_analysis(
        self, source_id: str, *, topic: str = "General", limit: int = 20, analyze: bool = True
    ) -> int:
        """Reuse a collection-owned source snapshot without fetching or scheduling it again."""
        self._writable()
        from .collection.persistence import CollectionRepository
        from .feeds.adapters import normalize_source

        source = CollectionRepository(self.repo.db_path).source(source_id)
        if source.platform not in {"telegram", "youtube", "reddit", "x"}:
            raise ValueError("Choose a saved Telegram, YouTube, Reddit, or X source.")
        normalized = normalize_source(source.platform, source.locator)
        payload = IngestionRequest(
            url=normalized.url,
            source_type="feed",
            platform=normalized.platform,
            locator=normalized.locator,
            collection_source_id=source.id,
            topic=topic,
            limit=limit,
            analyze=analyze,
        ).model_dump(mode="json")
        key = (
            "stored-source:"
            + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        )
        return self.repo.jobs.enqueue_job("ingest", payload, dedupe_key=key)

    def feed_scheduler_status(self) -> dict:
        return {
            "last_tick": self.repo.get_meta("feed_scheduler_last_tick"),
            "error": self.repo.get_meta("feed_scheduler_error"),
        }

    def save_feed(
        self,
        name: str,
        url: str = "",
        topic: str = "General",
        interval_minutes: int = 60,
        item_limit: int = 5,
        content_mode: str = "article_links",
        analyze: bool = True,
        platform: str = "rss",
        locator: str | None = None,
    ) -> dict:
        self._writable()
        from .feeds.contracts import FeedSubscriptionInput

        request = FeedSubscriptionInput(
            name=name,
            url=url,
            topic=topic,
            interval_minutes=interval_minutes,
            item_limit=item_limit,
            content_mode=content_mode,
            analyze=analyze,
            platform=platform,
            locator=locator,
        )
        return self._feed_service().save_feed(request).model_dump(mode="json")

    def enqueue_feed_refresh(self, feed_id: int) -> int:
        self._writable()
        return self._feed_service().enqueue_feed_refresh(feed_id)

    def set_feed_enabled(self, feed_id: int, enabled: bool) -> dict:
        self._writable()
        return self._feed_service().set_feed_enabled(feed_id, enabled).model_dump(mode="json")

    @staticmethod
    def _validate_mode(mode):
        if mode not in {"demo", "ollama"}:
            raise ValueError("Mode must be demo or ollama.")

    def status(self) -> dict:
        heartbeat = self.repo.get_meta("worker_heartbeat")
        online = False
        if heartbeat:
            try:
                stamp = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
                online = (
                    self.repo.get_meta("worker_state") == "running"
                    and 0 <= (datetime.now(UTC) - stamp).total_seconds() < 20
                )
            except (ValueError, TypeError):
                pass
        if self.settings.read_only:
            ollama = {
                "available": False,
                "models": [],
                "missing": [],
                "error": "Presentation mode: local inference is disabled.",
            }
            online = False
        else:
            if time.monotonic() - self._status_at > 10 or not self._ollama_status:
                with OllamaClient(self.settings) as model:
                    self._ollama_status = model.status()
                self._status_at = time.monotonic()
            ollama = deepcopy(self._ollama_status)
        return {
            "ollama": ollama,
            "worker": {"online": online, "last_seen": heartbeat},
            "stats": self.repo.stats(),
        }
