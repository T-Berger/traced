"""A recipient can inspect exact saved comparisons without creating local review state."""

import hashlib
import json
import sqlite3
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest

from evidence_explorer.config import Settings
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.controller import select_assessment, session_for_case
from evidence_explorer.review.integrations.web import create_app
from evidence_explorer.review.integrations.worker import WorkspaceWorker
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.navigation import NavigationContextError, apply_navigation
from evidence_explorer.review.saved_comparison import project_saved_comparison
from evidence_explorer.review.workflow import assessment_decisions
from evidence_explorer.review.workspace_views import saved_comparison_summary
from tests.integrations.test_handoff_flow import components, post_callback, text

PACKAGE = (
    Path(__file__).resolve().parents[1] / "fixtures/case_packages/evidence-trail-reviewed-swarm.zip"
)
PACKAGE_SHA256 = "db29d709848bfbc74e83b6a79dd99dd795e3532fccc23eed005b58bcb0379268"
CASE = "swarm:2b1d8ee8e7de:case"
SAVED_SCENARIO = CASE + ":scenario:e178636a7df2258db87c65b0"
BASE_RUN = "swarm:2b1d8ee8e7de:run:8c23e4f3ea8a92e92eb1bc64"
RESULT_RUN = "swarm:2b1d8ee8e7de:run:80899f5c2d104593c794a705"
REVIEW_ALIAS = "review:f8f7fc7b-d18d-4527-8e11-1293fad2e76e:v2"
RESULT_REVIEW = "review:834fd03c-f78f-4904-8f07-213e0dfee054:v1"


@pytest.fixture(scope="module")
def supplied():
    raw = PACKAGE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PACKAGE_SHA256
    with zipfile.ZipFile(PACKAGE) as package:
        bundle = json.loads(package.read("bundle.json"))
    return raw, bundle


@pytest.fixture
def recipient(tmp_path, supplied):
    settings = Settings(data_dir=tmp_path / "recipient")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, settings.data_dir / "artifacts")
    assert service.import_package(supplied[0]) == CASE
    return service


