"""Planning editor commands preserve exact revisions and independent browser drafts."""

import json
from types import SimpleNamespace

import pytest
from dash import Dash, dcc, html, no_update
from dash.development.base_component import Component

from evidence_explorer.planning_briefs.models import DraftFields
from evidence_explorer.planning_briefs.persistence import PlanningBriefRepository
from evidence_explorer.planning_briefs.service import PlanningBriefService
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.integrations import brief_editor_ui as editor
from evidence_explorer.review.integrations.brief_editor_forms import (
    FIELD_NAMES,
    PREFIX,
    blank_fields,
    form_values,
    parsed_fields,
)
from evidence_explorer.review.storage import ReviewRepository


@pytest.fixture
def desk(tmp_path):
    repository = ReviewRepository(tmp_path / "planning.sqlite3")
    repository.initialize()
    planning = PlanningBriefRepository(repository._connection)
    planning.initialize()
    service = SimpleNamespace(repository=repository, planning_briefs=PlanningBriefService(planning))
    app = Dash(__name__, suppress_callback_exceptions=True)
    app.layout = lambda: html.Div(
        [dcc.Store(id="rv-session", data={}), editor.planning_brief_panel()]
    )
    editor.register_planning_brief_callbacks(app, service)
    return service, app


def components(node):
    if isinstance(node, Component):
        yield node
        yield from components(getattr(node, "children", None))
    elif isinstance(node, (list, tuple)):
        for child in node:
            yield from components(child)


def callback(app, fragment):
    return next(
        value["callback"].__wrapped__ for key, value in app.callback_map.items() if fragment in key
    )


def post_callback(app, fragment, changed, values):
    """Exercise Dash's registered HTTP contract and apply only returned outputs."""
    key, metadata = next((key, row) for key, row in app.callback_map.items() if fragment in key)
    outputs = metadata["output"]
    response = app.server.test_client().post(
        "/_dash-update-component",
        json={
            "output": key,
            "outputs": [
                {"id": output.component_id, "property": output.component_property}
                for output in outputs
            ],
            "inputs": [
                {**entry, "value": values.get(entry["id"] + "." + entry["property"])}
                for entry in metadata["inputs"]
            ],
            "state": [
                {**entry, "value": values.get(entry["id"] + "." + entry["property"])}
                for entry in metadata["state"]
            ],
            "changedPropIds": [changed],
        },
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    result = response.get_json()["response"]
    for component_id, properties in result.items():
        for name, value in properties.items():
            values[component_id + "." + name] = value
    return result


def browser_values():
    values = {
        "rv-session.data": {},
        PREFIX + "edit.data": {},
        PREFIX + "case.data": {"case_id": None, "source_refs": []},
    }
    values.update({PREFIX + name + ".value": value for name, value in blank_fields().items()})
    values.update(
        {
            PREFIX + name + ".n_clicks": 0
            for name in (
                "load",
                "new",
                "save",
                "bind",
                "confirm",
                "adopt",
                "copy",
                "use-scope",
                "resume",
            )
        }
    )
    return values


def selected(store, case_id=None):
    return store.for_case(case_id)


def transition(service, action, store=None, session=None, values=None, **kwargs):
    wire = store.model_dump(mode="json") if store is not None else {}
    if values is None:
        values = selected(store, (session or {}).get("case_id")).fields if store else blank_fields()
    return editor.apply_editor_action(service, action, session or {}, wire, values, **kwargs)


def saved_case(service, case_id="planning:case-a"):
    data = load_demo_bundle().model_dump(mode="json")
    data["case"].update(id=case_id, title="Planning source " + case_id)
    service.repository.import_bundle(type(load_demo_bundle()).model_validate(data))
    return {
        "case_id": case_id,
        "case_fingerprint": service.repository.get_case_fingerprint(case_id),
    }


def evidence_counts(service):
    with service.repository._connection() as connection:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "review_cases",
                "review_findings",
                "review_decisions",
                "review_jobs",
                "review_events",
            )
        )


