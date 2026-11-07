"""Application DTOs validate workflow state without materializing source copies."""

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.models import ReviewJob, ReviewJobPayload, ReviewSession
from evidence_explorer.review.selectors import EvidenceIndex
from evidence_explorer.review.service import ReviewService
from evidence_explorer.review.storage import ReviewRepository


@pytest.fixture
def service(tmp_path):
    repository = ReviewRepository(tmp_path / "review.sqlite3")
    repository.initialize()
    instance = ReviewService(repository)
    instance.seed_demo()
    return instance


@pytest.mark.parametrize(
    "values",
    [
        {"scenario_version": True},
        {"scenario_version": 0},
        {"revision": -1},
        {"revision": "2"},
        {"record_id": {"id": "source"}},
        {"record_id": ""},
        {"unrecognized_state": "typo"},
    ],
)
def test_malformed_browser_state_is_rejected(values):
    with pytest.raises(ValidationError):
        ReviewSession.model_validate(values)


def test_session_round_trips_as_json_without_mapping_emulation():
    state = ReviewSession(
        case_id="case",
        finding_id="finding",
        scenario_id="scenario",
        scenario_version=2,
        comparison_scenario_id="comparison",
    )
    state.revision += 1
    wire = state.wire()
    assert wire["revision"] == 1
    assert ReviewSession.model_validate_json(state.model_dump_json()) == state
    assert not hasattr(state, "get")
    with pytest.raises(ValidationError):
        state.revision = False


def payload():
    finding = load_demo_bundle().findings[0]
    return {
        "case_id": "case",
        "case_fingerprint": "fingerprint",
        "finding_id": finding.snapshot_id,
        "finding_snapshot": finding,
        "scenario_id": "scenario",
        "scenario_version": 1,
        "excluded_record_ids": [],
        "base_run_id": finding.run_id,
        "mode": "demo",
    }


@pytest.mark.parametrize(
    "changes", [{"finding_id": "different"}, {"base_run_id": "different"}, {"mode": "typo"}]
)
def test_job_payload_pins_a_valid_finding_and_run(changes):
    with pytest.raises(ValidationError):
        ReviewJobPayload.model_validate({**payload(), **changes})


def test_load_view_retains_contract_objects_without_serializing_bundle(service):
    bundle = service.load_case(service.list_case_summaries()[0].id)
    fingerprint = service.case_fingerprint(bundle.case.id)
    with (
        patch.object(service.repository, "get_bundle_snapshot", return_value=(bundle, fingerprint)),
        patch.object(EvidenceBundle, "model_dump", side_effect=AssertionError("source copy")),
    ):
        view = service.load_view(bundle.case.id)
        index = EvidenceIndex(view.bundle, view.visible_findings)
        assert view.bundle is bundle
        assert index.records[bundle.records[0].id] is bundle.records[0]
        assert view.case_fingerprint == fingerprint
        assert view.events == ()
        assert index.initial() == ("harbor:c3-v1", "harbor:r01")


def test_queue_and_poll_use_metadata_without_loading_bundle(service, monkeypatch):
    case_id = service.list_case_summaries()[0].id
    finding_id = "harbor:c3-v1"

    def reject_full_bundle(*_args, **_kwargs):
        raise AssertionError("An identifier-only operation loaded all source content")

    monkeypatch.setattr(service.repository, "get_bundle", reject_full_bundle)
    monkeypatch.setattr(service.repository, "get_bundle_snapshot", reject_full_bundle)
    scenario = service.start_scenario_state(case_id, finding_id)
    queued = service.queue_challenge(case_id, finding_id, scenario.id)
    job = service.load_job(queued.id)
    assert isinstance(job, ReviewJob)
    assert job.payload.finding_id == finding_id
    assert not job.stale
    assert job.active
    assert service.job(job.id)["payload"]["excluded_record_ids"] == []
    updated = service.set_source_exclusion(scenario.id, "harbor:r01", True, "Uncertain", 1)
    assert updated.version == 2
    assert service.load_job(job.id).stale
    history = service.load_review_history(case_id, scenario.id)
    assert history.scenario.version == 2
    assert history.case_fingerprint == service.case_fingerprint(case_id)


def test_workspace_worker_loads_source_once_and_commits_against_projections(tmp_path, monkeypatch):
    from evidence_explorer.config import Settings
    from evidence_explorer.review.integrations.worker import WorkspaceWorker
    from evidence_explorer.review.integrations.workspace import WorkspaceRepository

    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    bundle = load_demo_bundle()
    repository.import_bundle(bundle)
    scenario = repository.create_scenario(bundle.case.id, bundle.findings[0].run_id)
    queued = repository.enqueue_job(
        bundle.case.id, bundle.findings[0].snapshot_id, scenario["id"], "demo"
    )
    worker = WorkspaceWorker(repository, settings)
    reads = []
    original = ReviewRepository.get_bundle_snapshot

    def snapshot(self, case_id):
        reads.append(case_id)
        return original(self, case_id)

    monkeypatch.setattr(ReviewRepository, "get_bundle_snapshot", snapshot)
    monkeypatch.setattr(worker, "_compute_review", lambda *_: ({"findings": []}, None))
    with patch.object(EvidenceBundle, "fingerprint", side_effect=AssertionError("source rehash")):
        assert worker.run_once()
    assert reads == [bundle.case.id]
    assert repository.get_job(queued["id"])["status"] == "completed"


