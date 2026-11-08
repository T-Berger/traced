"""Exercise the shared worker without running or downloading a real model."""

import json
from dataclasses import replace

import pytest

from evidence_explorer.compliance_review.model_ui import model_proposal_panel, queue_selected
from evidence_explorer.compliance_review.proposals import PROMPT_VERSION
from evidence_explorer.config import Settings
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.integrations.worker import WorkspaceWorker
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService


class FakeLocalModel:
    last_generation_metadata = {"requested_temperature": 0}

    def __init__(self, settings):
        self.settings = settings

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def model_key(self, kind):
        assert kind == "chat"
        return "ollama:fixture@sha256:" + "a" * 64 + ":v1"

    def generate_structured(self, messages, schema, *, max_tokens):
        text = json.loads(messages[1]["content"])["saved_text"]
        return schema.model_validate(
            {
                "candidates": [
                    {
                        "category": "harassment",
                        "quote": text[:12],
                        "rationale": "Fixture candidate for review.",
                    }
                ],
                "uncertainty": "Fictional.",
            }
        )


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    from evidence_explorer import models_client
    from evidence_explorer.review.integrations import worker

    monkeypatch.setattr(models_client, "OllamaClient", FakeLocalModel)
    monkeypatch.setattr(worker, "OllamaClient", FakeLocalModel)
    settings = Settings(data_dir=tmp_path, ollama_url="http://127.0.0.1:11434")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    bundle = load_demo_bundle()
    repository.import_bundle(bundle)
    scenario = service.start_scenario(bundle.case.id)
    record = next(r for r in bundle.records if r.text and len(r.text) >= 12)
    finding = next(f for f in bundle.findings if f.run_id == scenario["base_run_id"])
    return service, bundle, scenario, record, finding


def queue(service, bundle, scenario, record, finding):
    return service.queue_category_proposal(
        bundle.case.id,
        finding.snapshot_id,
        scenario["id"],
        record.id,
        record.content_hash,
        "Fictional target",
        service.case_fingerprint(bundle.case.id),
        scenario["version"],
    )


def test_worker_saves_exact_model_proposal_and_keeps_human_decisions_separate(prepared):
    service, bundle, scenario, record, finding = prepared
    job = queue(service, bundle, scenario, record, finding)
    assert job.payload.prompt_version == PROMPT_VERSION
    assert WorkspaceWorker(service.repository, service.settings).run_once()
    result = service.load_job(job.id)
    assert result.status == "completed" and not result.stale
    assert result.result.result_scenario_id
    assert (
        service.load_scenario(result.result.result_scenario_id).base_run_id
        == result.result.result_run_id
    )
    saved = service.repository.get_bundle(bundle.case.id)
    candidates = [f for f in saved.findings if f.details.get("domain") == "compliance_category"]
    assert len(candidates) == 1 and candidates[0].details["record_id"] == record.id
    assert candidates[0].details["origin"] == "model"
    assessments = service.assessments(bundle.case.id)
    assert any(a["id"] == result.result.result_scenario_id for a in assessments)
    view = service.view(bundle.case.id, result.result.result_scenario_id)
    assert candidates[0].snapshot_id in {f["snapshot_id"] for f in view["bundle"]["findings"]}
    assert len(saved.reviews) == len(bundle.reviews)
    link = next(link for link in saved.links if link.target.id == candidates[0].snapshot_id)
    fragment = next(f for f in saved.fragments if f.id == link.fragment_ids[0])
    assert fragment.quote == record.text[:12]
    decision = service.review(
        bundle.case.id,
        candidates[0].snapshot_id,
        result.result.result_scenario_id,
        "qualify",
        "Needs context",
    )
    assert decision["finding_id"] == candidates[0].snapshot_id


def test_queue_rejects_stale_case_and_excluded_record(prepared):
    service, bundle, scenario, record, finding = prepared
    with pytest.raises(ValueError, match="case changed"):
        service.queue_category_proposal(
            bundle.case.id,
            finding.snapshot_id,
            scenario["id"],
            record.id,
            record.content_hash,
            "Target",
            "old",
            scenario["version"],
        )
    changed = service.exclude(
        scenario["id"], record.id, True, "Exclude for review", scenario["version"]
    )
    with pytest.raises(ValueError, match="excluded"):
        queue(service, bundle, changed, record, finding)


