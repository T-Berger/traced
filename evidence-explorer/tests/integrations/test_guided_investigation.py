"""A saved investigation continues into gathered evidence without implied approval.

All source observations are constructed offline fixtures. No collector, model,
or worker process runs in this suite.
"""

from copy import deepcopy

import pytest

from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.integrations.brief_editor_forms import PREFIX
from evidence_explorer.review.integrations.brief_editor_state import EditorStore
from evidence_explorer.review.integrations.navigation_ui import render_next_action
from evidence_explorer.review.integrations.web import create_app
from evidence_explorer.review.navigation import apply_navigation
from evidence_explorer.review.workflow import assessment_context, assessment_decisions
from tests.integrations.test_collection_scope_ui import add_source, observation, offline_source
from tests.integrations.test_navigation_ui import action_button
from tests.integrations.test_planning_brief_ui import browser_values, post_callback
from tests.integrations.test_planning_workspace import confirmed_plan, workspace
from tests.review.test_navigation import snapshot


@pytest.fixture
def desk(tmp_path):
    service = workspace(tmp_path / "guided-workspace")
    return service, create_app(service)


def working(values):
    store = EditorStore.model_validate(values[PREFIX + "edit.data"])
    return store.for_case(values["rv-session.data"].get("case_id"))


def edit(app, values, action):
    key = PREFIX + action + ".n_clicks"
    values[key] = (values.get(key) or 0) + 1
    response = post_callback(app, PREFIX + "edit.data", key, values)
    if PREFIX + "refresh" in response:
        post_callback(app, PREFIX + "picker.options", PREFIX + "refresh.data", values)
    post_callback(app, PREFIX + "status.children", PREFIX + "edit.data", values)
    return response


def select_case(app, values, state):
    values["rv-session.data"] = state
    post_callback(app, PREFIX + "case.data", "rv-session.data", values)
    post_callback(app, PREFIX + "edit.data", "rv-session.data", values)
    post_callback(app, PREFIX + "picker.options", "rv-session.data", values)


def save_plan(app, values):
    fields = {
        "purpose": "cited_claim_investigation",
        "question": "What do the saved bridge notices establish?",
        "topics": "Public bridge inspection",
        "role": "Evidence reviewer",
        "output": "reviewed_brief",
    }
    values.update({PREFIX + name + ".value": value for name, value in fields.items()})
    edit(app, values, "save")
    return working(values)


def navigation(service, values, *, page="cases", note=None):
    before = snapshot(service)
    original = deepcopy(values)
    button = action_button(
        render_next_action(
            service,
            values["rv-session.data"],
            page,
            None,
            note=note,
            editor=values.get(PREFIX + "edit.data"),
        )
    )
    result = apply_navigation(service, values["rv-session.data"], button.id, note=note)
    assert snapshot(service) == before
    assert values == original
    return button, result


def gathered_case(service, app, values):
    body = "Constructed fixture: the public bridge will be inspected on Monday."
    first = offline_source(service, "first", item=observation(key="one", body=body))
    second = offline_source(service, "second", item=observation(key="two", body=body))
    empty = offline_source(service, "empty")
    failed = add_source(service, "Failed fixture source", failed=True)
    unrelated = offline_source(
        service, "unrelated", item=observation(key="other", body="Unselected fixture text.")
    )
    selected = [first, second, empty, failed]
    values["live-case-sources.value"] = selected
    values["live-case-preview-action.n_clicks"] = 1
    before = snapshot(service)
    post_callback(app, "live-case-preview.children", "live-case-preview-action.n_clicks", values)
    assert snapshot(service) == before
    assert values["live-case-create.disabled"] is False
    values["live-case-create.n_clicks"] = 1
    create_key = next(
        key
        for key, definition in app.callback_map.items()
        if "rv-import-success.children" in key
        and any(item["id"] == "live-case-create" for item in definition["inputs"])
    )
    post_callback(app, create_key, "live-case-create.n_clicks", values)
    state = values["rv-session.data"]
    assert state["case_id"] and values["rv-page.value"] == "review"
    select_case(app, values, state)
    return state["case_id"], selected, unrelated


