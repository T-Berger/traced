"""Durable SQLite queue, independent of article storage and model execution."""

import json
import math
import sqlite3
from datetime import UTC, datetime
from typing import Any

from ..records import JobRecord
from .connection import ConnectionFactory


def _now() -> str:
    return datetime.now(UTC).isoformat()


def quarantine_job(
    connection: sqlite3.Connection, job_id: int, payload: str, result: str | None
) -> None:
    """Keep the original serialized input/result while making an invalid job inspectable."""
    timestamp = _now()
    preserved = {
        "invalid_persisted_payload_json": payload,
        "invalid_persisted_result_json": result,
    }
    connection.execute(
        "UPDATE jobs SET status='failed', stage='invalid saved data', progress=0, payload=?, "
        "result=NULL, error=?, updated_at=?, finished_at=? WHERE id=?",
        (
            json.dumps(preserved),
            "Stored job data is invalid. Submit the operation again.",
            timestamp,
            timestamp,
            job_id,
        ),
    )


def normalize_invalid_jobs(connection: sqlite3.Connection) -> None:
    """Adopt malformed legacy JSON values without dropping or silently running them."""
    rows = connection.execute(
        "SELECT id, payload, result FROM jobs WHERE "
        "CASE WHEN json_valid(payload) THEN json_type(payload)<>'object' ELSE 1 END "
        "OR CASE WHEN result IS NULL THEN 0 WHEN json_valid(result) "
        "THEN json_type(result)<>'object' ELSE 1 END"
    )
    for row in rows:
        quarantine_job(connection, row[0], row[1], row[2])


class JobQueue:
    """Persist and claim jobs without knowing about articles, Dash, or inference."""

    def __init__(self, connection: ConnectionFactory) -> None:
        self._connection = connection

    def enqueue_job(self, kind: str, payload: dict[str, Any], dedupe_key: str | None = None) -> int:
        if kind not in {"analyze", "search", "ingest"}:
            raise ValueError(f"Unsupported job kind: {kind}")
        if not isinstance(payload, dict):
            raise ValueError("Job payload must be a JSON object.")
        encoded = json.dumps(payload, allow_nan=False)
        timestamp = _now()
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if dedupe_key is not None:
                row = connection.execute(
                    "SELECT id FROM jobs WHERE dedupe_key = ? AND status IN ('queued', 'running')",
                    (dedupe_key,),
                ).fetchone()
                if row:
                    return row["id"]
            cursor = connection.execute(
                "INSERT INTO jobs(kind, payload, dedupe_key, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, encoded, dedupe_key, timestamp, timestamp),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _job_record(row: sqlite3.Row) -> JobRecord:
        values = dict(row)
        values["payload"] = json.loads(values["payload"])
        values["result"] = json.loads(values["result"]) if values["result"] else None
        return JobRecord.model_validate(values)

    def claim_record(self) -> JobRecord | None:
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            while True:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
                ).fetchone()
                if not row:
                    return None
                try:
                    self._job_record(row)
                except (ValueError, TypeError):
                    quarantine_job(connection, row["id"], row["payload"], row["result"])
                    continue
                break
            timestamp = _now()
            connection.execute(
                "UPDATE jobs SET status='running', stage='starting', progress=0, "
                "attempts=attempts+1, started_at=?, updated_at=?, error=NULL WHERE id=?",
                (timestamp, timestamp, row["id"]),
            )
            claimed = connection.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            return self._job_record(claimed)

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
        values: dict[str, Any] = {"updated_at": _now()}
        if status is not None:
            if status not in {"queued", "running", "succeeded", "failed"}:
                raise ValueError(f"Unsupported job status: {status}")
            values["status"] = status
            if status in {"succeeded", "failed"}:
                values["finished_at"] = values["updated_at"]
        if stage is not None:
            values["stage"] = stage
        if progress is not None:
            if not math.isfinite(progress) or not 0 <= progress <= 1:
                raise ValueError("Job progress must be between 0 and 1.")
            values["progress"] = progress
        if result is not None:
            if not isinstance(result, dict):
                raise ValueError("Job result must be a JSON object.")
            values["result"] = json.dumps(result, allow_nan=False)
        if error is not None:
            values["error"] = error
        assignments = ", ".join(f"{key} = :{key}" for key in values)
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id = :id", {**values, "id": job_id}
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown job: {job_id}")

    def get_record(self, job_id: int) -> JobRecord | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._job_record(row) if row else None

    def list_records(self, limit: int = 20) -> list[JobRecord]:
        with self._connection() as connection:
            return [
                self._job_record(row)
                for row in connection.execute(
                    "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (max(0, int(limit)),)
                )
            ]

    def recover_interrupted_jobs(self) -> int:
        """Expose interrupted jobs as failed; retry requires an explicit new request."""
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status='failed', stage='interrupted', "
                "error='Worker stopped before completion. Submit the operation again to retry.', "
                "updated_at=?, finished_at=? WHERE status='running'",
                (_now(), _now()),
            )
            return cursor.rowcount

    # Explicit JSON adapters for established CLI and Dash callers.
    def claim_job(self) -> dict[str, Any] | None:
        record = self.claim_record()
        return record.model_dump(mode="json") if record else None

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        record = self.get_record(job_id)
        return record.model_dump(mode="json") if record else None

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        return [record.model_dump(mode="json") for record in self.list_records(limit)]
