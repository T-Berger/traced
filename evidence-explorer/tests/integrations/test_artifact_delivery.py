"""Deliver the exact verified bytes, even if the saved path changes afterward.

All media in these tests are labelled, harmless packaged SVG fixture cards.
There is no app server, model call, network collection, or real imagery.
"""

from __future__ import annotations

import hashlib
import io
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from dash import html
from dash.development.base_component import Component

from evidence_explorer.config import Settings
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.integrations import artifacts
from evidence_explorer.review.integrations.media import build_media_demo_bundle, media_artifact_root
from evidence_explorer.review.integrations.media_ui import transition_media
from evidence_explorer.review.integrations.packages import export_case_package
from evidence_explorer.review.integrations.web import create_app
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService


def database_rows(service):
    """Fingerprint every application table, including reviews, jobs, and events."""
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


def images(value):
    if isinstance(value, html.Img):
        yield value
    if isinstance(value, Component):
        yield from images(getattr(value, "children", None))
    elif isinstance(value, list | tuple):
        for child in value:
            yield from images(child)


@pytest.fixture
def desk(tmp_path):
    settings = Settings(data_dir=tmp_path / "workspace")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, settings.data_dir / "artifacts")
    bundle = build_media_demo_bundle()
    service.import_package(export_case_package(bundle, media_artifact_root()))
    frame = next(
        record
        for record in bundle.records
        if record.artifact_ref and record.metadata["frame_id"] == "frame-a"
    )
    other = next(
        record
        for record in bundle.records
        if record.artifact_ref and record.metadata["frame_id"] == "frame-b"
    )
    path = service.artifact(bundle.case.id, frame.id)
    original_bytes = path.read_bytes()
    state = session_for_case(service, bundle.case.id)
    state["record_id"] = frame.id
    app = create_app(service)
    before = database_rows(service)
    yield SimpleNamespace(
        service=service,
        client=app.server.test_client(),
        bundle=bundle,
        frame=frame,
        other=other,
        path=path,
        original_bytes=original_bytes,
        state=state,
    )
    assert database_rows(service) == before


def get_frame(desk, *, record=None, expected_hash=None, headers=None):
    query = {"case_id": desk.bundle.case.id, "record_id": (record or desk.frame).id}
    if expected_hash is not None:
        query["expected_hash"] = expected_hash
    return desk.client.get("/trail-artifact", query_string=query, headers=headers)


@pytest.mark.parametrize("path_change", ["replace", "delete"])
def test_delivery_uses_captured_bytes_when_path_changes_before_send(desk, monkeypatch, path_change):
    capture = desk.service.capture_artifact
    calls = []
    replacement = b'<svg xmlns="http://www.w3.org/2000/svg"><text>Other fixture</text></svg>'

    def capture_then_change(*args, **kwargs):
        captured = capture(*args, **kwargs)
        calls.append(captured)
        if path_change == "replace":
            captured.path.write_bytes(replacement)
        else:
            captured.path.unlink()
        return captured

    monkeypatch.setattr(desk.service, "capture_artifact", capture_then_change)
    response = get_frame(desk, expected_hash=desk.frame.content_hash)

    assert len(calls) == 1
    assert response.status_code == 200
    assert response.data == calls[0].content == desk.original_bytes
    assert response.data != replacement
    assert response.mimetype == "image/svg+xml"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
    assert response.get_etag() == (desk.frame.content_hash, False)


@pytest.mark.parametrize(
    ("record_name", "hash_kind", "status"),
    [
        ("frame", "omitted", 200),
        ("frame", "correct", 200),
        ("frame", "wrong", 404),
        ("other", "old-frame", 404),
        ("other", "correct", 200),
    ],
)
def test_expected_hash_binds_delivery_to_the_selected_saved_record(
    desk, record_name, hash_kind, status
):
    record = getattr(desk, record_name)
    expected = {
        "omitted": None,
        "correct": record.content_hash,
        "wrong": "0" * 64,
        "old-frame": desk.frame.content_hash,
    }[hash_kind]
    assert desk.frame.content_hash != desk.other.content_hash
    response = get_frame(desk, record=record, expected_hash=expected)
    assert response.status_code == status
    if status == 200:
        assert hashlib.sha256(response.data).hexdigest() == record.content_hash
    else:
        assert desk.original_bytes not in response.data


def test_revealed_image_request_includes_the_exact_selected_hash(desk):
    body, hidden = transition_media(desk.service, desk.state, "rv-session")
    assert list(images(body)) == []
    body, loading = transition_media(
        desk.service,
        desk.state,
        {"type": "trail-media-action", "context": hidden["context"], "action": "reveal"},
        clicks=[1],
        stored=hidden,
    )
    (image,) = images(body)
    query = parse_qs(urlsplit(image.src).query)
    assert loading["status"] == "loading"
    assert query["case_id"] == [desk.bundle.case.id]
    assert query["record_id"] == [desk.frame.id]
    assert query["expected_hash"] == [desk.frame.content_hash]
    assert query["attempt"] == [loading["attempt"]]
    response = desk.client.get(image.src)
    assert response.status_code == 200
    assert response.data == desk.original_bytes


