"""Saved media requires explicit reveal and recovers without changing evidence or reviews.

Every file used here is a labelled, harmless packaged SVG card. A False case-level
synthetic flag exercises the supplied-media warning branch, never real imagery.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from dash import html
from dash.development.base_component import Component

from evidence_explorer.config import Settings
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.controller import select_assessment, session_for_case
from evidence_explorer.review.integrations.media import build_media_demo_bundle, media_artifact_root
from evidence_explorer.review.integrations.media_ui import (
    resolve_media_selection,
    transition_media,
)
from evidence_explorer.review.integrations.packages import export_case_package
from evidence_explorer.review.integrations.web import create_app
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService


def components(value):
    if isinstance(value, Component):
        yield value
        yield from components(getattr(value, "children", None))
    elif isinstance(value, list | tuple):
        for child in value:
            yield from components(child)


def images(value):
    return [component for component in components(value) if isinstance(component, html.Img)]


def rendered_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple):
        return " ".join(rendered_text(child) for child in value)
    return rendered_text(getattr(value, "children", ""))


def snapshot(service):
    rows = {}
    with sqlite3.connect(service.repository.db_path) as connection:
        names = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (name,) in names:
            quoted = '"' + name.replace('"', '""') + '"'
            values = connection.execute("SELECT * FROM " + quoted).fetchall()
            rows[name] = (
                len(values),
                hashlib.sha256(repr(sorted(values, key=repr)).encode()).hexdigest(),
            )
    return rows


@pytest.fixture(params=[True, False], ids=["synthetic-case", "supplied-media-branch"])
def desk(tmp_path, request):
    settings = Settings(data_dir=tmp_path / "workspace")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, settings.data_dir / "artifacts")
    payload = build_media_demo_bundle().model_dump(mode="json")
    payload["case"]["id"] = "media:ui-harmless-test"
    payload["case"]["title"] = "Harmless SVG UI fixture — no real imagery"
    payload["case"]["synthetic"] = request.param
    bundle = EvidenceBundle.model_validate(payload)
    service.import_package(export_case_package(bundle, media_artifact_root()))
    frame = next(
        record
        for record in bundle.records
        if record.artifact_ref and record.metadata["frame_id"] == "frame-a"
    )
    path = service.artifact(bundle.case.id, frame.id)
    state = session_for_case(service, bundle.case.id)
    state["record_id"] = frame.id
    app = create_app(service)
    return SimpleNamespace(
        service=service,
        app=app,
        client=app.server.test_client(),
        state=state,
        bundle=bundle,
        frame=frame,
        path=path,
        original_bytes=path.read_bytes(),
        synthetic=request.param,
    )


def step(desk, trigger="rv-session", *, state=None, stored=None, clicks=None, event=None):
    state = desk.state if state is None else state
    original, before = deepcopy(state), snapshot(desk.service)
    original_store = deepcopy(stored)
    result = transition_media(
        desk.service, state, trigger, clicks=clicks, event=event, stored=stored
    )
    assert state == original
    assert stored == original_store
    assert snapshot(desk.service) == before
    return result


def action(desk, store, kind):
    return step(
        desk,
        {"type": "trail-media-action", "context": store["context"], "action": kind},
        clicks=[1],
        stored=store,
    )


def browser_event(desk, store, status, **extra):
    return step(
        desk,
        "trail-media-event",
        stored=store,
        event={
            "context": store["context"],
            "attempt": store["attempt"],
            "status": status,
            "sequence": 1,
            **extra,
        },
    )


def spy_artifacts(desk, monkeypatch):
    calls = []
    original = desk.service.artifact

    def checked(case_id, record_id, **kwargs):
        calls.append((case_id, record_id))
        return original(case_id, record_id, **kwargs)

    monkeypatch.setattr(desk.service, "artifact", checked)
    return calls


def test_initial_media_is_hidden_without_file_preflight_in_both_case_branches(desk, monkeypatch):
    calls = spy_artifacts(desk, monkeypatch)
    selection = resolve_media_selection(desk.service, desk.state)
    assert selection["selected_record_id"] == selection["media_record_id"] == desk.frame.id
    assert selection["expected_hash"] == desk.frame.content_hash
    body, store = step(desk)
    assert store["status"] == "hidden"
    assert store["attempt"] is None
    assert not images(body)
    assert not calls
    assert "Media is hidden" in rendered_text(body)
    assert any(
        isinstance(node, html.Button) and node.id.get("action") == "reveal"
        for node in components(body)
        if isinstance(getattr(node, "id", None), dict)
    )
    if not desk.synthetic:
        assert "may contain graphic content" in rendered_text(body)


def test_only_explicit_reveal_checks_the_file_and_load_or_hide_never_repeat_preflight(
    desk, monkeypatch
):
    calls = spy_artifacts(desk, monkeypatch)
    _, hidden = step(desk)
    body, loading = action(desk, hidden, "reveal")
    assert calls == [(desk.state["case_id"], desk.frame.id)]
    assert loading["status"] == "loading"
    image = images(body)[0]
    assert image.style.get("display") == "none"
    assert parse_qs(urlsplit(image.src).query)["record_id"] == [desk.frame.id]
    body, ready = browser_event(desk, loading, "loaded")
    assert ready["status"] == "ready"
    assert images(body)[0].src == image.src
    assert images(body)[0].style.get("display") != "none"
    body, same = step(desk, stored=ready)
    assert same == ready
    assert images(body)[0].src == image.src
    body, hidden_again = action(desk, ready, "hide")
    assert hidden_again["status"] == "hidden"
    assert not images(body)
    assert calls == [(desk.state["case_id"], desk.frame.id)]


def test_mounted_reveal_with_zero_clicks_does_not_load_a_file(desk, monkeypatch):
    calls = spy_artifacts(desk, monkeypatch)
    _, hidden = step(desk)
    trigger = {"type": "trail-media-action", "context": hidden["context"], "action": "reveal"}
    body, store = step(desk, trigger, clicks=[0], stored=hidden)
    assert store == hidden
    assert not images(body)
    assert not calls


@pytest.mark.parametrize("failure", ["missing", "changed"])
def test_preflight_failure_is_inline_and_retry_rechecks_restored_exact_bytes(
    desk, monkeypatch, failure
):
    if failure == "missing":
        desk.path.unlink()
    else:
        desk.path.write_bytes(b"Harmless invalid test bytes that do not match the saved hash.")
    calls = spy_artifacts(desk, monkeypatch)
    body, hidden = step(desk)
    assert hidden["status"] == "hidden" and not images(body)
    assert not calls
    body, failed = action(desk, hidden, "reveal")
    assert len(calls) == 1
    assert failed["status"] == "error"
    assert failed["error"] == ("unavailable" if failure == "missing" else "changed")
    assert not images(body)
    assert any(getattr(node, "role", None) == "alert" for node in components(body))
    assert any(
        isinstance(node, html.Button) and node.id.get("action") == "retry"
        for node in components(body)
        if isinstance(getattr(node, "id", None), dict)
    )
    assert str(desk.path) not in rendered_text(body)
    assert (
        desk.client.get(
            "/trail-artifact",
            query_string={"case_id": desk.state["case_id"], "record_id": desk.frame.id},
        ).status_code
        == 404
    )
    desk.path.write_bytes(desk.original_bytes)
    before_retry = len(calls)
    body, retried = action(desk, failed, "retry")
    assert len(calls) == before_retry + 1
    assert retried["status"] == "loading"
    assert retried["attempt"] != failed["attempt"]
    assert images(body)
    response = desk.client.get(images(body)[0].src)
    assert response.status_code == 200
    assert response.data == desk.original_bytes


def test_post_preflight_404_and_decode_error_remove_image_and_allow_fresh_attempt(desk):
    _, hidden = step(desk)
    body, first = action(desk, hidden, "reveal")
    first_url = images(body)[0].src
    # The file can disappear after preflight but before the independent HTTP hash guard.
    desk.path.unlink()
    assert desk.client.get(first_url).status_code == 404
    body, error = browser_event(
        desk, first, "error", message="RAW_BROWSER_DIAGNOSTIC/private/path must not be echoed"
    )
    assert error["status"] == "error" and error["error"] == "display"
    assert not images(body)
    assert "could not be displayed" in rendered_text(body)
    assert "RAW_BROWSER_DIAGNOSTIC" not in rendered_text(body)
    desk.path.write_bytes(desk.original_bytes)
    body, second = action(desk, error, "retry")
    second_url = images(body)[0].src
    assert second_url != first_url
    assert second["attempt"] != first["attempt"]
    # An old attempt must not fail a newer pending attempt or clear its successful image.
    stale_error = {
        "context": first["context"],
        "attempt": first["attempt"],
        "status": "error",
        "sequence": 99,
    }
    body, still_loading = step(desk, "trail-media-event", stored=second, event=stale_error)
    assert still_loading == second
    assert images(body)[0].src == second_url
    body, ready = browser_event(desk, second, "loaded")
    body, still_ready = step(desk, "trail-media-event", stored=ready, event=stale_error)
    assert still_ready == ready
    assert images(body)[0].src == second_url
    assert desk.client.get(second_url).data == desk.original_bytes


def test_metadata_resolves_only_its_documented_frame_and_clip_never_guesses(desk, monkeypatch):
    calls = spy_artifacts(desk, monkeypatch)
    frame = next(
        record
        for record in desk.bundle.records
        if record.artifact_ref and record.metadata["frame_id"] == "frame-b"
    )
    metadata_id = next(
        link.source.id
        for link in desk.bundle.links
        if link.relation == "part_of"
        and link.target.type == "record"
        and link.target.id == frame.id
        and link.source.type == "record"
    )
    state = {**desk.state, "record_id": metadata_id}
    selected = resolve_media_selection(desk.service, state)
    assert selected["media_record_id"] == frame.id
    assert selected["selected_record_id"] == metadata_id
    assert selected["metadata_record"]["id"] == metadata_id
    body, store = step(desk, state=state)
    assert not images(body) and store["status"] == "hidden"
    for record_id in (
        None,
        "absent-record",
        next(record.id for record in desk.bundle.records if record.kind == "supplied_clip"),
    ):
        state = {**desk.state, "record_id": record_id}
        assert resolve_media_selection(desk.service, state) is None
        body, store = step(desk, state=state)
        assert body == "" and store == {}
    assert not calls


@pytest.mark.parametrize(
    "change", ["case", "scenario", "version", "record", "selected-metadata", "fingerprint"]
)
def test_selection_scope_changes_reset_reveal_and_ignore_old_actions_or_events(
    desk, monkeypatch, change
):
    state = deepcopy(desk.state)
    if change == "version":
        scenario = desk.service.start_scenario(state["case_id"], state["finding_id"])
        select_assessment(desk.service, state, scenario["id"])
    _, hidden = step(desk, state=state)
    trigger = {"type": "trail-media-action", "context": hidden["context"], "action": "reveal"}
    _, loading = step(desk, trigger, state=state, clicks=[1], stored=hidden)
    _, ready = step(
        desk,
        "trail-media-event",
        state=state,
        stored=loading,
        event={"context": loading["context"], "attempt": loading["attempt"], "status": "loaded"},
    )
    if change == "case":
        payload = desk.bundle.model_dump(mode="json")
        payload["case"]["id"] = "media:another-harmless-case"
        case_id = desk.service.import_package(
            export_case_package(EvidenceBundle.model_validate(payload), media_artifact_root())
        )
        state = session_for_case(desk.service, case_id)
        state["record_id"] = desk.frame.id
    elif change == "scenario":
        scenario = desk.service.start_scenario(state["case_id"], state["finding_id"])
        select_assessment(desk.service, state, scenario["id"])
    elif change == "version":
        desk.service.exclude(
            state["scenario_id"],
            desk.frame.id,
            True,
            "Test changed review scope.",
            state["scenario_version"],
        )
        select_assessment(desk.service, state, state["scenario_id"])
    elif change == "record":
        state["record_id"] = next(
            record.id
            for record in desk.bundle.records
            if record.artifact_ref and record.metadata["frame_id"] == "frame-a-copy"
        )
    elif change == "selected-metadata":
        state["record_id"] = next(
            link.source.id
            for link in desk.bundle.links
            if link.relation == "part_of"
            and link.target.type == "record"
            and link.target.id == desk.frame.id
        )
    else:
        payload = desk.service.get_case(state["case_id"])
        payload["records"].append({**payload["records"][0], "id": "media:later-context-record"})
        desk.service.import_bundle(payload)
        state["case_fingerprint"] = desk.service.case_fingerprint(state["case_id"])
    calls = spy_artifacts(desk, monkeypatch)
    body, reset = step(desk, state=state, stored=ready)
    assert reset["status"] == "hidden"
    assert reset["context"] != ready["context"]
    assert not images(body)
    body, ignored = step(desk, trigger, state=state, clicks=[1], stored=ready)
    assert ignored == reset and not images(body)
    body, ignored = step(
        desk,
        "trail-media-event",
        state=state,
        stored=reset,
        event={"context": ready["context"], "attempt": ready["attempt"], "status": "loaded"},
    )
    assert ignored == reset and not images(body)
    assert not calls


@pytest.mark.parametrize("pin", ["case_fingerprint", "scenario_version"])
def test_stale_session_pin_hides_media_without_file_access(desk, monkeypatch, pin):
    state = deepcopy(desk.state)
    state[pin] = "older-evidence-fingerprint" if pin == "case_fingerprint" else 999
    calls = spy_artifacts(desk, monkeypatch)
    with pytest.raises(ValueError):
        resolve_media_selection(desk.service, state)
    body, store = step(desk, state=state)
    assert not images(body)
    assert store == {}
    assert any(getattr(node, "role", None) == "alert" for node in components(body))
    assert not calls


def test_reveal_load_failure_hide_and_retry_do_not_change_saved_decisions_or_session(desk):
    baseline = desk.service.baseline_scenario(desk.state["case_id"], desk.state["finding_id"])
    desk.service.review(
        desk.state["case_id"],
        desk.state["finding_id"],
        baseline["id"],
        "qualify",
        "A test-only human qualification remains separate from media display.",
    )
    original, before = deepcopy(desk.state), snapshot(desk.service)
    _, hidden = step(desk)
    _, loading = action(desk, hidden, "reveal")
    _, error = browser_event(desk, loading, "error")
    _, retry = action(desk, error, "retry")
    _, ready = browser_event(desk, retry, "loaded")
    body, hidden = action(desk, ready, "hide")
    assert not images(body)
    assert desk.state == original
    assert snapshot(desk.service) == before


def test_registered_media_callback_has_only_display_outputs_and_reveals_explicitly(desk):
    key = next(key for key in desk.app.callback_map if "trail-media.children" in key)
    definition = desk.app.callback_map[key]
    outputs = [
        {"id": item.component_id, "property": item.component_property}
        for item in definition["output"]
    ]
    assert outputs == [
        {"id": "trail-media", "property": "children"},
        {"id": "trail-media-view", "property": "data"},
    ]

    def invoke(trigger, stored, clicks):
        inputs = [
            {
                **item,
                "value": desk.state
                if item["id"] == "rv-session"
                else clicks
                if item["property"] == "n_clicks"
                else None,
            }
            for item in definition["inputs"]
        ]
        changed = (
            "rv-session.data"
            if trigger == "rv-session"
            else json.dumps(trigger, sort_keys=True, separators=(",", ":")) + ".n_clicks"
        )
        response = desk.client.post(
            "/_dash-update-component",
            json={
                "output": key,
                "outputs": outputs,
                "inputs": inputs,
                "state": [{"id": "trail-media-view", "property": "data", "value": stored}],
                "changedPropIds": [changed],
            },
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        return response.get_json()["response"]

    before = snapshot(desk.service)
    initial = invoke("rv-session", None, [])
    assert set(initial) == {"trail-media", "trail-media-view"}
    serialized = json.dumps(initial["trail-media"]["children"])
    assert '"type": "Img"' not in serialized
    store = initial["trail-media-view"]["data"]
    result = invoke(
        {"type": "trail-media-action", "context": store["context"], "action": "reveal"}, store, [1]
    )
    assert result["trail-media-view"]["data"]["status"] == "loading"
    assert '"type": "Img"' in json.dumps(result["trail-media"]["children"])
    assert snapshot(desk.service) == before
