"""Durable claim checks with immutable evidence snapshots and guarded completion."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from ..persistence.connection import ConnectionPolicy, sqlite_connection
from ..persistence.migrations import Migration, execute_script, migrate
from ..review.contracts import EvidenceBundle
from ..review.persistence.bundles import BundleRepository
from .contracts import ClaimCheckJob, ClaimCheckPayload, ClaimCheckResult, EvidenceSnapshot


class StaleClaimCheck(ValueError):
    """The source case or claimed worker attempt no longer matches this check."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _initial(connection: sqlite3.Connection) -> None:
    execute_script(
        connection,
        """
        CREATE TABLE claim_check_jobs (
            id TEXT PRIMARY KEY,
            source_case_id TEXT NOT NULL REFERENCES review_cases(id),
            source_finding_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK
                (status IN ('queued', 'running', 'completed', 'failed', 'stale')),
            stage TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            snapshot_json TEXT,
            result_json TEXT,
            error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            lease_expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE INDEX claim_check_jobs_queue ON claim_check_jobs(status, created_at);
        CREATE INDEX claim_check_jobs_source ON claim_check_jobs(source_case_id, created_at);
        """,
    )


MIGRATIONS = (Migration(1, "durable_external_claim_checks", _initial),)


def _job(row: sqlite3.Row) -> ClaimCheckJob:
    value = dict(row)
    for name in ("payload", "snapshot", "result"):
        raw = value.pop(f"{name}_json")
        value[name] = json.loads(raw) if raw else None
    return ClaimCheckJob.model_validate(value)


