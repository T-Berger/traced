"""Focused articles persistence with validated internal records."""

import hashlib
from collections.abc import Iterator

from ..models import ArticleInput
from ..records import ArticleRecord, ArticleScope, ArticleSummary, PageRequest
from .connection import ConnectionFactory, invalidate_claim_cache, now


class ArticleRepository:
    def __init__(self, connection: ConnectionFactory):
        self._connection = connection

    def count(self) -> int:
        with self._connection() as connection:
            return connection.execute("SELECT count(*) FROM articles").fetchone()[0]

    def scope(self) -> ArticleScope:
        """Capture a finite job's high-water ID and total in the same SQL snapshot."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT coalesce(max(id), 0) AS upper_id, count(*) AS count FROM articles"
            ).fetchone()
            return ArticleScope.model_validate(dict(row))

    def has_samples(self) -> bool:
        with self._connection() as connection:
            return bool(
                connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM articles WHERE is_sample = 1)"
                ).fetchone()[0]
            )

    def page(self, *, limit: int = 200, offset: int = 0) -> list[ArticleSummary]:
        page = PageRequest(limit=limit, offset=offset)
        with self._connection() as connection:
            # Old read-only exports remain viewable without writing a migration to them.
            existing = {row[1] for row in connection.execute("PRAGMA table_info(articles)")}
            provenance = ", ".join(
                column if column in existing else f"NULL AS {column}"
                for column in (
                    "requested_url",
                    "final_url",
                    "feed_url",
                    "retrieved_at",
                    "author",
                    "publication_source",
                    "content_source",
                    "source_platform",
                    "source_channel_id",
                    "source_native_id",
                    "source_author_id",
                    "acquisition_method",
                    "content_scope",
                )
            )
            rows = connection.execute(
                "SELECT id, title, publisher, url, published_at, language, topic, is_sample, "
                f"content_hash, created_at, updated_at, {provenance} FROM articles "
                "ORDER BY published_at DESC, id DESC LIMIT ? OFFSET ?",
                (page.limit, page.offset),
            )
            return [ArticleSummary.model_validate(dict(row)) for row in rows]

    def iter_ids(self, *, batch_size: int = 256, upper_id: int | None = None) -> Iterator[int]:
        """Scan a finite ID range; release each read connection before yielding work."""
        if not 1 <= batch_size <= 500:
            raise ValueError("Article batch size must be from 1 to 500.")
        upper = self.scope().upper_id if upper_id is None else upper_id
        after = 0
        while after < upper:
            with self._connection() as connection:
                ids = [
                    row[0]
                    for row in connection.execute(
                        "SELECT id FROM articles WHERE id > ? AND id <= ? ORDER BY id LIMIT ?",
                        (after, upper, batch_size),
                    )
                ]
            if not ids:
                break
            after = ids[-1]
            yield from ids

    def upsert_article(self, article: ArticleInput) -> int:
        values = article.model_dump()
        values["is_sample"] = int(article.is_sample)
        values["content_hash"] = hashlib.sha256(article.text.encode()).hexdigest()
        values["updated_at"] = now()
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT id, content_hash FROM articles WHERE url = ?", (article.url,)
            ).fetchone()
            if previous:
                article_id = previous["id"]
                if previous["content_hash"] != values["content_hash"]:
                    connection.execute("DELETE FROM chunks WHERE article_id = ?", (article_id,))
                    invalidate_claim_cache(connection, article_id)
                assignments = ", ".join(f"{key} = :{key}" for key in values)
                connection.execute(
                    f"UPDATE articles SET {assignments} WHERE id = :id",
                    {**values, "id": article_id},
                )
                return article_id
            values["created_at"] = values["updated_at"]
            keys = ", ".join(values)
            placeholders = ", ".join(f":{key}" for key in values)
            cursor = connection.execute(
                f"INSERT INTO articles ({keys}) VALUES ({placeholders})", values
            )
            return int(cursor.lastrowid)

    def list_articles(self) -> list[ArticleRecord]:
        with self._connection() as connection:
            return [
                ArticleRecord.model_validate(dict(row))
                for row in connection.execute(
                    "SELECT * FROM articles ORDER BY published_at DESC, id DESC"
                )
            ]

    def get_article(self, article_id: int) -> ArticleRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM articles WHERE id = ?", (article_id,)
            ).fetchone()
            return ArticleRecord.model_validate(dict(row)) if row else None

    def get_by_url(self, url: str) -> ArticleRecord | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM articles WHERE url = ?", (url,)).fetchone()
            return ArticleRecord.model_validate(dict(row)) if row else None