def test_worker_rejects_changed_assessment_before_generation(prepared):
    service, bundle, scenario, record, finding = prepared
    job = queue(service, bundle, scenario, record, finding)
    service.exclude(scenario["id"], record.id, True, "Exclude for review", scenario["version"])
    assert WorkspaceWorker(service.repository, service.settings).run_once()
    assert service.load_job(job.id).status == "failed"
    saved = service.repository.get_bundle(bundle.case.id)
    assert not any(f.details.get("domain") == "compliance_category" for f in saved.findings)


def test_queue_rejects_missing_model_digest_without_creating_job(prepared, monkeypatch):
    service, bundle, scenario, record, finding = prepared
    monkeypatch.setattr(FakeLocalModel, "model_key", lambda self, kind: "ollama:fixture@unknown:v1")
    with service.repository._connection() as connection:
        before = connection.execute("SELECT COUNT(*) FROM review_jobs").fetchone()[0]
    with pytest.raises(ValueError, match="valid SHA-256 model digest"):
        queue(service, bundle, scenario, record, finding)
    with service.repository._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_jobs").fetchone()[0] == before


def test_worker_rejects_missing_model_digest_before_inference(prepared, monkeypatch):
    service, bundle, scenario, record, finding = prepared
    job = queue(service, bundle, scenario, record, finding)
    monkeypatch.setattr(FakeLocalModel, "model_key", lambda self, kind: "ollama:fixture@unknown:v1")

    def no_inference(*args, **kwargs):
        raise AssertionError("Inference should not be called")

    monkeypatch.setattr(FakeLocalModel, "generate_structured", no_inference)
    assert WorkspaceWorker(service.repository, service.settings).run_once()
    result = service.load_job(job.id)
    assert result.status == "failed" and "valid SHA-256 model digest" in result.error
    assert not any(
        f.details.get("domain") == "compliance_category"
        for f in service.repository.get_bundle(bundle.case.id).findings
    )


def test_worker_rejects_assessment_changed_during_generation(prepared, monkeypatch):
    service, bundle, scenario, record, finding = prepared
    job = queue(service, bundle, scenario, record, finding)
    original = FakeLocalModel.generate_structured

    def mutate_and_generate(self, messages, schema, *, max_tokens):
        service.exclude(scenario["id"], record.id, True, "Changed", scenario["version"])
        return original(self, messages, schema, max_tokens=max_tokens)

    monkeypatch.setattr(FakeLocalModel, "generate_structured", mutate_and_generate)
    assert WorkspaceWorker(service.repository, service.settings).run_once()
    assert service.load_job(job.id).status == "failed"
    saved = service.repository.get_bundle(bundle.case.id)
    assert not any(f.details.get("domain") == "compliance_category" for f in saved.findings)


def test_explicit_baseline_action_creates_review_context_and_queues_selected_text(prepared):
    service, bundle, _scenario, record, finding = prepared
    state = {
        "case_id": bundle.case.id,
        "scenario_id": None,
        "scenario_version": None,
        "case_fingerprint": service.case_fingerprint(bundle.case.id),
        "record_id": record.id,
        "finding_id": finding.snapshot_id,
    }
    job = queue_selected(service, state, "Fictional target")
    assert job.payload.record_id == record.id and job.payload.target == "Fictional target"
    assert service.load_scenario(job.scenario_id).base_run_id == finding.run_id
    state["case_fingerprint"] = "old"
    with pytest.raises(ValueError, match="changed"):
        queue_selected(service, state, "Fictional target")


def test_read_only_never_creates_a_baseline_or_job(prepared):
    service, bundle, _scenario, record, finding = prepared
    state = {
        "case_id": bundle.case.id,
        "scenario_id": None,
        "scenario_version": None,
        "case_fingerprint": service.case_fingerprint(bundle.case.id),
        "record_id": record.id,
        "finding_id": finding.snapshot_id,
    }
    service.settings = replace(service.settings, read_only=True)
    with service.repository._connection() as connection:
        before = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("review_scenarios", "review_jobs", "review_events")
        )
    with pytest.raises(PermissionError, match="read-only"):
        queue_selected(service, state, "Fictional target")
    with service.repository._connection() as connection:
        after = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("review_scenarios", "review_jobs", "review_events")
        )
    assert before == after
    panel = model_proposal_panel(read_only=True)
    assert panel.children[3].disabled is True
    assert panel.children[5].disabled is True
    assert panel.children[6].disabled is True


