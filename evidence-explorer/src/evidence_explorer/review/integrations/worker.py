"""Compose article, review, claim-check, and collection work in one workspace worker.

The four queues and feed scheduling share Explorer's operating-system worker
lock and selected SQLite file. Queue handlers retain their own payloads, leases,
and completion rules. Review model calls and Swarm analysis run outside SQLite
transactions, followed by a context-guarded commit. Shared helpers own worker
presence and article completion; this loop retains domain recovery and queue order.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event, Thread
from typing import Any

from ...analysis import process_job
from ...collection.persistence import CollectionRepository
from ...collection.runtime import CollectionRuntime
from ...config import Settings
from ...db import Repository
from ...feeds.runtime import FeedScheduler
from ...models_client import OllamaClient
from ...worker import worker_lock
from ...worker_runtime import run_article_job, worker_heartbeat, worker_session
from ..analysis import challenge_finding
from ..contracts import EvidenceBundle
from ..models import ReviewJob, ReviewJobPayload, ReviewResult
from ..storage import ReviewRepository
from ..verifier import (
    PROMPT_VERSION,
    RETRIEVAL_VERSION,
    CheckError,
    OllamaVerifier,
    OpenRouterVerifier,
)

logger = logging.getLogger(__name__)


class StaleWorkspaceJob(ValueError):
    """A queued result no longer belongs to the current scenario or inputs."""


class _TransactionRepository(ReviewRepository):
    """Reuse repository operations on the caller's existing write transaction.

    The normal repository deliberately opens short connections per operation.
    This view lets its import and expected-attempt completion share one guarded
    commit without copying their validation, merge, or update implementations.
    """

    def __init__(self, repository: ReviewRepository, connection: sqlite3.Connection):
        super().__init__(repository.db_path)
        self._active_connection = connection

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        del write
        yield self._active_connection


# Compatibility alias: both entry points use the same local review prompt and validator.
_OllamaVerifier = OllamaVerifier


class WorkspaceWorker:
    """Rotate four queues under one process lock and schedule due source work.

    Construction initializes the required stores but does not start processing.
    run_once(), drain(), and run() acquire the shared workspace lock; heartbeat
    threads maintain worker/job leases within that lifecycle.
    """

    def __init__(self, repository: ReviewRepository, settings: Settings):
        if settings.read_only:
            raise PermissionError("A read-only workspace cannot run a worker.")
        if repository.db_path.resolve() != settings.db_path.resolve():
            raise ValueError("Workspace review and article queues must use the same SQLite file.")
        self.repository = repository
        self.settings = settings
        self.repository.initialize()
        self.article_repository = Repository(repository.db_path)
        self.article_repository.initialize()
        self._feed_scheduler = FeedScheduler(self.article_repository)
        from ...claim_check.service import ClaimCheckService

        self.claim_checks = ClaimCheckService(repository, settings)
        self._queue_order = ["review", "article", "claim_check", "collection"]
        self.collection_runtime = CollectionRuntime(CollectionRepository(settings.db_path))

    def _validate_context(self, repository: ReviewRepository, job: ReviewJob) -> None:
        """Check only pinned workflow projections, including inside the commit."""
        payload = job.payload
        current_job = repository.get_job_state(job.id)
        if (
            current_job is None
            or current_job.status != "running"
            or current_job.attempts != job.attempts
        ):
            raise StaleWorkspaceJob("This job attempt was reclaimed; its result was not accepted.")
        if current_job.payload != payload:
            raise StaleWorkspaceJob("Queued review context changed; submit a fresh operation.")
        if repository.get_case_fingerprint(payload.case_id) != payload.case_fingerprint:
            raise StaleWorkspaceJob(
                "Investigation changed after queueing; submit a fresh operation."
            )
        scenario = repository.get_scenario_state(payload.scenario_id)
        if (
            scenario.case_id != payload.case_id
            or scenario.base_run_id != payload.base_run_id
            or scenario.version != payload.scenario_version
            or set(scenario.excluded_record_ids) != set(payload.excluded_record_ids)
        ):
            raise StaleWorkspaceJob("Scenario changed after queueing; submit a fresh operation.")
        finding = repository.get_finding(payload.case_id, payload.finding_id)
        if finding is None or finding != payload.finding_snapshot:
            raise StaleWorkspaceJob("The queued finding snapshot changed; submit it again.")

    def _current_context(self, repository: ReviewRepository, job: ReviewJob) -> EvidenceBundle:
        self._validate_context(repository, job)
        bundle, fingerprint = repository.get_bundle_snapshot(job.payload.case_id)
        if fingerprint != job.payload.case_fingerprint:
            raise StaleWorkspaceJob(
                "Investigation changed after queueing; submit a fresh operation."
            )
        return bundle

    def _compute_review(
        self, bundle: EvidenceBundle, payload: ReviewJobPayload
    ) -> tuple[dict[str, Any], EvidenceBundle | None]:
        mode = payload.mode
        if mode == "category_proposal":
            from ...compliance_review.producer import category_batch_bundle
            from ...compliance_review.proposals import (
                PROMPT_VERSION as CATEGORY_PROMPT_VERSION,
            )
            from ...compliance_review.proposals import (
                propose_for_record,
                require_local_ollama,
                require_model_identity,
            )

            if payload.prompt_version != CATEGORY_PROMPT_VERSION:
                raise CheckError("Proposal method changed. Submit a fresh operation.")
            records = {r.id: r for r in bundle.records}
            selected = []
            for item in payload.record_manifest:
                record = records.get(item.id)
                if item.id in payload.excluded_record_ids:
                    raise StaleWorkspaceJob("A selected record is excluded from this assessment.")
                if (
                    record is None
                    or record.text is None
                    or record.content_hash != item.content_hash
                ):
                    raise StaleWorkspaceJob("A selected saved record changed or is unavailable.")
                selected.append(record)
            settings = (
                replace(self.settings, chat_model=payload.model) if payload.model else self.settings
            )
            require_local_ollama(settings)
            require_model_identity(payload.model_identity)
            with OllamaClient(settings) as client:
                if require_model_identity(client.model_key("chat")) != payload.model_identity:
                    raise CheckError("The installed local model changed after queueing.")
                proposals = [
                    propose_for_record(record.model_dump(mode="json"), payload.target, client)
                    for record in selected
                ]
            computed = category_batch_bundle(bundle, proposals)
            count = sum(len(proposal["annotations"]) for proposal in proposals)
            return {
                "kind": "category_proposal",
                "mode": "category_proposal",
                "label": "Local model proposals — human review required",
                "message": f"Saved {count} model proposals for review.",
                "result_run_id": computed.runs[-1].id,
                "output_fingerprint": computed.fingerprint(),
                "record_ids": [record.id for record in selected],
                "proposal_count": count,
            }, computed
        if mode in {"ollama", "openrouter"} and (
            payload.prompt_version not in {None, PROMPT_VERSION}
            or payload.retrieval_version not in {None, RETRIEVAL_VERSION}
        ):
            raise CheckError("Review configuration changed. Submit a fresh challenge.")
        if mode == "swarm_recompute":
            from .swarm import recompute_swarm_scenario

            context = getattr(bundle, "swarm_context", None) or {}
            if payload.base_run_id != context.get("baseline_run_id"):
                raise ValueError("Choose the original Swarm baseline run for recomputation.")
            reason = payload.reason or (
                f"Analyst exclusion scenario version {payload.scenario_version}"
            )
            computed = recompute_swarm_scenario(bundle, list(payload.excluded_record_ids), reason)
            selected = sorted(payload.excluded_record_ids)
            scenarios = [
                scenario.model_dump(mode="json")
                for scenario in computed.scenarios
                if sorted((scenario.model_extra or {}).get("requested_excluded_record_ids", []))
                == selected
                and scenario.base_run_id == payload.base_run_id
                and scenario.reason == reason.strip()
                and scenario.status == "completed"
                and scenario.result_run_id is not None
            ]
            if computed.case.id != payload.case_id or len(scenarios) != 1:
                raise ValueError("Swarm analysis did not produce one completed matching scenario.")
            scenario = scenarios[0]
            cached = any(previous.id == scenario["id"] for previous in bundle.scenarios)
            return {
                "kind": "swarm_recompute",
                "scenario_id": payload.scenario_id,
                "scenario_version": payload.scenario_version,
                "computed_scenario_id": scenario["id"],
                "result_run_id": scenario["result_run_id"],
                "correspondences": scenario.get("correspondences", []),
                "cached": cached,
                "message": (
                    "Reused the matching saved Swarm result."
                    if cached
                    else "Recomputed the complete declared subset with the recorded baseline "
                    "settings and seed. Changed findings require review."
                ),
            }, None if cached else computed
        if mode == "ollama":
            settings = (
                replace(self.settings, chat_model=payload.model) if payload.model else self.settings
            )
            with OllamaClient(settings) as client:
                verifier = _OllamaVerifier(client)
                if payload.model_identity is not None and verifier.model != payload.model_identity:
                    raise CheckError(
                        "The installed local model changed after queueing. Run a new check."
                    )
                cache_input = {
                    "bundle": payload.case_fingerprint,
                    "finding": payload.finding_id,
                    "excluded": sorted(payload.excluded_record_ids),
                    "model": verifier.model,
                    "prompt_version": PROMPT_VERSION,
                    "retrieval_version": RETRIEVAL_VERSION,
                }
                digest = hashlib.sha256(
                    json.dumps(cache_input, sort_keys=True).encode("utf-8")
                ).hexdigest()
                cache_key = "workspace-review:" + digest
                cached = self.article_repository.get_meta(cache_key)
                if cached is not None:
                    return {**cached, "cached": True}, None
                result = challenge_finding(
                    bundle,
                    payload.finding_id,
                    excluded_ids=list(payload.excluded_record_ids),
                    bundle_fingerprint=payload.case_fingerprint,
                    mode="openrouter",
                    verifier=verifier,
                )
                result.update(
                    {
                        "mode": "ollama",
                        "cached": False,
                        "label": "Local Ollama review — candidate objections for human assessment",
                    }
                )
                result["search_scope"]["provider"] = "ollama"
                self.article_repository.set_meta(cache_key, result)
                return result, None
        if mode not in {"demo", "openrouter"}:
            raise ValueError(f"Unsupported workspace review mode: {mode}")
        return challenge_finding(
            bundle,
            payload.finding_id,
            excluded_ids=list(payload.excluded_record_ids),
            bundle_fingerprint=payload.case_fingerprint,
            mode=mode,
            verifier=OpenRouterVerifier.from_env(payload.model) if payload.model else None,
        ), None

    def _commit_review(
        self, job: ReviewJob, result: dict[str, Any], computed: EvidenceBundle | None
    ) -> None:
        with self.repository._connection(write=True) as connection:
            transaction = _TransactionRepository(self.repository, connection)
            self._validate_context(transaction, job)
            if computed is not None:
                transaction.import_bundle(computed)
            if job.mode == "category_proposal" and computed is not None:
                assessment = transaction.create_scenario_state(
                    job.case_id, result["result_run_id"], assessment_kind="scenario"
                )
                result["result_scenario_id"] = assessment.id
                from ...compliance_review.package_guard import ensure_transaction_bundle_fits

                ensure_transaction_bundle_fits(
                    self.repository, connection, self.settings, job.case_id
                )
            validated = ReviewResult.model_validate(result)
            transaction.finish_job(
                job.id,
                validated.model_dump(mode="json", exclude_unset=True),
                expected_attempt=job.attempts,
            )

    def _run_review(self, job: ReviewJob) -> None:
        stopped = Event()

        def renew_lease() -> None:
            interval = min(30, max(0.05, self.repository.JOB_LEASE_SECONDS / 3))
            while not stopped.wait(interval):
                try:
                    if not self.repository.heartbeat_job(job.id, job.attempts):
                        return
                except Exception:
                    logger.warning("Could not renew workspace job lease %s", job.id)

        heartbeat = Thread(target=renew_lease, name="workspace-review-lease", daemon=True)
        heartbeat.start()
        try:
            bundle = self._current_context(self.repository, job)
            result, computed = self._compute_review(bundle, job.payload)
            self._commit_review(job, result, computed)
        except KeyboardInterrupt:
            self._fail_review(job, "Worker interrupted; submit the operation again to retry.")
            raise
        except Exception as exc:
            logger.warning("Workspace review job %s failed: %s", job.id, type(exc).__name__)
            self._fail_review(job, str(exc)[:1000] or type(exc).__name__)
        finally:
            stopped.set()
            heartbeat.join(timeout=1)

    def _fail_review(self, job: ReviewJob, message: str) -> None:
        try:
            self.repository.fail_job(job.id, message, expected_attempt=job.attempts)
        except ValueError:
            logger.warning("Workspace job %s was reclaimed; old attempt left untouched", job.id)

    def _run_article(self, job: dict[str, Any]) -> None:
        def report(stage: str, progress: float) -> None:
            self.article_repository.update_job(job["id"], stage=stage, progress=progress)

        run_article_job(
            self.article_repository,
            job["id"],
            lambda: process_job(self.article_repository, self.settings, job, report),
            error_limit=1000,
            on_failure=lambda exc: logger.warning(
                "Workspace article job %s failed: %s", job["id"], type(exc).__name__
            ),
        )

    def _run_once_unlocked(
        self, *, force_feeds: bool = False, schedule_cutoff: datetime | None = None
    ) -> bool:
        self._feed_scheduler.tick(force=force_feeds, now=schedule_cutoff)
        for queue in self._queue_order:
            if queue == "review":
                review_job = self.repository.claim_job_state()
                if review_job is None:
                    continue
                self._run_review(review_job)
            elif queue == "article":
                article_job = self.article_repository.claim_job()
                if article_job is None:
                    continue
                self._run_article(article_job)
            elif queue == "claim_check":
                if not self.claim_checks.run_next():
                    continue
            elif not self.collection_runtime.run_once(schedule_before=schedule_cutoff):
                continue
            index = self._queue_order.index(queue) + 1
            self._queue_order = self._queue_order[index:] + self._queue_order[:index]
            return True
        return False

    @contextmanager
    def _session(self) -> Iterator[None]:
        def recover() -> None:
            self.collection_runtime.recover()
            recovered = self.article_repository.recover_interrupted_jobs()
            if recovered:
                logger.warning("Marked %s interrupted article job(s) failed", recovered)
            self.repository.recover_jobs()
            self.claim_checks.recover()

        def heartbeat(repository: Repository, stopped: Event) -> None:
            worker_heartbeat(
                repository,
                stopped,
                immediate=False,
                on_error=lambda: logger.warning("Could not update workspace worker heartbeat"),
                timestamp=lambda: datetime.now(UTC).isoformat(),
            )

        with worker_session(
            self.article_repository,
            lock=worker_lock(self.settings.data_dir / "worker.lock"),
            recover=recover,
            heartbeat=heartbeat,
            thread_name="workspace-heartbeat",
            join_timeout=1,
            event_factory=Event,
            thread_factory=Thread,
            timestamp=lambda: datetime.now(UTC).isoformat(),
            process_id=os.getpid,
        ):
            yield

    def run_once(self) -> bool:
        """Run due scheduling and process at most one item from the four queues."""
        cutoff = datetime.now(UTC)
        with self._session():
            result = self._run_once_unlocked(force_feeds=True, schedule_cutoff=cutoff)
            self._feed_scheduler.tick(force=True, now=cutoff, limit=0)
            return result

    def drain(self) -> int:
        """Drain queued and scheduled work due at a fixed cutoff, then release the lock."""
        processed = 0
        cutoff = datetime.now(UTC)
        with self._session():
            while self._run_once_unlocked(force_feeds=True, schedule_cutoff=cutoff):
                processed += 1
        return processed

    def run(self, stop: Event | None = None) -> None:
        """Schedule and process all four queues until stopped, holding one worker lock."""
        stop = stop or Event()
        with self._session():
            while not stop.is_set():
                try:
                    if not self._run_once_unlocked():
                        stop.wait(0.75)
                except Exception:
                    logger.warning("Workspace worker iteration failed", exc_info=False)
                    stop.wait(2)
