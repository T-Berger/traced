"""The import form previews first, opens the saved case, and retains no content in stores."""

import base64
import json
from types import SimpleNamespace

import pytest
from dash import Dash, dcc, no_update
from dash.development.base_component import Component

from evidence_explorer.review import import_ui
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.service import ReviewService
from evidence_explorer.review.storage import ReviewRepository


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
def form(tmp_path):
    repository = ReviewRepository(tmp_path / "review.sqlite3")
    repository.initialize()
    service = ReviewService(repository)
    app = Dash(__name__, suppress_callback_exceptions=True)
    app.layout = import_ui.import_panel
    import_ui.register_import_callbacks(app, service)
    callbacks = {
        "preview": next(
            item["callback"].__wrapped__
            for key, item in app.callback_map.items()
            if "rv-import-preview.children" in key
        ),
        "commit": next(
            item["callback"].__wrapped__
            for key, item in app.callback_map.items()
            if "rv-case.options" in key
        ),
    }
    data = load_demo_bundle().model_dump(mode="json")
    data["case"].update(id="import-ui:case", synthetic=False, title="UI import test case")
    return service, app, callbacks, data


def encoded(data):
    return "data:application/json;base64," + base64.b64encode(json.dumps(data).encode()).decode()


def test_upload_previews_then_commits_and_selects_the_real_case(form, monkeypatch):
    service, _, callbacks, data = form
    content = encoded(data)
    monkeypatch.setattr(import_ui, "ctx", SimpleNamespace(triggered_id="rv-import-upload"))
    card, token, disabled, notice = callbacks["preview"](content, 0, "", "case.json")
    assert data["case"]["title"] in text(card)
    assert "Imported investigation" in text(card)
    assert "Nothing has been imported" in text(notice)
    assert disabled is False
    assert set(token) == {"fingerprint", "source"}
    assert service.list_cases() == []
    result = callbacks["commit"](1, token, content, "case.json", "")
    options, case_id, state, notice, cleared_upload, cleared_paste = result[:6]
    assert result[6] == "review" and result[7] is False
    assert "imported" in text(result[8]).lower()
    assert {"label": data["case"]["title"], "value": data["case"]["id"]} in options
    assert case_id == state["case_id"] == data["case"]["id"]
    assert state["finding_id"] == "harbor:c3-v1"
    assert state["record_id"] == "harbor:r01"
    assert state["case_fingerprint"] == service.case_fingerprint(case_id)
    assert state["scenario_id"] is None
    assert cleared_upload is None and cleared_paste == ""
    assert "imported" in text(notice).lower()


def test_editing_json_invalidates_preview_and_changed_content_is_rejected(form, monkeypatch):
    service, _, callbacks, data = form
    raw = json.dumps(data)
    monkeypatch.setattr(import_ui, "ctx", SimpleNamespace(triggered_id="rv-import-validate"))
    _, token, disabled, _ = callbacks["preview"](None, 1, raw, None)
    assert disabled is False
    data["case"]["title"] = "Changed content"
    changed = json.dumps(data)
    monkeypatch.setattr(import_ui, "ctx", SimpleNamespace(triggered_id="rv-import-paste"))
    _, invalidated, disabled, _ = callbacks["preview"](None, 1, changed, None)
    assert invalidated is None and disabled is True
    result = callbacks["commit"](1, token, None, None, changed)
    assert all(item is no_update for item in result[:3])
    assert "preview" in text(result[3]).lower()
    assert service.list_cases() == []


def test_invalid_upload_keeps_existing_case_and_disables_import(form, monkeypatch):
    service, _, callbacks, data = form
    original_id = service.import_bundle(data)
    monkeypatch.setattr(import_ui, "ctx", SimpleNamespace(triggered_id="rv-import-upload"))
    _, token, disabled, message = callbacks["preview"]("not json", 0, "", "bad.json")
    assert token is None and disabled is True
    assert "JSON" in text(message)
    result = callbacks["commit"](1, token, "not json", "bad.json", "")
    assert all(item is no_update for item in result[:3])
    assert [case["id"] for case in service.list_cases()] == [original_id]


def test_reimport_of_current_case_refreshes_context_instead_of_retaining_old_decision(
    form, monkeypatch
):
    service, _, callbacks, data = form
    service.import_bundle(data)
    old_fingerprint = service.case_fingerprint(data["case"]["id"])
    data["records"].append({**data["records"][0], "id": "additional-evidence"})
    content = encoded(data)
    monkeypatch.setattr(import_ui, "ctx", SimpleNamespace(triggered_id="rv-import-upload"))
    _, token, _, _ = callbacks["preview"](content, 0, "", "case.json")
    options, case_id, state, *_ = callbacks["commit"](1, token, content, "case.json", "")
    assert len(options) == 1
    assert state["case_id"] == case_id
    assert state["case_fingerprint"] != old_fingerprint
    assert state["job_id"] is None and state["scenario_id"] is None


def test_upload_content_has_no_persistent_store_or_secret_field(form):
    _, app, _, _ = form
    nodes = list(components(app.layout()))
    stores = [node for node in nodes if isinstance(node, dcc.Store)]
    assert stores and all(node.storage_type == "memory" for node in stores)
    assert {node.id for node in stores} == {"rv-import-token"}
    assert not any(getattr(node, "type", None) == "password" for node in nodes)
