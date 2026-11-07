"""Assessment identity and portable review applicability, beyond UI rendering."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from evidence_explorer.config import Settings
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.workflow import assessment_decisions, brief_projection


def workspace(path):
    settings = Settings(data_dir=path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    return WorkspaceService(repository, settings, path / "artifacts")


@pytest.fixture
def service(tmp_path):
    result = workspace(tmp_path / "original")
    result.seed_demo()
    return result


def test_first_concurrent_baseline_reviews_share_one_context_and_reads_do_not_write(service):
    case_id = load_demo_bundle().case.id
    assert service.repository.list_events(case_id) == []
    assert service.view(case_id)["baseline_scenario"] is None
    assert service.repository.list_events(case_id) == []
    with ThreadPoolExecutor(max_workers=2) as pool:
        contexts = list(pool.map(lambda _: service.baseline_scenario(case_id), range(2)))
    assert contexts[0]["id"] == contexts[1]["id"]
    assert len(service.repository.list_events(case_id)) == 1


def test_baseline_cannot_be_mutated_into_an_exclusion_scenario(service):
    case_id = load_demo_bundle().case.id
    baseline = service.baseline_scenario(case_id)
    with pytest.raises(ValueError, match="baseline is preserved"):
        service.exclude(baseline["id"], "harbor:r01", True, "Questionable provenance", 1)
    assert service.repository.get_scenario(baseline["id"])["excluded_record_ids"] == []


def test_latest_withhold_removes_earlier_included_wording_from_brief(service):
    case_id, finding_id = load_demo_bundle().case.id, "harbor:c3-v1"
    baseline = service.baseline_scenario(case_id, finding_id)
    service.review(case_id, finding_id, baseline["id"], "use", "Earlier interpretation.")
    service.review(case_id, finding_id, baseline["id"], "withhold", "Control is not established.")
    projection = brief_projection(service.view(case_id))
    assert not projection["included"]
    row = next(item for item in projection["omitted"] if item["finding_id"] == finding_id)
    assert row["status"] == "withheld"
    assert row["reason"] == "Control is not established."
    assert "Earlier interpretation." not in service.export_brief(case_id, scenario_id=None)


def test_imported_baseline_remains_visible_when_a_local_review_context_is_created(
    service, tmp_path
):
    case_id, finding_id = load_demo_bundle().case.id, "harbor:c3-v1"
    baseline = service.baseline_scenario(case_id, finding_id)
    saved = service.review(
        case_id, finding_id, baseline["id"], "qualify", "Original qualification."
    )
    restored = workspace(tmp_path / "restored")
    restored.import_package(service.export_package(case_id))
    assert assessment_decisions(restored.view(case_id))[finding_id]["id"] == saved["id"]
    restored.baseline_scenario(case_id, finding_id)
    assert assessment_decisions(restored.view(case_id))[finding_id]["id"] == saved["id"]


def test_additive_evidence_invalidates_imported_approval_without_erasing_its_history(
    service, tmp_path
):
    case_id, finding_id = load_demo_bundle().case.id, "harbor:c3-v1"
    baseline = service.baseline_scenario(case_id, finding_id)
    saved = service.review(case_id, finding_id, baseline["id"], "use", "Review before later data.")
    restored = workspace(tmp_path / "restored")
    restored.import_package(service.export_package(case_id))
    payload = restored.get_case(case_id)
    payload["runs"].append(dict(payload["runs"][0], id="harbor:newly-imported-run"))
    restored.import_bundle(payload)
    assert finding_id not in assessment_decisions(restored.view(case_id))
    omitted = brief_projection(restored.view(case_id))["omitted"]
    assert (
        next(row for row in omitted if row["finding_id"] == finding_id)["status"] == "needs_review"
    )
    assert saved["id"] in {review.id for review in restored.export_bundle(case_id).reviews}
