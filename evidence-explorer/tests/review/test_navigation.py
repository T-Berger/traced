"""Contextual next actions move only within the current saved assessment."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy

import pytest

from evidence_explorer.config import Settings
from evidence_explorer.review.controller import select_assessment, session_for_case
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.drafts import draft_key
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.navigation import (
    NavigationContextError,
    NextAction,
    apply_navigation,
    contextual_next_action,
    navigation_context,
)
from evidence_explorer.review.workflow import assessment_context, assessment_decisions

PURPOSE = "cited_claim_investigation"


@pytest.fixture
def service(tmp_path):
    settings = Settings(data_dir=tmp_path / "navigation")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, settings.data_dir / "artifacts")
    service.seed_demo()
    return service


def snapshot(service):
    """Detect persistent writes, including jobs/events and overwritten scenario rows."""
    result = {}
    with sqlite3.connect(service.repository.db_path) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (name,) in tables:
            safe_name = '"' + name.replace('"', '""') + '"'
            rows = connection.execute("SELECT * FROM " + safe_name).fetchall()
            encoded = repr(sorted(rows, key=repr)).encode()
            result[name] = (len(rows), hashlib.sha256(encoded).hexdigest())
    return result


def view_for(service, state):
    return service.view(state["case_id"], state.get("scenario_id")) if state.get("case_id") else {}


def action_for(service, state, **kwargs):
    before = snapshot(service)
    original = deepcopy(state)
    view = view_for(service, state)
    original_view = deepcopy(view)
    action = contextual_next_action(view, state, **kwargs)
    assert state == original
    assert view == original_view
    assert snapshot(service) == before
    assert action.label and action.explanation
    button = action.button_id()
    assert button == {
        "type": "rv-next-action",
        "action": action.kind,
        "context": action.context,
        "fid": action.finding_id,
        "rid": action.record_id,
        "location": action.location,
    }
    return action


def navigate_without_writes(service, state, action, *, note=None):
    before, original = snapshot(service), deepcopy(state)
    button = action.button_id() if hasattr(action, "button_id") else action
    original_button = deepcopy(button)
    result = apply_navigation(service, state, button, note=note)
    assert state == original
    assert button == original_button
    assert result is not state
    assert snapshot(service) == before
    return result


def assert_rejected_without_writes(service, state, button):
    before, original = snapshot(service), deepcopy(state)
    with pytest.raises(NavigationContextError):
        apply_navigation(service, state, button)
    assert state == original
    assert snapshot(service) == before


def save_current(service, state, note="A saved qualification for this finding."):
    baseline = service.baseline_scenario(state["case_id"], state["finding_id"])
    service.review(
        state["case_id"],
        state["finding_id"],
        baseline["id"],
        "qualify",
        note,
        expected_version=baseline["version"],
        expected_fingerprint=state["case_fingerprint"],
    )
    return note


def save_all_current_findings(service, state):
    for finding in view_for(service, state)["bundle"]["findings"]:
        baseline = service.baseline_scenario(state["case_id"], finding["snapshot_id"])
        service.review(
            state["case_id"],
            finding["snapshot_id"],
            baseline["id"],
            "qualify",
            "Reviewed wording for " + finding["snapshot_id"],
        )


@pytest.mark.parametrize(
    ("purpose_id", "kind", "target"),
    [(None, "purpose", "trail-planning-question"), (PURPOSE, "intake", "trail-gather-evidence")],
)
def test_no_case_routes_to_purpose_then_intake_without_creating_work(
    service, purpose_id, kind, target
):
    state = session_for_case(service, None)
    action = action_for(service, state, page="cases", purpose_id=purpose_id)
    assert action.kind == kind
    result = navigate_without_writes(service, state, action)
    assert result["navigate_to"] == "cases"
    assert result["navigate_target"] == target
    assert result.get("case_id") is None


def test_cases_next_action_opens_an_exact_source_for_the_current_finding(service):
    state = session_for_case(service, load_demo_bundle().case.id)
    action = action_for(service, state, page="cases", purpose_id=PURPOSE)
    assert action.kind == "source"
    assert action.finding_id == state["finding_id"]
    assert (
        action.record_id
        in view_for(service, state)["impact"]["remaining_evidence"][action.finding_id]
    )
    result = navigate_without_writes(service, state, action)
    assert result["navigate_to"] == "review"
    assert result["navigate_target"] == "rv-source"
    assert result["finding_id"] == action.finding_id
    assert result["record_id"] == action.record_id


def test_two_mounts_have_unique_button_ids_but_share_the_same_read_only_navigation(service):
    state = session_for_case(service, load_demo_bundle().case.id)
    guidance = action_for(service, state, page="cases", purpose_id=PURPOSE)
    omission = NextAction(
        kind=guidance.kind,
        label=guidance.label,
        explanation=guidance.explanation,
        context=guidance.context,
        finding_id=guidance.finding_id,
        record_id=guidance.record_id,
        location="handoff-omission-0",
    )
    assert guidance.location == "guidance"
    assert guidance.context == omission.context
    assert guidance.context == navigation_context(view_for(service, state), state)
    buttons = [guidance.button_id(), omission.button_id()]
    assert len({json.dumps(button, sort_keys=True) for button in buttons}) == 2
    assert {key: value for key, value in buttons[0].items() if key != "location"} == {
        key: value for key, value in buttons[1].items() if key != "location"
    }
    first = navigate_without_writes(service, state, guidance)
    second = navigate_without_writes(service, state, omission)
    assert first == second
    assert first["finding_id"] == guidance.finding_id
    assert first["record_id"] == guidance.record_id


def test_stale_projection_cannot_create_a_public_context_or_recommendation(service):
    state = session_for_case(service, load_demo_bundle().case.id)
    bundle = service.get_case(state["case_id"])
    bundle["records"].append({**bundle["records"][0], "id": "navigation:later-record"})
    service.import_bundle(bundle)
    view = view_for(service, state)
    before, original = snapshot(service), deepcopy(state)
    with pytest.raises(NavigationContextError):
        navigation_context(view, state)
    with pytest.raises(NavigationContextError):
        contextual_next_action(view, state, page="cases", purpose_id=PURPOSE)
    assert state == original
    assert snapshot(service) == before


def test_review_next_action_keeps_an_undecided_current_finding(service):
    state = session_for_case(service, load_demo_bundle().case.id)
    action = action_for(service, state, page="review", purpose_id=PURPOSE)
    assert action.kind == "review"
    assert action.finding_id == state["finding_id"]
    result = navigate_without_writes(service, state, action)
    assert result["navigate_to"] == "review"
    assert result["navigate_target"] == "rv-review-reason"
    assert result["finding_id"] == state["finding_id"]


def test_saved_review_advances_to_another_undecided_finding_in_the_same_run(service):
    state = session_for_case(service, load_demo_bundle().case.id)
    note = save_current(service, state)
    view = view_for(service, state)
    action = action_for(service, state, page="review", purpose_id=PURPOSE, note=note)
    assert action.kind == "review"
    assert action.finding_id != state["finding_id"]
    assert action.finding_id not in assessment_decisions(view)
    finding = next(f for f in view["bundle"]["findings"] if f["snapshot_id"] == action.finding_id)
    assert finding["run_id"] == assessment_context(view)["base_run_id"]
    result = navigate_without_writes(service, state, action, note=note)
    assert result["finding_id"] == action.finding_id


def test_source_only_case_routes_to_source_comparison_without_inventing_a_finding(service):
    bundle = load_demo_bundle().model_dump(mode="json")
    bundle["case"]["id"] = "navigation:source-only"
    bundle["findings"], bundle["links"], bundle["reviews"], bundle["scenarios"] = [], [], [], []
    case_id = service.import_bundle(bundle)
    state = session_for_case(service, case_id)
    action = action_for(service, state, page="cases", purpose_id=PURPOSE)
    assert action.kind == "compare"
    assert not action.finding_id
    result = navigate_without_writes(service, state, action)
    assert result["navigate_to"] == "compare"
    assert result["navigate_target"] == "rv-source-heading"
    assert not result.get("finding_id")


def test_every_current_finding_reviewed_routes_to_brief(service):
    state = session_for_case(service, load_demo_bundle().case.id)
    save_all_current_findings(service, state)
    action = action_for(service, state, page="review", purpose_id=PURPOSE)
    assert action.kind == "brief"
    result = navigate_without_writes(service, state, action)
    assert result["navigate_to"] == "brief"
    assert result["navigate_target"] == "rv-brief-page"


def test_unsupported_finding_remains_reviewable_and_never_gets_an_invented_source(service):
    bundle = load_demo_bundle().model_dump(mode="json")
    bundle["case"]["id"] = "navigation:unsupported"
    bundle["findings"] = [bundle["findings"][-1]]
    bundle["links"], bundle["reviews"], bundle["scenarios"] = [], [], []
    case_id = service.import_bundle(bundle)
    state = session_for_case(service, case_id)
    action = action_for(service, state, page="cases", purpose_id=PURPOSE)
    assert action.kind == "review"
    assert action.finding_id == state["finding_id"]
    assert not action.record_id
    result = navigate_without_writes(service, state, action)
    assert result["finding_id"] == state["finding_id"]
    assert not result.get("record_id")


def test_active_unsaved_note_keeps_the_same_finding_and_late_click_cannot_leave_it(service):
    state = session_for_case(service, load_demo_bundle().case.id)
    saved_note = save_current(service, state)
    clean_action = action_for(service, state, page="review", purpose_id=PURPOSE, note=saved_note)
    assert clean_action.finding_id != state["finding_id"]
    dirty_note = "A new qualification entered since the next button was rendered."
    action = action_for(service, state, page="review", purpose_id=PURPOSE, note=dirty_note)
    assert action.kind == "review"
    assert action.finding_id == state["finding_id"]
    result = navigate_without_writes(service, state, clean_action, note=dirty_note)
    assert result["finding_id"] == state["finding_id"]
    assert result["record_id"] == state["record_id"]
    assert result["navigate_to"] == "review"
    assert result["navigate_target"] == "rv-review-reason"


def test_another_unsaved_note_resumes_only_its_exact_current_assessment(service):
    state = session_for_case(service, load_demo_bundle().case.id)
    saved_note = save_current(service, state)
    other = {**state, "finding_id": "harbor:c1-v1"}
    view = view_for(service, state)
    key = draft_key(other, view)
    drafts = {"active": draft_key(state, view), "notes": {key: "An unsaved observation note."}}
    action = action_for(
        service, state, page="review", purpose_id=PURPOSE, drafts=drafts, note=saved_note
    )
    assert action.kind == "review"
    assert action.finding_id == other["finding_id"]
    result = navigate_without_writes(service, state, action, note=saved_note)
    assert result["finding_id"] == other["finding_id"]


@pytest.mark.parametrize("scope_index", [0, 2, 3, 4])
def test_notes_from_another_context_are_not_resumed_as_current_work(service, scope_index):
    state = session_for_case(service, load_demo_bundle().case.id)
    saved_note = save_current(service, state)
    view = view_for(service, state)
    other = {**state, "finding_id": "harbor:c2-v1"}
    identity = json.loads(draft_key(other, view))
    identity[scope_index] = identity[scope_index] + 1 if scope_index == 3 else "old-context"
    drafts = {"notes": {json.dumps(identity, separators=(",", ":")): "An older scoped note."}}
    action = action_for(
        service, state, page="review", purpose_id=PURPOSE, drafts=drafts, note=saved_note
    )
    assert action.kind == "review"
    assert action.finding_id == "harbor:c1-v1"


def test_advisory_progress_cannot_replace_a_missing_human_decision(service):
    state = session_for_case(service, load_demo_bundle().case.id)
    progress = {"steps": [{"id": "human_review", "status": "completed"}]}
    original = deepcopy(progress)
    action = action_for(service, state, page="review", purpose_id=PURPOSE, progress=progress)
    assert action.kind == "review"
    assert action.finding_id == state["finding_id"]
    assert progress == original


@pytest.mark.parametrize("field", ["action", "context", "fid", "rid", "location"])
def test_arbitrary_browser_context_or_target_tampering_is_rejected_without_side_effects(
    service, field
):
    state = session_for_case(service, load_demo_bundle().case.id)
    button = action_for(service, state, page="cases", purpose_id=PURPOSE).button_id()
    button[field] = {
        "action": "delete_case",
        "context": "nav-v1:tampered",
        "fid": "missing-finding",
        "rid": "record-from-another-case",
        "location": "",
    }[field]
    assert_rejected_without_writes(service, state, button)


def test_existing_but_unlinked_record_is_not_opened_as_a_findings_exact_source(service):
    case_id = load_demo_bundle().case.id
    bundle = service.get_case(case_id)
    bundle["records"].append({**bundle["records"][0], "id": "navigation:unlinked-copy"})
    service.import_bundle(bundle)
    state = session_for_case(service, case_id)
    button = action_for(service, state, page="cases", purpose_id=PURPOSE).button_id()
    button["rid"] = "navigation:unlinked-copy"
    assert_rejected_without_writes(service, state, button)


def test_context_only_source_action_preserves_the_assessment_without_creating_a_support_link(
    service,
):
    case_id = load_demo_bundle().case.id
    bundle = service.get_case(case_id)
    record_id = "navigation:context-only-record"
    bundle["records"].append({**bundle["records"][0], "id": record_id})
    service.import_bundle(bundle)
    state = session_for_case(service, case_id)
    scenario = service.start_scenario(case_id, state["finding_id"])
    select_assessment(service, state, scenario["id"])
    view = view_for(service, state)
    assert record_id not in view["impact"]["remaining_evidence"][state["finding_id"]]
    action = NextAction(
        kind="source",
        label="Inspect saved context",
        explanation="This saved record is context, not declared support for the finding.",
        context=navigation_context(view, state),
        record_id=record_id,
        location="handoff-source-0",
    )
    assert action.finding_id == ""
    links = deepcopy(service.get_case(case_id)["links"])
    result = navigate_without_writes(service, state, action)
    assert result["navigate_to"] == "review"
    assert result["navigate_target"] == "rv-source"
    assert result["finding_id"] == state["finding_id"]
    assert result["record_id"] == record_id
    assert result["scenario_id"] == state["scenario_id"]
    assert result["scenario_version"] == state["scenario_version"]
    assert result["case_fingerprint"] == state["case_fingerprint"]
    assert service.get_case(case_id)["links"] == links


def test_a_stored_run_identity_must_match_the_current_assessment(service):
    state = session_for_case(service, load_demo_bundle().case.id)
    button = action_for(service, state, page="cases", purpose_id=PURPOSE).button_id()
    state["base_run_id"] = "navigation:old-run"
    assert_rejected_without_writes(service, state, button)


def test_old_case_action_cannot_open_its_target_after_the_case_switches(service):
    state = session_for_case(service, load_demo_bundle().case.id)
    button = action_for(service, state, page="cases", purpose_id=PURPOSE).button_id()
    other = load_demo_bundle().model_dump(mode="json")
    other["case"]["id"] = "navigation:other-case"
    case_id = service.import_bundle(other)
    switched = session_for_case(service, case_id)
    assert_rejected_without_writes(service, switched, button)


def test_old_assessment_action_cannot_be_reused_in_a_separate_scenario(service):
    state = session_for_case(service, load_demo_bundle().case.id)
    first = service.start_scenario(state["case_id"], state["finding_id"])
    select_assessment(service, state, first["id"])
    button = action_for(service, state, page="cases", purpose_id=PURPOSE).button_id()
    second = service.start_scenario(state["case_id"], state["finding_id"])
    switched = deepcopy(state)
    select_assessment(service, switched, second["id"])
    assert_rejected_without_writes(service, switched, button)


@pytest.mark.parametrize("refresh_state", [False, True])
def test_an_exclusion_version_change_rejects_old_state_and_old_action_context(
    service, refresh_state
):
    state = session_for_case(service, load_demo_bundle().case.id)
    scenario = service.start_scenario(state["case_id"], state["finding_id"])
    select_assessment(service, state, scenario["id"])
    button = action_for(service, state, page="cases", purpose_id=PURPOSE).button_id()
    service.exclude(
        scenario["id"], state["record_id"], True, "Test a changed assessment.", scenario["version"]
    )
    if refresh_state:
        select_assessment(service, state, scenario["id"])
    assert_rejected_without_writes(service, state, button)


@pytest.mark.parametrize("refresh_state", [False, True])
def test_changed_evidence_fingerprint_rejects_old_state_and_old_action_context(
    service, refresh_state
):
    state = session_for_case(service, load_demo_bundle().case.id)
    button = action_for(service, state, page="cases", purpose_id=PURPOSE).button_id()
    bundle = service.get_case(state["case_id"])
    bundle["records"].append({**bundle["records"][0], "id": "navigation:added-record"})
    service.import_bundle(bundle)
    if refresh_state:
        state = session_for_case(service, state["case_id"])
    assert_rejected_without_writes(service, state, button)


def test_an_unreviewed_finding_from_another_run_is_not_a_navigation_candidate(service):
    state = session_for_case(service, load_demo_bundle().case.id)
    bundle = service.get_case(state["case_id"])
    bundle["runs"].append({**bundle["runs"][0], "id": "navigation:other-run"})
    bundle["findings"].append(
        {
            **bundle["findings"][0],
            "snapshot_id": "navigation:other-run-finding",
            "lineage_id": "navigation:other-lineage",
            "run_id": "navigation:other-run",
        }
    )
    service.import_bundle(bundle)
    state = session_for_case(service, state["case_id"])
    save_all_current_findings(service, state)
    action = action_for(service, state, page="review", purpose_id=PURPOSE)
    assert action.kind == "brief"
    result = navigate_without_writes(service, state, action)
    assert result["finding_id"] != "navigation:other-run-finding"
    invalid_target = action.button_id()
    invalid_target.update(action="review", fid="navigation:other-run-finding", rid="")
    assert_rejected_without_writes(service, state, invalid_target)
    invalid_active = {**state, "finding_id": "navigation:other-run-finding"}
    assert_rejected_without_writes(service, invalid_active, action.button_id())
