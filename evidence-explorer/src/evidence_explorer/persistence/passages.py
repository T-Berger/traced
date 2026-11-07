"""Focused passages persistence with validated internal records."""

import re
from typing import Any

from ..models import ChunkInput
from ..records import ChunkRecord, KeywordMatch
from .connection import ConnectionFactory, invalidate_claim_cache


class PassageRepository:
    def __init__(self, connection: ConnectionFactory):
        self._connection = connection

    def by_ids(self, chunk_ids: set[int]) -> dict[int, ChunkRecord]:
        if len(chunk_ids) > 500:
            raise ValueError("Load at most 500 selected passages per request.")
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" for _ in chunk_ids)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT c.*, a.title, a.title AS article_title, a.publisher, a.url, "
                "a.published_at, a.language, a.topic, a.is_sample, a.content_hash "
                "FROM chunks c JOIN articles a ON a.id=c.article_id "
                f"WHERE c.id IN ({placeholders})",
                tuple(sorted(chunk_ids)),
            )
            return {row["id"]: ChunkRecord.model_validate(dict(row)) for row in rows}

    def replace_chunks(self, article_id: int, chunks: list[ChunkInput]) -> list[ChunkRecord]:
        if len({chunk.ordinal for chunk in chunks}) != len(chunks):
            raise ValueError("Chunk ordinals must be unique within an article.")
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            article = connection.execute(
                "SELECT text FROM articles WHERE id = ?", (article_id,)
            ).fetchone()
            if article is None:
                raise ValueError(f"Article {article_id} does not exist.")
            source = article["text"]
            if any(
                item.ordinal < 0
                or not 0 <= item.start_char < item.end_char <= len(source)
                or source[item.start_char : item.end_char] != item.text
                for item in chunks
            ):
                raise ValueError(
                    "Passages do not match the current source text. "
                    "The article may have changed; submit analysis again."
                )
            existing = connection.execute(
                "SELECT ordinal, text, start_char, end_char FROM chunks "
                "WHERE article_id = ? ORDER BY ordinal",
                (article_id,),
            ).fetchall()
            ordered = sorted(chunks, key=lambda item: item.ordinal)
            if [dict(row) for row in existing] != [item.model_dump() for item in ordered]:
                connection.execute("DELETE FROM chunks WHERE article_id = ?", (article_id,))
                invalidate_claim_cache(connection, article_id)
                connection.executemany(
                    "INSERT INTO chunks(article_id, ordinal, text, start_char, end_char) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        (article_id, item.ordinal, item.text, item.start_char, item.end_char)
                        for item in ordered
                    ],
                )
        return self.list_chunks(article_id)

    def list_chunks(self, article_id: int | None = None) -> list[ChunkRecord]:
        query = (
            "SELECT c.*, a.title, a.title AS article_title, a.publisher, a.url, "
            "a.published_at, a.language, a.topic, a.is_sample, a.content_hash "
            "FROM chunks c JOIN articles a ON a.id = c.article_id"
        )
        parameters: tuple[Any, ...] = ()
        if article_id is not None:
            query += " WHERE c.article_id = ?"
            parameters = (article_id,)
        query += " ORDER BY c.article_id, c.ordinal"
        with self._connection() as connection:
            return [
                ChunkRecord.model_validate(dict(row))
                for row in connection.execute(query, parameters)
            ]

    def keyword_search(self, query: str, limit: int = 10) -> list[KeywordMatch]:
        tokens = re.findall(r"\w+", query, re.UNICODE)[:30]
        if not tokens or limit <= 0:
            return []
        expression = " AND ".join(f'"{token}"' for token in tokens)
        with self._connection() as connection:
            return [
                KeywordMatch.model_validate(dict(row))
                for row in connection.execute(
                    "SELECT c.*, a.title, a.title AS article_title, a.publisher, a.url, "
                    "a.topic, a.published_at, a.is_sample, a.language, a.content_hash, "
                    "-bm25(chunks_fts) AS score FROM chunks_fts "
                    "JOIN chunks c ON c.id = chunks_fts.rowid "
                    "JOIN articles a ON a.id = c.article_id "
                    "WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts), c.id LIMIT ?",
                    (expression, min(int(limit), 500)),
                )
            ]
