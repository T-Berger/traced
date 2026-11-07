"""Shared worker mechanics preserve each entry point's durable outcomes and seams."""

from contextlib import contextmanager
from threading import Event

import pytest

from evidence_explorer import analysis
from evidence_explorer import worker as article_worker
from evidence_explorer.review.integrations import worker as workspace_worker
from evidence_explorer.review.storage import ReviewRepository
from evidence_explorer.worker_runtime import worker_heartbeat, worker_session


def entrypoint(kind, repo, settings, monkeypatch, processor):
    if kind == "article":
        # The standalone entry point intentionally resolves this at invocation.
        monkeypatch.setattr(analysis, "process_job", processor)
        return lambda: article_worker.run_worker(settings, once=True)
    worker = workspace_worker.WorkspaceWorker(ReviewRepository(settings.db_path), settings)
    monkeypatch.setattr(workspace_worker, "process_job", processor)
    return worker.drain


@pytest.mark.parametrize("kind,error_limit", [("article", 4000), ("workspace", 1000)])
def test_entrypoint_keeps_failure_limit_progress_and_next_job(
    kind, error_limit, repo, settings, monkeypatch, caplog
):
    first = repo.enqueue_job("analyze", {})
    second = repo.enqueue_job("search", {})
    seen = []

    def processor(repository, _settings, job, report):
        job_id = job.id if hasattr(job, "id") else job["id"]
        seen.append(job_id)
        report("checking", 0.25)
        assert repository.get_job(job_id)["progress"] == 0.25
        if job_id == first:
            raise ValueError("x" * 5000)
        return {"checked": job_id}

    run = entrypoint(kind, repo, settings, monkeypatch, processor)
    assert run() == 2
    assert seen == [first, second]
    failed = repo.get_job(first)
    assert failed["status"] == "failed"
    assert failed["stage"] == "failed"
    assert failed["error"] == ("ValueError: " + "x" * 5000)[:error_limit]
    assert failed["result"] is None
    completed = repo.get_job(second)
    assert completed["status"] == "succeeded"
    assert completed["progress"] == 1
    assert completed["result"] == {"checked": second}
    assert repo.get_meta("worker_state") == "stopped"
    assert repo.get_meta("worker_heartbeat")
    if kind == "workspace":
        assert f"Workspace article job {first} failed: ValueError" in caplog.text
    else:
        assert f"Job {first} (analyze) failed" in caplog.text
    with article_worker.worker_lock(settings.data_dir / "worker.lock"):
        pass


@pytest.mark.parametrize("kind", ["article", "workspace"])
def test_entrypoint_saves_interruption_and_releases_lock_without_claiming_next(
    kind, repo, settings, monkeypatch
):
    first = repo.enqueue_job("analyze", {})
    second = repo.enqueue_job("analyze", {})

    def interrupted(*_):
        raise KeyboardInterrupt

    run = entrypoint(kind, repo, settings, monkeypatch, interrupted)
    with pytest.raises(KeyboardInterrupt):
        run()
    failed = repo.get_job(first)
    assert (failed["status"], failed["stage"]) == ("failed", "interrupted")
    assert failed["error"] == "Worker interrupted. Submit the operation again to retry."
    assert repo.get_job(second)["status"] == "queued"
    assert repo.get_meta("worker_state") == "stopped"
    with article_worker.worker_lock(settings.data_dir / "worker.lock"):
        pass


def test_workspace_recovery_order_precedes_presence_and_cleanup_precedes_unlock(
    repo, settings, monkeypatch
):
    worker = workspace_worker.WorkspaceWorker(ReviewRepository(settings.db_path), settings)
    calls = []
    original_lock = workspace_worker.worker_lock

    @contextmanager
    def lock(path):
        assert path == settings.data_dir / "worker.lock"
        with original_lock(path):
            calls.append("locked")
            try:
                yield
            finally:
                calls.append("unlocked")

    class Thread:
        def __init__(self, *, target, args, name, daemon):
            assert name == "workspace-heartbeat" and daemon
            self.stopped = args[1]

        def start(self):
            calls.append("started")

        def join(self, timeout):
            assert timeout == 1
            assert self.stopped.is_set()
            calls.append("joined")

    def article_recovery():
        calls.append("article")
        return 1

    original_meta = worker.article_repository.set_meta

    def set_meta(key, value):
        calls.append(value if key == "worker_state" else key)
        original_meta(key, value)

    monkeypatch.setattr(workspace_worker, "worker_lock", lock)
    monkeypatch.setattr(workspace_worker, "Thread", Thread)
    monkeypatch.setattr(worker.collection_runtime, "recover", lambda: calls.append("collection"))
    monkeypatch.setattr(worker.article_repository, "recover_interrupted_jobs", article_recovery)
    monkeypatch.setattr(worker.repository, "recover_jobs", lambda: calls.append("review"))
    monkeypatch.setattr(worker.claim_checks, "recover", lambda: calls.append("claim_check"))
    monkeypatch.setattr(worker.article_repository, "set_meta", set_meta)
    with pytest.raises(RuntimeError, match="iteration failed"):
        with worker._session():
            calls.append("processing")
            raise RuntimeError("iteration failed")
    assert calls == [
        "locked",
        "collection",
        "article",
        "review",
        "claim_check",
        "worker_pid",
        "running",
        "worker_heartbeat",
        "started",
        "processing",
        "joined",
        "stopped",
        "worker_heartbeat",
        "unlocked",
    ]
    assert repo.get_meta("worker_state") == "stopped"


def test_recovery_failure_never_publishes_running_and_releases_lock(repo, settings):
    def fail():
        raise RuntimeError("recovery failed")

    def must_not_start(*_):
        pytest.fail("No heartbeat may start when recovery failed")

    with pytest.raises(RuntimeError, match="recovery failed"):
        with worker_session(
            repo,
            lock=article_worker.worker_lock(settings.data_dir / "worker.lock"),
            recover=fail,
            heartbeat=must_not_start,
            thread_name="unused",
            join_timeout=1,
            thread_factory=must_not_start,
        ):
            pytest.fail("No processing may begin when recovery failed")
    assert repo.get_meta("worker_state") is None
    with article_worker.worker_lock(settings.data_dir / "worker.lock"):
        pass


@pytest.mark.parametrize("immediate", [True, False])
def test_heartbeat_first_tick_and_failure_do_not_change_cadence(immediate):
    calls = []

    class Stop:
        def __init__(self):
            self.waits = 0

        def is_set(self):
            return self.waits >= 2

        def wait(self, seconds):
            assert seconds == 5
            self.waits += 1
            calls.append("wait")
            return self.is_set()

    class Repository:
        def set_meta(self, key, value):
            assert (key, value) == ("worker_heartbeat", "clock")
            calls.append("heartbeat")
            raise OSError("database busy")

    worker_heartbeat(
        Repository(),
        Stop(),
        immediate=immediate,
        on_error=lambda: calls.append("error"),
        timestamp=lambda: "clock",
    )
    assert calls == (
        ["heartbeat", "error", "wait", "heartbeat", "error", "wait"]
        if immediate
        else ["wait", "heartbeat", "error", "wait"]
    )


def test_pre_stopped_heartbeat_never_writes_or_reports_failure():
    stopped = Event()
    stopped.set()

    def unexpected():
        pytest.fail("A stopped heartbeat must not call its repository")

    worker_heartbeat(None, stopped, immediate=True, on_error=unexpected)
    worker_heartbeat(None, stopped, immediate=False, on_error=unexpected)
