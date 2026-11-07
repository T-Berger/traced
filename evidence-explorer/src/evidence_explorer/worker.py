"""Standalone article worker and the lock shared with the integrated workspace.

This entry point schedules saved feeds and processes Explorer ingestion, analysis,
and search jobs. Review, claim-check, and direct-collection queues are served by
``review.integrations.worker.WorkspaceWorker``. Both entry points use the same
data-directory lock; they are alternative worker lifecycles for that workspace.
"""

import fcntl
import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from .config import Settings
from .db import Repository
from .feeds.runtime import FeedScheduler
from .worker_runtime import run_article_job, worker_heartbeat, worker_session

logger = logging.getLogger(__name__)


class WorkerAlreadyRunning(RuntimeError):
    """Another worker owns the queue for this data directory."""


@contextmanager
def worker_lock(path: Path) -> Iterator[None]:
    """Use a stable lock file; the OS releases its lock even after a crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkerAlreadyRunning(
                f"Another worker is already running for {path.parent}."
            ) from exc
        try:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(str(os.getpid()))
            lock_file.flush()
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _heartbeat(repo: Repository, stop: threading.Event) -> None:
    worker_heartbeat(
        repo,
        stop,
        immediate=True,
        on_error=lambda: logger.exception("Could not update worker heartbeat"),
        timestamp=lambda: datetime.now(UTC).isoformat(),
    )


def _report_progress(repo: Repository, job_id: int, stage: str, progress: float) -> None:
    repo.update_job(job_id, stage=stage, progress=progress)


def run_worker(settings: Settings, *, once: bool = False, poll_interval: float = 1.0) -> int:
    """Drain Explorer jobs once, or poll its queue until interrupted.

    Initialize the article store, schedule due saved feeds, and persist job status
    and worker heartbeats. This does not service the integrated domain queues.
    Failed or interrupted jobs remain visible for manual resubmission. No automatic
    retries silently repeat expensive model calls or conceal persistent errors.
    """
    from .analysis import process_job

    if settings.read_only:
        raise PermissionError("A read-only snapshot cannot run an analysis worker.")
    repo = Repository(settings.db_path)
    repo.initialize()
    processed = 0
    scheduler = FeedScheduler(repo)
    # A one-shot worker drains feeds due at startup, rather than replaying new
    # periodic cycles if local analysis takes longer than the refresh interval.
    schedule_cutoff = datetime.now(UTC) if once else None

    def recover() -> None:
        recovered = repo.recover_interrupted_jobs()
        if recovered:
            logger.warning("Marked %s interrupted job(s) failed; submit again to retry", recovered)

    with worker_session(
        repo,
        lock=worker_lock(settings.data_dir / "worker.lock"),
        recover=recover,
        heartbeat=_heartbeat,
        thread_name="worker-heartbeat",
        join_timeout=2,
        event_factory=threading.Event,
        thread_factory=threading.Thread,
        timestamp=lambda: datetime.now(UTC).isoformat(),
        process_id=os.getpid,
    ) as stopped:
        while True:
            scheduler.tick(force=once, now=schedule_cutoff)
            job = repo.jobs.claim_record()
            if job is None:
                if once:
                    return processed
                stopped.wait(max(0.1, poll_interval))
                continue

            report = partial(_report_progress, repo, job.id)

            def execute(job=job, report=report) -> dict:
                if job.kind not in {"analyze", "search", "ingest"}:
                    raise ValueError(f"Unsupported job kind: {job.kind}")
                return process_job(repo, settings, job, report)

            run_article_job(
                repo,
                job.id,
                execute,
                error_limit=4000,
                on_failure=lambda _exc, job=job: logger.exception(
                    "Job %s (%s) failed", job.id, job.kind
                ),
                on_success=lambda job=job: logger.info("Job %s (%s) completed", job.id, job.kind),
            )
            processed += 1