def test_saved_plan_continues_through_gathered_scope_to_first_undecided_finding(desk):
    service, app = desk
    values = browser_values()
    saved = save_plan(app, values)
    pointer = values[PREFIX + "edit.data"]["continuation"]
    assert pointer["brief_id"] == saved.brief_id
    assert pointer["revision_id"] == saved.selected_revision_id
    assert pointer["origin_case_id"] is None
    action, destination = navigation(service, values)
    assert action.id["action"] == "intake"
    assert destination["navigate_target"] == "trail-gather-evidence"

    case_id, selected, unrelated = gathered_case(service, app, values)
    case = service.export_bundle(case_id)
    assert set(case.case.coverage["collection_scope"]["source_ids"]) == set(selected)
    assert unrelated not in case.case.coverage["collection_scope"]["source_ids"]
    assert len(case.findings) >= 2

    # An existing human disposition must survive planning. The guided first
    # finding must advance to an undecided statement rather than reselecting it.
    first_finding = case.findings[0].snapshot_id
    baseline = service.baseline_scenario(case_id, first_finding)
    service.review(
        case_id,
        first_finding,
        baseline["id"],
        "qualify",
        "Practice decision: these are saved fixture notices, not verified events.",
    )
    select_case(app, values, session_for_case(service, case_id))
    native = service.export_bundle(case_id)
    decisions = assessment_decisions(service.view(case_id))
    action, destination = navigation(service, values, page="review")
    assert action.id["action"] == "plan_continue"
    assert destination["navigate_target"] == PREFIX + "resume"

    before = snapshot(service)
    edit(app, values, "resume")
    assert snapshot(service) == before  # Resume copies working fields only.
    resumed = working(values)
    assert resumed.brief_id == saved.brief_id
    assert resumed.selected_revision_id == saved.selected_revision_id
    assert resumed.binding_case_id is resumed.confirmation_id is None
    expected_refs = service.planning_briefs.case_binding(case_id).scope.source_refs
    assert set(resumed.fields["sources"].splitlines()) == set(expected_refs)
    assert resumed.dirty
    assert service.planning_briefs.state_for_case(case_id) is None
    assert navigation(service, values)[0].id["action"] == "plan_save"

    edit(app, values, "save")
    assert navigation(service, values)[0].id["action"] == "plan_attach"
    assert service.planning_briefs.state_for_case(case_id) is None
    edit(app, values, "bind")
    assert values[PREFIX + "edit.data"].get("continuation") is None
    assert navigation(service, values)[0].id["action"] == "plan_confirm"
    assert service.planning_progress_facts(service.view(case_id)).brief is None
    edit(app, values, "confirm")
    assert service.planning_progress_facts(service.view(case_id)).brief.confirmed
    action, destination = navigation(service, values)
    assert action.id["action"] == "finding"
    assert destination["navigate_to"] == "review"
    assert destination["navigate_target"] == "rv-finding-heading"
    assert action.id["fid"] not in decisions
    assert action.id["fid"] == next(
        finding.snapshot_id for finding in native.findings if finding.snapshot_id not in decisions
    )
    assert service.export_bundle(case_id) == native
    assert assessment_decisions(service.view(case_id)) == decisions