def test_typed_jobs_and_existing_json_adapters_share_the_same_wire_contract(service):
    case_id = service.list_case_summaries()[0].id
    scenario = service.start_scenario_state(case_id, "harbor:c3-v1")
    typed = service.queue_challenge(case_id, "harbor:c3-v1", scenario.id)
    wire = service.challenge(case_id, "harbor:c3-v1", scenario.id)
    assert wire == service.repository.get_job(typed.id)
    assert wire == typed.model_dump(mode="json", exclude_unset=True)
    assert wire["payload"]["excluded_record_ids"] == []
    assert service.load_job(typed.id).payload == typed.payload
    malformed = {**wire, "scenario_id": "another-scenario"}
    with pytest.raises(ValidationError, match="does not match its payload"):
        ReviewJob.model_validate(malformed)


def test_an_audit_event_cannot_mislabel_an_exclusion_as_a_review(service):
    from evidence_explorer.review.models import AuditEvent, ExclusionChanged

    case_id = service.list_case_summaries()[0].id
    scenario = service.start_scenario_state(case_id, "harbor:c3-v1")
    service.set_source_exclusion(scenario.id, "harbor:r01", True, "Needs corroboration", 1)
    event = service.load_events(case_id)[-1]
    assert isinstance(event.payload, ExclusionChanged)
    assert event.payload.excluded is True
    assert event.decision is None
    mislabeled = event.model_dump(mode="json")
    mislabeled["event_type"] = "finding_reviewed"
    with pytest.raises(ValidationError, match="does not match its event type"):
        AuditEvent.model_validate(mislabeled)


@pytest.mark.parametrize("installed_digest", ["queued-revision", "changed-revision"])
def test_workspace_worker_honors_queued_local_model_identity(
    tmp_path, monkeypatch, installed_digest
):
    import json

    import httpx

    from evidence_explorer.config import Settings
    from evidence_explorer.models_client import OllamaClient
    from evidence_explorer.review.integrations import worker as worker_module
    from evidence_explorer.review.integrations.worker import WorkspaceWorker
    from evidence_explorer.review.verifier import PROMPT_VERSION, RETRIEVAL_VERSION

    settings = Settings(data_dir=tmp_path, chat_model="different-worker-default:1")
    repository = ReviewRepository(settings.db_path)
    worker = WorkspaceWorker(repository, settings)
    bundle = load_demo_bundle()
    repository.import_bundle(bundle)
    finding = bundle.findings[-1]
    scenario = repository.create_scenario_state(bundle.case.id, finding.run_id)
    queued_model = "queued-model:1"
    job = repository.enqueue_job_state(
        bundle.case.id,
        finding.snapshot_id,
        scenario.id,
        "ollama",
        model=queued_model,
        model_identity=f"ollama:{queued_model}@queued-revision:v1",
        prompt_version=PROMPT_VERSION,
        retrieval_version=RETRIEVAL_VERSION,
    )
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": queued_model, "digest": installed_digest}]}
            )
        assert json.loads(request.content)["model"] == queued_model
        return httpx.Response(
            200, json={"model": queued_model, "message": {"content": '{"findings": []}'}}
        )

    monkeypatch.setattr(
        worker_module,
        "OllamaClient",
        lambda selected: OllamaClient(selected, transport=httpx.MockTransport(respond)),
    )
    assert worker.run_once()
    finished = repository.get_job_state(job.id)
    posts = [request for request in requests if request.method == "POST"]
    if installed_digest == "queued-revision":
        assert finished.status == "completed", finished.error
        assert len(posts) == 1
        assert finished.result.search_scope.model == job.payload.model_identity
    else:
        assert finished.status == "failed"
        assert "changed after queueing" in finished.error
        assert posts == []


def test_recomputation_queue_uses_capability_metadata_without_loading_sources(
    tmp_path, monkeypatch
):
    from evidence_explorer.config import Settings
    from evidence_explorer.review.integrations.workspace import (
        WorkspaceRepository,
        WorkspaceService,
    )

    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    data = load_demo_bundle().model_dump(mode="json")
    data["runs"][0]["full_inputs_available"] = True
    data["swarm_context"] = {"baseline_run_id": data["runs"][0]["id"]}
    bundle = EvidenceBundle.model_validate(data)
    repository.import_bundle(bundle)
    finding = bundle.findings[-1]
    scenario = service.start_scenario_state(bundle.case.id, finding.snapshot_id)
    service.set_source_exclusion(scenario.id, "harbor:r01", True, "Sensitivity check", 1)

    def reject_full_source(*_args, **_kwargs):
        raise AssertionError("Queue capability checks must not deserialize the saved dataset")

    monkeypatch.setattr(repository, "get_bundle", reject_full_source)
    monkeypatch.setattr(repository, "get_bundle_snapshot", reject_full_source)
    queued = service.queue_recomputation(bundle.case.id, finding.snapshot_id, scenario.id)
    assert queued.mode == "swarm_recompute"
    assert queued.payload.excluded_record_ids == ("harbor:r01",)
    assert repository.get_swarm_baseline_run_id(bundle.case.id) == finding.run_id