def complete_values(
    scope, *, purpose="cited_claim_investigation", question="Which claim is supported?"
):
    return {
        **blank_fields(),
        "purpose": purpose,
        "question": question,
        "topics": "Public infrastructure",
        "sources": "\n".join(scope["source_refs"]),
        "role": "Research reviewer",
        "output": "reviewed_brief",
    }


def opened_saved(service, session, values):
    store, _, _ = transition(service, "context", session=session)
    store, message, error = transition(service, "save", store, session, values)
    assert not error, message
    return store


def test_panel_reads_have_unique_private_ids_and_no_workflow_writes(desk):
    service, app = desk
    before = evidence_counts(service)
    nodes = list(components(app.layout()))
    ids = [node.id for node in nodes if hasattr(node, "id")]
    assert len(ids) == len(set(ids))
    assert all(identifier == "rv-session" or identifier.startswith(PREFIX) for identifier in ids)
    assert (
        next(node for node in nodes if getattr(node, "id", None) == PREFIX + "edit").storage_type
        == "session"
    )
    assert not any("rv-session.data" in key or "rv-page.value" in key for key in app.callback_map)
    for _ in range(2):
        assert app.server.test_client().get("/_dash-layout").status_code == 200
    assert evidence_counts(service) == before
    assert service.planning_briefs.list() == ()


def test_first_save_dirty_context_has_case_identity_without_saved_authority():
    draft = editor.EditorDraft(case_id="planning:case-a")
    assert editor.selected_revision_context(draft) is None
    draft.fields.update(
        purpose="cited_claim_investigation", question="A question before the first save"
    )
    context = editor.selected_revision_context(draft)
    assert context["case_id"] == "planning:case-a" and context["dirty"]
    assert not context["is_active_for_case"] and not context["imported"]
    assert all(
        context[name] is None
        for name in (
            "brief_id",
            "revision_id",
            "base_revision_id",
            "expected_head_revision_id",
            "head_revision_id",
            "bound_case_id",
            "scope_fingerprint",
            "content_hash",
            "confirmation_id",
        )
    )
    assert "A question" not in json.dumps(context)  # Raw unsaved fields stay private to the editor.


def test_http_first_save_dirty_projection_is_case_scoped_and_clears_after_save(desk):
    service, app = desk
    case_a, case_b = saved_case(service), saved_case(service, "planning:case-b")
    before = evidence_counts(service)
    values = browser_values()
    values["rv-session.data"] = case_a
    values[PREFIX + "case.data"] = editor.case_scope_context(service, case_a)
    post_callback(app, PREFIX + "edit.data", "rv-session.data", values)
    post_callback(app, PREFIX + "status.children", PREFIX + "edit.data", values)
    assert values[PREFIX + "selected.data"] is None

    values[PREFIX + "purpose.value"] = "cited_claim_investigation"
    values[PREFIX + "question.value"] = "Do not miss these unsaved first-draft edits."
    post_callback(app, PREFIX + "edit.data", PREFIX + "question.value", values)
    post_callback(app, PREFIX + "status.children", PREFIX + "edit.data", values)
    context = values[PREFIX + "selected.data"]
    assert context["case_id"] == case_a["case_id"] and context["dirty"]
    assert context["brief_id"] is context["revision_id"] is context["confirmation_id"] is None
    assert values.get(PREFIX + "refresh.data") is None
    assert service.planning_briefs.list() == ()

    # A delayed scope callback cannot turn case A's dirty fields into case B's context.
    values[PREFIX + "case.data"] = editor.case_scope_context(service, case_b)
    post_callback(app, PREFIX + "status.children", PREFIX + "case.data", values)
    assert values[PREFIX + "selected.data"]["case_id"] != case_b["case_id"]
    values["rv-session.data"] = case_b
    post_callback(app, PREFIX + "edit.data", "rv-session.data", values)
    post_callback(app, PREFIX + "status.children", PREFIX + "edit.data", values)
    assert values[PREFIX + "selected.data"] is None

    values["rv-session.data"] = case_a
    values[PREFIX + "case.data"] = editor.case_scope_context(service, case_a)
    post_callback(app, PREFIX + "edit.data", "rv-session.data", values)
    post_callback(app, PREFIX + "status.children", PREFIX + "edit.data", values)
    assert values[PREFIX + "selected.data"]["dirty"]
    assert values[PREFIX + "question.value"] == "Do not miss these unsaved first-draft edits."
    post_callback(app, PREFIX + "edit.data", PREFIX + "save.n_clicks", values)
    post_callback(app, PREFIX + "status.children", PREFIX + "edit.data", values)
    saved = values[PREFIX + "selected.data"]
    assert saved["case_id"] == case_a["case_id"] and not saved["dirty"]
    assert saved["brief_id"] and saved["revision_id"]
    assert saved["confirmation_id"] is saved["bound_case_id"] is None
    assert not saved["is_active_for_case"]
    assert evidence_counts(service) == before


