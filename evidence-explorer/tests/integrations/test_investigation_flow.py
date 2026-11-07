"""The guided entry point preserves explicit planning and evidence decisions."""

from copy import deepcopy

import pytest

from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.integrations.brief_editor_forms import PREFIX
from evidence_explorer.review.integrations.brief_editor_state import EditorStore
from evidence_explorer.review.integrations.navigation_ui import (
    planning_navigation,
    render_next_action,
)
from evidence_explorer.review.integrations.web import create_app
from evidence_explorer.review.navigation import apply_navigation
from tests.integrations.test_navigation_ui import action_button, components
from tests.integrations.test_planning_brief_ui import browser_values, post_callback
from tests.integrations.test_planning_workspace import confirmed_plan, workspace
from tests.review.test_navigation import snapshot

PURPOSE = "cited_claim_investigation"


@pytest.fixture
def desk(tmp_path):
    service = workspace(tmp_path / "investigation")
    return service, create_app(service)


def editor_action(app, values, action):
    """Use the registered HTTP callback, including its returned visible fields."""
    result = post_callback(app, PREFIX + "edit.data", PREFIX + action + ".n_clicks", values)
    if PREFIX + "refresh" in result:
        post_callback(app, PREFIX + "picker.options", PREFIX + "refresh.data", values)
    post_callback(app, PREFIX + "status.children", PREFIX + "edit.data", values)
    return result


def select_case(app, values, state):
    values["rv-session.data"] = state
    post_callback(app, PREFIX + "case.data", "rv-session.data", values)
    post_callback(app, PREFIX + "edit.data", "rv-session.data", values)
    post_callback(app, PREFIX + "picker.options", "rv-session.data", values)


def recommend(service, values, *, page="cases", selection=None, note=None):
    before = snapshot(service)
    original = deepcopy(values)
    rendered = render_next_action(
        service,
        values["rv-session.data"],
        page,
        selection,
        note=note,
        editor=values.get(PREFIX + "edit.data"),
    )
    assert values == original
    assert snapshot(service) == before
    return action_button(rendered)


def assert_destination(service, values, button, target):
    before = snapshot(service)
    result = apply_navigation(service, values["rv-session.data"], button.id)
    assert result["navigate_target"] == target
    assert snapshot(service) == before
    return result


def saved_draft(values):
    store = EditorStore.model_validate(values[PREFIX + "edit.data"])
    return store.for_case(values["rv-session.data"].get("case_id"))


def save_unbound_plan(app, values):
    fields = {
        "purpose": PURPOSE,
        "question": "What supports the public harbor statement?",
        "topics": "Public harbor access",
        "role": "Research reviewer",
        "output": "reviewed_brief",
    }
    values.update({PREFIX + key + ".value": value for key, value in fields.items()})
    editor_action(app, values, "save")
    return saved_draft(values)


def test_empty_start_and_optional_guide_never_create_work(desk):
    service, app = desk
    values = browser_values()
    before = snapshot(service)
    assert app.server.test_client().get("/_dash-layout").status_code == 200
    choose = app.callback_map["trail-use-case-purpose.data"]["callback"].__wrapped__
    guide = choose(PURPOSE)
    start = recommend(service, values, selection=guide)
    assert start.children == "Start investigation"
    assert start.id["action"] == "purpose"
    state = assert_destination(service, values, start, "trail-planning-question")
    assert not state.get("case_id")
    assert service.planning_briefs.list() == ()
    assert snapshot(service) == before


