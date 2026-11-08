"""Version-pinned review jobs, atomic claims, leases, and guarded completion."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from ..contracts import EvidenceBundle
from ..models import CategoryRecord, ReviewJob, ReviewJobPayload, ReviewResult
from .common import (
    ConnectionFactory,
    job_from_row,
    json_text,
    required_text,
    review_context,
    utc_now,
)


class ReviewJobRepository:
    def __init__(self, connect: ConnectionFactory, lease_seconds: Callable[[], float]) -> None:
        self._connect = connect
        self._lease_seconds = lease_seconds

    def enqueue(
        self,
        case_id: str,
        finding_id: str,
        scenario_id: str,
        mode: str,
        *,
        model: str | None = None,
        model_identity: str | None = None,
        prompt_version: str | None = None,
        retrieval_version: str | None = None,
        reason: str | None = None,
        record_id: str | None = None,
        record_content_hash: str | None = None,
        record_manifest: list[dict[str, str]] | None = None,
        target: str | None = None,
        expected_fingerprint: str | None = None,
        expected_scenario_version: int | None = None,
    ) -> ReviewJob:
        mode = required_text(mode, "Mode")
        with self._connect(write=True) as connection:
            context = review_context(connection, case_id, finding_id, scenario_id)
            if mode == "category_proposal" and not expected_fingerprint:
                raise ValueError("The selected case fingerprint is required.")
            if mode == "category_proposal" and (
                type(expected_scenario_version) is not int
                or expected_scenario_version != context.scenario.version
            ):
                raise ValueError("The selected assessment changed. Reopen it before analysis.")
            if (
                expected_fingerprint is not None
                and expected_fingerprint != context.case.fingerprint
            ):
                raise ValueError(
                    "The saved case changed. Reopen the evidence before running analysis."
                )
            if mode == "category_proposal":
                row = connection.execute(
                    "SELECT bundle_json FROM review_cases WHERE id = ?", (case_id,)
                ).fetchone()
                bundle = EvidenceBundle.model_validate(json.loads(row["bundle_json"]))
                manifest = [CategoryRecord.model_validate(item) for item in record_manifest or []]
                ids = [item.id for item in manifest]
                if not 1 <= len(ids) <= 20 or len(ids) != len(set(ids)):
                    raise ValueError("Select 1 to 20 distinct saved records.")
                records = {r.id: r for r in bundle.records}
                for item in manifest:
                    record = records.get(item.id)
                    if (
                        record is None
                        or not record.text
                        or len(record.text) > 12000
                        or record.content_hash != item.content_hash
                    ):
                        raise ValueError("A selected saved text or its version is unavailable.")
                    if item.id in context.scenario.excluded_record_ids:
                        raise ValueError("A selected record is excluded from this assessment.")
            configuration = {
                key: value
                for key, value in {
                    "model": model,
                    "model_identity": model_identity,
                    "prompt_version": prompt_version,
                    "retrieval_version": retrieval_version,
                    "reason": reason,
                    "record_id": record_id,
                    "record_content_hash": record_content_hash,
                    "record_manifest": record_manifest,
                    "target": target,
                }.items()
                if value is not None
            }
            payload = ReviewJobPayload.model_validate(
                {
                    "case_id": case_id,
                    "case_fingerprint": context.case.fingerprint,
                    "finding_id": finding_id,
                    "finding_snapshot": context.finding,
                    "scenario_id": scenario_id,
                    "scenario_version": context.scenario.version,
                    "excluded_record_ids": context.scenario.excluded_record_ids,
                    "base_run_id": context.scenario.base_run_id,
                    "mode": mode,
                    **configuration,
                }
            )
            serialized = json_text(payload.model_dump(mode="json", exclude_unset=True))
            previous = connection.execute(
                "SELECT * FROM review_jobs WHERE payload_json = ? "
                "AND status IN ('queued', 'running') ORDER BY rowid LIMIT 1",
                (serialized,),
            ).fetchone()
            if previous is not None:
                return job_from_row(previous)
            job_id = str(uuid4())
            now = utc_now()
            connection.execute(
                "INSERT INTO review_jobs "
                "(id, case_id, finding_id, scenario_id, mode, status, payload_json, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
                (job_id, case_id, finding_id, scenario_id, mode, serialized, now, now),
            )
            row = connection.execute("SELECT * FROM review_jobs WHERE id = ?", (job_id,)).fetchone()
            return job_from_row(row)

    def get(self, job_id: str) -> ReviewJob | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM review_jobs WHERE id = ?", (job_id,)).fetchone()
            return job_from_row(row) if row is not None else None

    def scenario_computation(self, scenario_id: str, version: int) -> ReviewJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_jobs WHERE scenario_id = ? AND mode = 'swarm_recompute' "
                "AND json_extract(payload_json, '$.scenario_version') = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (scenario_id, version),
            ).fetchone()
            return job_from_row(row) if row is not None else None

    def completed_swarm_results(self, case_id: str) -> dict[tuple[str, str, str], bool | None]:
        """Distinguish proven local execution from a saved result with local job history."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT json_extract(result_json, '$.computed_scenario_id') AS native_id,
                          json_extract(payload_json, '$.base_run_id') AS baseline_id,
                          json_extract(result_json, '$.result_run_id') AS result_id,
                          MAX(json_type(result_json, '$.cached') = 'false') AS locally_computed
                   FROM review_jobs
                   WHERE case_id = ? AND mode = 'swarm_recompute' AND status = 'completed'
                     AND json_extract(result_json, '$.kind') = 'swarm_recompute'
                     AND json_extract(result_json, '$.scenario_id') = scenario_id
                     AND json_extract(result_json, '$.scenario_version')
                         = json_extract(payload_json, '$.scenario_version')
                     AND COALESCE(json_extract(result_json, '$.stale'), 0) = 0
                   GROUP BY native_id, baseline_id, result_id""",
                (case_id,),
            )
            return {
                (row["native_id"], row["baseline_id"], row["result_id"]): (
                    True if row["locally_computed"] else None
                )
                for row in rows
                if all(
                    isinstance(row[key], str) and row[key]
                    for key in ("native_id", "baseline_id", "result_id")
                )
            }

    @staticmethod
    def _recover(connection: sqlite3.Connection, now: str) -> int:
        cursor = connection.execute(
            "UPDATE review_jobs SET status = 'queued', lease_expires_at = NULL, "
            "updated_at = ?, error = 'Previous worker lease expired; retrying.' "
            "WHERE status = 'running' AND lease_expires_at <= ?",
            (now, now),
        )
        return cursor.rowcount

    def recover(self) -> int:
        with self._connect(write=True) as connection:
            return self._recover(connection, utc_now())

    def claim(self) -> ReviewJob | None:
        with self._connect(write=True) as connection:
            now = utc_now()
            self._recover(connection, now)
            row = connection.execute(
                "SELECT * FROM review_jobs WHERE status = 'queued' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            lease_expires_at = (
                datetime.now(UTC) + timedelta(seconds=self._lease_seconds())
            ).isoformat()
            connection.execute(
                "UPDATE review_jobs SET status = 'running', attempts = attempts + 1, "
                "lease_expires_at = ?, started_at = ?, updated_at = ?, "
                "finished_at = NULL, error = NULL WHERE id = ?",
                (lease_expires_at, now, now, row["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM review_jobs WHERE id = ?", (row["id"],)
            ).fetchone()
            return job_from_row(updated)

    def heartbeat(self, job_id: str, expected_attempt: int) -> bool:
        with self._connect(write=True) as connection:
            now = datetime.now(UTC)
            lease_expires_at = (now + timedelta(seconds=self._lease_seconds())).isoformat()
            cursor = connection.execute(
                "UPDATE review_jobs SET lease_expires_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'running' AND attempts = ?",
                (lease_expires_at, now.isoformat(), job_id, expected_attempt),
            )
            return cursor.rowcount == 1

    def finish(
        self, job_id: str, result: dict[str, Any], *, expected_attempt: int | None = None
    ) -> None:
        if not isinstance(result, dict):
            raise ValueError("Job result must be a JSON object.")
        ReviewResult.model_validate(result)
        serialized = json_text(result)
        with self._connect(write=True) as connection:
            now = utc_now()
            query = (
                "UPDATE review_jobs SET status = 'completed', result_json = ?, error = NULL, "
                "lease_expires_at = NULL, finished_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'running'"
            )
            parameters: list[str | int] = [serialized, now, now, job_id]
            if expected_attempt is not None:
                query += " AND attempts = ?"
                parameters.append(expected_attempt)
            cursor = connection.execute(query, parameters)
            if not cursor.rowcount:
                raise ValueError("Only a running job with the current attempt can be completed.")

    def fail(self, job_id: str, error: str, *, expected_attempt: int | None = None) -> None:
        error = required_text(error, "Error")
        with self._connect(write=True) as connection:
            now = utc_now()
            query = (
                "UPDATE review_jobs SET status = 'failed', error = ?, lease_expires_at = NULL, "
                "finished_at = ?, updated_at = ? WHERE id = ? AND status = 'running'"
            )
            parameters: list[str | int] = [error, now, now, job_id]
            if expected_attempt is not None:
                query += " AND attempts = ?"
                parameters.append(expected_attempt)
            cursor = connection.execute(query, parameters)
            if not cursor.rowcount:
                raise ValueError("Only a running job with the current attempt can be failed.")