def test_explicit_continue_preserves_other_working_edits_and_unsaved_review(desk):
    service, app = desk
    values = browser_values()
    saved = save_plan(app, values)
    # Keep unfinished changes in the origin before moving to another case.
    values[PREFIX + "question.value"] = "Unsaved question in the origin workspace."
    post_callback(app, PREFIX + "edit.data", PREFIX + "question.value", values)
    origin_fields = deepcopy(working(values).fields)
    case_id = service.import_bundle(load_demo_bundle().model_dump(mode="json"))
    select_case(app, values, session_for_case(service, case_id))
    values[PREFIX + "question.value"] = "Separate unfinished question for this case."
    post_callback(app, PREFIX + "edit.data", PREFIX + "question.value", values)
    target_fields = deepcopy(working(values).fields)
    before = snapshot(service)
    edit(app, values, "resume")
    assert snapshot(service) == before
    store = EditorStore.model_validate(values[PREFIX + "edit.data"])
    assert any(draft.fields == origin_fields for draft in store.drafts.values())
    assert any(draft.fields == target_fields for draft in store.drafts.values())
    assert working(values).brief_id == saved.brief_id
    raw = deepcopy(values[PREFIX + "edit.data"])
    action, destination = navigation(service, values, note="Keep this unsaved review note.")
    assert action.id["action"] == "review"
    assert destination["finding_id"] == values["rv-session.data"]["finding_id"]
    assert values[PREFIX + "edit.data"] == raw
    assert service.planning_briefs.state_for_case(case_id) is None


@pytest.mark.parametrize("conflict", ["head", "case", "scope"])
def test_continue_rejects_stale_saved_identity_without_losing_fields(desk, conflict):
    service, app = desk
    values = browser_values()
    saved = save_plan(app, values)
    case_id = service.import_bundle(load_demo_bundle().model_dump(mode="json"))
    select_case(app, values, session_for_case(service, case_id))
    values[PREFIX + "question.value"] = "Keep this text after a rejected continuation."
    post_callback(app, PREFIX + "edit.data", PREFIX + "question.value", values)
    original_fields = deepcopy(working(values).fields)
    if conflict == "head":
        revision = service.planning_briefs.load(saved.brief_id).revision
        service.planning_briefs.save_draft(
            saved.brief_id,
            revision.fields.model_copy(update={"purpose": "New question saved by another editor"}),
            base_revision_id=revision.revision_id,
            expected_head_revision_id=revision.revision_id,
            author="Another fixture editor",
        )
    elif conflict == "case":
        values[PREFIX + "case.data"]["case_fingerprint"] = "old-case-fingerprint"
    else:
        values[PREFIX + "case.data"]["source_refs"] = ["not-the-saved-scope"]
    before = snapshot(service)
    response = edit(app, values, "resume")
    assert snapshot(service) == before
    assert working(values).fields == original_fields
    assert PREFIX + "refresh" not in response
    assert service.planning_briefs.state_for_case(case_id) is None


def test_start_another_plan_clears_continuation_without_losing_saved_plan(desk):
    service, app = desk
    values = browser_values()
    saved = save_plan(app, values)
    before = snapshot(service)
    edit(app, values, "new")
    assert snapshot(service) == before
    assert values[PREFIX + "edit.data"].get("continuation") is None
    assert working(values).brief_id is None
    assert (
        service.planning_briefs.load(saved.brief_id).revision.revision_id
        == saved.selected_revision_id
    )


@pytest.mark.parametrize("visible_matches", [True, False])
def test_case_switch_captures_only_the_identified_visible_working_copy(desk, visible_matches):
    service, app = desk
    payload = load_demo_bundle().model_dump(mode="json")
    first_id = service.import_bundle(payload)
    payload["case"]["id"] = "guided:second-evidence-case"
    second_id = service.import_bundle(payload)
    values = browser_values()
    select_case(app, values, session_for_case(service, second_id))
    values[PREFIX + "question.value"] = "Existing target-case draft."
    post_callback(app, PREFIX + "edit.data", PREFIX + "question.value", values)
    target_fields = deepcopy(working(values).fields)
    select_case(app, values, session_for_case(service, first_id))
    values[PREFIX + "question.value"] = "Acknowledged first-case edit."
    post_callback(app, PREFIX + "edit.data", PREFIX + "question.value", values)
    origin_key = values[PREFIX + "edit.data"]["active"]
    assert values[PREFIX + "visible.data"] == origin_key

    # The final keystroke has not reached the edit callback when the user
    # switches cases. Only the explicit visible-copy key identifies its owner.
    values[PREFIX + "question.value"] = "Final unsaved first-case keystroke."
    if not visible_matches:
        values[PREFIX + "visible.data"] = "a-different-working-copy"
    before = snapshot(service)
    select_case(app, values, session_for_case(service, second_id))
    assert snapshot(service) == before
    assert working(values).fields == target_fields
    origin = values[PREFIX + "edit.data"]["drafts"][origin_key]
    assert origin["fields"]["question"] == (
        "Final unsaved first-case keystroke."
        if visible_matches
        else "Acknowledged first-case edit."
    )
    assert values[PREFIX + "visible.data"] == values[PREFIX + "edit.data"]["active"]


