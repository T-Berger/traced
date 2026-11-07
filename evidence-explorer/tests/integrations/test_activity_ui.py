"""ACT-01 evaluates complete saved snapshots without workflow or draft mutations."""

from __future__ import annotations

import hashlib
import runpy
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from dash import Dash, dcc, html
from dash.development.base_component import Component

from evidence_explorer.config import Settings
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.integrations import activity_ui
from evidence_explorer.review.integrations.brief_editor_state import EditorStore
from evidence_explorer.review.integrations.snapshots import _SnapshotRepository
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.navigation import apply_navigation

_fixtures = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/activity_preview.py"))
activity_fixture = _fixtures["activity_fixture"]
seed_workspace = _fixtures["seed_workspace"]

KIND = "saved_source_item"


def nodes(node):
    if isinstance(node, Component):
        yield node
        yield from nodes(getattr(node, "children", None))
    elif isinstance(node, (tuple, list)):
        for child in node:
            yield from nodes(child)


def text(node):
    if isinstance(node, str):
        return node
    if isinstance(node, (tuple, list)):
        return " ".join(text(child) for child in node)
    return text(getattr(node, "children", ""))


def stored_rows(service):
    result = {}
    with sqlite3.connect(service.repository.db_path) as connection:
        names = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (name,) in names:
            quoted = '"' + name.replace('"', '""') + '"'
            rows = connection.execute("SELECT * FROM " + quoted).fetchall()
            result[name] = hashlib.sha256(repr(sorted(rows, key=repr)).encode()).hexdigest()
    return result


@pytest.fixture
def desk(tmp_path):
    settings = Settings(data_dir=tmp_path, ollama_url="http://127.0.0.1:1")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    seed_workspace(service)
    app = Dash(__name__, suppress_callback_exceptions=True)
    app.layout = lambda: html.Div(
        [
            dcc.Store(id="rv-session"),
            dcc.Store(id="trail-planning-refresh"),
            dcc.Store(id="trail-planning-selected"),
            dcc.Store(id="trail-planning-edit"),
            activity_ui.activity_panel(),
        ]
    )
    activity_ui.register_activity_callbacks(app, service)
    callback = next(iter(app.callback_map.values()))["callback"].__wrapped__
    return service, app, callback


def run(
    callback,
    monkeypatch,
    state,
    *,
    trigger="trail-activity-refresh",
    kind=KIND,
    basis="publication",
    expected=None,
    selected=None,
    editor=None,
):
    monkeypatch.setattr(activity_ui, "ctx", SimpleNamespace(triggered_id=trigger))
    return callback(1, 1, kind, basis, state, None, selected, editor, expected)


def test_layout_has_explicit_inputs_and_only_memory_results(desk):
    service, app, _ = desk
    before = stored_rows(service)
    panel_nodes = list(nodes(activity_ui.activity_panel()))
    ids = [node.id for node in panel_nodes if hasattr(node, "id")]
    assert len(ids) == len(set(ids))
    for suffix in ("kind", "basis"):
        assert (
            next(
                node
                for node in panel_nodes
                if getattr(node, "id", None) == activity_ui.PREFIX + suffix
            ).value
            is None
        )
    assert {node.storage_type for node in panel_nodes if isinstance(node, dcc.Store)} == {"memory"}
    assert "scenario or a later release" in text(activity_ui.activity_panel())
    assert app.server.test_client().get("/_dash-layout").status_code == 200
    assert stored_rows(service) == before


def test_capture_uses_complete_export_and_one_read_connection(desk, monkeypatch):
    service, _, _ = desk
    connections = []
    original = _SnapshotRepository._connection

    @contextmanager
    def observed(self, *, write=False):
        with original(self, write=write) as connection:
            assert connection.in_transaction
            connections.append(id(connection))
            yield connection

    monkeypatch.setattr(_SnapshotRepository, "_connection", observed)
    monkeypatch.setattr(
        service,
        "view",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("A filtered view must not be used as evaluation input")
        ),
    )
    captured = activity_ui.capture_activity_context(service, "activity:cue")
    assert len(captured.bundle.records) == 13
    assert captured.planning.status == "confirmed"
    assert connections and len(set(connections)) == 1
    overview = text(activity_ui.render_activity_context(captured))
    assert captured.planning.revision.brief_id in overview
    assert captured.planning.revision.revision_id in overview
    assert captured.planning.revision.fields.alert_rule.id in overview
    assert captured.planning.revision.binding.scope.basis in overview
    assert all(ref in overview for ref in captured.planning.revision.fields.source_selection)


