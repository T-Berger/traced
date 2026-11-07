"""Unsaved review text survives navigation and cannot silently differ from an export."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from dash import no_update

from evidence_explorer.config import Settings
from evidence_explorer.review import workspace_ui
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.drafts import draft_key, unsaved_for_export
from evidence_explorer.review.integrations import web as workspace_web
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService


def _callback(app, output_fragment):
    return next(
        definition["callback"].__wrapped__
        for key, definition in app.callback_map.items()
        if output_fragment in key
    )


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (tuple, list)):
        return " ".join(_text(child) for child in value)
    return _text(getattr(value, "children", ""))


@pytest.fixture
def desk(tmp_path):
    settings = Settings(data_dir=tmp_path / "workspace")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, settings.data_dir / "artifacts")
    case_id = service.seed_demo()
    return service, workspace_web.create_app(service), session_for_case(service, case_id)


def _change_draft(monkeypatch, app, trigger, state, note, store):
    monkeypatch.setattr(workspace_ui, "ctx", SimpleNamespace(triggered_id=trigger))
    return _callback(app, "rv-draft-store.data")(state, note, store)


def _save_baseline(service, state, note):
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
    return {
        **state,
        "scenario_id": baseline["id"],
        "scenario_version": baseline["version"],
        "assessment_kind": "baseline",
    }


def test_draft_survives_finding_switch_and_first_baseline_save(desk, monkeypatch):
    service, app, state = desk
    note = "The shared business address does not establish account ownership."
    initial, empty_note, _ = _change_draft(monkeypatch, app, "rv-session", state, "", {})
    assert empty_note == ""
    draft, returned, status = _change_draft(
        monkeypatch, app, "rv-review-reason", state, note, initial
    )
    assert returned is no_update
    assert draft["dirty"]
    assert "Unsaved changes" in _text(status)

    # Moving to another finding preserves the outgoing text and presents a clean field.
    other = {**state, "finding_id": "harbor:c1-v1"}
    switched, other_note, _ = _change_draft(monkeypatch, app, "rv-session", other, note, draft)
    assert other_note == ""
    assert switched["notes"][draft_key(state, service.view(state["case_id"]))] == note
    restored, restored_note, _ = _change_draft(
        monkeypatch, app, "rv-session", state, other_note, switched
    )
    assert restored_note == note

    # The storage ID introduced by the first save must not create a different draft identity.
    saved = _save_baseline(service, state, note)
    view = service.view(state["case_id"], saved["scenario_id"])
    assert draft_key(state, service.view(state["case_id"])) == draft_key(saved, view)
    final, saved_note, status = _change_draft(
        monkeypatch, app, "rv-session", saved, restored_note, restored
    )
    assert saved_note == note
    assert not final["dirty"]
    assert "Reviewed" in _text(status)
    assert not unsaved_for_export(saved, view, final, saved_note)


@pytest.mark.parametrize("trigger", [None, "rv-session"])
def test_reload_hydrates_saved_note_without_marking_it_unsaved(desk, monkeypatch, trigger):
    service, app, state = desk
    note = "Saved qualification remains readable after a browser reload."
    saved = _save_baseline(service, state, note)
    view = service.view(state["case_id"], saved["scenario_id"])
    key = draft_key(saved, view)
    persisted = {"active": key, "notes": {key: note}, "dirty": False}

    # Session storage can hydrate before the textarea, whose initial value is empty.
    hydrated, visible_note, status = _change_draft(monkeypatch, app, trigger, saved, "", persisted)
    assert visible_note == note
    assert hydrated["notes"][key] == note
    assert not hydrated["dirty"]
    assert "Reviewed" in _text(status)
    assert not unsaved_for_export(saved, view, hydrated, visible_note)


@pytest.mark.parametrize("trigger", [None, "rv-session"])
def test_reload_preserves_unsaved_note_and_its_warning(desk, monkeypatch, trigger):
    service, app, state = desk
    note = "Unsaved qualification must survive the empty textarea on reload."
    view = service.view(state["case_id"])
    key = draft_key(state, view)
    persisted = {"active": key, "notes": {key: note}, "dirty": True}

    hydrated, visible_note, status = _change_draft(monkeypatch, app, trigger, state, "", persisted)
    assert visible_note == note
    assert hydrated["notes"][key] == note
    assert hydrated["dirty"]
    assert "Unsaved changes" in _text(status)
    assert unsaved_for_export(state, view, hydrated, visible_note)


def test_explicitly_cleared_saved_note_stays_empty_after_reload(desk, monkeypatch):
    service, app, state = desk
    saved_note = "A saved qualification that the reviewer intentionally clears."
    saved = _save_baseline(service, state, saved_note)
    view = service.view(state["case_id"], saved["scenario_id"])
    key = draft_key(saved, view)
    persisted = {"active": key, "notes": {key: saved_note}, "dirty": False}

    cleared, value, _ = _change_draft(monkeypatch, app, "rv-review-reason", saved, "", persisted)
    assert value is no_update
    assert cleared["notes"][key] == ""
    assert cleared["dirty"]
    hydrated, visible_note, status = _change_draft(monkeypatch, app, None, saved, "", cleared)
    assert visible_note == ""
    assert hydrated["notes"][key] == ""
    assert hydrated["dirty"]
    assert "Unsaved changes" in _text(status)
    assert unsaved_for_export(saved, view, hydrated, visible_note)


def test_same_assessment_refresh_does_not_replace_a_draft_with_stale_text(desk, monkeypatch):
    service, app, state = desk
    note = "The latest explicit edit must survive an unrelated state update."
    view = service.view(state["case_id"])
    key = draft_key(state, view)
    persisted = {"active": key, "notes": {key: note}, "dirty": True}
    refreshed_state = {**state, "revision": state["revision"] + 1}

    refreshed, visible_note, status = _change_draft(
        monkeypatch, app, "rv-session", refreshed_state, "Stale rendered textarea text.", persisted
    )
    assert draft_key(refreshed_state, view) == key
    assert visible_note == note
    assert refreshed["notes"] == persisted["notes"]
    assert refreshed["dirty"]
    assert "Unsaved changes" in _text(status)


def test_unsaved_note_on_another_finding_still_requires_save_before_export(desk):
    service, _, state = desk
    saved = _save_baseline(service, state, "Saved attribution qualification.")
    view = service.view(state["case_id"], saved["scenario_id"])
    other = {**saved, "finding_id": "harbor:c1-v1"}
    store = {
        "active": draft_key(saved, view),
        "notes": {draft_key(other, view): "Unsaved observation qualification."},
        "dirty": False,
    }
    assert unsaved_for_export(saved, view, store, "Saved attribution qualification.")
    service.review(
        state["case_id"],
        other["finding_id"],
        saved["scenario_id"],
        "qualify",
        "Unsaved observation qualification.",
    )
    refreshed = service.view(state["case_id"], saved["scenario_id"])
    assert not unsaved_for_export(saved, refreshed, store, "Saved attribution qualification.")


@pytest.mark.parametrize("different_context", ["case", "assessment", "version", "fingerprint"])
def test_drafts_from_another_assessment_do_not_block_the_current_saved_brief(
    desk, different_context
):
    service, _, state = desk
    saved = _save_baseline(service, state, "Saved qualification.")
    view = service.view(state["case_id"], saved["scenario_id"])
    identity = json.loads(draft_key(saved, view))
    index = {"case": 0, "assessment": 2, "version": 3, "fingerprint": 4}[different_context]
    identity[index] = identity[index] + 1 if index == 3 else "different-context"
    store = {"active": draft_key(saved, view), "notes": {json.dumps(identity): "Unrelated draft"}}
    assert not unsaved_for_export(saved, view, store, "Saved qualification.")


@pytest.mark.parametrize("trigger", ["trail-brief", "trail-package", "trail-json"])
@pytest.mark.parametrize("draft_location", ["selected-finding", "another-finding"])
def test_download_rejects_unsaved_review_text(desk, monkeypatch, trigger, draft_location):
    service, app, state = desk
    saved_note = "Saved qualification."
    saved = _save_baseline(service, state, saved_note)
    view = service.view(state["case_id"], saved["scenario_id"])
    if draft_location == "selected-finding":
        draft_state, visible_note = saved, "New text that has not been saved."
    else:
        draft_state = {**saved, "finding_id": "harbor:c1-v1"}
        visible_note = saved_note
    store = {
        "active": draft_key(saved, view),
        "notes": {draft_key(draft_state, view): "New text that has not been saved."},
        "dirty": draft_location == "selected-finding",
    }
    before = service.repository.list_events(state["case_id"])
    monkeypatch.setattr(workspace_web, "ctx", SimpleNamespace(triggered_id=trigger))
    download, attempt = _callback(app, "trail-download.data")(1, 1, 1, saved, store, visible_note)
    assert download is no_update
    assert attempt["status"] == "failed" and "Unsaved changes" in attempt["message"]
    assert service.repository.list_events(state["case_id"]) == before
    assert "New text that has not been saved" not in service.export_brief(
        state["case_id"], scenario_id=None
    )
