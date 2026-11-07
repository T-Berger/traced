"""Portable imports cross the real HTTP boundary without relaxing payload limits."""

import base64
import hashlib
import json

import pytest

from evidence_explorer.config import Settings
from evidence_explorer.review.integrations import web
from evidence_explorer.review.integrations.packages import MAX_PACKAGE_BYTES
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.web import create_app as create_review_app


@pytest.fixture
def desk(tmp_path):
    settings = Settings(data_dir=tmp_path / "portable-import")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, settings.data_dir / "artifacts")
    app = web.create_app(service)
    return service, app, app.server.test_client()


def bundle_bytes(text="Saved synthetic article for the upload test."):
    return json.dumps(
        {
            "case": {
                "id": "portable:large-json",
                "title": "Synthetic portable upload test",
                "question": "Can its saved text be imported?",
                "synthetic": True,
            },
            "records": [
                {
                    "id": "portable:article",
                    "kind": "article",
                    "source_label": "Synthetic saved article",
                    "text": text,
                    "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                }
            ],
            "fragments": [],
            "runs": [],
            "findings": [],
        }
    ).encode()


def upload_request(app, raw):
    key = next(key for key in app.callback_map if "trail-import-message.children" in key)
    return {
        "output": key,
        "outputs": [
            {"id": "rv-case", "property": "options"},
            {"id": "rv-case", "property": "value"},
            {"id": "trail-import-message", "property": "children"},
        ],
        "inputs": [
            {
                "id": "trail-upload",
                "property": "contents",
                "value": "data:application/json;base64," + base64.b64encode(raw).decode("ascii"),
            },
            {"id": "trail-explorer", "property": "n_clicks", "value": 0},
        ],
        "state": [],
        "changedPropIds": ["trail-upload.contents"],
    }


def test_valid_portable_json_above_standalone_http_limit_imports_exactly(desk):
    service, app, client = desk
    text = "Saved source passage.\n" * 350_000
    raw = bundle_bytes(text)
    body = json.dumps(upload_request(app, raw)).encode()
    assert len(raw) < MAX_PACKAGE_BYTES
    assert len(body) > 8 * 1024 * 1024
    response = client.post("/_dash-update-component", data=body, content_type="application/json")
    assert response.status_code == 200
    result = response.get_json()["response"]
    assert result["rv-case"]["value"] == "portable:large-json"
    saved = service.load_case("portable:large-json").records[0]
    assert saved.text == text
    assert saved.content_hash == hashlib.sha256(text.encode()).hexdigest()


def test_integrated_http_allowance_is_bounded_and_standalone_cap_is_unchanged(desk):
    service, app, client = desk
    encoded_limit = 4 * ((MAX_PACKAGE_BYTES + 2) // 3)
    assert encoded_limit < app.server.config["MAX_CONTENT_LENGTH"] <= encoded_limit + 128 * 1024
    standalone = create_review_app(service)
    assert standalone.server.config["MAX_CONTENT_LENGTH"] == 8 * 1024 * 1024
    response = client.post(
        "/_dash-update-component",
        data="{}",
        content_type="application/json",
        environ_overrides={"CONTENT_LENGTH": str(app.server.config["MAX_CONTENT_LENGTH"] + 1)},
    )
    assert response.status_code == 413
    assert service.list_cases() == []


@pytest.mark.parametrize("extra_byte", [False, True])
def test_exact_decoded_payload_limit_applies_before_json_import(desk, monkeypatch, extra_byte):
    service, app, client = desk
    # Adjacent raw sizes share a base64 length; the decoded check is essential.
    limit = 1024
    monkeypatch.setattr(web, "MAX_PACKAGE_BYTES", limit)
    raw = bundle_bytes()
    raw += b" " * (limit + int(extra_byte) - len(raw))
    assert len(raw) == limit + int(extra_byte)
    response = client.post("/_dash-update-component", json=upload_request(app, raw))
    assert response.status_code == 200
    result = response.get_json()["response"]
    if extra_byte:
        assert "rv-case" not in result
        assert "64 MB" in json.dumps(result["trail-import-message"])
        assert service.list_cases() == []
    else:
        assert result["rv-case"]["value"] == "portable:large-json"
        assert len(service.list_cases()) == 1