def test_saved_plan_to_explicit_scope_confirmation_and_portable_history(desk, tmp_path):
    service, app = desk
    values = browser_values()
    native_before = snapshot(service)
    unbound = save_unbound_plan(app, values)
    assert unbound.binding_case_id is unbound.confirmation_id is None
    assert service.list_cases() == []
    assert all(
        row == native_before[table]
        for table, row in snapshot(service).items()
        if not table.startswith("monitoring_brief")
    )
    collect = recommend(service, values)
    assert collect.id["action"] == "intake"
    assert_destination(service, values, collect, "trail-gather-evidence")

    # Import is explicit; it neither binds the saved plan nor starts analysis.
    case_id = service.import_bundle(load_demo_bundle().model_dump(mode="json"))
    state = session_for_case(service, case_id)
    native = service.export_bundle(case_id)
    select_case(app, values, state)
    attach = recommend(service, values)
    assert attach.id["action"] == "plan_continue"
    assert_destination(service, values, attach, "trail-planning-resume")
    # Import opens Review; its next action must still expose the pending plan attachment.
    assert recommend(service, values, page="review").id["action"] == "plan_continue"
    assert service.planning_briefs.state_for_case(case_id) is None

    editor_action(app, values, "load")
    assert saved_draft(values).brief_id == unbound.brief_id
    scope_action = recommend(service, values)
    assert scope_action.id["action"] == "scope"
    assert recommend(service, values, page="review").id["action"] == "scope"
    assert_destination(service, values, scope_action, "trail-planning-scope")
    editor_action(app, values, "use-scope")
    assert saved_draft(values).dirty
    assert recommend(service, values).id["action"] == "plan_save"
    assert service.planning_briefs.state_for_case(case_id) is None

    editor_action(app, values, "save")
    editor_action(app, values, "bind")
    bound = saved_draft(values)
    assert bound.binding_case_id == case_id and bound.confirmation_id is None
    assert service.planning_progress_facts(service.view(case_id)).brief is None
    confirm = recommend(service, values)
    assert confirm.id["action"] == "plan_confirm"
    assert_destination(service, values, confirm, "trail-planning-confirm")

    editor_action(app, values, "confirm")
    confirmed = saved_draft(values)
    assert confirmed.status == "confirmed" and confirmed.confirmation_id
    facts = service.planning_progress_facts(service.view(case_id))
    assert facts.brief is not None and facts.brief.confirmed
    assert recommend(service, values).id["action"] == "finding"
    assert service.export_bundle(case_id) == native
    with service.repository._connection() as connection:
        for table in ("review_decisions", "review_jobs", "collection_jobs"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0

    expected_history = service.planning_briefs.history_for_case(case_id)
    receiver = workspace(tmp_path / "recipient")
    assert receiver.import_package(service.export_package(case_id)) == case_id
    assert receiver.export_bundle(case_id) == native
    assert receiver.planning_briefs.history_for_case(case_id) == expected_history
    assert receiver.planning_progress_facts(receiver.view(case_id)).brief is None
    assert receiver.planning_briefs.state_for_case(case_id) is None
    recipient_values = browser_values()
    recipient_values["rv-session.data"] = session_for_case(receiver, case_id)
    assert recommend(receiver, recipient_values).id["action"] == "saved_plan"


def test_saved_confirmed_purpose_overrides_optional_workflow_choice(desk):
    service, _ = desk
    bundle, _, _ = confirmed_plan(service)
    values = browser_values()
    state = session_for_case(service, bundle.case.id)
    values["rv-session.data"] = state
    assert planning_navigation(service, state) == ("confirmed", PURPOSE)
    without_guide = recommend(service, values)
    unrelated_guide = recommend(
        service, values, selection={"purpose_id": "public_information_monitoring"}
    )
    assert without_guide.id == unrelated_guide.id
    assert without_guide.id["action"] == "finding"


def test_dirty_plan_and_review_notes_remain_in_their_exact_context(desk):
    service, app = desk
    values = browser_values()
    save_unbound_plan(app, values)
    case_id = service.import_bundle(load_demo_bundle().model_dump(mode="json"))
    select_case(app, values, session_for_case(service, case_id))
    editor_action(app, values, "load")
    values[PREFIX + "question.value"] = "Unfinished question in this case."
    post_callback(app, PREFIX + "edit.data", PREFIX + "question.value", values)
    original = deepcopy(values[PREFIX + "edit.data"])
    assert recommend(service, values).id["action"] == "plan_save"
    assert values[PREFIX + "edit.data"] == original
    review = recommend(service, values, note="An unsaved qualification also needs attention.")
    assert review.id["action"] == "review"
    assert review.id["fid"] == values["rv-session.data"]["finding_id"]
    assert values[PREFIX + "edit.data"] == original
    assert service.planning_briefs.state_for_case(case_id) is None


@pytest.mark.parametrize(
    ("imported", "case_selected"), [(False, True), (True, True), (True, False)]
)
@pytest.mark.parametrize("dirty", [False, True])
def test_historical_unbound_revision_routes_to_adoption_without_writes(
    desk, tmp_path, imported, case_selected, dirty
):
    service, _ = desk
    bundle, bound, _ = confirmed_plan(service)
    first = service.planning_briefs.history(bound.brief_id).revisions[0]
    if imported:
        recipient = workspace(tmp_path / "history-recipient")
        recipient.import_package(service.export_package(bundle.case.id))
        service = recipient
    case_id = bundle.case.id if case_selected else None
    saved = service.planning_briefs.load(first.brief_id, first.revision_id)
    assert saved.revision.binding is None and not saved.is_head
    assert saved.imported is imported
    store = EditorStore()
    draft = store.open_state(case_id, saved)
    if dirty:
        draft.fields["question"] = "Preserve these edits while choosing adoption or copy."
    assert draft.dirty is dirty
    values = browser_values()
    values["rv-session.data"] = session_for_case(service, case_id)
    values[PREFIX + "edit.data"] = store.model_dump(mode="json")
    action = recommend(service, values)
    assert action.id["action"] == "plan_history"
    assert_destination(service, values, action, "trail-planning-history")


def test_legacy_evidence_without_planning_storage_remains_inspectable(desk):
    service, _ = desk
    case_id = service.import_bundle(load_demo_bundle().model_dump(mode="json"))
    with service.repository._connection() as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'monitoring_brief%'"
        ).fetchall()
        connection.execute("PRAGMA foreign_keys=OFF")
        for (table,) in tables:
            connection.execute('DROP TABLE "' + table.replace('"', '""') + '"')
    assert not service._has_planning_storage()
    values = browser_values()
    values["rv-session.data"] = session_for_case(service, case_id)
    action = recommend(service, values)
    assert action.id["action"] == "source"
    assert_destination(service, values, action, "rv-source")


@pytest.mark.parametrize("invalid", ["stale_case", "invalid_editor", "missing_saved_revision"])
def test_invalid_context_never_reuses_navigation_or_writes(desk, invalid):
    service, _ = desk
    bundle, bound, _ = confirmed_plan(service)
    state = session_for_case(service, bundle.case.id)
    store = EditorStore()
    store.open_state(bundle.case.id, service.planning_briefs.load(bound.brief_id))
    editor = store.model_dump(mode="json")
    if invalid == "stale_case":
        state["case_fingerprint"] = "an-old-case-fingerprint"
    elif invalid == "invalid_editor":
        editor = {"drafts": "invalid browser payload"}
    else:
        editor["drafts"][editor["active"]]["selected_revision_id"] = "missing-revision"
    before = snapshot(service)
    rendered = render_next_action(service, state, "cases", None, editor=editor)
    assert not any(isinstance(getattr(node, "id", None), dict) for node in components(rendered))
    assert snapshot(service) == before
