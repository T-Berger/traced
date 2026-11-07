import pytest

from evidence_explorer.review.service import ReviewService
from evidence_explorer.review.storage import ReviewRepository
from evidence_explorer.review.worker import ReviewWorker


def workspace(path):
    repo = ReviewRepository(path)
    repo.initialize()
    service = ReviewService(repo)
    case_id = service.seed_demo()
    return repo, service, case_id


def test_review_workflow_survives_restart(tmp_path):
    path = tmp_path / "evidence.sqlite3"
    repo, service, case_id = workspace(path)
    assert service.seed_demo() == case_id
    assert len(service.list_cases()) == 1
    scenario = service.start_scenario(case_id)
    scenario = service.exclude(
        scenario["id"], "harbor:r01", True, "Address alone is insufficient", scenario["version"]
    )
    view = service.view(case_id, scenario["id"])
    assert set(view["impact"]["affected_finding_ids"]) == {"harbor:c3-v1"}
    assert len(view["bundle"]["records"]) == 10
    service.review(
        case_id, "harbor:c3-v1", scenario["id"], "withhold", "Ownership is unestablished"
    )
    job = service.challenge(case_id, "harbor:c3-v1", scenario["id"])
    assert ReviewWorker(repo).run_once()
    completed = service.job(job["id"])
    assert completed["status"] == "completed", completed.get("error")
    assert completed["result"]["mode"] == "demo"
    assert completed["stale"] is False
    assert completed["result"]["findings"][0]["fragment_ids"] == ["harbor:f02"]
    _, reopened, _ = workspace(path)
    assert reopened.view(case_id, scenario["id"])["scenario"]["excluded_record_ids"] == [
        "harbor:r01"
    ]
    assert any(
        event["event_type"] == "finding_reviewed"
        for event in reopened.repository.list_events(case_id)
    )
    reopened.exclude(
        scenario["id"], "harbor:r01", False, "Restore original evidence", scenario["version"]
    )
    assert reopened.view(case_id, scenario["id"])["impact"]["affected_finding_ids"] == []
    assert reopened.job(job["id"])["stale"] is True


def test_pending_check_retains_original_scenario_version(tmp_path):
    repo, service, case_id = workspace(tmp_path / "evidence.sqlite3")
    scenario = service.start_scenario(case_id)
    job = service.challenge(case_id, "harbor:c3-v1", scenario["id"])
    service.exclude(scenario["id"], "harbor:r01", True, "Changed after queue", scenario["version"])
    assert ReviewWorker(repo).run_once()
    assert service.job(job["id"])["stale"] is True
    assert service.job(job["id"])["payload"]["excluded_record_ids"] == []


def test_unknown_mode_is_not_silently_demo(tmp_path):
    _, service, case_id = workspace(tmp_path / "evidence.sqlite3")
    scenario = service.start_scenario(case_id)
    with pytest.raises(ValueError, match="Choose demo"):
        service.challenge(case_id, "harbor:c3-v1", scenario["id"], mode="unknown")


def test_selected_historical_finding_uses_its_own_run(tmp_path):
    from evidence_explorer.review.contracts import EvidenceBundle

    repo, service, case_id = workspace(tmp_path / "evidence.sqlite3")
    data = service.get_case(case_id)
    later_run = dict(data["runs"][0], id="harbor:run-v2", created_at="2026-11-06T10:00:00+01:00")
    later_finding = dict(
        data["findings"][2], snapshot_id="harbor:c3-v2", run_id="harbor:run-v2", version=2
    )
    data["runs"].append(later_run)
    data["findings"].append(later_finding)
    repo.import_bundle(EvidenceBundle.model_validate(data))
    scenario = service.start_scenario(case_id, finding_id="harbor:c3-v1")
    assert scenario["base_run_id"] == "harbor:run-v1"
    job = service.challenge(case_id, "harbor:c3-v1", scenario["id"])
    assert ReviewWorker(repo).run_once()
    assert service.job(job["id"])["status"] == "completed"


def test_review_rejects_changed_scenario_and_evidence(tmp_path):
    from evidence_explorer.review.contracts import EvidenceBundle
    from evidence_explorer.review.storage import VersionConflictError

    repo, service, case_id = workspace(tmp_path / "evidence.sqlite3")
    scenario = service.start_scenario(case_id, "harbor:c3-v1")
    fingerprint = service.case_fingerprint(case_id)
    changed = service.exclude(
        scenario["id"], "harbor:r01", True, "Review change", scenario["version"]
    )
    with pytest.raises(VersionConflictError):
        service.review(
            case_id,
            "harbor:c3-v1",
            scenario["id"],
            "use",
            "Old view",
            expected_version=scenario["version"],
            expected_fingerprint=fingerprint,
        )
    valid = service.review(
        case_id,
        "harbor:c3-v1",
        scenario["id"],
        "withhold",
        "New view",
        expected_version=changed["version"],
        expected_fingerprint=fingerprint,
    )
    assert valid["case_fingerprint"] == fingerprint
    data = service.get_case(case_id)
    data["links"].append(
        dict(
            data["links"][0],
            id="harbor:additional-link",
            target={"type": "finding", "id": "harbor:c3-v1"},
        )
    )
    repo.import_bundle(EvidenceBundle.model_validate(data))
    assert service.case_fingerprint(case_id) != fingerprint
    with pytest.raises(VersionConflictError):
        service.review(
            case_id,
            "harbor:c3-v1",
            scenario["id"],
            "use",
            "Old evidence graph",
            expected_version=changed["version"],
            expected_fingerprint=fingerprint,
        )
