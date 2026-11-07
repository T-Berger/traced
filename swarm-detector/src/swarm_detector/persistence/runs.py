"""Immutable analysis results and lightweight run identity."""

from __future__ import annotations

from ..models import AnalysisRun, RunInfo
from ..serialization import canonical_json, utc_now
from .database import Database
from .projections import project_run


class RunRepository:
    def __init__(self, database: Database):
        self.database = database

    def active_id(self) -> str | None:
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT r.run_id FROM state s JOIN run_info r ON r.run_id=s.value "
                "JOIN state d ON d.key='active_dataset' AND d.value=r.dataset_id "
                "WHERE s.key='active_run'"
            ).fetchone()
        return row[0] if row else None

    def info(self, run_id: str | None = None) -> RunInfo | None:
        run_id = run_id or self.active_id()
        with self.database.connection() as conn:
            row = conn.execute("SELECT payload FROM run_info WHERE run_id=?", (run_id,)).fetchone()
        return RunInfo.model_validate_json(row[0]) if row else None

    def candidate_count(self, run_id: str) -> int:
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT candidate_count FROM run_info WHERE run_id=?", (run_id,)
            ).fetchone()
        return row[0] if row else 0

    def write(self, run: AnalysisRun, wire_record: dict | None = None) -> None:
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT dataset_id FROM runs WHERE run_id=?", (run.run_id,)
            ).fetchone()
            if existing and existing[0] != run.dataset_id:
                raise ValueError("An existing run cannot be assigned to another dataset.")
            if existing is None:
                conn.execute(
                    "INSERT INTO runs VALUES (?,?,?,?)",
                    (
                        run.run_id,
                        run.dataset_id,
                        canonical_json(wire_record if wire_record is not None else run.to_record()),
                        utc_now(),
                    ),
                )
                project_run(conn, run)
            active_dataset = conn.execute(
                "SELECT value FROM state WHERE key='active_dataset'"
            ).fetchone()
            # A batch job that finishes after a different import is saved, not activated.
            if active_dataset is None or active_dataset[0] == run.dataset_id:
                conn.execute(
                    "INSERT INTO state VALUES ('active_run',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (run.run_id,),
                )

    def materialize(self, run_id: str | None = None) -> AnalysisRun | None:
        """Explicitly load all pairs/events/groups for analysis bridges and snapshots."""
        run_id = run_id or self.active_id()
        with self.database.connection() as conn:
            row = conn.execute("SELECT payload FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return AnalysisRun.model_validate_json(row[0]) if row else None
