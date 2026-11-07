import json
import sqlite3

import pytest

from swarm_detector.service import Workbench
from swarm_detector.storage import Store


@pytest.fixture
def workbench(tmp_path):
    wb = Workbench(tmp_path / "swarm.db")
    wb.ensure_sample()
    return wb


def test_empty_bootstrap_is_idempotent_and_reviews_survive_restart(workbench):
    case = workbench.list_cases()[0]
    review = workbench.save_review(
        case["case_id"], "Reviewer", "Insufficient evidence", "Context needs more investigation."
    )
    restarted = Workbench(workbench.store.path)
    restarted.ensure_sample()
    assert restarted.overview()["run_id"] == workbench.overview()["run_id"]
    assert restarted.get_review(case["case_id"])["revision"] == review["revision"]
    second = restarted.save_review(
        case["case_id"],
        "Reviewer",
        "Alternative explanation plausible",
        "The public context explains participation.",
    )
    history = restarted.store.review_history(review["run_id"], case["case_id"])
    assert len(history) == 2
    assert second["revision"] > review["revision"]
    assert history[0]["decision"] == "Insufficient evidence"


def test_review_requires_reasons_and_narrative_source_evidence(workbench):
    case = workbench.list_cases()[0]
    with pytest.raises(ValueError, match="ten characters"):
        workbench.save_review(case["case_id"], "Reviewer", "Coordination supported", "yes")
    with pytest.raises(ValueError, match="supporting comments"):
        workbench.save_review(
            case["case_id"],
            "Reviewer",
            "Insufficient evidence",
            "Adequate review reason.",
            claim="A contextual claim",
        )
    detail = workbench.case_detail(case["case_id"])
    source = detail["messages"][0]["message_key"]
    saved = workbench.save_review(
        case["case_id"],
        "Reviewer",
        "Insufficient evidence",
        "A review of the available context.",
        claim="Example claim",
        evidence_refs=[source],
        narrative_notes="Reviewed against the parent post.",
    )
    assert saved["evidence_refs"] == [source]
    with pytest.raises(ValueError, match="comments in this case"):
        workbench.save_review(
            case["case_id"],
            "Reviewer",
            "Insufficient evidence",
            "A review of the available context.",
            evidence_refs=["not-a-source"],
        )


def test_export_resolves_pair_sources_and_does_not_include_unrelated_comments(workbench):
    case = workbench.list_cases()[0]
    exported = json.loads(workbench.export_case(case["case_id"], "json"))
    refs = {message["message_key"] for message in exported["messages"]}
    for pair in exported["pairs"]:
        for event in pair["events"]:
            assert event["message_a"] in refs and event["message_b"] in refs
    assert len(exported["messages"]) < workbench.overview()["message_count"]
    assert exported["synthetic"] is True
    assert "SYNTHETIC DEMONSTRATION" in workbench.export_case(case["case_id"])
    assert workbench.export_case(case["case_id"], "csv").startswith("synthetic,run_id,case_id")


def test_fts_search_is_literal_and_backup_is_consistent(workbench, tmp_path):
    assert workbench.search("fictional")
    assert workbench.search('" OR 1=1 --') == []
    path = workbench.store.backup(tmp_path / "backup.db")
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            == workbench.overview()["message_count"]
        )
    with pytest.raises(ValueError, match="new file"):
        workbench.store.backup(path)


def test_dataset_ids_cannot_overwrite_saved_provenance(workbench):
    dataset = workbench.store.get_dataset()
    dataset["messages"][0]["text_original"] = "Changed source"
    with pytest.raises(ValueError, match="different content"):
        workbench.store.put_dataset(dataset)


def test_comparison_cases_are_available_without_fabricating_groups(workbench):
    comparisons = workbench.list_cases(kind="comparison")
    assert len(comparisons) >= 2
    for case in comparisons:
        detail = workbench.case_detail(case["case_id"])
        assert detail["messages"]
        assert detail["case"]["kind"] == "comparison"
        if case["case_id"] in ("comparison-organic", "comparison-critic"):
            assert detail["stress_test"]["retained_pair_count"] == 0
        elif case["case_id"] == "comparison-open-activism":
            # Transparent human coordination is a legitimate positive finding.
            assert detail["stress_test"]["retained_pair_count"] > 0
            assert any("public volunteer call" in note for note in detail["counterevidence"])


def test_removing_strongest_family_recomputes_controls(workbench):
    candidate = workbench.list_cases(kind="candidate")[0]
    result = workbench.stress_test(candidate["case_id"])
    assert result["available"]
    assert result["excluded_family"]
    assert result["remaining_pairs"] <= result["original_pairs"]
    assert result["remaining_accounts"] <= result["original_accounts"]


def test_store_uses_working_directory_for_default(monkeypatch, tmp_path):
    monkeypatch.delenv("SWARM_DATA_DIR", raising=False)
    monkeypatch.delenv("SWARM_DB_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    assert Store().path == tmp_path / "data" / "swarm.db"


@pytest.mark.parametrize("operation", ["snapshot", "overview", "case_detail"])
def test_public_results_cannot_mutate_saved_analysis(workbench, operation):
    original = workbench.snapshot()
    if operation == "case_detail":
        case = workbench.list_cases()[0]
        result = workbench.case_detail(case["case_id"])
        result["messages"][0]["text_original"] = "Changed by a caller"
    else:
        result = getattr(workbench, operation)()
    if operation == "overview":
        result["config"]["seed"] = -1
        result["manifest"]["limitations"] = ["Changed by a caller"]
    else:
        result["run"]["config"]["seed"] = -1
        if operation == "snapshot":
            result["dataset"]["messages"].clear()
        else:
            assert "messages" not in result["dataset"]
            result["dataset"]["channels"].clear()
    assert workbench.snapshot() == original


def test_case_detail_keeps_its_captured_run_when_active_run_changes(workbench, monkeypatch):
    case_id = workbench.list_cases()[0]["case_id"]
    review = workbench.save_review(
        case_id, "Reviewer", "Insufficient evidence", "This review belongs to the first run."
    )
    original_run = review["run_id"]
    changed_run = workbench.run_analysis({"seed": 43})["run_id"]
    active_runs = iter([original_run, changed_run, changed_run])
    monkeypatch.setattr(workbench.store, "active_run_id", lambda: next(active_runs))

    detail = workbench.case_detail(case_id)

    assert detail["run"]["run_id"] == original_run
    assert detail["review"]["run_id"] == original_run
    assert detail["review"]["revision"] == review["revision"]
