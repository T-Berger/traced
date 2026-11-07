"""Immutable source writes and metadata reads; full reads are explicit."""

from __future__ import annotations

from ..models import Dataset, DatasetCounts, DatasetInfo
from ..serialization import canonical_json, fingerprint, utc_now
from .database import Database
from .projections import project_dataset


class DatasetRepository:
    def __init__(self, database: Database):
        self.database = database

    def active_id(self) -> str | None:
        return self.database.state("active_dataset")

    def info(self, dataset_id: str | None = None) -> DatasetInfo | None:
        dataset_id = dataset_id or self.active_id()
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT payload FROM dataset_info WHERE dataset_id=?", (dataset_id,)
            ).fetchone()
        return DatasetInfo.model_validate_json(row[0]) if row else None

    def counts(self, dataset_id: str) -> DatasetCounts:
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT channel_count,post_count,message_count,account_count "
                "FROM dataset_info WHERE dataset_id=?",
                (dataset_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Dataset was not found.")
        return DatasetCounts.model_validate(dict(row))

    def write(self, dataset: Dataset, wire_record: dict | None = None) -> str:
        """Keep provenance payloads and normalized rows in the same transaction."""
        payload = wire_record if wire_record is not None else dataset.to_record()
        digest = fingerprint(payload)
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT fingerprint FROM datasets WHERE dataset_id=?", (dataset.dataset_id,)
            ).fetchone()
            if existing:
                if existing[0] != digest:
                    raise ValueError(
                        "Dataset ID already exists with different content. Use a new dataset_id; "
                        "existing runs are immutable."
                    )
            else:
                conn.execute(
                    "INSERT INTO datasets VALUES (?,?,?,?)",
                    (dataset.dataset_id, digest, canonical_json(payload), utc_now()),
                )
                conn.executemany(
                    "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
                    (
                        (
                            dataset.dataset_id,
                            message.message_key,
                            message.post_key,
                            message.account_key,
                            message.channel_key,
                            message.timestamp_utc,
                            message.text_original,
                            canonical_json(message.to_record()),
                        )
                        for message in dataset.messages
                    ),
                )
                conn.executemany(
                    "INSERT INTO messages_fts VALUES (?,?,?)",
                    (
                        (dataset.dataset_id, message.message_key, message.text_original)
                        for message in dataset.messages
                    ),
                )
                project_dataset(conn, dataset)
            conn.execute(
                "INSERT INTO state VALUES ('active_dataset',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (dataset.dataset_id,),
            )
            # An imported source cannot inherit readiness or findings from another dataset.
            active = conn.execute(
                "SELECT r.dataset_id FROM state s JOIN runs r ON r.run_id=s.value WHERE s.key='active_run'"
            ).fetchone()
            if active is not None and active[0] != dataset.dataset_id:
                conn.execute("DELETE FROM state WHERE key='active_run'")
        return dataset.dataset_id

    def materialize(self, dataset_id: str | None = None) -> Dataset | None:
        """Decode a full corpus only for explicit analysis/snapshot operations."""
        dataset_id = dataset_id or self.active_id()
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT payload FROM datasets WHERE dataset_id=?", (dataset_id,)
            ).fetchone()
        return Dataset.model_validate_json(row[0]) if row else None