def test_selected_records_share_one_run_and_assessment(prepared):
    service, bundle, scenario, record, finding = prepared
    second = next(r for r in bundle.records if r.id != record.id and r.text and len(r.text) >= 12)
    state = {
        "case_id": bundle.case.id,
        "scenario_id": scenario["id"],
        "scenario_version": scenario["version"],
        "case_fingerprint": service.case_fingerprint(bundle.case.id),
        "record_id": record.id,
        "finding_id": finding.snapshot_id,
    }
    job = queue_selected(service, state, "Fictional target", [record.id, second.id])
    assert [entry.id for entry in job.payload.record_manifest] == [record.id, second.id]
    assert WorkspaceWorker(service.repository, service.settings).run_once()
    result = service.load_job(job.id)
    assert result.status == "completed" and not result.stale
    saved = service.repository.get_bundle(bundle.case.id)
    proposals = [f for f in saved.findings if f.run_id == result.result.result_run_id]
    assert {f.details["record_id"] for f in proposals} == {record.id, second.id}
    assert len({f.run_id for f in proposals}) == 1
    assert (
        service.load_scenario(result.result.result_scenario_id).base_run_id == proposals[0].run_id
    )
    assert len(saved.reviews) == len(bundle.reviews)


def test_batch_failure_saves_no_partial_results(prepared, monkeypatch):
    service, bundle, scenario, record, finding = prepared
    second = next(r for r in bundle.records if r.id != record.id and r.text and len(r.text) >= 12)
    state = {
        "case_id": bundle.case.id,
        "scenario_id": scenario["id"],
        "scenario_version": scenario["version"],
        "case_fingerprint": service.case_fingerprint(bundle.case.id),
        "record_id": record.id,
        "finding_id": finding.snapshot_id,
    }
    job = queue_selected(service, state, "Fictional target", [record.id, second.id])
    original = FakeLocalModel.generate_structured
    calls = 0

    def fail_second(self, messages, schema, *, max_tokens):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("Second record failed")
        return original(self, messages, schema, max_tokens=max_tokens)

    monkeypatch.setattr(FakeLocalModel, "generate_structured", fail_second)
    assert WorkspaceWorker(service.repository, service.settings).run_once()
    assert service.load_job(job.id).status == "failed" and calls == 2
    saved = service.repository.get_bundle(bundle.case.id)
    assert not any(f.details.get("domain") == "compliance_category" for f in saved.findings)


def test_batch_rejects_second_record_excluded_after_queue(prepared):
    service, bundle, scenario, record, finding = prepared
    second = next(r for r in bundle.records if r.id != record.id and r.text and len(r.text) >= 12)
    state = {
        "case_id": bundle.case.id,
        "scenario_id": scenario["id"],
        "scenario_version": scenario["version"],
        "case_fingerprint": service.case_fingerprint(bundle.case.id),
        "record_id": record.id,
        "finding_id": finding.snapshot_id,
    }
    job = queue_selected(service, state, "Fictional target", [record.id, second.id])
    service.exclude(scenario["id"], second.id, True, "Removed", scenario["version"])
    assert WorkspaceWorker(service.repository, service.settings).run_once()
    assert service.load_job(job.id).status == "failed"
    assert not any(
        f.details.get("domain") == "compliance_category"
        for f in service.repository.get_bundle(bundle.case.id).findings
    )


def test_oversize_batch_rolls_back_proposals_and_assessment(prepared, monkeypatch):
    from evidence_explorer.review.integrations import packages

    service, bundle, scenario, record, finding = prepared
    job = queue(service, bundle, scenario, record, finding)
    before = service.repository.get_bundle(bundle.case.id)
    assessments = service.assessments(bundle.case.id)
    existing_export = service.export_bundle(bundle.case.id)
    monkeypatch.setattr(
        packages, "MAX_FILE_BYTES", len(packages._json(existing_export.model_dump(mode="json"))) + 1
    )

    assert WorkspaceWorker(service.repository, service.settings).run_once()
    result = service.load_job(job.id)
    assert result.status == "failed"
    assert "too large" in result.error
    assert service.repository.get_bundle(bundle.case.id).fingerprint() == before.fingerprint()
    assert service.assessments(bundle.case.id) == assessments