def test_capture_reads_once_with_the_twenty_megabyte_bound(desk, monkeypatch):
    original_open = Path.open
    requested = []

    class Reader:
        def __enter__(self):
            self.stream = original_open(desk.path, "rb")
            return self

        def read(self, size):
            requested.append(size)
            return self.stream.read(size)

        def __exit__(self, *args):
            self.stream.close()

    def checked_open(path, *args, **kwargs):
        if path == desk.path:
            assert args == ("rb",)
            return Reader()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    captured = desk.service.capture_artifact(
        desk.bundle.case.id, desk.frame.id, expected_hash=desk.frame.content_hash
    )
    assert requested == [20 * 1024 * 1024 + 1]
    assert isinstance(captured.content, bytes)
    assert captured.content == desk.original_bytes
    assert captured.content_hash == hashlib.sha256(captured.content).hexdigest()


def test_file_growth_after_small_stat_is_rejected_after_one_bounded_read(desk, monkeypatch):
    limit = 16
    original_open, original_stat = Path.open, Path.stat
    requested, reported_sizes = [], []

    class GrowingFile(io.BytesIO):
        def read(self, size):
            requested.append(size)
            return super().read(size)

    def grown_open(path, *args, **kwargs):
        if path == desk.path:
            assert args == ("rb",)
            return GrowingFile(b"x" * (limit + 1))
        return original_open(path, *args, **kwargs)

    def small_stat(path, *args, **kwargs):
        actual = original_stat(path, *args, **kwargs)
        if path != desk.path:
            return actual
        values = list(actual)
        values[6] = 1
        reported_sizes.append(1)
        return os.stat_result(values)

    monkeypatch.setattr(artifacts, "MAX_FILE_BYTES", limit)
    monkeypatch.setattr(Path, "open", grown_open)
    monkeypatch.setattr(Path, "stat", small_stat)
    with pytest.raises(ValueError, match="unavailable or too large"):
        desk.service.capture_artifact(desk.bundle.case.id, desk.frame.id)
    assert reported_sizes
    assert requested == [limit + 1]


def test_existing_local_artifact_caller_still_receives_a_verified_path(desk, monkeypatch):
    capture = desk.service.capture_artifact
    captured = []

    def checked_capture(*args, **kwargs):
        result = capture(*args, **kwargs)
        captured.append(result)
        return result

    monkeypatch.setattr(desk.service, "capture_artifact", checked_capture)
    path = desk.service.artifact(desk.bundle.case.id, desk.frame.id, bundle=desk.bundle)
    assert isinstance(path, Path)
    assert path == desk.path == captured[0].path
    assert path.read_bytes() == desk.original_bytes


@pytest.mark.parametrize("missing", ["case", "record", "path-in-record"])
def test_delivery_does_not_resolve_unreferenced_paths(desk, missing):
    query = {"case_id": desk.bundle.case.id, "record_id": desk.frame.id}
    if missing == "case":
        query["case_id"] = "absent-case"
    else:
        query["record_id"] = "absent-record" if missing == "record" else "../../private.svg"
    response = desk.client.get("/trail-artifact", query_string=query)
    assert response.status_code == 404
    assert desk.original_bytes not in response.data


def test_http_rejects_unsupported_captured_filename(desk, monkeypatch):
    captured = desk.service.capture_artifact(desk.bundle.case.id, desk.frame.id)
    monkeypatch.setattr(
        desk.service,
        "capture_artifact",
        lambda *args, **kwargs: replace(captured, path=captured.path.with_suffix(".txt")),
    )
    response = get_frame(desk, expected_hash=desk.frame.content_hash)
    assert response.status_code == 415
    assert desk.original_bytes not in response.data


def test_conditional_request_still_validates_current_bytes_before_not_modified(desk):
    response = get_frame(desk, expected_hash=desk.frame.content_hash)
    assert response.status_code == 200
    etag = response.headers["ETag"]
    unchanged = get_frame(
        desk, expected_hash=desk.frame.content_hash, headers={"If-None-Match": etag}
    )
    assert unchanged.status_code == 304
    assert unchanged.data == b""
    desk.path.write_bytes(
        b'<svg xmlns="http://www.w3.org/2000/svg"><text>Changed fixture</text></svg>'
    )
    changed = get_frame(
        desk, expected_hash=desk.frame.content_hash, headers={"If-None-Match": etag}
    )
    assert changed.status_code == 404