def test_callback_refresh_contains_saved_context_only_and_confirms_explicitly(desk, monkeypatch):
    service, app = desk
    session = saved_case(service)
    scope = editor.case_scope_context(service, session)
    values = complete_values(scope)
    edit = callback(app, PREFIX + "edit.data")
    choose = callback(app, PREFIX + "picker.options")
    stored = {}
    picker = revision = None

    def invoke(action):
        nonlocal stored, picker, revision
        monkeypatch.setattr(editor, "ctx", SimpleNamespace(triggered_id=PREFIX + action))
        result = edit(
            session,
            *[values[name] for name in FIELD_NAMES],
            *([0] * 9),
            stored,
            picker,
            revision,
            scope,
            None,
        )
        stored, _, refresh, *_ = result
        if refresh is not no_update:
            monkeypatch.setattr(editor, "ctx", SimpleNamespace(triggered_id=PREFIX + "refresh"))
            _, picker, _, revision, *_ = choose(session, refresh, picker, revision)
        return result, refresh

    before = evidence_counts(service)
    _, refreshed = invoke("save")
    assert refreshed["case_id"] == session["case_id"]
    assert refreshed["confirmation_id"] is None
    assert refreshed["revision_id"] == refreshed["head_revision_id"]
    assert json.loads(refreshed["token"]) == {
        name: value for name, value in refreshed.items() if name != "token"
    }

    values["question"] = "Unsaved wording must not become a planning progress fact."
    result, refreshed = invoke("question")
    assert refreshed is no_update
    assert all(value is no_update for value in result[3:])
    assert "Unsaved wording" not in json.dumps(
        editor.selected_revision_context(
            editor.EditorStore.model_validate(stored).for_case(session["case_id"])
        )
    )

    _, refreshed = invoke("bind")
    assert refreshed is no_update  # Binding never discards an unsaved edit.
    assert editor.EditorStore.model_validate(stored).for_case(session["case_id"]).dirty
    _, refreshed = invoke("save")
    assert refreshed["confirmation_id"] is None
    _, refreshed = invoke("bind")
    bound_id = refreshed["revision_id"]
    assert refreshed["scope_fingerprint"] == scope["scope_fingerprint"]
    _, refreshed = invoke("confirm")
    assert refreshed["revision_id"] == bound_id
    assert refreshed["confirmation_id"]
    assert evidence_counts(service) == before


