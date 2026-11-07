"""Native drafting binds exact evidence and keeps unfinished analyst text on navigation."""

import json
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace

import pytest
from dash import Dash, dcc, html, no_update
from dash.development.base_component import Component

from evidence_explorer.config import Settings
from evidence_explorer.investigation import intake_ui
from evidence_explorer.investigation.bundle import (
    InvestigationDraft,
    build_saved_investigation_bundle,
)
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.drafts import draft_key, unsaved_for_export
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.source_bundles import build_source_bundle
from evidence_explorer.source_contracts import SourceImport

QUOTE = "creationDate: 2007-10-09"


def nodes(node):
    if isinstance(node, Component):
        yield node
        yield from nodes(getattr(node, "children", None))
    elif isinstance(node, list | tuple):
        for child in node:
            yield from nodes(child)


def text(node):
    if isinstance(node, str):
        return node
    if isinstance(node, list | tuple):
        return " ".join(text(child) for child in node)
    return text(getattr(node, "children", ""))


@pytest.fixture
def desk(tmp_path):
    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    source = SourceImport.model_validate(
        {
            "source_schema": "1.0",
            "case": {
                "title": "Fictional saved domain",
                "question": "When was it created?",
                "synthetic": True,
            },
            "items": [
                {
                    "platform": "news",
                    "channel_id": "fictional",
                    "channel_name": "Fictional registry record",
                    "native_id": "1",
                    "text": "domainName: example.org\n" + QUOTE,
                    "acquired_at": "2026-11-07T00:00:00Z",
                }
            ],
        }
    )
    bundle = build_source_bundle(source).model_dump(mode="json")
    case_id = service.import_bundle(bundle)
    selected = session_for_case(service, case_id)
    app = Dash(__name__, suppress_callback_exceptions=True)
    app.layout = html.Div([intake_ui.saved_investigation_tools(), intake_ui.manual_draft_panel()])
    intake_ui.register_intake_callbacks(app, service)

    def callback(trigger, output=None):
        return next(
            value["callback"].__wrapped__
            for key, value in app.callback_map.items()
            if any(item["id"] == trigger for item in value["inputs"])
            and (output is None or output in key)
        )

    return service, selected, bundle, app, callback


def begin(desk):
    service, selected, bundle, app, callback = desk
    result = callback("investigation-draft-start")(1, selected)
    assert isinstance(result[0], str)
    return result[0], bundle["records"][0]["id"]


def save(desk, token, record_id, *, state=None, quote=QUOTE, note="", stored=None):
    _service, selected, _bundle, _app, callback = desk
    return callback("investigation-draft-save")(
        1,
        token,
        state or selected,
        record_id,
        "example.org was created in 2007.",
        "The creation date is 2007-10-09.",
        quote,
        "",
        stored if stored is not None else {"notes": {"earlier": "Keep this other note"}},
        note,
    )


def test_layout_uses_shared_controls_and_keeps_native_form_text_in_session(desk):
    _service, _state, _bundle, app, _callback = desk
    components = list(nodes(app.layout))
    ids = [node.id for node in components if hasattr(node, "id")]
    assert len(ids) == len(set(ids))
    assert "Open saved investigator draft" in text(app.layout)
    assert "Write a checkable claim" in text(app.layout)
    fields = [node for node in components if isinstance(node, dcc.Textarea)]
    assert len(fields) == 4
    assert all(node.persistence_type == "session" for node in fields)
    assert all(node.className == "rv-textarea" for node in fields)
    assert not any("model" in str(getattr(node, "id", "")) for node in components)


def test_start_is_read_only_and_requires_an_explicit_selected_case(desk):
    service, selected, _bundle, _app, callback = desk
    before = deepcopy(service.get_case(selected["case_id"]))
    denied = callback("investigation-draft-start")(1, {})
    assert all(value is no_update for value in denied[:7])
    token, record_id = begin(desk)
    assert service.get_case(selected["case_id"]) == before
    available = callback("rv-session", "investigation-draft-save.disabled")
    assert available(selected, token, None)[0] is True
    assert available(selected, token, record_id)[0] is False


def test_source_is_exact_saved_text_and_forged_record_is_not_shown(desk):
    _service, selected, bundle, _app, callback = desk
    token, record_id = begin(desk)
    source = callback("investigation-draft-record", "investigation-draft-source.children")
    assert text(source(record_id, token, selected)) == bundle["records"][0]["text"]
    assert "context changed" in text(source("unknown-record", token, selected))