@pytest.mark.parametrize(
    ("case_id", "expected_status", "observation_count"),
    [
        ("activity:cue", "review_needed", 7),
        ("activity:below", "not_triggered", 6),
        ("activity:unknown", "unavailable", 7),
        ("activity:zero", "unavailable", 7),
    ],
)
def test_explicit_evaluate_uses_saved_rule_counts_and_honest_unknowns(
    desk, monkeypatch, case_id, expected_status, observation_count
):
    service, _, callback = desk
    before = stored_rows(service)
    state = session_for_case(service, case_id)
    state_before = deepcopy(state)
    preview = run(callback, monkeypatch, state)
    assert preview[4] is None and "30%" in text(preview[1])
    result = run(
        callback, monkeypatch, state, trigger="trail-activity-evaluate", expected=preview[0]
    )
    assert result[4]["status"] == expected_status
    assert result[4]["observation_count"] == observation_count
    assert result[4]["baseline_count"] == (0 if case_id.endswith("zero") else 5)
    assert result[4]["outside_window_record_ids"] == [case_id + ":outside:1"]
    assert "Synthetic case" in text(result[5])
    if expected_status != "unavailable":
        assert result[4]["evaluation"]["threshold_percent"] == 30
    assert state == state_before
    assert stored_rows(service) == before


def test_preview_requires_explicit_kind_and_basis_and_never_evaluates(desk, monkeypatch):
    service, _, callback = desk
    state = session_for_case(service, "activity:cue")

    def forbidden(*args, **kwargs):
        raise AssertionError("Only the explicit Evaluate action may calculate activity")

    monkeypatch.setattr(activity_ui, "evaluate_saved_activity", forbidden)
    for kind, basis in ((None, None), (KIND, None), (None, "publication")):
        preview = run(callback, monkeypatch, state, kind=kind, basis=basis)
        assert preview[3] is True and preview[4] is None
    assert run(callback, monkeypatch, state)[3] is False


def test_planning_edits_including_new_drafts_block_only_their_case(desk, monkeypatch):
    service, _, callback = desk
    state = session_for_case(service, "activity:cue")
    preview = run(callback, monkeypatch, state)
    new = EditorStore()
    draft = new.for_case(state["case_id"])
    draft.fields["question"] = "An unsaved new planning question."
    editor = new.model_dump(mode="json")
    blocked = run(
        callback,
        monkeypatch,
        state,
        trigger="trail-activity-evaluate",
        expected=preview[0],
        editor=editor,
    )
    assert blocked[4] is None and "Save or discard" in text(blocked[1])
    assert activity_ui.planning_is_dirty(state["case_id"], editor=editor)
    assert not activity_ui.planning_is_dirty("activity:below", editor=editor)
    selected = {"case_id": state["case_id"], "brief_id": None, "dirty": True}
    assert run(callback, monkeypatch, state, selected=selected)[3] is True


def test_form_case_refresh_and_planning_changes_clear_results(desk, monkeypatch):
    service, _, callback = desk
    state = session_for_case(service, "activity:cue")
    preview = run(callback, monkeypatch, state)
    assert (
        run(callback, monkeypatch, state, trigger="trail-activity-evaluate", expected=preview[0])[4]
        is not None
    )
    for trigger in (
        "trail-activity-basis",
        "trail-activity-kind",
        "rv-session",
        "trail-planning-refresh",
        "trail-planning-selected",
        "trail-planning-edit",
        "trail-activity-refresh",
    ):
        cleared = run(callback, monkeypatch, state, trigger=trigger, expected=preview[0])
        assert cleared[4] is None
        assert "Saved source-window result" not in text(cleared[5])


def test_stale_or_forged_display_context_is_rejected(desk, monkeypatch):
    service, _, callback = desk
    state = session_for_case(service, "activity:cue")
    preview = run(callback, monkeypatch, state)
    wrong_case = session_for_case(service, "activity:below")
    stale = run(
        callback, monkeypatch, wrong_case, trigger="trail-activity-evaluate", expected=preview[0]
    )
    assert stale[4] is None and "Refresh saved context" in text(stale[1])
    wrong_basis = run(
        callback,
        monkeypatch,
        state,
        basis="acquisition",
        trigger="trail-activity-evaluate",
        expected=preview[0],
    )
    assert wrong_basis[4] is None
    forged = {**preview[0], "confirmed": True, "coverage_fraction": 1, "threshold_percent": 1}
    rejected = run(callback, monkeypatch, state, trigger="trail-activity-evaluate", expected=forged)
    assert rejected[4] is None and "Refresh saved context" in text(rejected[1])
    saved = service.planning_briefs.state_for_case(state["case_id"])
    service.planning_briefs.save_draft(
        saved.revision.brief_id,
        saved.revision.fields.model_copy(update={"purpose": "Changed saved investigation scope."}),
        base_revision_id=saved.revision.revision_id,
        expected_head_revision_id=saved.revision.revision_id,
        author="Another local analyst",
    )
    changed = run(
        callback, monkeypatch, state, trigger="trail-activity-evaluate", expected=preview[0]
    )
    assert changed[4] is None and "Refresh saved context" in text(changed[1])