def table_hashes(service):
    """Hash every saved row in every table, including new/empty tables and metadata."""
    result = {}
    with sqlite3.connect(f"file:{service.settings.db_path}?mode=ro", uri=True) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        for (name,) in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            cursor = connection.execute(f"SELECT * FROM {quoted}")
            rows = sorted(
                json.dumps(
                    list(row),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=lambda value: {"bytes": value.hex()},
                )
                for row in cursor.fetchall()
            )
            payload = json.dumps(
                {"columns": [column[0] for column in cursor.description], "rows": rows},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            result[name] = {"count": len(rows), "sha256": hashlib.sha256(payload).hexdigest()}
    return result


def native_scenario(bundle):
    return next(row for row in bundle["scenarios"] if row["id"] == SAVED_SCENARIO)


def source_actions(node):
    return [
        component.id
        for component in components(node)
        if isinstance(getattr(component, "id", None), dict)
        and component.id.get("type") == "rv-next-action"
        and component.id.get("action") == "source"
    ]


def test_fresh_import_exposes_exact_native_relationships_and_evidence_without_local_job(
    recipient, supplied
):
    before = table_hashes(recipient)
    original_bundle = recipient.export_bundle(CASE)
    assert original_bundle == EvidenceBundle.model_validate(supplied[1])
    view = recipient.view(CASE, SAVED_SCENARIO)
    projection = view["saved_comparison"]
    assert projection["origin"] == "imported_saved"
    assert view["scenario"]["locally_computed"] is False
    assert view["scenario"]["label"].startswith("Imported assessment ")
    assert projection["status"] == "ready"
    assert projection["result_available"] is True
    assert projection["baseline_run_id"] == BASE_RUN
    assert projection["result_run_id"] == RESULT_RUN
    assert view.get("computation") is None
    assert assessment_decisions(view) == {}
    expected = native_scenario(supplied[1])["correspondences"]
    assert len(projection["correspondences"]) == len(expected) == 2
    rendered_text = text(saved_comparison_summary(projection, view))
    assert projection["label"] in rendered_text
    assert BASE_RUN in rendered_text and RESULT_RUN in rendered_text
    saved_findings = {row["snapshot_id"]: row for row in supplied[1]["findings"]}
    saved_records = {row["id"]: row for row in supplied[1]["records"]}
    for row, original in zip(projection["correspondences"], expected, strict=True):
        assert row["status"] == "valid"
        for key in ("relation", "basis", "baseline_snapshot_ids", "result_snapshot_ids"):
            assert row[key] == original[key]
        assert row["basis"] in rendered_text
        for side, run_id in (("baseline_findings", BASE_RUN), ("result_findings", RESULT_RUN)):
            for finding in row[side]:
                assert finding["run_id"] == run_id
                assert finding["statement"] == saved_findings[finding["snapshot_id"]]["statement"]
                assert finding["snapshot_id"] in rendered_text
                assert finding["evidence"], "Both saved sides have inspectable source support"
                for evidence in finding["evidence"]:
                    record = saved_records[evidence["record_id"]]
                    assert evidence["source_label"] == record["source_label"]
                    selector = evidence.get("selector") or {}
                    if selector.get("kind") == "text":
                        assert (
                            evidence["quote"] == record["text"][selector["start"] : selector["end"]]
                        )
    assert recipient.export_bundle(CASE) == original_bundle
    assert table_hashes(recipient) == before
    assert hashlib.sha256(PACKAGE.read_bytes()).hexdigest() == PACKAGE_SHA256


def test_review_version_alias_does_not_inherit_native_correspondences_or_transfer_approval(
    recipient,
):
    before = table_hashes(recipient)
    alias = recipient.view(CASE, REVIEW_ALIAS)
    projection = alias["saved_comparison"]
    assert projection["status"] == "missing_correspondences"
    assert projection["result_available"] is True
    assert projection["correspondences"] == []
    assert assessment_decisions(alias) == {}
    saved_review = recipient.view(CASE, RESULT_REVIEW)
    decisions = assessment_decisions(saved_review)
    assert len(decisions) == 1
    assert next(iter(decisions.values()))["disposition"] == "qualify"
    assert saved_review["saved_comparison"]["result_available"] is False
    assert table_hashes(recipient) == before


@pytest.mark.parametrize(
    ("status", "expected"),
    [("dependency_preview", "preview"), ("failed", "failed"), ("running", "incomplete")],
)
def test_unfinished_saved_scenarios_do_not_present_a_completed_result(supplied, status, expected):
    bundle = deepcopy(supplied[1])
    native_scenario(bundle)["status"] = status
    projection = project_saved_comparison(bundle, SAVED_SCENARIO, imported=True)
    assert projection["status"] == expected
    assert projection["result_available"] is False


@pytest.mark.parametrize("result_id", [None, "missing:run", 17, [RESULT_RUN]])
def test_absent_or_malformed_saved_result_run_stays_unavailable(supplied, result_id):
    bundle = deepcopy(supplied[1])
    native_scenario(bundle)["result_run_id"] = result_id
    projection = project_saved_comparison(bundle, SAVED_SCENARIO, imported=True)
    assert projection["status"] in {"missing_result", "invalid"}
    assert projection["result_available"] is False
    if projection["status"] == "missing_result":
        assert "result run is not available" in projection["summary"]
    else:
        assert projection["issues"]


@pytest.mark.parametrize("absent", [True, False])
def test_missing_correspondence_does_not_guess_relationships_from_matching_finding_names(
    supplied, absent
):
    bundle = deepcopy(supplied[1])
    scenario = native_scenario(bundle)
    if absent:
        del scenario["correspondences"]
    else:
        scenario["correspondences"] = []
    projection = project_saved_comparison(bundle, SAVED_SCENARIO, imported=True)
    assert projection["status"] == "missing_correspondences"
    assert projection["result_available"] is True
    assert projection["correspondences"] == []
    assert len(projection["result_findings"]) == 2


@pytest.mark.parametrize(
    ("scenario_id", "status"), [(None, "not_selected"), ("missing:scenario", "not_saved")]
)
def test_unselected_or_unknown_scenario_does_not_borrow_an_available_comparison(
    supplied, scenario_id, status
):
    projection = project_saved_comparison(supplied[1], scenario_id, imported=True)
    assert projection["status"] == status
    assert projection["result_available"] is False
    assert projection["correspondences"] == []


@pytest.mark.parametrize(
    "change", ["missing_finding", "wrong_run", "invalid_relation", "bad_basis"]
)
def test_invalid_relationship_is_visible_without_contaminating_valid_saved_row(supplied, change):
    bundle = deepcopy(supplied[1])
    first = native_scenario(bundle)["correspondences"][0]
    if change == "missing_finding":
        first["baseline_snapshot_ids"] = ["missing:finding"]
    elif change == "wrong_run":
        first["baseline_snapshot_ids"] = first["result_snapshot_ids"][:]
    elif change == "invalid_relation":
        first["relation"] = "proven_identity"
    else:
        first["basis"] = {"unsupported": "basis object"}
    before = deepcopy(bundle)
    projection = project_saved_comparison(bundle, SAVED_SCENARIO, imported=True)
    assert projection["status"] == "partial"
    assert projection["correspondences"][0]["status"] == "invalid"
    assert projection["correspondences"][0]["issues"]
    assert projection["correspondences"][1]["status"] == "valid"
    assert (
        projection["correspondences"][1]["basis"]
        == native_scenario(supplied[1])["correspondences"][1]["basis"]
    )
    assert bundle == before


def test_uncertain_correspondence_retains_supplied_basis_without_identity_or_approval_claim(
    supplied,
):
    bundle = deepcopy(supplied[1])
    first = native_scenario(bundle)["correspondences"][0]
    first["relation"] = "uncertain"
    first["basis"] = "Shared identifiers alone do not establish that these groups are the same."
    first["uncertainty"] = "The sender did not establish persistent group identity."
    projection = project_saved_comparison(bundle, SAVED_SCENARIO, imported=True)
    row = projection["correspondences"][0]
    assert row["relation"] == "uncertain"
    assert row["basis"] == first["basis"]
    assert row["uncertainty"] == first["uncertainty"]
    assert row["status"] == "valid"
    assert first["uncertainty"] in text(saved_comparison_summary(projection, {}))


@pytest.mark.parametrize("change", ["altered_quote", "wrong_record"])
def test_invalid_saved_citation_is_flagged_and_never_rendered_as_verified_evidence(
    supplied, change
):
    bundle = deepcopy(supplied[1])
    original = project_saved_comparison(bundle, SAVED_SCENARIO, imported=True)
    finding = original["baseline_findings"][0]
    cited = next(row for row in finding["evidence"] if row["fragment_id"])
    fragment = next(row for row in bundle["fragments"] if row["id"] == cited["fragment_id"])
    if change == "altered_quote":
        fragment["quote"] += " This was not in the saved source."
    else:
        fragment["record_id"] = "missing:record"
    projection = project_saved_comparison(bundle, SAVED_SCENARIO, imported=True)
    projected = next(
        row
        for row in projection["baseline_findings"]
        if row["snapshot_id"] == finding["snapshot_id"]
    )
    assert projected["issues"]
    assert cited["fragment_id"] not in {row["fragment_id"] for row in projected["evidence"]}
    assert all(
        "This was not in the saved source." not in (row["quote"] or "")
        for row in projected["evidence"]
    )


def test_imported_result_and_exact_source_navigation_leave_every_saved_table_unchanged(recipient):
    state = session_for_case(recipient, CASE)
    select_assessment(recipient, state, SAVED_SCENARIO)
    app = create_app(recipient)
    # Normal app bootstrap installs its existing claim-check schema before the
    # read-only user actions whose complete table contents are audited here.
    before = table_hashes(recipient)
    original = recipient.export_bundle(CASE)
    compared = post_callback(
        app, "trail-comparison.children", "rv-session", {"rv-session": state, "trail-tick": 0}
    )
    assert compared["trail-recompute"]["disabled"] is True
    assert compared["trail-result"]["disabled"] is False
    assert compared["trail-tick"]["disabled"] is True
    response = post_callback(
        app, "trail-operation-message.children", "trail-result", {"rv-session": state}
    )
    opened = response["rv-session"]["data"]
    assert opened["navigate_to"] == "compare"
    assert opened["navigate_target"] == "rv-compare-summary"
    assert all(opened[key] == value for key, value in state.items())
    view = recipient.view(CASE, SAVED_SCENARIO)
    rendered = saved_comparison_summary(view["saved_comparison"], view)
    actions = source_actions(rendered)
    assert actions
    assert len({json.dumps(action, sort_keys=True) for action in actions}) == len(actions)
    assert all(action["fid"] == "" for action in actions)
    assert {action["rid"] for action in actions} <= {
        record["id"] for record in view["bundle"]["records"]
    }
    for action in (actions[0], actions[-1]):
        navigated = apply_navigation(recipient, opened, action)
        assert navigated["record_id"] == action["rid"]
        assert navigated["finding_id"] == state["finding_id"]
        assert navigated["scenario_id"] == SAVED_SCENARIO
        assert navigated["navigate_to"] == "review"
    assert recipient.export_bundle(CASE) == original
    assert table_hashes(recipient) == before


def test_comparison_source_buttons_bind_current_assessment_and_preserve_dirty_note(recipient):
    state = session_for_case(recipient, CASE)
    current = recipient.view(CASE)
    saved = recipient.view(CASE, SAVED_SCENARIO)["saved_comparison"]
    before = table_hashes(recipient)
    action = source_actions(saved_comparison_summary(saved, current))[0]
    assert action["fid"] == ""
    opened = apply_navigation(recipient, state, action)
    assert opened["record_id"] == action["rid"]
    assert opened["scenario_id"] is None
    derived_finding = saved["result_findings"][0]["snapshot_id"]
    with pytest.raises(NavigationContextError, match="outside"):
        apply_navigation(recipient, state, {**action, "fid": derived_finding})
    for stale_state in (
        {**state, "case_id": "missing:case"},
        {**state, "case_fingerprint": "stale-fingerprint"},
        {**state, "scenario_version": 2},
    ):
        with pytest.raises(NavigationContextError):
            apply_navigation(recipient, stale_state, action)
    switched = dict(state)
    select_assessment(recipient, switched, SAVED_SCENARIO)
    with pytest.raises(NavigationContextError, match="earlier"):
        apply_navigation(recipient, switched, action)
    diverted = apply_navigation(recipient, state, action, note="A note still being edited.")
    assert diverted["finding_id"] == state["finding_id"]
    assert diverted["record_id"] == state["record_id"]
    assert diverted["navigate_to"] == "review"
    assert diverted["navigate_target"] == "rv-review-reason"
    assert table_hashes(recipient) == before


def test_local_completed_job_remains_distinct_from_imported_saved_analysis(recipient):
    original = recipient.repository.get_bundle(CASE)
    finding = next(row for row in original.findings if row.run_id == BASE_RUN)
    scenario = recipient.start_scenario(CASE, finding.snapshot_id)
    job = recipient.repository.enqueue_job(
        CASE, finding.snapshot_id, scenario["id"], "swarm_recompute"
    )
    running = recipient.repository.claim_job()
    assert recipient.view(CASE, SAVED_SCENARIO)["scenario"]["locally_computed"] is False
    recipient.repository.finish_job(
        job["id"],
        {
            "kind": "swarm_recompute",
            "scenario_id": scenario["id"],
            "scenario_version": scenario["version"],
            "result_run_id": RESULT_RUN,
            "computed_scenario_id": SAVED_SCENARIO,
            "cached": False,
            "correspondences": native_scenario(original.model_dump(mode="json"))["correspondences"],
        },
        expected_attempt=running["attempts"],
    )
    state = session_for_case(recipient, CASE)
    select_assessment(recipient, state, scenario["id"])
    app = create_app(recipient)
    before = table_hashes(recipient)
    view = recipient.view(CASE, scenario["id"])
    assert view["computation"]["id"] == job["id"]
    assert view["computation"]["status"] == "completed"
    response = post_callback(
        app,
        "trail-comparison.children",
        "rv-session",
        {"rv-session": state, "trail-tick": 0},
    )
    assert response["trail-result"]["disabled"] is False
    assert response["trail-tick"]["disabled"] is True
    assert "Completed Swarm recomputation" in json.dumps(response)

    select_assessment(recipient, state, SAVED_SCENARIO)
    saved = recipient.view(CASE, SAVED_SCENARIO)
    assert saved["scenario"]["label"].startswith("Locally computed assessment ")
    assert saved["scenario"]["locally_computed"] is True
    assert saved["scenario"]["imported"] is True  # The native scenario remains read-only.
    assert saved["saved_comparison"]["origin"] == "locally_computed"
    assert saved["saved_comparison"]["label"] == "Locally computed Swarm analysis"
    assert saved.get("computation") is None
    assert not recipient.view(CASE, REVIEW_ALIAS)["scenario"]["locally_computed"]
    response = post_callback(
        app, "trail-comparison.children", "rv-session", {"rv-session": state, "trail-tick": 0}
    )
    assert response["trail-recompute"]["disabled"] is True
    assert response["trail-result"]["disabled"] is False
    assert "Locally computed Swarm analysis" in json.dumps(response)
    response = post_callback(
        app, "trail-operation-message.children", "trail-result", {"rv-session": state}
    )
    assert response["rv-session"]["data"]["navigate_target"] == "rv-compare-summary"
    assert "Inspecting the saved locally computed result." in json.dumps(response)
    assert table_hashes(recipient) == before


@pytest.mark.parametrize("prior_history", ["none", "legacy", "locally_computed"])
def test_replaying_an_imported_saved_result_reuses_it_without_running_detector(
    recipient, supplied, monkeypatch, prior_history
):
    from evidence_explorer.review.integrations import swarm

    def detector_forbidden():
        pytest.fail("The matching saved replay must reuse its result without the detector.")

    monkeypatch.setattr(swarm, "_swarm_modules", detector_forbidden)
    native = native_scenario(supplied[1])
    original = recipient.export_bundle(CASE)
    finding = next(row for row in original.findings if row.run_id == BASE_RUN)
    scenario = recipient.start_scenario(CASE, finding.snapshot_id)
    (record_id,) = native["requested_excluded_record_ids"]
    scenario = recipient.exclude(
        scenario["id"],
        record_id,
        True,
        "Test the strongest observed episode family.",
        scenario["version"],
    )
    key = (SAVED_SCENARIO, BASE_RUN, RESULT_RUN)
    locally_computed = prior_history == "locally_computed"
    expected_proof = True if locally_computed else None
    worker = WorkspaceWorker(recipient.repository, recipient.settings)
    if prior_history != "none":
        previous = recipient.queue_recomputation(CASE, finding.snapshot_id, scenario["id"])
        running = recipient.repository.claim_job_state()
        if locally_computed:
            # Supply a frozen detector output to exercise the new-result branch without a run.
            without_native = original.model_copy(
                update={
                    "scenarios": [row for row in original.scenarios if row.id != SAVED_SCENARIO]
                }
            )
            with monkeypatch.context() as adapter:
                adapter.setattr(swarm, "recompute_swarm_scenario", lambda *_: original)
                previous_result, updated = worker._compute_review(without_native, previous.payload)
            assert previous_result["cached"] is False
            assert updated is original
        else:
            previous_result = {
                "kind": "swarm_recompute",
                "scenario_id": scenario["id"],
                "scenario_version": scenario["version"],
                "result_run_id": RESULT_RUN,
                "computed_scenario_id": SAVED_SCENARIO,
            }
        recipient.repository.finish_job(
            previous.id, previous_result, expected_attempt=running.attempts
        )
        assert recipient.repository.jobs.completed_swarm_results(CASE) == {key: expected_proof}
        assert (
            recipient.view(CASE, SAVED_SCENARIO)["scenario"]["locally_computed"] is expected_proof
        )

    queued = recipient.queue_recomputation(CASE, finding.snapshot_id, scenario["id"])
    running = recipient.repository.claim_job_state()
    assert running.id == queued.id
    before = table_hashes(recipient)
    worker._run_review(running)
    completed = recipient.repository.get_job_state(queued.id)
    assert completed.status == "completed", completed.error
    assert completed.result.cached is True
    assert completed.result.computed_scenario_id == SAVED_SCENARIO
    assert completed.result.result_run_id == RESULT_RUN
    assert [row.model_dump(mode="json") for row in completed.result.correspondences] == native[
        "correspondences"
    ]
    assert recipient.repository.jobs.completed_swarm_results(CASE) == {key: expected_proof}

    saved = recipient.view(CASE, SAVED_SCENARIO)
    assert saved["scenario"]["locally_computed"] is expected_proof
    assert saved["scenario"]["imported"] is True
    assert saved["scenario"]["label"].startswith(
        "Locally computed assessment " if locally_computed else "Saved assessment "
    )
    assert saved["saved_comparison"]["label"] == (
        "Locally computed Swarm analysis" if locally_computed else "Saved Swarm analysis"
    )
    assert assessment_decisions(saved) == {}

    state = session_for_case(recipient, CASE)
    select_assessment(recipient, state, scenario["id"])
    app = create_app(recipient)
    compared = post_callback(
        app,
        "trail-comparison.children",
        "rv-session",
        {
            "rv-session": state,
            "trail-tick": 0,
        },
    )
    assert "Reused the matching saved Swarm result." in json.dumps(compared)
    assert "Completed Swarm recomputation" not in json.dumps(compared)
    assert compared["trail-tick"]["disabled"] is True
    select_assessment(recipient, state, SAVED_SCENARIO)
    inspected = post_callback(
        app, "trail-operation-message.children", "trail-result", {"rv-session": state}
    )
    assert inspected["rv-session"]["data"]["navigate_target"] == "rv-compare-summary"
    expected_notice = (
        "Inspecting the saved locally computed result."
        if locally_computed
        else "Inspecting the saved result."
    )
    assert expected_notice in json.dumps(inspected)
    assert recipient.export_bundle(CASE).model_dump(mode="json")["runs"] == supplied[1]["runs"]
    after = table_hashes(recipient)
    assert {name for name in before if before[name] != after[name]} == {"review_jobs"}


def test_imported_scenario_metadata_cannot_claim_a_local_computation(tmp_path, supplied):
    bundle = deepcopy(supplied[1])
    native_scenario(bundle)["locally_computed"] = True
    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, settings.data_dir / "artifacts")
    assert repository.import_bundle(EvidenceBundle.model_validate(bundle)) == CASE
    view = service.view(CASE, SAVED_SCENARIO)
    assert view["scenario"]["locally_computed"] is False
    assert view["scenario"]["label"].startswith("Imported assessment ")
    assert view["saved_comparison"]["origin"] == "imported_saved"
