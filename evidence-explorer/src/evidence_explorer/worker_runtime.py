"""Shared worker lifecycle and article completion mechanics.

Entry points supply their lock, recovery order, heartbeat policy, and processor.
Queue selection, review leases, collection scheduling, and domain validation stay
with those callers. This module does not start a worker on import.
"""

import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from threading import Event, Thread

from .db import Repository


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def worker_heartbeat(
    repository: Repository,
    stopped: Event,
    *,
    immediate: bool,
    on_error: Callable[[], None],
    timestamp: Callable[[], str] = _timestamp,
) -> None:
    """Refresh worker presence every five seconds with the caller's first-tick policy."""
    while not (stopped.is_set() if immediate else stopped.wait(5)):
        try:
            repository.set_meta("worker_heartbeat", timestamp())
        except Exception:
            on_error()
        if immediate:
            stopped.wait(5)


@contextmanager
def worker_session(
    repository: Repository,
    *,
    lock: AbstractContextManager,
    recover: Callable[[], None],
    heartbeat: Callable[[Repository, Event], None],
    thread_name: str,
    join_timeout: float,
    event_factory: Callable[[], Event] = Event,
    thread_factory: Callable[..., Thread] = Thread,
    timestamp: Callable[[], str] = _timestamp,
    process_id: Callable[[], int] = os.getpid,
) -> Iterator[Event]:
    """Recover under the lock and publish worker presence until the caller exits.

    Recovery and startup failures propagate before processing begins. Once the
    heartbeat starts, normal exit and interruptions stop it and record the final
    state before releasing the same lock. Factories retain entry-point injection.
    """
    with lock:
        recover()
        repository.set_meta("worker_pid", process_id())
        repository.set_meta("worker_state", "running")
        repository.set_meta("worker_heartbeat", timestamp())
        stopped = event_factory()
        thread = thread_factory(
            target=heartbeat,
            args=(repository, stopped),
            name=thread_name,
            daemon=True,
        )
        thread.start()
        try:
            yield stopped
        finally:
            stopped.set()
            thread.join(timeout=join_timeout)
            repository.set_meta("worker_state", "stopped")
            repository.set_meta("worker_heartbeat", timestamp())


def run_article_job(
    repository: Repository,
    job_id: int,
    execute: Callable[[], dict],
    *,
    error_limit: int,
    on_failure: Callable[[Exception], None],
    on_success: Callable[[], None] | None = None,
) -> None:
    """Persist one article attempt's result while retaining caller logging policy.

    Ordinary failures remain saved and allow the worker to continue. A keyboard
    interruption is saved and re-raised. The caller supplies input validation,
    progress reporting, and processing; no queue is claimed or retried here.
    """
    try:
        result = execute()
        repository.update_job(
            job_id,
            status="succeeded",
            stage="complete",
            progress=1.0,
            result=result,
        )
        if on_success is not None:
            on_success()
    except KeyboardInterrupt:
        repository.update_job(
            job_id,
            status="failed",
            stage="interrupted",
            error="Worker interrupted. Submit the operation again to retry.",
        )
        raise
    except Exception as exc:
        on_failure(exc)
        repository.update_job(
            job_id,
            status="failed",
            stage="failed",
            error=f"{type(exc).__name__}: {exc}"[:error_limit],
        )
