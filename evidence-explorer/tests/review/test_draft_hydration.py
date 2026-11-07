"""Session hydration must not mistake an initial empty textarea for an analyst edit."""

from types import SimpleNamespace

import pytest
from dash import no_update

from evidence_explorer.review import web, workspace_ui
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.drafts import draft_key
from evidence_explorer.review.service import ReviewService
from evidence_explorer.review.storage import ReviewRepository


@pytest.fixture
def saved_desk(tmp_path):
    repository = ReviewRepository(tmp_path / "review.sqlite3")
    repository.initialize()
    service = ReviewService(repository)
    case_id = service.seed_demo()
    state = session_for_case(service, case_id)
    baseline = service.baseline_scenario(case_id, state["finding_id"])
    saved_note = "Use the observation; attribution remains unestablished."
    service.review(
        case_id,
        state["finding_id"],
        baseline["id"],
        "qualify",
        saved_note,
        expected_version=baseline["version"],
        expected_fingerprint=state["case_fingerprint"],
    )
    state.update(scenario_id=baseline["id"], scenario_version=baseline["version"])
    view = service.view(case_id, baseline["id"])
    key = draft_key(state, view)
    app = web.create_app(service)
    callback = next(
        definition["callback"].__wrapped__
        for output, definition in app.callback_map.items()
        if "rv-draft-store.data" in output
    )
    return service, callback, state, key, saved_note


def invoke(monkeypatch, callback, trigger, state, note, store):
    monkeypatch.setattr(workspace_ui, "ctx", SimpleNamespace(triggered_id=trigger))
    return callback(state, note, store)


def test_saved_note_reload_restores_text_without_marking_it_dirty(saved_desk, monkeypatch):
    service, callback, state, key, saved_note = saved_desk
    events = service.repository.list_events(state["case_id"])
    persisted = {"active": key, "notes": {key: saved_note}, "dirty": False}
    result, note, _ = invoke(monkeypatch, callback, None, state, "", persisted)
    assert note == saved_note
    assert result["notes"][key] == saved_note
    assert result["dirty"] is False
    assert service.repository.list_events(state["case_id"]) == events


def test_unsaved_draft_reload_restores_text_and_remains_dirty(saved_desk, monkeypatch):
    _, callback, state, key, _ = saved_desk
    draft = "Draft qualification still awaiting analyst approval."
    persisted = {"active": key, "notes": {key: draft}, "dirty": True}
    result, note, _ = invoke(monkeypatch, callback, None, state, "", persisted)
    assert note == draft
    assert result["notes"][key] == draft
    assert result["dirty"] is True


def test_intentional_clear_remains_empty_and_dirty(saved_desk, monkeypatch):
    _, callback, state, key, saved_note = saved_desk
    persisted = {"active": key, "notes": {key: saved_note}, "dirty": False}
    cleared, returned, _ = invoke(monkeypatch, callback, "rv-review-reason", state, "", persisted)
    assert returned is no_update
    assert cleared["notes"][key] == ""
    assert cleared["dirty"] is True
    restored, note, _ = invoke(monkeypatch, callback, None, state, "", cleared)
    assert note == ""
    assert restored["notes"][key] == ""
    assert restored["dirty"] is True


def test_non_edit_refresh_in_same_context_cannot_overwrite_latest_draft(saved_desk, monkeypatch):
    _, callback, state, key, _ = saved_desk
    draft = "The latest unsaved qualification."
    persisted = {"active": key, "notes": {key: draft}, "dirty": True}
    refreshed = {**state, "revision": state.get("revision", 0) + 1}
    result, note, _ = invoke(
        monkeypatch, callback, "rv-session", refreshed, "An older textarea value", persisted
    )
    assert result["active"] == key
    assert result["notes"][key] == draft
    assert note == draft
    assert result["dirty"] is True