def test_http_saved_picker_survives_case_switch_and_opens_exact_revision(desk):
    service, app = desk
    values = browser_values()
    post_callback(app, PREFIX + "edit.data", PREFIX + "save.n_clicks", values)
    saved = values[PREFIX + "refresh.data"]
    brief_id, revision_id = saved["brief_id"], saved["revision_id"]

    # A reopened page starts with only durable data, without an editor working copy.
    values = browser_values()
    options = post_callback(app, PREFIX + "picker.options", "rv-session.data", values)
    assert options[PREFIX + "load"]["disabled"]
    values[PREFIX + "picker.value"] = brief_id
    choices = post_callback(app, PREFIX + "picker.options", PREFIX + "picker.value", values)
    assert choices[PREFIX + "revision"]["value"] == revision_id
    assert not choices[PREFIX + "load"]["disabled"]

    # Import/select an actual case while the saved, unbound target is pending.
    session = saved_case(service)
    values["rv-session.data"] = session
    values[PREFIX + "case.data"] = editor.case_scope_context(service, session)
    response = post_callback(app, PREFIX + "edit.data", "rv-session.data", values)
    assert PREFIX + "picker" not in response and PREFIX + "revision" not in response
    choices = post_callback(app, PREFIX + "picker.options", "rv-session.data", values)
    assert choices[PREFIX + "picker"]["value"] == brief_id
    assert choices[PREFIX + "revision"]["value"] == revision_id

    values[PREFIX + "question.value"] = "Preserve this case's separate unsaved work."
    response = post_callback(app, PREFIX + "edit.data", PREFIX + "question.value", values)
    assert PREFIX + "picker" not in response and PREFIX + "revision" not in response
    post_callback(app, PREFIX + "edit.data", PREFIX + "load.n_clicks", values)
    store = editor.EditorStore.model_validate(values[PREFIX + "edit.data"])
    draft = store.for_case(session["case_id"])
    assert draft.selected_revision_id == draft.base_revision_id == revision_id
    assert draft.expected_head_revision_id == revision_id
    assert any(
        row.fields["question"].startswith("Preserve this case") for row in store.drafts.values()
    )


def test_http_historical_choice_sticks_and_failed_open_preserves_targets_and_fields(desk):
    service, app = desk
    first = service.planning_briefs.create_draft(author="Analyst")
    second = service.planning_briefs.save_draft(
        first.brief_id,
        DraftFields(purpose="Later saved question"),
        base_revision_id=first.revision_id,
        expected_head_revision_id=first.revision_id,
        author="Analyst",
    )
    values = browser_values()
    values[PREFIX + "picker.value"] = first.brief_id
    post_callback(app, PREFIX + "picker.options", PREFIX + "picker.value", values)
    assert values[PREFIX + "revision.value"] == second.revision_id
    values[PREFIX + "revision.value"] = first.revision_id
    post_callback(app, PREFIX + "picker.options", PREFIX + "revision.value", values)
    post_callback(app, PREFIX + "picker.options", "rv-session.data", values)
    assert values[PREFIX + "revision.value"] == first.revision_id
    post_callback(app, PREFIX + "edit.data", PREFIX + "load.n_clicks", values)
    draft = editor.EditorStore.model_validate(values[PREFIX + "edit.data"]).for_case(None)
    assert draft.base_revision_id == first.revision_id
    assert draft.expected_head_revision_id == second.revision_id

    # Even an invalid/manual request cannot erase the pending target or a last keystroke.
    values[PREFIX + "question.value"] = "Keep this unsaved last keystroke."
    values[PREFIX + "revision.value"] = None
    response = post_callback(app, PREFIX + "edit.data", PREFIX + "load.n_clicks", values)
    assert PREFIX + "picker" not in response and PREFIX + "revision" not in response
    assert values[PREFIX + "picker.value"] == first.brief_id
    assert values[PREFIX + "revision.value"] is None
    assert values[PREFIX + "question.value"] == "Keep this unsaved last keystroke."
    draft = editor.EditorStore.model_validate(values[PREFIX + "edit.data"]).for_case(None)
    assert draft.fields["question"] == values[PREFIX + "question.value"]
    assert service.planning_briefs.load(first.brief_id).revision.revision_id == second.revision_id


