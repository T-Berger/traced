"""A single worker handles both queues and rejects stale review computations."""

from __future__ import annotations

import json
from dataclasses import replace
from threading import Event

import httpx
import pytest

from evidence_explorer.config import Settings
from evidence_explorer.demo import seed_demo
from evidence_explorer.models_client import ModelError, OllamaClient
from evidence_explorer.review.integrations import worker as worker_module
from evidence_explorer.review.integrations.media import build_media_demo_bundle
from evidence_explorer.review.integrations.worker import WorkspaceWorker
from evidence_explorer.review.storage import ReviewRepository
from evidence_explorer.worker import WorkerAlreadyRunning, worker_lock


@pytest.fixture
def workspace(tmp_path):
    settings = Settings(data_dir=tmp_path, ollama_url="http://127.0.0.1:1")
    repository = ReviewRepository(settings.db_path)
    worker = WorkspaceWorker(repository, settings)
    bundle = build_media_demo_bundle()
    repository.import_bundle(bundle)
    scenario = repository.create_scenario(bundle.case.id, bundle.runs[0].id)
    finding = next(item for item in bundle.findings if item.kind == "inference")
    return worker, repository, bundle, scenario, finding


def _enqueue(workspace, mode="demo"):
    _, repo, bundle, scenario, finding = workspace
    return repo.enqueue_job(bundle.case.id, finding.snapshot_id, scenario["id"], mode)


def test_one_worker_drains_article_and_review_jobs_from_the_same_database(workspace):
    worker, repository, _, _, _ = workspace
    seed_demo(worker.article_repository)
    review_job = _enqueue(workspace)
    article_job_id = worker.article_repository.enqueue_job(
        "search", {"mode": "demo", "query": "bridge"}
    )
    assert worker.drain() == 2
    assert repository.get_job(review_job["id"])["status"] == "completed"
    assert repository.get_job(review_job["id"])["result"]["mode"] == "demo"
    assert worker.article_repository.get_job(article_job_id)["status"] == "succeeded"
    assert worker.article_repository.get_job(article_job_id)["result"]["matches"]
    assert worker.article_repository.get_meta("worker_state") == "stopped"
    assert worker.article_repository.get_meta("worker_heartbeat")
    assert worker.run_once() is False


def test_queue_selection_alternates_when_both_have_pending_work(workspace):
    worker, repository, bundle, scenario, _ = workspace
    first = _enqueue(workspace)
    second_finding = bundle.findings[0]
    repository.enqueue_job(bundle.case.id, second_finding.snapshot_id, scenario["id"], "openrouter")
    article_job = worker.article_repository.enqueue_job("search", {"mode": "demo", "query": "x"})
    assert worker.run_once() is True
    assert repository.get_job(first["id"])["status"] == "completed"
    assert worker.article_repository.get_job(article_job)["status"] == "queued"
    assert worker.run_once() is True
    assert worker.article_repository.get_job(article_job)["status"] == "succeeded"


def test_stale_scenario_is_rejected_before_model_or_analysis_runs(workspace, monkeypatch):
    worker, repository, _, scenario, _ = workspace
    job = _enqueue(workspace)
    repository.set_exclusion(
        scenario["id"], workspace[2].records[0].id, True, "Check source", scenario["version"]
    )

    def must_not_compute(*_):
        pytest.fail("A stale job must be rejected before computation")

    monkeypatch.setattr(worker, "_compute_review", must_not_compute)
    assert worker.run_once() is True
    completed = repository.get_job(job["id"])
    assert completed["status"] == "failed"
    assert "Scenario changed" in completed["error"]
    assert completed["result"] is None


def test_scenario_changed_during_computation_cannot_accept_its_result(workspace, monkeypatch):
    worker, repository, bundle, scenario, _ = workspace
    job = _enqueue(workspace)

    def changed_while_computing(_bundle, _payload):
        repository.set_exclusion(
            scenario["id"],
            bundle.records[0].id,
            True,
            "Changed during analysis",
            scenario["version"],
        )
        return {"findings": [], "mode": "demo"}, None

    monkeypatch.setattr(worker, "_compute_review", changed_while_computing)
    worker.run_once()
    failed = repository.get_job(job["id"])
    assert failed["status"] == "failed"
    assert "Scenario changed" in failed["error"]
    assert failed["result"] is None
    assert repository.get_bundle(bundle.case.id).fingerprint() == bundle.fingerprint()


def test_changed_case_fingerprint_is_rejected_before_review(workspace):
    worker, repository, bundle, _, _ = workspace
    job = _enqueue(workspace)
    data = bundle.model_dump(mode="json")
    duplicate = dict(data["records"][0])
    duplicate["id"] += ":new"
    data["records"].append(duplicate)
    repository.import_bundle(type(bundle).model_validate(data))
    worker.run_once()
    failed = repository.get_job(job["id"])
    assert failed["status"] == "failed"
    assert "Investigation changed" in failed["error"]


