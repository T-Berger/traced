"""Compatibility facade for focused review persistence components.

Application code can use the typed component repositories directly. Existing
JSON callers retain their wire adapters here. Every component receives this
facade's connection factory, including subclasses bound to an existing transaction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..persistence.connection import ConnectionPolicy, enable_wal, sqlite_connection
from ..persistence.migrations import migrate
from .contracts import EvidenceBundle, Finding, Run
from .models import AuditEvent, CaseMetadata, CaseSummary, ReviewDecision, ReviewJob, ScenarioState
from .persistence import schema
from .persistence.bundles import BundleRepository
from .persistence.common import VersionConflictError as VersionConflictError
from .persistence.common import job_from_row
from .persistence.history import ReviewHistoryRepository
from .persistence.jobs import ReviewJobRepository
from .persistence.scenarios import ScenarioRepository


class ReviewRepository:
    """Compose review repositories over one single-host SQLite connection factory.

    Construction wires components without opening the database. initialize()
    applies the review migration history; ordinary operations use short-lived
    connections, with BEGIN IMMEDIATE for writes. Snapshot and worker adapters
    override _connection so multiple repository operations share one transaction.
    Article, collection, and planning schemas retain their own migration owners.
    """

    JOB_LEASE_SECONDS = 300

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.bundles = BundleRepository(self._connection)
        self.scenarios = ScenarioRepository(self._connection)
        self.history = ReviewHistoryRepository(self._connection)
        self.jobs = ReviewJobRepository(self._connection, lambda: self.JOB_LEASE_SECONDS)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with sqlite_connection(
            self.db_path, write=write, policy=ConnectionPolicy(immediate_writes=True)
        ) as connection:
            yield connection

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            migrate(connection, "review", schema.MIGRATIONS)
            enable_wal(connection)

    def import_bundle(self, bundle: EvidenceBundle) -> str:
        return self.bundles.import_bundle(bundle)

    def preview_import(self, bundle: EvidenceBundle) -> str:
        return self.bundles.preview_import(bundle)

    def list_case_page(self, *, limit: int = 100, offset: int = 0) -> list[CaseSummary]:
        return self.bundles.list_cases(limit=limit, offset=offset)

    def list_cases(self) -> list[dict[str, Any]]:
        return [case.model_dump(mode="json") for case in self.bundles.list_cases()]

    def get_case_metadata(self, case_id: str) -> CaseMetadata:
        return self.bundles.get_metadata(case_id)

    def get_case_fingerprint(self, case_id: str) -> str:
        return self.bundles.get_fingerprint(case_id)

    def get_swarm_baseline_run_id(self, case_id: str) -> str | None:
        return self.bundles.get_swarm_baseline_run_id(case_id)

    def get_bundle_snapshot(self, case_id: str) -> tuple[EvidenceBundle, str]:
        return self.bundles.get_snapshot(case_id)

    def get_bundle(self, case_id: str) -> EvidenceBundle:
        return self.get_bundle_snapshot(case_id)[0]

    def get_finding(self, case_id: str, finding_id: str) -> Finding:
        return self.bundles.get_finding(case_id, finding_id)

    def list_runs(self, case_id: str) -> list[Run]:
        return self.bundles.list_runs(case_id)

    def has_record(self, case_id: str, record_id: str) -> bool:
        return self.bundles.has_record(case_id, record_id)

    def get_fragment_records(self, case_id: str, fragment_ids: Collection[str]) -> dict[str, str]:
        return self.bundles.get_fragment_records(case_id, fragment_ids)

    def create_scenario_state(
        self,
        case_id: str,
        base_run_id: str,
        reviewer: str = "Analyst",
        *,
        assessment_kind: str = "scenario",
    ) -> ScenarioState:
        return self.scenarios.create(
            case_id, base_run_id, reviewer, assessment_kind=assessment_kind
        )

    def create_scenario(
        self,
        case_id: str,
        base_run_id: str,
        reviewer: str = "Analyst",
        *,
        assessment_kind: str = "scenario",
    ) -> dict[str, Any]:
        return self.create_scenario_state(
            case_id, base_run_id, reviewer, assessment_kind=assessment_kind
        ).model_dump(mode="json")

    def get_scenario_state(self, scenario_id: str) -> ScenarioState:
        return self.scenarios.get(scenario_id)

    def get_scenario(self, scenario_id: str) -> dict[str, Any]:
        return self.get_scenario_state(scenario_id).model_dump(mode="json")

    def list_scenario_states(self, case_id: str) -> list[tuple[ScenarioState, str]]:
        return self.scenarios.list_states(case_id)

    def get_scenario_kind(self, scenario_id: str) -> str:
        return self.scenarios.get_kind(scenario_id)

    def set_exclusion_state(
        self, scenario_id: str, record_id: str, excluded: bool, reason: str, expected_version: int
    ) -> ScenarioState:
        return self.scenarios.set_exclusion(
            scenario_id, record_id, excluded, reason, expected_version
        )

    def set_exclusion(
        self, scenario_id: str, record_id: str, excluded: bool, reason: str, expected_version: int
    ) -> dict[str, Any]:
        return self.set_exclusion_state(
            scenario_id, record_id, excluded, reason, expected_version
        ).model_dump(mode="json")

    def list_audit_events(
        self, case_id: str, *, limit: int | None = None, offset: int = 0
    ) -> list[AuditEvent]:
        return self.history.list_events(case_id, limit=limit, offset=offset)

    def list_events(self, case_id: str) -> list[dict[str, Any]]:
        return [event.model_dump(mode="json") for event in self.list_audit_events(case_id)]

    def save_review_state(
        self,
        case_id: str,
        finding_id: str,
        scenario_id: str,
        reviewer: str,
        disposition: str,
        reason: str,
        *,
        expected_version: int | None = None,
        expected_fingerprint: str | None = None,
    ) -> ReviewDecision:
        return self.history.save_review(
            case_id,
            finding_id,
            scenario_id,
            reviewer,
            disposition,
            reason,
            expected_version=expected_version,
            expected_fingerprint=expected_fingerprint,
        )

    def save_review(
        self,
        case_id: str,
        finding_id: str,
        scenario_id: str,
        reviewer: str,
        disposition: str,
        reason: str,
        *,
        expected_version: int | None = None,
        expected_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        return self.save_review_state(
            case_id,
            finding_id,
            scenario_id,
            reviewer,
            disposition,
            reason,
            expected_version=expected_version,
            expected_fingerprint=expected_fingerprint,
        ).model_dump(mode="json")

    def enqueue_job_state(
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
    ) -> ReviewJob:
        return self.jobs.enqueue(
            case_id,
            finding_id,
            scenario_id,
            mode,
            model=model,
            model_identity=model_identity,
            prompt_version=prompt_version,
            retrieval_version=retrieval_version,
            reason=reason,
        )

    def enqueue_job(
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
    ) -> dict[str, Any]:
        return self.enqueue_job_state(
            case_id,
            finding_id,
            scenario_id,
            mode,
            model=model,
            model_identity=model_identity,
            prompt_version=prompt_version,
            retrieval_version=retrieval_version,
            reason=reason,
        ).model_dump(mode="json", exclude_unset=True)

    def get_job_state(self, job_id: str) -> ReviewJob | None:
        return self.jobs.get(job_id)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.get_job_state(job_id)
        return job.model_dump(mode="json", exclude_unset=True) if job is not None else None

    def get_scenario_computation(self, scenario_id: str, version: int) -> ReviewJob | None:
        return self.jobs.scenario_computation(scenario_id, version)

    @staticmethod
    def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
        # Retained for WorkspaceRepository's existing SQL adapter.
        return job_from_row(row).model_dump(mode="json", exclude_unset=True)

    def recover_jobs(self) -> int:
        return self.jobs.recover()

    def claim_job_state(self) -> ReviewJob | None:
        return self.jobs.claim()

    def claim_job(self) -> dict[str, Any] | None:
        job = self.claim_job_state()
        return job.model_dump(mode="json", exclude_unset=True) if job is not None else None

    def heartbeat_job(self, job_id: str, expected_attempt: int) -> bool:
        return self.jobs.heartbeat(job_id, expected_attempt)

    def finish_job(
        self, job_id: str, result: dict[str, Any], *, expected_attempt: int | None = None
    ) -> None:
        self.jobs.finish(job_id, result, expected_attempt=expected_attempt)

    def fail_job(self, job_id: str, error: str, *, expected_attempt: int | None = None) -> None:
        self.jobs.fail(job_id, error, expected_attempt=expected_attempt)