def test_revision_picker_includes_head_and_keeps_exact_choice_beyond_history_page(
    desk, monkeypatch
):
    service, app = desk
    planner = service.planning_briefs
    saved = planner.create_draft(author="Analyst")
    for _ in range(1001):
        saved = planner.save_draft(
            saved.brief_id,
            DraftFields(),
            base_revision_id=saved.revision_id,
            expected_head_revision_id=saved.revision_id,
            author="Analyst",
        )
    previous = saved
    history = planner.history
    calls = []

    def bounded_history(brief_id, *, limit=100, offset=0):
        calls.append((limit, offset))
        assert limit == 1000 and offset == 0
        return history(brief_id, limit=limit, offset=offset)

    monkeypatch.setattr(planner, "history", bounded_history)
    values = browser_values()
    values[PREFIX + "picker.value"] = saved.brief_id
    response = post_callback(app, PREFIX + "picker.options", PREFIX + "picker.value", values)
    choices = response[PREFIX + "revision"]["options"]
    assert choices[0]["value"] == previous.revision_id
    assert values[PREFIX + "revision.value"] == previous.revision_id
    assert len(choices) == 1001  # One bounded page plus the exact current head.
    post_callback(app, PREFIX + "edit.data", PREFIX + "load.n_clicks", values)
    draft = editor.EditorStore.model_validate(values[PREFIX + "edit.data"]).for_case(None)
    assert draft.base_revision_id == draft.expected_head_revision_id == previous.revision_id

    # A new external head must not replace the user's pending historical choice.
    latest = planner.save_draft(
        saved.brief_id,
        DraftFields(purpose="New external head"),
        base_revision_id=previous.revision_id,
        expected_head_revision_id=previous.revision_id,
        author="Other analyst",
    )
    response = post_callback(app, PREFIX + "picker.options", "rv-session.data", values)
    ids = {row["value"] for row in response[PREFIX + "revision"]["options"]}
    assert {previous.revision_id, latest.revision_id} <= ids
    assert values[PREFIX + "revision.value"] == previous.revision_id
    assert len(ids) == 1002  # At most two exact additions, with no history scan.
    assert calls == [(1000, 0), (1000, 0)]
    retained = editor.EditorStore.model_validate(values[PREFIX + "edit.data"]).for_case(None)
    assert retained.base_revision_id == retained.expected_head_revision_id == previous.revision_id


def test_readonly_case_context_exposes_saved_manifest_and_rejects_stale_session(desk):
    service, app = desk
    session = saved_case(service)
    before = evidence_counts(service)
    case_scope = callback(app, PREFIX + "case.data")
    context, _ = case_scope(session)
    assert context["basis"] == "saved_evidence_manifest"
    assert set(context["source_refs"]) == {
        record.id for record in service.repository.get_bundle(session["case_id"]).records
    }
    stale, _ = case_scope({**session, "case_fingerprint": "0" * 64})
    assert stale["error"] and stale["source_refs"] == []
    assert evidence_counts(service) == before
    assert service.planning_briefs.list() == ()


def test_empty_unbound_draft_saves_and_reopens_exact_revision_without_evidence(desk):
    service, _ = desk
    before = evidence_counts(service)
    store, message, error = transition(service, "save")
    assert not error, message
    draft = selected(store)
    assert draft.number == 1 and draft.binding_case_id is None
    assert draft.saved_fields == DraftFields()
    assert evidence_counts(service) == before
    reopened_service = SimpleNamespace(
        repository=service.repository,
        planning_briefs=PlanningBriefService(
            PlanningBriefRepository(service.repository._connection)
        ),
    )
    reopened, message, error = transition(
        reopened_service, "load", picker=draft.brief_id, revision=draft.selected_revision_id
    )
    assert not error, message
    assert selected(reopened).selected_revision_id == draft.selected_revision_id
    assert selected(reopened).fields == blank_fields()
    assert evidence_counts(service) == before


def test_explicit_save_uses_current_visible_fields_even_before_edit_autosave(desk):
    service, _ = desk
    store, _, _ = transition(service, "context")
    latest = {**blank_fields(), "question": "The last typed words must be saved."}
    saved, message, error = transition(service, "save", store, values=latest)
    assert not error, message
    assert selected(saved).saved_fields.purpose == latest["question"]


def test_historical_revision_load_keeps_base_and_head_separate(desk):
    service, _ = desk
    planner = service.planning_briefs
    first = planner.create_draft(author="Analyst", fields=DraftFields(purpose="Earlier question"))
    latest = planner.save_draft(
        first.brief_id,
        DraftFields(purpose="Later question"),
        base_revision_id=first.revision_id,
        expected_head_revision_id=first.revision_id,
        author="Analyst",
    )
    store, message, error = transition(
        service, "load", picker=first.brief_id, revision=first.revision_id
    )
    assert not error, message
    draft = selected(store)
    assert draft.base_revision_id == first.revision_id
    assert draft.expected_head_revision_id == latest.revision_id
    assert draft.fields["question"] == "Earlier question"
    attempted = {**draft.fields, "question": "An edit based on the historical revision"}
    failed, message, error = transition(service, "save", store, values=attempted)
    assert error and "historical" in message
    assert selected(failed).fields["question"] == attempted["question"]
    assert planner.load(first.brief_id).revision.revision_id == latest.revision_id
    assert len(planner.history(first.brief_id).revisions) == 2