def test_switching_case_or_forging_context_disables_save_without_clearing_fields(desk):
    service, selected, bundle, app, callback = desk
    token, record_id = begin(desk)
    other = deepcopy(bundle)
    other["case"]["id"] = "different-case"
    other_id = service.import_bundle(other)
    other_state = session_for_case(service, other_id)
    available = callback("rv-session", "investigation-draft-save.disabled")
    assert available(other_state, token, record_id)[0] is True
    assert "different case" in text(available(other_state, token, record_id)[1])
    assert available(selected, {"context": "forged"}, record_id)[0] is True
    assert available(selected, token, record_id)[0] is False
    for item in app.callback_map.values():
        if any(value["id"] == "rv-session" for value in item["inputs"]):
            outputs = item["output"] if isinstance(item["output"], list) else [item["output"]]
            assert not any(
                value.component_id
                in {
                    "investigation-draft-claim",
                    "investigation-draft-step",
                    "investigation-draft-quote",
                    "investigation-draft-assumption",
                }
                for value in outputs
            )


def test_save_opens_exact_new_claim_and_preserves_original_review_note(desk):
    service, selected, _bundle, _app, _callback = desk
    token, record_id = begin(desk)
    old_key = draft_key(selected, service.view(selected["case_id"]))
    result = save(desk, token, record_id, note="Unsaved qualification on the original finding")
    session, page, kept, message, cleared = result
    assert session["case_id"] == selected["case_id"]
    assert session["finding_id"] != selected["finding_id"]
    assert session["scenario_id"]
    assert page == "review"
    assert kept["notes"][old_key] == "Unsaved qualification on the original finding"
    original_view = service.view(selected["case_id"], selected.get("scenario_id"))
    original_state = {**selected, "case_fingerprint": original_view["case_fingerprint"]}
    restored_key = draft_key(original_state, original_view)
    assert restored_key != old_key
    assert kept["notes"][restored_key] == "Unsaved qualification on the original finding"
    assert (
        draft_key(session, service.view(session["case_id"], session["scenario_id"]))
        not in kept["notes"]
    )
    assert kept["notes"]["earlier"] == "Keep this other note"
    assert "Manual draft saved" in text(message)
    assert cleared is None
    view = service.view(session["case_id"], session["scenario_id"])
    finding = next(
        item for item in view["bundle"]["findings"] if item["snapshot_id"] == session["finding_id"]
    )
    assert finding["details"]["role"] == "claim"
    assert not service.get_case(session["case_id"])["reviews"]


def test_bad_quote_and_stale_case_leave_saved_evidence_and_form_unchanged(desk):
    service, selected, _bundle, _app, callback = desk
    token, record_id = begin(desk)
    before = service.get_case(selected["case_id"])
    rejected = save(desk, token, record_id, quote="invented quote")
    assert all(rejected[index] is no_update for index in [0, 1, 2, 4])
    assert "exact quote" in text(rejected[3])
    assert service.get_case(selected["case_id"]) == before
    save(desk, token, record_id)
    after = service.get_case(selected["case_id"])
    stale = save(desk, token, record_id)
    assert stale[0] is no_update
    assert "changed" in text(stale[3])
    assert service.get_case(selected["case_id"]) == after
    assert callback("rv-session", "investigation-draft-save.disabled")(selected, token, record_id)[
        0
    ]


def saved_folder(desk):
    service, selected, _bundle, _app, _callback = desk
    token, record_id = begin(desk)
    save(desk, token, record_id)
    bundle = service.get_case(selected["case_id"])
    name = "20261107T000000Z-saved-draft"
    folder = service.settings.data_dir / "investigations" / name
    folder.mkdir(parents=True)
    (folder / "bundle.json").write_text(json.dumps(bundle))
    (folder / "investigation.json").write_text(
        json.dumps(
            {
                "schema": "traced.investigation/1",
                "bundle": {
                    "path": "bundle.json",
                    "fingerprint": EvidenceBundle.model_validate(bundle).fingerprint(),
                },
            }
        )
    )
    return name, folder


def test_saved_draft_preview_then_open_uses_server_fingerprint_and_exact_run(desk):
    service, _selected, _bundle, _app, callback = desk
    name, _folder = saved_folder(desk)
    options, message = callback(
        "investigation-saved-refresh", "investigation-saved-picker.options"
    )(1)
    assert options[0]["value"] == name
    assert "1 saved drafts" in text(message)
    preview, token, disabled = callback("investigation-saved-picker")(name, 1)
    assert not disabled
    assert "Draft author: manual" in text(preview)
    assert "Human review required" in text(preview)
    result = callback("investigation-saved-open")(1, name, token, {}, {}, None)
    assert result[2]["case_id"] == result[1]
    assert result[2]["finding_id"].startswith("investigation:finding:")
    assert result[3] == "review"
    assert service.view(result[1], result[2]["scenario_id"])["is_baseline"]