class ClaimCheckStorage:
    """One queue with shared connection plumbing and claim-check-owned migrations.

    Writes retain immediate transactions; provider requests stay outside them.
    """

    LEASE_SECONDS = 120

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).expanduser().resolve()

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with sqlite_connection(
            self.db_path, write=write, policy=ConnectionPolicy(immediate_writes=True)
        ) as connection:
            yield connection

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            migrate(connection, "claim_check", MIGRATIONS)

    @staticmethod
    def _validate_source(connection: sqlite3.Connection, payload: ClaimCheckPayload) -> None:
        source = connection.execute(
            "SELECT fingerprint FROM review_cases WHERE id=?", (payload.source_case_id,)
        ).fetchone()
        if source is None or source["fingerprint"] != payload.source_case_fingerprint:
            raise StaleClaimCheck("Source investigation changed; submit a fresh claim check.")
        finding = connection.execute(
            "SELECT finding_json FROM review_findings WHERE case_id=? AND finding_id=?",
            (payload.source_case_id, payload.source_finding_id),
        ).fetchone()
        if finding is None or json.loads(
            finding["finding_json"]
        ) != payload.finding_snapshot.model_dump(mode="json"):
            raise StaleClaimCheck("Source finding changed; submit a fresh claim check.")

    @staticmethod
    def _validate_attempt(connection: sqlite3.Connection, job: ClaimCheckJob) -> None:
        current = connection.execute(
            "SELECT status, attempts, payload_json FROM claim_check_jobs WHERE id=?", (job.id,)
        ).fetchone()
        if (
            current is None
            or current["status"] != "running"
            or current["attempts"] != job.attempts
            or ClaimCheckPayload.model_validate_json(current["payload_json"]).model_dump(
                mode="json"
            )
            != job.payload.model_dump(mode="json")
        ):
            raise StaleClaimCheck("Claim-check attempt is no longer active; result was rejected.")

    def enqueue(
        self, payload: ClaimCheckPayload, *, snapshot: EvidenceSnapshot | None = None
    ) -> ClaimCheckJob:
        serialized = payload.model_dump_json()
        with self.connection(write=True) as connection:
            self._validate_source(connection, payload)
            existing = connection.execute(
                "SELECT * FROM claim_check_jobs WHERE payload_json=? "
                "AND status IN ('queued', 'running') ORDER BY rowid LIMIT 1",
                (serialized,),
            ).fetchone()
            if existing:
                return _job(existing)
            identifier, now = f"claim-check:{uuid4()}", _now()
            connection.execute(
                "INSERT INTO claim_check_jobs "
                "(id,source_case_id,source_finding_id,status,stage,payload_json,snapshot_json,"
                "created_at,updated_at) VALUES (?,?,?,'queued','queued',?,?,?,?)",
                (
                    identifier,
                    payload.source_case_id,
                    payload.source_finding_id,
                    serialized,
                    snapshot.model_dump_json() if snapshot is not None else None,
                    now,
                    now,
                ),
            )
            return _job(
                connection.execute(
                    "SELECT * FROM claim_check_jobs WHERE id=?", (identifier,)
                ).fetchone()
            )

    def get(self, job_id: str) -> ClaimCheckJob | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM claim_check_jobs WHERE id=?", (job_id,)
            ).fetchone()
            return _job(row) if row else None

    def list(
        self, case_id: str, *, finding_id: str | None = None, limit: int = 20
    ) -> list[ClaimCheckJob]:
        if not 1 <= limit <= 50:
            raise ValueError("Claim-check history limit must be between 1 and 50.")
        finding_filter = " AND source_finding_id=?" if finding_id is not None else ""
        parameters = (case_id, finding_id, limit) if finding_id is not None else (case_id, limit)
        with self.connection() as connection:
            return [
                _job(row)
                for row in connection.execute(
                    f"SELECT * FROM claim_check_jobs WHERE source_case_id=?{finding_filter} "
                    "ORDER BY rowid DESC LIMIT ?",
                    parameters,
                )
            ]

    @staticmethod
    def _recover(connection: sqlite3.Connection, now: str) -> int:
        cursor = connection.execute(
            "UPDATE claim_check_jobs SET status='failed', stage='interrupted', error=?, "
            "lease_expires_at=NULL, updated_at=?, finished_at=? "
            "WHERE status='running' AND (lease_expires_at IS NULL OR lease_expires_at < ?)",
            (
                "Worker interrupted. Retry explicitly; a saved evidence snapshot will be reused.",
                now,
                now,
                now,
            ),
        )
        return cursor.rowcount

    def recover(self) -> int:
        with self.connection(write=True) as connection:
            return self._recover(connection, _now())

    def claim(self) -> ClaimCheckJob | None:
        with self.connection(write=True) as connection:
            now = _now()
            self._recover(connection, now)
            row = connection.execute(
                "SELECT * FROM claim_check_jobs WHERE status='queued' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            expires = (datetime.now(UTC) + timedelta(seconds=self.LEASE_SECONDS)).isoformat()
            connection.execute(
                "UPDATE claim_check_jobs SET status='running',stage='checking_input', "
                "attempts=attempts+1,lease_expires_at=?,updated_at=? WHERE id=?",
                (expires, now, row["id"]),
            )
            return _job(
                connection.execute(
                    "SELECT * FROM claim_check_jobs WHERE id=?", (row["id"],)
                ).fetchone()
            )

    def heartbeat(self, job: ClaimCheckJob) -> bool:
        with self.connection(write=True) as connection:
            expires = (datetime.now(UTC) + timedelta(seconds=self.LEASE_SECONDS)).isoformat()
            return bool(
                connection.execute(
                    "UPDATE claim_check_jobs SET lease_expires_at=?,updated_at=? "
                    "WHERE id=? AND status='running' AND attempts=?",
                    (expires, _now(), job.id, job.attempts),
                ).rowcount
            )

    def validate_context(self, job: ClaimCheckJob) -> None:
        with self.connection() as connection:
            self._validate_attempt(connection, job)
            self._validate_source(connection, job.payload)

    def stage(self, job: ClaimCheckJob, stage: str) -> None:
        with self.connection(write=True) as connection:
            self._validate_attempt(connection, job)
            connection.execute(
                "UPDATE claim_check_jobs SET stage=?,updated_at=? WHERE id=?",
                (stage, _now(), job.id),
            )

    def freeze(self, job: ClaimCheckJob, snapshot: EvidenceSnapshot) -> None:
        snapshot = EvidenceSnapshot.model_validate_json(snapshot.model_dump_json())
        with self.connection(write=True) as connection:
            self._validate_attempt(connection, job)
            self._validate_source(connection, job.payload)
            existing = connection.execute(
                "SELECT snapshot_json FROM claim_check_jobs WHERE id=?", (job.id,)
            ).fetchone()[0]
            if existing is not None:
                previous = EvidenceSnapshot.model_validate_json(existing)
                if previous.fingerprint() != snapshot.fingerprint():
                    raise ValueError("Frozen evidence cannot change; submit a new check.")
                return
            connection.execute(
                "UPDATE claim_check_jobs SET snapshot_json=?,stage='evidence_frozen',updated_at=? "
                "WHERE id=?",
                (snapshot.model_dump_json(), _now(), job.id),
            )

    def complete(
        self,
        job: ClaimCheckJob,
        result: ClaimCheckResult,
        bundle: EvidenceBundle,
    ) -> None:
        with self.connection(write=True) as connection:
            self._validate_attempt(connection, job)
            self._validate_source(connection, job.payload)
            raw = connection.execute(
                "SELECT snapshot_json FROM claim_check_jobs WHERE id=?", (job.id,)
            ).fetchone()[0]
            if raw is None:
                raise ValueError("Assessment requires a saved frozen evidence snapshot.")
            if (
                EvidenceSnapshot.model_validate_json(raw).fingerprint()
                != result.evidence_fingerprint
            ):
                raise ValueError("Assessment does not match its frozen evidence snapshot.")
            if bundle.case.id != result.result_case_id or bundle.case.id == job.source_case_id:
                raise ValueError("Claim check must create its own result investigation.")

            @contextmanager
            def bound_connection(*, write: bool = False) -> Iterator[sqlite3.Connection]:
                del write
                yield connection

            BundleRepository(bound_connection).import_bundle(bundle)
            now = _now()
            connection.execute(
                "UPDATE claim_check_jobs SET status='completed',stage='awaiting_review',"
                "result_json=?,error=NULL,lease_expires_at=NULL,updated_at=?,finished_at=? "
                "WHERE id=?",
                (result.model_dump_json(), now, now, job.id),
            )

    def fail(self, job: ClaimCheckJob, error: str, *, stale: bool = False) -> None:
        with self.connection(write=True) as connection:
            self._validate_attempt(connection, job)
            now = _now()
            connection.execute(
                "UPDATE claim_check_jobs SET status=?,error=?,lease_expires_at=NULL,"
                "updated_at=?,finished_at=? WHERE id=?",
                ("stale" if stale else "failed", error[:1000], now, now, job.id),
            )