def test_optimistic_conflict_preserves_fields_and_captured_expectation(desk):
    service, _ = desk
    store = opened_saved(service, {}, {**blank_fields(), "question": "Original question"})
    draft = selected(store)
    opened_id = draft.selected_revision_id
    latest = service.planning_briefs.save_draft(
        draft.brief_id,
        DraftFields(purpose="Saved by another editor"),
        base_revision_id=opened_id,
        expected_head_revision_id=opened_id,
        author="Other analyst",
    )
    unsaved = {**draft.fields, "question": "My conflicting unsaved wording"}
    failed, message, error = transition(service, "save", store, values=unsaved)
    assert error and "unsaved fields are kept" in message
    working = selected(failed)
    assert working.fields["question"] == unsaved["question"]
    assert working.base_revision_id == working.expected_head_revision_id == opened_id
    assert working.current_head_revision_id == latest.revision_id
    assert len(service.planning_briefs.history(draft.brief_id).revisions) == 2
    opened_latest, _, error = transition(
        service, "load", failed, picker=draft.brief_id, revision=latest.revision_id
    )
    assert not error
    assert selected(opened_latest).fields["question"] == "Saved by another editor"
    assert any(
        row.fields["question"] == unsaved["question"] for row in opened_latest.drafts.values()
    )


def test_case_changes_and_reload_keep_independent_unsaved_fields(desk):
    service, _ = desk
    case_a, case_b = saved_case(service), saved_case(service, "planning:case-b")
    before = evidence_counts(service)
    store, _, _ = transition(service, "context", session=case_a)
    a = {**blank_fields(), "question": "Unsaved case A question"}
    store, _, _ = transition(service, "question", store, case_a, a)
    store, _, _ = transition(service, "context", store, case_b, a)
    assert selected(store, case_b["case_id"]).fields == blank_fields()
    b = {**blank_fields(), "question": "Unsaved case B question"}
    store, _, _ = transition(service, "question", store, case_b, b)
    restored, _, _ = transition(service, "context", store, case_a, blank_fields())
    assert selected(restored, case_a["case_id"]).fields["question"] == a["question"]
    assert any(row.fields["question"] == b["question"] for row in restored.drafts.values())
    assert evidence_counts(service) == before
    assert service.planning_briefs.list() == ()


def test_a_case_change_cannot_save_another_cases_stale_visible_form(desk):
    service, _ = desk
    case_a, case_b = saved_case(service), saved_case(service, "planning:case-b")
    store, _, _ = transition(service, "context", session=case_a)
    a = {**blank_fields(), "question": "Case A only"}
    store, _, _ = transition(service, "question", store, case_a, a)
    failed, message, error = transition(service, "save", store, case_b, a)
    assert error and "case changed" in message
    assert service.planning_briefs.list() == ()
    assert any(row.fields["question"] == a["question"] for row in failed.drafts.values())


def test_binding_requires_exact_saved_refs_but_can_preserve_other_incomplete_fields(desk):
    service, _ = desk
    session = saved_case(service)
    scope = editor.case_scope_context(service, session)
    before = evidence_counts(service)
    fields = {**blank_fields(), "sources": "\n".join(scope["source_refs"])}
    store = opened_saved(service, session, fields)
    initial = selected(store, session["case_id"]).selected_revision_id
    bound, message, error = transition(service, "bind", store, session, scope=scope)
    assert not error, message
    draft = selected(bound, session["case_id"])
    assert draft.binding_case_id == session["case_id"]
    assert draft.saved_fields.purpose is None
    assert len(service.planning_briefs.history(draft.brief_id).revisions) == 2
    assert service.planning_briefs.load(draft.brief_id, initial).revision.binding is None
    rejected, message, error = transition(service, "confirm", bound, session, scope=scope)
    assert error and "Supply" in message
    assert selected(rejected, session["case_id"]).status != "confirmed"
    assert service.planning_briefs.history(draft.brief_id).confirmations == ()
    assert evidence_counts(service) == before


