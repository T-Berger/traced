import json
from copy import deepcopy

import pytest

from swarm_detector.service import Workbench


@pytest.fixture
def workbench(tmp_path):
    wb = Workbench(tmp_path / "reviews.db")
    wb.ensure_sample()
    return wb


def test_revision_compare_and_swap_rejects_stale_edits_without_appending(workbench):
    case_id = workbench.list_cases()[0]["case_id"]
    first = workbench.save_review(
        case_id,
        "Reviewer",
        "Insufficient evidence",
        "Original source assessment.",
        expected_revision=0,
    )
    with pytest.raises(ValueError, match="changed since you opened"):
        workbench.save_review(
            case_id,
            "Other reviewer",
            "Coordination supported",
            "A stale competing source assessment.",
            expected_revision=0,
        )
    latest = workbench.get_review(case_id)
    assert latest["revision"] == first["revision"]
    assert len(workbench.store.review_history(first["run_id"], case_id)) == 1
    second = workbench.save_review(
        case_id,
        "Reviewer",
        "Insufficient evidence",
        "Updated source assessment.",
        expected_revision=first["revision"],
    )
    assert second["revision"] > first["revision"]


def test_preview_and_export_pin_saved_revision_across_further_edits_and_run_switch(workbench):
    case_id = workbench.list_cases()[0]["case_id"]
    first = workbench.save_review(
        case_id,
        "Reviewer",
        "Insufficient evidence",
        "Assessment in the previewed saved revision.",
        expected_revision=0,
    )
    preview = workbench.brief_preview(case_id)
    workbench.save_review(
        case_id,
        "Reviewer",
        "Coordination supported",
        "A subsequent saved revision that must not replace the preview.",
        expected_revision=first["revision"],
    )
    workbench.run_analysis({"seed": 43})
    content = workbench.export_case(case_id, run_id=preview["run_id"], revision=preview["revision"])
    assert content == preview["content"]
    wire = json.loads(
        workbench.export_case(case_id, "json", run_id=preview["run_id"], revision=first["revision"])
    )
    assert wire["review"]["revision"] == first["revision"]
    assert wire["run_id"] == first["run_id"]
    assert workbench.get_review(case_id, run_id=first["run_id"], revision=0) is None
    assert workbench.case_detail(case_id, run_id=first["run_id"], revision=0)["review"] is None


def test_import_invalidates_current_readiness_but_pinned_old_run_remains_accessible(workbench):
    previous = workbench.snapshot()
    run_id = previous["run"]["run_id"]
    case_id = workbench.list_cases()[0]["case_id"]
    imported = deepcopy(previous["dataset"])
    imported.update(dataset_id="new-import", synthetic=False)
    workbench.store.put_dataset(imported)
    assert workbench.store.active_run_id() is None
    assert workbench.overview()["run_id"] is None
    assert workbench.readiness()["run_id"] is None
    assert workbench.ensure_sample()["run_id"] is None
    assert workbench.overview(run_id=run_id)["run_id"] == run_id
    assert workbench.list_cases(run_id=run_id)[0]["case_id"] == case_id
    assert workbench.case_detail(case_id, run_id=run_id)["run"]["run_id"] == run_id
    # A late completion is retained for its source but cannot activate on the new source.
    workbench.store.put_run(previous["run"])
    assert workbench.store.active_run_id() is None


def test_revision_from_another_case_cannot_be_exported(workbench):
    first, other = workbench.list_cases()[:2]
    saved = workbench.save_review(
        other["case_id"],
        "Reviewer",
        "Insufficient evidence",
        "A source assessment for another case.",
    )
    with pytest.raises(ValueError, match="does not belong"):
        workbench.export_case(first["case_id"], revision=saved["revision"])