def test_two_saved_plans_keep_independent_continuations_and_binding_consumes_only_one(desk):
    service, app = desk
    first_values, second_values = browser_values(), browser_values()
    first = save_plan(app, first_values)
    second = save_plan(app, second_values)
    assert first.brief_id != second.brief_id
    payload = load_demo_bundle().model_dump(mode="json")
    first_case = service.import_bundle(payload)
    payload["case"]["id"] = "guided:other-plan-case"
    second_case = service.import_bundle(payload)

    select_case(app, first_values, session_for_case(service, first_case))
    select_case(app, second_values, session_for_case(service, second_case))
    assert navigation(service, first_values)[0].id["action"] == "plan_continue"
    assert navigation(service, second_values)[0].id["action"] == "plan_continue"
    before = snapshot(service)
    edit(app, first_values, "resume")
    edit(app, second_values, "resume")
    assert snapshot(service) == before
    assert working(first_values).brief_id == first.brief_id
    assert working(second_values).brief_id == second.brief_id

    edit(app, first_values, "save")
    edit(app, first_values, "bind")
    assert first_values[PREFIX + "edit.data"].get("continuation") is None
    assert second_values[PREFIX + "edit.data"]["continuation"]["brief_id"] == second.brief_id
    assert service.planning_briefs.load(second.brief_id).revision.binding is None
    select_case(app, first_values, session_for_case(service, second_case))
    assert navigation(service, first_values)[0].id["action"] != "plan_continue"


def test_another_cases_confirmed_revision_cannot_confirm_the_visible_case(desk):
    service, app = desk
    original, bound, _ = confirmed_plan(service)
    payload = original.model_dump(mode="json")
    payload["case"]["id"] = "guided:unconfirmed-target-case"
    target_id = service.import_bundle(payload)
    values = browser_values()
    select_case(app, values, session_for_case(service, target_id))
    store = EditorStore()
    forged = store.open_state(target_id, service.planning_briefs.load(bound.brief_id))
    # The revision really is confirmed, but only for the original case. Browser
    # claims about its binding and active status must not transfer that authority.
    forged.binding_case_id = target_id
    forged.is_active_for_case = True
    values[PREFIX + "edit.data"] = store.model_dump(mode="json")
    before, original_editor = snapshot(service), deepcopy(values[PREFIX + "edit.data"])
    post_callback(app, PREFIX + "status.children", PREFIX + "edit.data", values)
    assert "Scope confirmed" not in str(values[PREFIX + "status.children"])
    assert "Open first finding" not in str(values[PREFIX + "status.children"])
    selected = values[PREFIX + "selected.data"]
    assert selected["status"] == "needs_review"
    assert selected["is_active_for_case"] is False
    assert selected["bound_case_id"] == original.case.id
    assert values[PREFIX + "confirm.disabled"] is True
    assert navigation(service, values)[0].id["action"] == "purpose"
    assert service.planning_progress_facts(service.view(target_id)).brief is None
    assert values[PREFIX + "edit.data"] == original_editor
    assert snapshot(service) == before