def test_complete_plan_confirms_without_approving_evidence_or_starting_work(desk):
    service, _ = desk
    session = saved_case(service)
    scope = editor.case_scope_context(service, session)
    before = evidence_counts(service)
    store = opened_saved(service, session, complete_values(scope))
    bound, message, error = transition(service, "bind", store, session, scope=scope)
    assert not error, message
    confirmed, message, error = transition(service, "confirm", bound, session, scope=scope)
    assert not error, message
    draft = selected(confirmed, session["case_id"])
    assert draft.status == "confirmed" and draft.confirmation_id
    assert editor.selected_revision_context(draft)["revision_id"] == draft.selected_revision_id
    assert evidence_counts(service) == before


def test_guided_plan_to_evidence_keeps_save_attach_and_confirmation_explicit(desk):
    service, app = desk
    fields = complete_values({"source_refs": []})
    store, _, error = transition(service, "save", values=fields)
    assert not error
    saved = selected(store)
    brief_id, revision_id = saved.brief_id, saved.selected_revision_id
    assert evidence_counts(service) == (0, 0, 0, 0, 0)

    status = callback(app, PREFIX + "status.children")

    def next_link(store, scope):
        rendered, *_ = status(store.model_dump(mode="json"), scope)
        return next(node.href for node in components(rendered) if isinstance(node, html.A))

    assert next_link(store, {}) == "#trail-gather-evidence"
    session = saved_case(service)
    scope = editor.case_scope_context(service, session)
    before = evidence_counts(service)
    store, _, _ = transition(service, "context", store, session)
    store, _, error = transition(
        service, "load", store, session, picker=brief_id, revision=revision_id
    )
    assert not error
    assert next_link(store, scope) == "#" + PREFIX + "use-scope"
    assert selected(store, session["case_id"]).binding_case_id is None

    store, _, error = transition(service, "use-scope", store, session, scope=scope)
    assert not error
    assert next_link(store, scope) == "#" + PREFIX + "save"
    refused, _, error = transition(service, "bind", store, session, scope=scope)
    assert error
    assert selected(refused, session["case_id"]).dirty
    assert selected(refused, session["case_id"]).binding_case_id is None

    store, _, error = transition(service, "save", refused, session)
    assert not error
    assert next_link(store, scope) == "#" + PREFIX + "bind"
    store, _, error = transition(service, "bind", store, session, scope=scope)
    assert not error
    assert next_link(store, scope) == "#" + PREFIX + "confirm"
    assert not service.planning_briefs.history(brief_id).confirmations

    store, _, error = transition(service, "confirm", store, session, scope=scope)
    assert not error
    assert next_link(store, scope) == "#rv-next-action-slot"
    assert selected(store, session["case_id"]).confirmation_id
    assert evidence_counts(service) == before


@pytest.mark.parametrize(
    "window_values",
    [
        {},
        {"baseline-known": "known", "baseline-start": "2026-11-07T09:00:00"},
        {
            "baseline-known": "known",
            "baseline-start": "2026-11-08T09:00:00Z",
            "baseline-end": "2026-11-07T09:00:00Z",
        },
    ],
)
def test_invalid_or_unknown_activity_windows_never_confirm(desk, window_values):
    service, _ = desk
    session = saved_case(service)
    scope = editor.case_scope_context(service, session)
    values = {**complete_values(scope, purpose="public_information_monitoring"), **window_values}
    store, _, _ = transition(service, "context", session=session)
    saved, _, error = transition(service, "save", store, session, values)
    if window_values:
        assert error
        assert selected(saved, session["case_id"]).fields == values
    else:
        assert not error  # Explicitly unknown windows remain valid in a draft.
        bound, _, error = transition(service, "bind", saved, session, scope=scope)
        assert not error
        rejected, message, error = transition(service, "confirm", bound, session, scope=scope)
        assert error and "baseline and observation" in message
        assert selected(rejected, session["case_id"]).status != "confirmed"