def test_saved_draft_forgery_and_changes_require_fresh_preview(desk):
    _service, _selected, _bundle, _app, callback = desk
    name, folder = saved_folder(desk)
    _preview, token, _disabled = callback("investigation-saved-picker")(name, 1)
    forged = callback("investigation-saved-open")(1, name, {"fingerprint": "pretend"}, {}, {}, None)
    assert all(value is no_update for value in forged[:5])
    assert "preview" in text(forged[5])
    content = json.loads((folder / "bundle.json").read_text())
    content["case"]["title"] = "Changed after preview"
    (folder / "bundle.json").write_text(json.dumps(content))
    changed = callback("investigation-saved-open")(1, name, token, {}, {}, None)
    assert all(value is no_update for value in changed[:5])
    assert "unavailable" in text(changed[5])


def test_opening_older_saved_draft_uses_its_previewed_run(desk):
    service, selected, _bundle, _app, callback = desk
    name, _folder = saved_folder(desk)
    preview = intake_ui.workspace.preview_saved_investigation(service, name)
    first_session = intake_ui.workspace.open_investigation_session(service, selected["case_id"])
    newer = intake_ui.workspace.save_manual_draft(
        service,
        first_session,
        claim="The record states 2007 as the creation year.",
        step="The creation date is 2007-10-09.",
        record_id=first_session["record_id"],
        quote=QUOTE,
    )
    assert newer["run_id"] != preview["run_id"]
    _card, token, _disabled = callback("investigation-saved-picker")(name, 1)
    opened = callback("investigation-saved-open")(1, name, token, {}, {}, None)
    view = service.view(opened[1], opened[2]["scenario_id"])
    assert view["scenario"]["base_run_id"] == preview["run_id"]


def test_note_rekey_refuses_a_changed_original_dependency(desk, monkeypatch):
    service, selected, _bundle, _app, _callback = desk
    before = service.get_case(selected["case_id"])
    view = service.view(selected["case_id"])
    kept = intake_ui.preserve_review_draft(service, selected, {}, "Keep on old evidence")
    changed = deepcopy(before)
    changed["records"][0]["text"] = "A changed saved source"
    # The real merger forbids this mutation; the helper independently refuses it too.
    monkeypatch.setattr(intake_ui, "workspace_snapshot", lambda service: nullcontext(service))
    original = service.get_case
    service.get_case = lambda _case: changed
    try:
        assert intake_ui.rekey_exact_review_draft(service, selected, before, view, kept) == kept
    finally:
        service.get_case = original


def test_save_keeps_notes_for_other_findings_and_their_own_assessments(desk):
    service, selected, _bundle, _app, callback = desk
    token, record_id = begin(desk)
    first_claim = save(desk, token, record_id)[0]
    before = service.view(selected["case_id"])
    original = {**selected, "case_fingerprint": before["case_fingerprint"]}
    scenario = service.start_scenario(selected["case_id"], selected["finding_id"])
    scenario_state = {
        **original,
        "scenario_id": scenario["id"],
        "scenario_version": scenario["version"],
    }
    scenario_view = service.view(selected["case_id"], scenario["id"])
    original_key = draft_key(original, before)
    scenario_key = draft_key(scenario_state, scenario_view)
    invalid_keys = [
        "not a draft key",
        json.dumps([selected["case_id"], selected["finding_id"], "baseline", 1, "stale"]),
        draft_key({**original, "case_id": "another-case"}, before),
        draft_key({**original, "finding_id": "not-an-original-finding"}, before),
        draft_key({**scenario_state, "scenario_version": 99}, scenario_view),
    ]
    notes = {
        original_key: "Original source finding note",
        scenario_key: "Separate assessment note",
        **{key: "Do not move" for key in invalid_keys},
    }
    started = callback("investigation-draft-start")(2, first_claim)
    result = save(desk, started[0], record_id, state=first_claim, stored={"notes": notes})
    kept = result[2]
    after = service.view(selected["case_id"])
    restored = {**original, "case_fingerprint": after["case_fingerprint"]}
    restored_scenario = {**scenario_state, "case_fingerprint": after["case_fingerprint"]}
    after_scenario = service.view(selected["case_id"], scenario["id"])
    assert kept["notes"][draft_key(restored, after)] == notes[original_key]
    assert kept["notes"][draft_key(restored_scenario, after_scenario)] == notes[scenario_key]
    assert all(kept["notes"][key] == value for key, value in notes.items())
    assert unsaved_for_export(restored, after, kept)
    assert unsaved_for_export(restored_scenario, after_scenario, kept)
    assert len(kept["notes"]) == len(notes) + 4  # Active note old/new; two cached new keys.
    new_session = result[0]
    assert (
        draft_key(new_session, service.view(selected["case_id"], new_session["scenario_id"]))
        not in kept["notes"]
    )
    assert not service.get_case(selected["case_id"])["reviews"]