def test_contributor_buttons_use_existing_navigation_and_preserve_dirty_note(desk, monkeypatch):
    service, _, callback = desk
    state = session_for_case(service, "activity:cue")
    preview = run(callback, monkeypatch, state)
    result = run(
        callback, monkeypatch, state, trigger="trail-activity-evaluate", expected=preview[0]
    )
    buttons = [node for node in nodes(result[5]) if isinstance(getattr(node, "id", None), dict)]
    assert len(buttons) == 12
    button = buttons[-1].id
    assert button["type"] == "rv-next-action" and button["action"] == "source"
    assert button["fid"] == "" and button["location"].startswith("activity:")
    before, original = stored_rows(service), deepcopy(state)
    opened = apply_navigation(service, state, button)
    assert opened["record_id"] == button["rid"]
    assert opened["navigate_to"] == "review"
    dirty = apply_navigation(service, state, button, note="An unsaved analyst review note.")
    assert dirty["navigate_target"] == "rv-review-reason"
    assert state == original
    assert stored_rows(service) == before


def test_real_dash_callback_endpoint_evaluates_only_after_preview(desk):
    service, app, _ = desk
    state = session_for_case(service, "activity:cue")
    key, metadata = next(iter(app.callback_map.items()))
    values = {
        "trail-activity-evaluate.n_clicks": 0,
        "trail-activity-refresh.n_clicks": 1,
        "trail-activity-kind.value": KIND,
        "trail-activity-basis.value": "publication",
        "rv-session.data": state,
    }

    def post(changed):
        response = app.server.test_client().post(
            "/_dash-update-component",
            json={
                "output": key,
                "outputs": [
                    {"id": item.component_id, "property": item.component_property}
                    for item in metadata["output"]
                ],
                "inputs": [
                    {**item, "value": values.get(item["id"] + "." + item["property"])}
                    for item in metadata["inputs"]
                ],
                "state": [
                    {**item, "value": values.get(item["id"] + "." + item["property"])}
                    for item in metadata["state"]
                ],
                "changedPropIds": [changed],
            },
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        return response.get_json()["response"]

    preview = post("trail-activity-refresh.n_clicks")
    assert preview["trail-activity-result"]["data"] is None
    values["trail-activity-context.data"] = preview["trail-activity-context"]["data"]
    values["trail-activity-evaluate.n_clicks"] = 1
    evaluated = post("trail-activity-evaluate.n_clicks")
    assert evaluated["trail-activity-result"]["data"]["status"] == "review_needed"


def test_record_navigation_is_bounded_but_complete_exact_reference_list_is_inspectable(
    desk, monkeypatch
):
    service, _, callback = desk
    state = session_for_case(service, "activity:cue")
    preview = run(callback, monkeypatch, state)
    computed, _ = activity_ui.evaluate_activity_selection(
        service, state, KIND, "publication", preview[0]
    )
    larger = activity_fixture("cue", 55, 7)
    ids = tuple(record.id for record in larger.records if ":baseline:" in record.id)
    captured = activity_ui.SavedActivityContext(larger, None, "render-only-context")
    result = computed.model_copy(update={"baseline_record_ids": ids, "baseline_count": 55})
    rendered = activity_ui.render_activity_result(result, captured)
    content = text(rendered)
    assert "Showing the first 50 of 55 references" in content
    assert "All exact contributing record IDs (55)" in content
    assert ids[-1] in content
    assert "Outside-window record IDs" in content
    assert "Other omitted record IDs" in content
    assert "Unknown-time record IDs" in content
    assert "saved record snapshot" in content


def test_mounted_workspace_uses_one_activity_panel_and_existing_session_navigation(desk):
    from evidence_explorer.review.integrations.web import create_app

    service, _, _ = desk
    app = create_app(service)
    ids = [node.id for node in nodes(app.layout()) if hasattr(node, "id")]
    assert ids.count("trail-activity-panel") == 1
    dependencies = app.server.test_client().get("/_dash-dependencies").get_json()
    activity = next(row for row in dependencies if "trail-activity-result.data" in row["output"])
    assert {entry["id"] for entry in activity["inputs"]} >= {
        "rv-session",
        "trail-planning-selected",
        "trail-planning-edit",
    }
    assert "rv-session" not in activity["output"] and "rv-page" not in activity["output"]