def test_reclaimed_attempt_is_never_overwritten_by_the_old_computation(workspace, monkeypatch):
    worker, repository, _, _, _ = workspace
    job = _enqueue(workspace)

    def reclaimed(_bundle, _payload):
        with repository._connection(write=True) as connection:
            connection.execute(
                "UPDATE review_jobs SET status = 'queued', attempts = attempts + 1 WHERE id = ?",
                (job["id"],),
            )
        repository.claim_job()
        return {"findings": []}, None

    monkeypatch.setattr(worker, "_compute_review", reclaimed)
    worker.run_once()
    current = repository.get_job(job["id"])
    assert current["attempts"] == 3
    assert current["status"] == "running"
    assert current["result"] is None
    assert current["error"] is None


def test_article_model_failure_stays_visible_without_stopping_review_queue(workspace, monkeypatch):
    worker, repository, _, _, _ = workspace
    job = _enqueue(workspace)
    article_job = worker.article_repository.enqueue_job("analyze", {"mode": "ollama"})

    def unavailable(*_):
        raise ModelError("Local model unavailable")

    monkeypatch.setattr(worker_module, "process_job", unavailable)
    assert worker.drain() == 2
    assert repository.get_job(job["id"])["status"] == "completed"
    failed = worker.article_repository.get_job(article_job)
    assert failed["status"] == "failed"
    assert "Local model unavailable" in failed["error"]
    assert failed["result"] is None


def test_ollama_challenges_use_existing_client_validate_quotes_and_cache_by_model(
    workspace, monkeypatch
):
    worker, repository, _, _, _ = workspace
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": worker.settings.chat_model, "digest": "fixture-digest"}]},
            )
        body = json.loads(request.content)
        assert body["model"] == worker.settings.chat_model
        assert body["stream"] is False
        assert body["think"] is False
        assert body["options"] == {"temperature": 0, "num_ctx": 8192, "num_predict": 1800}
        assert "findings" in body["format"]["properties"]
        prompt = json.loads(body["messages"][1]["content"])
        passage = prompt["passages"][0]
        result = {
            "findings": [
                {
                    "finding_type": "unsupported_inference",
                    "explanation": "The metadata does not establish event identity.",
                    "fragment_ids": [passage["fragment_id"]],
                    "quotes": [passage["text"][:80]],
                }
            ]
        }
        return httpx.Response(200, json={"message": {"content": json.dumps(result)}})

    monkeypatch.setattr(
        worker_module,
        "OllamaClient",
        lambda settings: OllamaClient(settings, transport=httpx.MockTransport(handler)),
    )
    first = _enqueue(workspace, "ollama")
    worker.run_once()
    completed = repository.get_job(first["id"])
    assert completed["status"] == "completed"
    assert completed["result"]["mode"] == "ollama"
    assert "Local Ollama" in completed["result"]["label"]
    assert "fixture-digest" in completed["result"]["search_scope"]["model"]
    assert completed["result"]["cached"] is False
    second = _enqueue(workspace, "ollama")
    worker.run_once()
    assert repository.get_job(second["id"])["result"]["cached"] is True
    assert calls.count("/api/chat") == 1


def test_ollama_cannot_accept_nonexistent_evidence_or_fall_back_to_demo(workspace, monkeypatch):
    worker, repository, _, _, _ = workspace

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": worker.settings.chat_model, "digest": "fixture"}]}
            )
        result = {
            "findings": [
                {
                    "finding_type": "unsupported_inference",
                    "explanation": "Not real evidence",
                    "fragment_ids": ["invented-fragment"],
                    "quotes": ["invented quote"],
                }
            ]
        }
        return httpx.Response(200, json={"message": {"content": json.dumps(result)}})

    monkeypatch.setattr(
        worker_module,
        "OllamaClient",
        lambda settings: OllamaClient(settings, transport=httpx.MockTransport(handler)),
    )
    job = _enqueue(workspace, "ollama")
    worker.run_once()
    failed = repository.get_job(job["id"])
    assert failed["status"] == "failed"
    assert "unknown passage alias" in failed["error"]
    assert failed["result"] is None


def test_missing_ollama_is_a_failed_job_with_no_provider_substitution(workspace, monkeypatch):
    worker, repository, _, _, _ = workspace

    def unavailable(_settings):
        raise ModelError("Cannot reach Ollama; start the local model service.")

    monkeypatch.setattr(worker_module, "OllamaClient", unavailable)
    job = _enqueue(workspace, "ollama")
    worker.run_once()
    failed = repository.get_job(job["id"])
    assert failed["status"] == "failed"
    assert "Cannot reach Ollama" in failed["error"]
    assert failed["result"] is None


def test_malformed_ollama_review_response_fails_without_a_clean_result(workspace, monkeypatch):
    worker, repository, _, _, _ = workspace

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": worker.settings.chat_model, "digest": "fixture"}]}
            )
        return httpx.Response(200, json={"message": []})

    monkeypatch.setattr(
        worker_module,
        "OllamaClient",
        lambda settings: OllamaClient(settings, transport=httpx.MockTransport(handler)),
    )
    job = _enqueue(workspace, "ollama")
    worker.run_once()
    failed = repository.get_job(job["id"])
    assert failed["status"] == "failed"
    assert "invalid structured JSON" in failed["error"]
    assert failed["result"] is None


