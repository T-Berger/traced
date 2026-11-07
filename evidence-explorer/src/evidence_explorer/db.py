"""Compatibility facade over focused, typed Explorer repositories.

New application code uses ``articles``, ``passages``, ``embeddings`` and ``claims``.
Legacy methods serialize records explicitly for existing CLI, Dash and export APIs.
These repositories, the article queue, and saved feeds share a connection factory.
This facade initializes Explorer's schema component; it does not initialize or own
the separate review, collection, or planning repositories in the same workspace.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .feeds.persistence import FeedRepository
from .models import ArticleInput, ChunkInput, ClaimInput
from .persistence.articles import ArticleRepository
from .persistence.claims import ClaimRepository
from .persistence.connection import SQLiteDatabase, enable_wal
from .persistence.embeddings import EmbeddingRepository
from .persistence.jobs import JobQueue
from .persistence.migrations import check_schema, migrate
from .persistence.passages import PassageRepository
from .persistence.schema import MIGRATIONS
from .records import CollectionTotals, CollectionView


class Repository:
    """Compose article repositories at one path without starting any services.

    Construction creates repository facades only. ``initialize`` creates or checks
    Explorer tables; individual operations open short connections. Network and
    model execution belong to callers, not to this persistence facade.
    """

    def __init__(self, db_path: Path, read_only: bool = False):
        self.database = SQLiteDatabase(db_path, read_only=read_only)
        self.db_path = self.database.path
        self.read_only = read_only
        self.articles = ArticleRepository(self._connection)
        self.passages = PassageRepository(self._connection)
        self.embeddings = EmbeddingRepository(self._connection)
        self.claims = ClaimRepository(self._connection)
        self.jobs = JobQueue(self._connection)
        self.feeds = FeedRepository(self._connection)

    def _connection(self, *, write: bool = False):
        return self.database.connection(write=write)

    def initialize(self) -> None:
        if self.read_only:
            if not self.db_path.is_file():
                raise FileNotFoundError(f"Snapshot database not found: {self.db_path}")
            with self._connection() as connection:
                check_schema(connection, "explorer", MIGRATIONS)
                connection.execute("SELECT id FROM articles LIMIT 1")
                connection.execute("SELECT id FROM jobs LIMIT 1")
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection(write=True) as connection:
            migrate(connection, "explorer", MIGRATIONS)
            enable_wal(connection)

    def collection(self, *, article_limit: int = 200, claim_limit: int = 500) -> CollectionView:
        """Bounded display metadata plus exact SQL totals; source bodies stay on disk."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT count(*) AS articles, count(DISTINCT publisher) AS sources, "
                "count(DISTINCT language) AS languages FROM articles"
            ).fetchone()
        totals = CollectionTotals.model_validate(
            {**dict(row), "claims": self.claims.count_active()}
        )
        return CollectionView(
            articles=self.articles.page(limit=article_limit),
            claims=self.claims.verified_active_page(limit=claim_limit),
            totals=totals,
            article_limit=article_limit,
            claim_limit=claim_limit,
        )

    def upsert_article(self, article: ArticleInput) -> int:
        return self.articles.upsert_article(article)

    def has_samples(self) -> bool:
        return self.articles.has_samples()

    def list_articles(self) -> list[dict[str, Any]]:
        """Materialize the full corpus for legacy export; use articles.page for views."""
        return [record.model_dump(mode="json") for record in self.articles.list_articles()]

    def get_article(self, article_id: int) -> dict[str, Any] | None:
        record = self.articles.get_article(article_id)
        return record.model_dump(mode="json") if record else None

    def replace_chunks(self, article_id: int, chunks: list[ChunkInput]) -> list[dict[str, Any]]:
        return [
            record.model_dump(mode="json")
            for record in self.passages.replace_chunks(article_id, chunks)
        ]

    def list_chunks(self, article_id: int | None = None) -> list[dict[str, Any]]:
        return [record.model_dump(mode="json") for record in self.passages.list_chunks(article_id)]

    def get_embedding(self, chunk_id: int, model_key: str) -> list[float] | None:
        return self.embeddings.get_embedding(chunk_id, model_key)

    def save_embedding(self, chunk_id: int, model_key: str, vector: list[float]) -> None:
        self.embeddings.save_embedding(chunk_id, model_key, vector)

    def list_embeddings(self, model_key: str) -> list[dict[str, Any]]:
        return [
            record.model_dump(mode="json") for record in self.embeddings.list_embeddings(model_key)
        ]

    def replace_claims(
        self,
        article_id: int,
        claims: list[ClaimInput],
        model_key: str,
        *,
        expected_content_hash: str | None = None,
        cache_key: str | None = None,
    ) -> int:
        return self.claims.replace_claims(
            article_id,
            claims,
            model_key,
            expected_content_hash=expected_content_hash,
            cache_key=cache_key,
        )

    def list_claims(self, article_id: int | None = None) -> list[dict[str, Any]]:
        return [record.model_dump(mode="json") for record in self.claims.list_claims(article_id)]

    def list_active_claims(self, article_id: int | None = None) -> list[dict[str, Any]]:
        return [
            record.model_dump(mode="json") for record in self.claims.list_active_claims(article_id)
        ]

    def keyword_search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return [
            record.model_dump(mode="json") for record in self.passages.keyword_search(query, limit)
        ]

    def enqueue_job(self, kind: str, payload: dict[str, Any], dedupe_key: str | None = None) -> int:
        return self.jobs.enqueue_job(kind, payload, dedupe_key)

    def claim_job(self) -> dict[str, Any] | None:
        return self.jobs.claim_job()

    def update_job(
        self,
        job_id: int,
        *,
        status: str | None = None,
        stage: str | None = None,
        progress: float | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.jobs.update_job(
            job_id, status=status, stage=stage, progress=progress, result=result, error=error
        )

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        return self.jobs.get_job(job_id)

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.jobs.list_jobs(limit)

    def recover_interrupted_jobs(self) -> int:
        return self.jobs.recover_interrupted_jobs()

    def stats(self) -> dict[str, int]:
        with self._connection() as connection:
            counts = {
                table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("articles", "chunks", "claims", "embeddings", "jobs")
            }
            for status in ("queued", "running", "succeeded", "failed"):
                counts[f"jobs_{status}"] = connection.execute(
                    "SELECT count(*) FROM jobs WHERE status = ?", (status,)
                ).fetchone()[0]
            return counts

    def set_meta(self, key: str, value: Any) -> None:
        with self._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, allow_nan=False)),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._connection() as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
            return json.loads(row["value"]) if row else default

    def backup(self, destination: Path) -> Path:
        target = Path(destination).expanduser().resolve()
        if target == self.db_path:
            raise ValueError("The backup destination must differ from the live database.")
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as source, closing(sqlite3.connect(target)) as output, output:
            source.backup(output)
            output.execute("PRAGMA journal_mode = DELETE")
        return target