def test_findings_in_another_run_do_not_make_the_selected_assessment_ready(desk, monkeypatch):
    service, _ = desk
    payload = load_demo_bundle().model_dump(mode="json")
    empty_run = {**payload["runs"][0], "id": "guided:run-without-findings"}
    payload["runs"].insert(0, empty_run)
    bundle, _, _ = confirmed_plan(service, EvidenceBundle.model_validate(payload))
    original_view = service.view

    def mixed_run_view(case_id, scenario_id=None):
        # The navigation contract can also receive a complete multi-run bundle.
        # Keep the real empty assessment context and expose the saved other run.
        result = original_view(case_id, scenario_id)
        result["bundle"]["findings"] = deepcopy(payload["findings"])
        return result

    monkeypatch.setattr(service, "view", mixed_run_view)
    values = browser_values()
    state = session_for_case(service, bundle.case.id)
    state["finding_id"] = None
    values["rv-session.data"] = state
    view = service.view(bundle.case.id)
    assert assessment_context(view)["base_run_id"] == empty_run["id"]
    assert view["bundle"]["findings"]
    assert all(finding["run_id"] != empty_run["id"] for finding in view["bundle"]["findings"])
    before = snapshot(service)
    rendered = render_next_action(service, state, "cases", None)
    assert "Ready to inspect" not in str(rendered)
    assert "Inspect saved evidence" in str(rendered)
    action, destination = navigation(service, values)
    assert action.id["action"] == "compare"
    assert not action.id["fid"]
    assert destination["navigate_to"] == "compare"
    assert not destination.get("finding_id")
    assert snapshot(service) == before


def test_confirmed_plan_opens_first_unsupported_finding_without_a_substitute_source(desk):
    service, _ = desk
    payload = load_demo_bundle().model_dump(mode="json")
    supported = payload["findings"][0]
    unsupported = payload["findings"][-1]
    payload["findings"] = [unsupported, *payload["findings"][:-1]]
    payload["links"] = [
        link for link in payload["links"] if link["target"]["id"] != unsupported["snapshot_id"]
    ]
    bundle, _, _ = confirmed_plan(service, EvidenceBundle.model_validate(payload))
    values = browser_values()
    state = session_for_case(service, bundle.case.id)
    state.update(finding_id=supported["snapshot_id"], record_id=bundle.records[0].id)
    values["rv-session.data"] = state
    native = service.export_bundle(bundle.case.id)
    action, destination = navigation(service, values)
    assert action.id["action"] == "finding"
    assert action.id["fid"] == unsupported["snapshot_id"]
    assert not action.id["rid"]
    assert destination["navigate_target"] == "rv-finding-heading"
    assert destination["finding_id"] == unsupported["snapshot_id"]
    assert destination["record_id"] is None
    assert not assessment_decisions(service.view(bundle.case.id))
    assert service.export_bundle(bundle.case.id) == native


def test_confirmed_plan_with_every_finding_reviewed_opens_briefing(desk):
    service, app = desk
    bundle, bound, _ = confirmed_plan(service)
    for finding in bundle.findings:
        baseline = service.baseline_scenario(bundle.case.id, finding.snapshot_id)
        service.review(
            bundle.case.id,
            finding.snapshot_id,
            baseline["id"],
            "withhold",
            "Fixture decision: withhold this wording pending further support.",
        )
    values = browser_values()
    select_case(app, values, session_for_case(service, bundle.case.id))
    store = EditorStore()
    store.open_state(bundle.case.id, service.planning_briefs.load(bound.brief_id))
    values[PREFIX + "edit.data"] = store.model_dump(mode="json")
    before = snapshot(service)
    decisions = assessment_decisions(service.view(bundle.case.id))
    assert set(decisions) == {finding.snapshot_id for finding in bundle.findings}
    post_callback(app, PREFIX + "status.children", PREFIX + "edit.data", values)
    assert "Preview reviewed briefing" in str(values[PREFIX + "status.children"])
    assert "Open first finding" not in str(values[PREFIX + "status.children"])
    action, destination = navigation(service, values)
    assert action.id["action"] == "brief"
    assert destination["navigate_to"] == "brief"
    assert destination["navigate_target"] == "rv-brief-page"
    assert assessment_decisions(service.view(bundle.case.id)) == decisions
    assert snapshot(service) == before
