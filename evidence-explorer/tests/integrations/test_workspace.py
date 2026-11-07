"""Shared workbench acceptance paths across producer adapters and saved reviews."""

import hashlib

import pytest

from evidence_explorer.config import Settings
from evidence_explorer.db import Repository
from evidence_explorer.review.analysis import group_sources
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService


def workspace(path):
    settings = Settings(data_dir=path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    return WorkspaceService(repository, settings, path / "evidence")


@pytest.fixture
def shared_workspace(tmp_path):
    return workspace(tmp_path / "original")


def test_seed_examples_is_idempotent_and_never_requires_a_swarm_run(shared_workspace):
    first = shared_workspace.seed_examples(include_swarm=False)
    bundles = {
        case["id"]: shared_workspace.repository.get_bundle(case["id"]).fingerprint()
        for case in shared_workspace.list_cases()
    }
    assert len(first) == 2
    assert load_demo_bundle().case.id in first
    assert any(case_id.startswith("media:") for case_id in first)
    assert shared_workspace.seed_examples(include_swarm=False) == []
    assert len(shared_workspace.list_cases()) == 2
    assert {
        case["id"]: shared_workspace.repository.get_bundle(case["id"]).fingerprint()
        for case in shared_workspace.list_cases()
    } == bundles


def test_media_package_reopens_with_exact_local_artifacts_and_unknown_dates(
    shared_workspace, tmp_path
):
    shared_workspace.seed_examples(include_swarm=False)
    case_id = next(
        case["id"] for case in shared_workspace.list_cases() if case["id"].startswith("media:")
    )
    original = shared_workspace.export_bundle(case_id)
    package = shared_workspace.export_package(case_id)
    restored = workspace(tmp_path / "restored")
    assert restored.import_package(package) == case_id
    reopened = restored.repository.get_bundle(case_id)
    assert reopened.model_dump() == original.model_dump()
    assert reopened.case.synthetic is True
    frames = [record for record in reopened.records if record.artifact_ref]
    assert len(frames) == 3
    for frame in frames:
        path = restored.artifact(case_id, frame.id)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == frame.content_hash
        assert path.is_relative_to(restored.artifact_root)
        assert frame.published_at is None
        assert frame.metadata["capture_time"] is None
        assert frame.metadata["event_time"] is None
        assert frame.metadata["location"] is None
        assert frame.metadata["depicted_side"] is None
    assert reopened.reviews[0].origin == "synthetic_review_fixture"
    assert reopened.reviews[0].evidence_refs
    # Reopening the package again does not duplicate imports or review history.
    assert restored.import_package(package) == case_id
    assert len(restored.repository.get_bundle(case_id).reviews) == 1


def test_export_retains_original_and_revised_decisions_with_their_exclusions(
    shared_workspace, tmp_path
):
    shared_workspace.seed_examples(include_swarm=False)
    case_id, finding_id = load_demo_bundle().case.id, "harbor:c3-v1"
    scenario = shared_workspace.start_scenario(case_id)
    first = shared_workspace.review(
        case_id, finding_id, scenario["id"], "qualify", "The shared address needs checking."
    )
    changed = shared_workspace.exclude(
        scenario["id"],
        "harbor:r01",
        True,
        "Exclude the ambiguous address record.",
        scenario["version"],
    )
    second = shared_workspace.review(
        case_id,
        finding_id,
        scenario["id"],
        "withhold",
        "The available evidence does not establish control.",
    )
    assert changed["version"] > scenario["version"]
    exported = shared_workspace.export_bundle(case_id)
    assert {review.id for review in exported.reviews} >= {first["id"], second["id"]}
    original_review = next(review for review in exported.reviews if review.id == first["id"])
    revised_review = next(review for review in exported.reviews if review.id == second["id"])
    versions = {state.id: state for state in exported.scenarios}
    assert original_review.scenario_id != revised_review.scenario_id
    assert versions[original_review.scenario_id].excluded_record_ids == []
    assert versions[revised_review.scenario_id].excluded_record_ids == ["harbor:r01"]
    assert original_review.finding_snapshot_id == revised_review.finding_snapshot_id == finding_id

    restored = workspace(tmp_path / "restored")
    restored.import_package(shared_workspace.export_package(case_id))
    reopened = restored.repository.get_bundle(case_id)
    assert reopened.model_dump() == exported.model_dump()
    EvidenceBundle.model_validate_json(reopened.model_dump_json())
    brief = restored.export_brief(case_id)
    assert "qualify" in brief and "withhold" in brief
    assert first["reason"] in brief and second["reason"] in brief
    assert shared_workspace.repository.get_bundle(case_id).records == exported.records


def test_view_keeps_baseline_findings_separate_from_another_run(shared_workspace):
    shared_workspace.seed_examples(include_swarm=False)
    case_id = load_demo_bundle().case.id
    bundle = shared_workspace.repository.get_bundle(case_id)
    payload = bundle.model_dump(mode="json")
    baseline_id = bundle.runs[0].id
    another_run = dict(payload["runs"][0], id="harbor:run-v2")
    payload["runs"].append(another_run)
    changed_finding = dict(
        payload["findings"][0],
        snapshot_id="harbor:c1-v2",
        version=2,
        run_id=another_run["id"],
        statement="A separately versioned result awaiting review.",
    )
    payload["findings"].append(changed_finding)
    shared_workspace.import_bundle(payload)
    scenario = shared_workspace.start_scenario(case_id)
    assert scenario["base_run_id"] == baseline_id
    for view in (
        shared_workspace.view(case_id),
        shared_workspace.view(case_id, scenario["id"]),
    ):
        assert {finding["run_id"] for finding in view["bundle"]["findings"]} == {baseline_id}
        assert "harbor:c1-v2" not in {
            finding["snapshot_id"] for finding in view["bundle"]["findings"]
        }
    # Filtering presentation must not delete the additional saved analysis.
    assert len(shared_workspace.repository.get_bundle(case_id).findings) == len(bundle.findings) + 1

    selected_scenario = shared_workspace.start_scenario(case_id, "harbor:c1-v2")
    assert selected_scenario["base_run_id"] == another_run["id"]
    selected_view = shared_workspace.view(case_id, selected_scenario["id"])
    assert [finding["snapshot_id"] for finding in selected_view["bundle"]["findings"]] == [
        "harbor:c1-v2"
    ]
    assert (
        shared_workspace.review(
            case_id,
            "harbor:c1-v2",
            selected_scenario["id"],
            "qualify",
            "This review belongs to the selected result run.",
        )["finding_id"]
        == "harbor:c1-v2"
    )
    with pytest.raises(ValueError, match="Selected finding is not in this case"):
        shared_workspace.start_scenario(case_id, "unrelated:finding")


def test_explorer_import_reuses_identical_snapshot_and_versions_changed_corpus(
    shared_workspace, article
):
    source = Repository(shared_workspace.settings.db_path)
    source.initialize()
    source.upsert_article(article)
    original_id = shared_workspace.import_explorer()
    original = shared_workspace.repository.get_bundle(original_id)
    assert shared_workspace.import_explorer() == original_id
    assert len(shared_workspace.list_cases()) == 1
    assert len(original.records) == 1
    source.upsert_article(
        article.model_copy(
            update={
                "url": "https://second.example/review-update",
                "title": "A separately saved update for review",
                "text": article.text + " A later source adds a new statement.",
            }
        )
    )
    updated_id = shared_workspace.import_explorer()
    assert updated_id != original_id
    assert len(shared_workspace.list_cases()) == 2
    assert len(shared_workspace.repository.get_bundle(updated_id).records) == 2
    assert (
        shared_workspace.repository.get_bundle(original_id).fingerprint() == original.fingerprint()
    )
    assert shared_workspace.import_explorer() == updated_id


def test_missing_or_changed_local_media_is_not_served_as_verified_evidence(shared_workspace):
    shared_workspace.seed_examples(include_swarm=False)
    case_id = next(
        case["id"] for case in shared_workspace.list_cases() if case["id"].startswith("media:")
    )
    bundle = shared_workspace.repository.get_bundle(case_id)
    frame = next(record for record in bundle.records if record.artifact_ref)
    path = shared_workspace.artifact(case_id, frame.id)
    path.write_text("Modified bytes", encoding="utf-8")
    with pytest.raises(ValueError, match="no longer matches"):
        shared_workspace.artifact(case_id, frame.id)
    with pytest.raises(ValueError, match="hash mismatch"):
        shared_workspace.export_package(case_id)


@pytest.fixture(scope="module")
def swarm_bundle():
    detector = pytest.importorskip("swarm_detector.detector")
    source = pytest.importorskip("swarm_detector.sample_data")
    from evidence_explorer.review.integrations.swarm import build_swarm_bundle

    dataset = source.build_sample()
    return build_swarm_bundle(dataset, detector.analyze(dataset, {"randomizations": 5, "seed": 42}))


def test_swarm_source_groups_focus_on_findings_while_retaining_background_inputs(
    shared_workspace, swarm_bundle
):
    shared_workspace.import_bundle(swarm_bundle.model_dump(mode="json"))
    case_id = swarm_bundle.case.id
    complete_before = shared_workspace.repository.get_bundle(case_id)
    original_groups = group_sources(complete_before)
    view = shared_workspace.view(case_id)
    relevant = set()
    for finding in view["bundle"]["findings"]:
        relevant.update(view["impact"]["remaining_evidence"].get(finding["snapshot_id"], []))
        relevant.update(finding.get("details", {}).get("context_record_ids", []))
    unrelated_members = {
        record_id
        for group in original_groups
        for record_id in group["record_ids"]
        if record_id not in relevant
    }
    assert unrelated_members, "The fixture must include repeated background traffic."
    assert all(set(group["record_ids"]) <= relevant for group in view["groups"])
    assert all(len(group["record_ids"]) > 1 for group in view["groups"])
    retained = shared_workspace.repository.get_bundle(case_id)
    assert retained.model_dump(mode="json") == complete_before.model_dump(mode="json")
    assert unrelated_members <= {record.id for record in retained.records}
    assert (
        retained.model_extra["swarm_context"]["dataset"]
        == swarm_bundle.model_extra["swarm_context"]["dataset"]
    )


def test_recompute_rejects_derived_run_before_queueing_work(shared_workspace, swarm_bundle):
    payload = swarm_bundle.model_dump(mode="json")
    derived_run = dict(payload["runs"][0], id="regression:derived-run")
    payload["runs"].append(derived_run)
    derived_finding = dict(
        payload["findings"][0],
        snapshot_id="regression:derived-finding",
        run_id=derived_run["id"],
        version=2,
    )
    payload["findings"].append(derived_finding)
    case_id = shared_workspace.import_bundle(payload)
    scenario = shared_workspace.start_scenario(case_id, derived_finding["snapshot_id"])
    shared_workspace.exclude(
        scenario["id"], payload["records"][0]["id"], True, "Another scenario", scenario["version"]
    )
    with pytest.raises(ValueError, match="Return to the original Swarm assessment"):
        shared_workspace.recompute(case_id, derived_finding["snapshot_id"], scenario["id"])
    with shared_workspace.repository._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_jobs").fetchone()[0] == 0


def test_swarm_review_recompute_and_revised_review_reopen_with_complete_history(
    shared_workspace, swarm_bundle, tmp_path
):
    from evidence_explorer.review.integrations.episodes import strongest_episode_record
    from evidence_explorer.review.integrations.worker import WorkspaceWorker

    case_id = shared_workspace.import_bundle(swarm_bundle.model_dump(mode="json"))
    baseline_finding = swarm_bundle.findings[0]
    baseline_scenario = shared_workspace.start_scenario(case_id, baseline_finding.snapshot_id)
    baseline_fingerprint = shared_workspace.case_fingerprint(case_id)
    original_review = shared_workspace.review(
        case_id,
        baseline_finding.snapshot_id,
        baseline_scenario["id"],
        "qualify",
        "Repeated arrival timing is observed; intent remains unknown.",
        expected_version=baseline_scenario["version"],
        expected_fingerprint=baseline_fingerprint,
    )
    strongest = strongest_episode_record(swarm_bundle, baseline_finding.snapshot_id)
    excluded = shared_workspace.exclude(
        baseline_scenario["id"],
        strongest.id,
        True,
        "Test sensitivity to the strongest documented episode family.",
        baseline_scenario["version"],
    )
    queued = shared_workspace.recompute(
        case_id, baseline_finding.snapshot_id, baseline_scenario["id"]
    )
    assert WorkspaceWorker(shared_workspace.repository, shared_workspace.settings).run_once()
    completed = shared_workspace.job(queued["id"])
    assert completed["status"] == "completed", completed.get("error")
    assert completed["stale"] is False
    result_run_id = completed["result"]["result_run_id"]
    assert result_run_id != baseline_finding.run_id
    current = shared_workspace.repository.get_bundle(case_id)
    result_finding = next(
        finding for finding in current.findings if finding.run_id == result_run_id
    )
    assert all(
        review.finding_snapshot_id != result_finding.snapshot_id
        for review in shared_workspace.export_bundle(case_id).reviews
    ), "A baseline approval must not migrate to a recomputed finding."
    result_scenario = shared_workspace.start_scenario(case_id, result_finding.snapshot_id)
    revised_review = shared_workspace.review(
        case_id,
        result_finding.snapshot_id,
        result_scenario["id"],
        "qualify",
        "Reviewing the recomputed observation with its narrower remaining evidence.",
        expected_version=result_scenario["version"],
        expected_fingerprint=shared_workspace.case_fingerprint(case_id),
    )
    # A recomputed run's selection and qualification must survive the typed
    # service-to-JSON adapter used by the Assessment picker and Brief page.
    from evidence_explorer.review.workflow import (
        assessment_context,
        assessment_decisions,
        brief_projection,
    )

    result_view = shared_workspace.view(case_id, result_scenario["id"])
    assert assessment_context(result_view)["id"] == result_scenario["id"]
    assert any(item["id"] == result_scenario["id"] for item in result_view["assessments"])
    assert (
        assessment_decisions(result_view)[result_finding.snapshot_id]["id"] == revised_review["id"]
    )
    assert any(
        item["finding_id"] == result_finding.snapshot_id and item["disposition"] == "qualify"
        for item in brief_projection(result_view)["included"]
    )
    exported = shared_workspace.export_bundle(case_id)
    restored = workspace(tmp_path / "restored-swarm")
    restored.import_package(shared_workspace.export_package(case_id))
    reopened = restored.repository.get_bundle(case_id)
    assert reopened.model_dump(mode="json") == exported.model_dump(mode="json")
    EvidenceBundle.model_validate_json(reopened.model_dump_json())
    assert {baseline_finding.run_id, result_run_id} <= {run.id for run in reopened.runs}
    reviews = {review.id: review for review in reopened.reviews}
    assert reviews[original_review["id"]].run_id == baseline_finding.run_id
    assert reviews[revised_review["id"]].run_id == result_run_id
    assert reviews[original_review["id"]].scenario_id != reviews[revised_review["id"]].scenario_id
    local_exclusion = next(
        scenario
        for scenario in reopened.scenarios
        if scenario.model_extra.get("source_scenario_id") == baseline_scenario["id"]
        and scenario.model_extra.get("source_scenario_version") == excluded["version"]
    )
    assert strongest.id in local_exclusion.excluded_record_ids
    assert local_exclusion.result_run_id == result_run_id
    assert (
        reopened.model_extra["swarm_context"]["dataset"]
        == swarm_bundle.model_extra["swarm_context"]["dataset"]
    )
