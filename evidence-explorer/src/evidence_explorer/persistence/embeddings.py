"""Focused embeddings persistence with validated internal records."""

import json
import math
from collections.abc import Iterator

from ..records import EmbeddingRecord
from .connection import ConnectionFactory


class EmbeddingRepository:
    def __init__(self, connection: ConnectionFactory):
        self._connection = connection

    def iter_batches(
        self, model_key: str, *, batch_size: int = 256
    ) -> Iterator[list[EmbeddingRecord]]:
        """Stream vectors in bounded pages; never retain source text or a corpus matrix."""
        if not 1 <= batch_size <= 500:
            raise ValueError("Embedding batch size must be from 1 to 500.")
        with self._connection() as connection:
            upper = connection.execute(
                "SELECT coalesce(max(chunk_id), 0) FROM embeddings WHERE model_key=?",
                (model_key,),
            ).fetchone()[0]
        after = 0
        while after < upper:
            with self._connection() as connection:
                batch = [
                    EmbeddingRecord(chunk_id=row["chunk_id"], vector=json.loads(row["vector_json"]))
                    for row in connection.execute(
                        "SELECT e.chunk_id, e.vector_json FROM embeddings e "
                        "JOIN chunks c ON c.id=e.chunk_id "
                        "WHERE e.model_key=? AND e.chunk_id>? AND e.chunk_id<=? "
                        "ORDER BY e.chunk_id LIMIT ?",
                        (model_key, after, upper, batch_size),
                    )
                ]
            if not batch:
                break
            after = batch[-1].chunk_id
            yield batch

    def get_embedding(self, chunk_id: int, model_key: str) -> list[float] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT vector_json FROM embeddings WHERE chunk_id = ? AND model_key = ?",
                (chunk_id, model_key),
            ).fetchone()
            return json.loads(row["vector_json"]) if row else None

    def save_embedding(self, chunk_id: int, model_key: str, vector: list[float]) -> None:
        values = [float(value) for value in vector]
        if not values or not all(math.isfinite(value) for value in values):
            raise ValueError("Embedding vectors must contain finite numbers.")
        with self._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO embeddings(chunk_id, model_key, vector_json) VALUES (?, ?, ?) "
                "ON CONFLICT(chunk_id, model_key) DO UPDATE SET vector_json=excluded.vector_json",
                (chunk_id, model_key, json.dumps(values, allow_nan=False)),
            )

    def list_embeddings(self, model_key: str) -> list[EmbeddingRecord]:
        with self._connection() as connection:
            return [
                EmbeddingRecord(chunk_id=row["chunk_id"], vector=json.loads(row["vector_json"]))
                for row in connection.execute(
                    "SELECT chunk_id, vector_json FROM embeddings "
                    "WHERE model_key = ? ORDER BY chunk_id",
                    (model_key,),
                )
            ]