def test_source_scope_changes_show_needs_review_and_preserve_earlier_revision(desk):
    service, _ = desk
    session = saved_case(service)
    scope = editor.case_scope_context(service, session)
    store = opened_saved(service, session, complete_values(scope))
    bound, _, _ = transition(service, "bind", store, session, scope=scope)
    confirmed, _, _ = transition(service, "confirm", bound, session, scope=scope)
    revision_id = selected(confirmed, session["case_id"]).selected_revision_id
    bundle = service.repository.get_bundle(session["case_id"]).model_dump(mode="json")
    extra = {**bundle["records"][0], "id": "new:saved-record"}
    bundle["records"].append(extra)
    service.repository.import_bundle(type(load_demo_bundle()).model_validate(bundle))
    refreshed, _, _ = transition(service, "context", confirmed, session)
    draft = selected(refreshed, session["case_id"])
    assert draft.status == "needs_review"
    assert draft.selected_revision_id == revision_id
    assert service.planning_briefs.load(draft.brief_id, revision_id).confirmation is not None
    with pytest.raises(ValueError, match="case changed"):
        editor.case_scope_context(service, session)


def test_imported_confirmation_requires_explicit_local_adoption(desk, tmp_path):
    service, _ = desk
    session = saved_case(service)
    scope = editor.case_scope_context(service, session)
    store = opened_saved(service, session, complete_values(scope))
    bound, _, _ = transition(service, "bind", store, session, scope=scope)
    confirmed, _, _ = transition(service, "confirm", bound, session, scope=scope)
    original = selected(confirmed, session["case_id"])
    receiver_repo = ReviewRepository(tmp_path / "receiver.sqlite3")
    receiver_repo.initialize()
    receiver_repo.import_bundle(service.repository.get_bundle(session["case_id"]))
    planning_repo = PlanningBriefRepository(receiver_repo._connection)
    planning_repo.initialize()
    receiver = SimpleNamespace(
        repository=receiver_repo, planning_briefs=PlanningBriefService(planning_repo)
    )
    receiver.planning_briefs.import_history(service.planning_briefs.history(original.brief_id))
    loaded, message, error = transition(
        receiver,
        "load",
        session=session,
        picker=original.brief_id,
        revision=original.selected_revision_id,
    )
    assert not error, message
    imported = selected(loaded, session["case_id"])
    assert imported.imported and imported.status != "confirmed"
    rejected, _, error = transition(receiver, "confirm", loaded, session, scope=scope)
    assert error
    adopted, message, error = transition(receiver, "adopt", rejected, session, scope=scope)
    assert not error, message
    local = selected(adopted, session["case_id"])
    assert local.selected_revision_id != imported.selected_revision_id
    assert not local.imported and local.confirmation_id is None and local.status != "confirmed"
    assert (
        receiver.planning_briefs.load(local.brief_id, imported.selected_revision_id).confirmation
        is not None
    )


def test_form_conversion_preserves_saved_alert_configuration_and_explicit_timezone():
    fields = DraftFields(
        purpose="Existing question",
        baseline_window={
            "id": "kept-window-id",
            "start": "2026-11-07T09:00:00+01:00",
            "end": "2026-11-08T09:00:00+01:00",
        },
        languages=("de",),
        alert_rule={
            "id": "saved-rule",
            "title": "Analyst configured rule",
            "metric": "count",
            "threshold_percent": 50,
            "threshold_basis": "analyst_configured",
            "direction": "increase",
            "comparison_basis": "exposure_normalized",
            "min_baseline_count": 5,
            "min_current_count": 1,
            "min_coverage_fraction": 0.8,
        },
    )
    raw = form_values(fields)
    raw["question"] = "Edited question"
    updated = parsed_fields(raw, fields.model_dump(mode="json"))
    assert updated.baseline_window.id == fields.baseline_window.id
    assert updated.baseline_window.start == fields.baseline_window.start
    assert updated.languages == ("de",)
    assert updated.alert_rule == fields.alert_rule