@pytest.fixture(scope="module")
def swarm_bundle():
    detector = pytest.importorskip("swarm_detector.detector")
    sample = pytest.importorskip("swarm_detector.sample_data")
    from evidence_explorer.review.integrations.swarm import build_swarm_bundle

    dataset = sample.build_sample()
    return build_swarm_bundle(dataset, detector.analyze(dataset, {"randomizations": 5, "seed": 42}))


def _queue_swarm(worker, bundle):
    repository = worker.repository
    repository.import_bundle(bundle)
    scenario = repository.create_scenario(bundle.case.id, bundle.runs[0].id)
    episode = next(
        record
        for record in bundle.records
        if record.kind == "telegram_post"
        and record.metadata["swarm"]["post_key"] == "syn-episode-01"
    )
    scenario = repository.set_exclusion(
        scenario["id"], episode.id, True, "Remove strongest episode", scenario["version"]
    )
    job = repository.enqueue_job(
        bundle.case.id, bundle.findings[0].snapshot_id, scenario["id"], "swarm_recompute"
    )
    return scenario, job


def test_real_swarm_recomputation_commits_new_run_and_matching_result(workspace, swarm_bundle):
    worker, repository, _, _, _ = workspace
    scenario, job = _queue_swarm(worker, swarm_bundle)
    before = repository.get_bundle(swarm_bundle.case.id)
    worker.run_once()
    completed = repository.get_job(job["id"])
    assert completed["status"] == "completed", completed["error"]
    result = completed["result"]
    assert result["kind"] == "swarm_recompute"
    assert result["scenario_id"] == scenario["id"]
    assert result["scenario_version"] == scenario["version"]
    assert result["computed_scenario_id"]
    assert result["correspondences"]
    after = repository.get_bundle(swarm_bundle.case.id)
    assert result["result_run_id"] in {run.id for run in after.runs}
    assert len(after.runs) == len(before.runs) + 1
    assert after.runs[0] == before.runs[0]
    computed = next(run for run in after.runs if run.id == result["result_run_id"])
    assert computed.configuration == before.runs[0].configuration


def test_swarm_case_import_and_job_completion_share_one_rollback(
    workspace, swarm_bundle, monkeypatch
):
    worker, repository, _, _, _ = workspace
    _, job = _queue_swarm(worker, swarm_bundle)
    before = repository.get_bundle(swarm_bundle.case.id).fingerprint()

    def fail_completion(*_args, **_kwargs):
        raise ValueError("Deliberate completion failure")

    monkeypatch.setattr(worker_module._TransactionRepository, "finish_job", fail_completion)
    worker.run_once()
    failed = repository.get_job(job["id"])
    assert failed["status"] == "failed"
    assert "Deliberate completion failure" in failed["error"]
    assert repository.get_bundle(swarm_bundle.case.id).fingerprint() == before


def test_swarm_scenario_changed_during_analysis_never_imports_a_stale_run(
    workspace, swarm_bundle, monkeypatch
):
    from evidence_explorer.review.integrations import swarm

    worker, repository, _, _, _ = workspace
    scenario, job = _queue_swarm(worker, swarm_bundle)
    before = repository.get_bundle(swarm_bundle.case.id).fingerprint()
    original_compute = swarm.recompute_swarm_scenario

    def changed(bundle, excluded_record_ids, reason):
        computed = original_compute(bundle, excluded_record_ids, reason)
        repository.set_exclusion(
            scenario["id"], excluded_record_ids[0], False, "Restore source", scenario["version"]
        )
        return computed

    monkeypatch.setattr(swarm, "recompute_swarm_scenario", changed)
    worker.run_once()
    failed = repository.get_job(job["id"])
    assert failed["status"] == "failed"
    assert "Scenario changed" in failed["error"]
    assert repository.get_bundle(swarm_bundle.case.id).fingerprint() == before


def test_workspace_worker_uses_the_existing_article_worker_lock(workspace):
    worker, _, _, _, _ = workspace
    with worker_lock(worker.settings.data_dir / "worker.lock"):
        with pytest.raises(WorkerAlreadyRunning):
            worker.run_once()


def test_read_only_or_different_databases_cannot_create_a_workspace_worker(workspace, tmp_path):
    worker, repository, _, _, _ = workspace
    with pytest.raises(PermissionError, match="read-only"):
        WorkspaceWorker(repository, replace(worker.settings, read_only=True))
    with pytest.raises(ValueError, match="same SQLite"):
        WorkspaceWorker(repository, replace(worker.settings, data_dir=tmp_path / "another"))


def test_pre_stopped_worker_leaves_queued_work_untouched(workspace):
    worker, repository, _, _, _ = workspace
    job = _enqueue(workspace)
    stopped = Event()
    stopped.set()
    worker.run(stopped)
    assert repository.get_job(job["id"])["status"] == "queued"
    assert worker.article_repository.get_meta("worker_state") == "stopped"