def test_rekey_refuses_changed_assessment_and_keeps_newer_destination_note(desk):
    service, selected, _bundle, _app, _callback = desk
    scenario = service.start_scenario(selected["case_id"], selected["finding_id"])
    scenario_state = {
        **selected,
        "scenario_id": scenario["id"],
        "scenario_version": scenario["version"],
    }
    before = service.get_case(selected["case_id"])
    before_view = service.view(selected["case_id"])
    old_key = draft_key(selected, before_view)
    scenario_key = draft_key(scenario_state, service.view(selected["case_id"], scenario["id"]))
    token, record_id = begin(desk)
    save(desk, token, record_id)
    service.exclude(
        scenario["id"], record_id, True, "Review without this source", scenario["version"]
    )
    after = service.view(selected["case_id"])
    restored = {**selected, "case_fingerprint": after["case_fingerprint"]}
    new_key = draft_key(restored, after)
    kept = {"notes": {old_key: "Older text", new_key: "Newer text", scenario_key: "Old scenario"}}
    result = intake_ui.rekey_exact_review_draft(service, selected, before, before_view, kept)
    assert result == kept


def test_same_case_saved_import_keeps_active_and_cached_notes(desk):
    service, selected, _bundle, _app, callback = desk
    token, record_id = begin(desk)
    first_claim = save(desk, token, record_id)[0]
    before = service.get_case(selected["case_id"])
    before_view = service.view(selected["case_id"])
    original = {**selected, "case_fingerprint": before_view["case_fingerprint"]}
    old_key = draft_key(original, before_view)
    draft = InvestigationDraft.model_validate(
        {
            "claims": [
                {
                    "id": "C1",
                    "statement": "Another proposed claim",
                    "steps": [],
                    "assumption_ids": [],
                }
            ],
            "assumptions": [],
            "gaps": [],
        }
    )
    built = build_saved_investigation_bundle(
        before,
        draft,
        goal="When was it created?",
        created_at="2026-11-07T14:00:00Z",
        record_ids=[record_id],
    )
    name = "20261107T140000Z-another-draft"
    folder = service.settings.data_dir / "investigations" / name
    folder.mkdir(parents=True)
    (folder / "bundle.json").write_text(json.dumps(built.bundle))
    (folder / "investigation.json").write_text(
        json.dumps(
            {
                "schema": "traced.investigation/1",
                "bundle": {
                    "path": "bundle.json",
                    "fingerprint": EvidenceBundle.model_validate(built.bundle).fingerprint(),
                },
            }
        )
    )
    _preview, preview_token, _disabled = callback("investigation-saved-picker")(name, 1)
    opened = callback("investigation-saved-open")(
        1,
        name,
        preview_token,
        first_claim,
        {"notes": {old_key: "Cached original note"}},
        "Active claim note",
    )
    after = service.view(selected["case_id"])
    restored = {**original, "case_fingerprint": after["case_fingerprint"]}
    assert opened[2]["finding_id"] != first_claim["finding_id"]
    assert opened[4]["notes"][draft_key(restored, after)] == "Cached original note"
    first_view = service.view(selected["case_id"], first_claim["scenario_id"])
    first_restored = {**first_claim, "case_fingerprint": after["case_fingerprint"]}
    assert opened[4]["notes"][draft_key(first_restored, first_view)] == "Active claim note"
    assert unsaved_for_export(restored, after, opened[4])
    assert not service.get_case(selected["case_id"])["reviews"]


def test_read_only_workspace_cannot_start_or_open_saved_drafts(desk):
    service, selected, _bundle, _app, callback = desk
    name, _folder = saved_folder(desk)
    service.settings = replace(service.settings, read_only=True)
    result = callback("investigation-draft-start")(1, selected)
    assert all(value is no_update for value in result[:7])
    assert "read-only" in text(result[7])
    _preview, token, disabled = callback("investigation-saved-picker")(name, 1)
    assert disabled
    opened = callback("investigation-saved-open")(1, name, token, {}, {}, None)
    assert all(value is no_update for value in opened[:5])
    assert "read-only" in text(opened[5])
