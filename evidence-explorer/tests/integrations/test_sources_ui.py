"""Saved-source forms validate before saving; comparison filters preserve evidence."""

import base64
import json
from types import SimpleNamespace

import pytest
from dash import Dash, dcc, html, no_update
from dash.development.base_component import Component

from evidence_explorer.config import Settings
from evidence_explorer.review.integrations import sources_ui
from evidence_explorer.review.integrations.sources import parse_source_import
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService


def components(node):
    if isinstance(node, Component):
        yield node
        yield from components(getattr(node, "children", None))
    elif isinstance(node, (list, tuple)):
        for child in node:
            yield from components(child)


def text(node):
    if isinstance(node, str):
        return node
    if isinstance(node, (list, tuple)):
        return " ".join(text(child) for child in node)
    return text(getattr(node, "children", ""))


@pytest.fixture
def source_form(tmp_path):
    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    app = Dash(__name__, suppress_callback_exceptions=True)
    app.layout = lambda: html.Div([sources_ui.source_import_tools(), sources_ui.source_browser()])
    sources_ui.register_source_callbacks(app, service)

    def callback(output):
        return next(
            item["callback"].__wrapped__ for key, item in app.callback_map.items() if output in key
        )

    return service, app, callback, sources_ui.example_source_import()


def encoded(payload):
    return "data:application/json;base64," + base64.b64encode(json.dumps(payload).encode()).decode()


def test_source_upload_preview_has_no_write_then_imports_a_reviewable_case(
    source_form, monkeypatch
):
    service, _, callback, payload = source_form
    content = encoded(payload)
    monkeypatch.setattr(sources_ui, "ctx", SimpleNamespace(triggered_id="trail-source-upload"))
    card, token, disabled, notice = callback("trail-source-preview.children")(
        content, 0, "", "sources.json"
    )
    assert payload["case"]["title"] in text(card)
    assert disabled is False
    assert set(token) == {"fingerprint", "source"}
    assert "No sources have been saved" in text(notice)
    assert service.list_cases() == []

    result = callback("trail-source-panel.open")(1, token, content, "sources.json", "")
    options, case_id, state, message, upload, pasted, page, opened, success = result
    assert case_id == state["case_id"]
    assert state["finding_id"] and state["record_id"]
    assert options == [{"label": payload["case"]["title"], "value": case_id}]
    assert upload is None and pasted == "" and page == "review" and opened is False
    assert "Sources imported" in text(message) and "Sources imported" in text(success)
    bundle = service.get_case(case_id)
    assert len(bundle["records"]) == 5
    assert bundle["case"]["synthetic"] is True


def test_paste_edit_invalidates_validation_and_stale_commit_saves_nothing(source_form, monkeypatch):
    service, _, callback, payload = source_form
    monkeypatch.setattr(sources_ui, "ctx", SimpleNamespace(triggered_id="trail-source-validate"))
    raw = json.dumps(payload)
    _, token, disabled, _ = callback("trail-source-preview.children")(None, 1, raw, None)
    assert disabled is False
    payload["items"][0]["text"] = "Changed saved text."
    changed = json.dumps(payload)
    monkeypatch.setattr(sources_ui, "ctx", SimpleNamespace(triggered_id="trail-source-paste"))
    _, invalidated, disabled, _ = callback("trail-source-preview.children")(None, 1, changed, None)
    assert invalidated is None and disabled is True
    result = callback("trail-source-panel.open")(1, token, None, None, changed)
    assert all(value is no_update for value in result[:3])
    assert "preview" in text(result[3]).lower() or "validate" in text(result[3]).lower()
    assert service.list_cases() == []


def test_source_example_download_is_valid_and_content_is_not_kept_in_a_store(source_form):
    _, app, callback, _ = source_form
    downloaded = callback("trail-source-download.data")(1)
    assert downloaded["filename"] == "saved-sources-example.json"
    bundle = parse_source_import(downloaded["content"])
    assert bundle.case.synthetic is True
    assert len(bundle.records) == 5
    assert any(record.published_at is None for record in bundle.records)
    nodes = list(components(app.layout()))
    ids = [node.id for node in nodes if hasattr(node, "id")]
    assert len(ids) == len(set(ids))
    stores = [node for node in nodes if isinstance(node, dcc.Store)]
    assert len(stores) == 2 and all(store.storage_type == "memory" for store in stores)
    assert {store.id for store in stores} == {"trail-source-token", "trail-source-filter-case"}


def test_timeline_filters_are_read_only_and_keep_publication_separate_from_collection(source_form):
    service, _, callback, payload = source_form
    raw = json.dumps(payload)
    summary = service.preview_source_import(raw)
    case_id = service.commit_source_import(raw, expected_fingerprint=summary["fingerprint"])
    before = service.repository.get_bundle(case_id).fingerprint()
    browse = callback("trail-source-timeline.children")
    platforms, channels, summary, timeline, groups = browse({"case_id": case_id}, [], [])
    assert {option["value"] for option in platforms} == {
        "telegram",
        "x",
        "reddit",
        "youtube",
        "news",
    }
    assert len(channels) == 5
    assert "5 of 5" in text(summary)
    assert "Unknown publication time" in text(timeline)
    assert "Collected:" in text(timeline)
    assert "Exact saved text (1)" in text(groups)
    assert "Shared links (1)" in text(groups)
    assert "Documented reposts (1)" in text(groups)
    buttons = [
        node
        for node in components(timeline)
        if isinstance(getattr(node, "id", None), dict) and node.id.get("type") == "rv-record"
    ]
    assert len(buttons) == 5
    assert all(button.id["location"].startswith("source-") for button in buttons)

    _, _, filtered_summary, filtered_timeline, filtered_groups = browse(
        {"case_id": case_id}, ["news"], []
    )
    assert "1 of 5" in text(filtered_summary)
    assert "Harbor News" in text(filtered_timeline)
    assert "Harbor video" not in text(filtered_timeline)
    assert "Exact saved text (0)" in text(filtered_groups)
    assert service.repository.get_bundle(case_id).fingerprint() == before
    assert callback("trail-source-platform.value")(case_id, None) == ([], [], case_id)


def test_opening_a_source_preserves_filters_until_the_case_changes(source_form):
    _, _, callback, _ = source_form
    reset = callback("trail-source-filter-case.data")
    assert reset("same-case", "same-case") == (no_update, no_update, no_update)
    assert reset("different-case", "same-case") == ([], [], "different-case")


def test_invalid_source_upload_cannot_be_committed(source_form, monkeypatch):
    service, _, callback, _ = source_form
    monkeypatch.setattr(sources_ui, "ctx", SimpleNamespace(triggered_id="trail-source-upload"))
    _, token, disabled, message = callback("trail-source-preview.children")(
        "not json", 0, "", "sources.json"
    )
    assert token is None and disabled is True
    assert "JSON" in text(message)
    result = callback("trail-source-panel.open")(1, None, "not json", "sources.json", "")
    assert all(value is no_update for value in result[:3])
    assert service.list_cases() == []
