"""Evidence Explorer acceptance tests against a small synthetic release."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from urllib.parse import quote

import pytest

from silent_storm.contracts import ALL_FILTER, MAX_PAGE_SIZE, UNKNOWN_FILTER, ExplorerFilters

from .conftest import FRAME_FIELDS, file_fingerprints, image_bytes, write_csv


def test_group_identity_and_whole_clip_context(store):
    results = store.list_clips("fixture", ExplorerFilters(query="shared"))
    assert results["total"] == 2
    assert results["total_frames"] == 3
    assert {item["video_id"] for item in results["items"]} == {"videoA", "videoB"}
    assert len({item["clip_key"] for item in results["items"]}) == 2

    filtered = store.list_clips("fixture", ExplorerFilters(week="11", camera="RGB"))
    assert filtered["total"] == 1
    assert filtered["total_frames"] == 1
    item = filtered["items"][0]
    assert item["frame_count"] == 2
    assert item["matched_frame_count"] == 1
    clip = store.get_clip("fixture", item["clip_key"])
    assert {frame["frame_id"] for frame in clip["frames"]} == {"alpha", "zeta"}
    assert clip["frame_count"] == 2
    assert [frame["frame_id"] for frame in clip["frames"]] == [
        frame["frame_id"] for frame in store.get_clip("fixture", item["clip_key"])["frames"]
    ]


@pytest.mark.parametrize(
    ("field", "value", "expected_frames"),
    [
        ("week", "11", 2),
        ("side", "Blue Force", 2),
        ("camera", UNKNOWN_FILTER, 1),
        ("week", UNKNOWN_FILTER, 1),
        ("side", UNKNOWN_FILTER, 1),
        ("image_type", "Misc", 1),
    ],
)
def test_supplied_metadata_filters_preserve_unknowns(store, field, value, expected_frames):
    result = store.list_clips("fixture", ExplorerFilters(**{field: value}))
    assert result["total_frames"] == expected_frames
    assert result["total"] == 1


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("%", {"literal%id"}),
        ("_", {"under_score"}),
        ("' OR 1=1 --", set()),
        ("videoA", {"alpha", "zeta"}),
    ],
)
def test_id_search_treats_query_as_literal_text(store, query, expected):
    result = store.list_clips("fixture", ExplorerFilters(query=query))
    matched = {
        frame["frame_id"]
        for item in result["items"]
        for frame in store.get_clip("fixture", item["clip_key"])["frames"]
    }
    assert matched == expected


def test_pagination_is_bounded_disjoint_and_keeps_global_totals(store):
    pages = [
        store.list_clips("fixture", ExplorerFilters(page=page, page_size=2)) for page in (1, 2, 3)
    ]
    assert all(page["total"] == 6 and page["total_frames"] == 7 for page in pages)
    keys = [item["clip_key"] for page in pages for item in page["items"]]
    assert len(keys) == len(set(keys)) == 6
    assert all(len(page["items"]) <= 2 for page in pages)
    assert store.list_clips("fixture", ExplorerFilters(page=4, page_size=2))["items"] == []

    clip = store.get_frame("fixture", "zeta")["clip_key"]
    first = store.get_clip("fixture", clip, offset=0, limit=1)
    second = store.get_clip("fixture", clip, offset=1, limit=1)
    assert first["frame_count"] == second["frame_count"] == 2
    assert len(first["frames"]) == len(second["frames"]) == 1
    assert first["frames"][0]["frame_id"] != second["frames"][0]["frame_id"]


def test_contact_sheet_default_limit_preserves_a_fifteen_frame_clip(tmp_path, release_root):
    from silent_storm.ingest import import_release
    from silent_storm.storage import Store

    metadata = release_root / "metadata" / "frames.csv"
    with metadata.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    for number in range(13):
        frame_id = f"extra{number:02d}"
        filename = f"{frame_id}.jpg"
        rows.append({**rows[0], "frame_id": frame_id, "file": filename})
        (release_root / "frames" / filename).write_bytes(image_bytes(frame_id))
    write_csv(metadata, FRAME_FIELDS, rows)
    repository = Store(tmp_path / "long-clip.sqlite")
    import_release(repository, release_root, release_id="fixture", title="Fixture")
    clip_key = repository.get_frame("fixture", "zeta")["clip_key"]
    first = repository.get_clip("fixture", clip_key)
    last = repository.get_clip("fixture", clip_key, offset=12)
    assert first["frame_count"] == last["frame_count"] == 15
    assert len(first["frames"]) == 12 and len(last["frames"]) == 3
    frame_ids = [frame["frame_id"] for page in (first, last) for frame in page["frames"]]
    assert len(frame_ids) == len(set(frame_ids)) == 15


def test_provenance_lookup_and_unknown_hash_state(store):
    overview = store.overview("fixture")
    assert (overview["frame_count"], overview["clip_count"], overview["video_count"]) == (7, 6, 6)
    assert overview["missing_media_count"] == 1
    assert overview["orphan_detection_count"] == 1
    assert overview["hash_counts"] == {"total": 7, "hashed": 0, "pending": 7, "failed": 0}
    for field in ("week", "side", "camera"):
        assert UNKNOWN_FILTER in overview["filter_options"][field]

    frame = store.get_frame("fixture", "zeta")
    assert frame["file"] == "different-file-name.jpg"
    assert frame["relative_path"] == "frames/different-file-name.jpg"
    assert frame["source_row"] == 2
    assert frame["week"] == 11
    assert frame["width_px"] == frame["height_px"] == 1
    assert frame["media_available"] is True
    assert frame["media_sha256"] is None and frame["hash_status"] == "pending"
    assert frame["detection_count"] == 51
    assert len(store.get_detections("fixture", "zeta")) <= MAX_PAGE_SIZE
    for unavailable in (
        "original_url",
        "source_url",
        "caption",
        "latitude",
        "longitude",
        "capture_timestamp",
        "event_time",
        "unit_id",
        "frame_index",
    ):
        assert frame.get(unavailable) in (None, "")
    assert store.get_frame("fixture", "missing")["media_available"] is False
    assert store.get_frame("fixture", "not-present") is None


def test_import_is_repeatable_and_does_not_mutate_inputs(tmp_path, release_root):
    from silent_storm.ingest import import_release
    from silent_storm.storage import Store

    before = file_fingerprints(release_root)
    instance = Store(tmp_path / "repeat.sqlite")
    import_release(instance, release_root, release_id="fixture", title="Fixture")
    import_release(instance, release_root, release_id="fixture", title="Fixture")
    assert file_fingerprints(release_root) == before
    assert len(instance.list_releases()) == 1
    assert instance.overview("fixture")["frame_count"] == 7


@pytest.mark.parametrize("unsafe_file", ["../outside.jpg", "/tmp/outside.jpg", "unsupported.txt"])
def test_unsafe_media_reference_rejects_atomic_import(tmp_path, release_root, unsafe_file):
    from silent_storm.ingest import import_release
    from silent_storm.storage import Store

    metadata = release_root / "metadata" / "frames.csv"
    with metadata.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[0]["file"] = unsafe_file
    write_csv(metadata, FRAME_FIELDS, rows)
    instance = Store(tmp_path / "unsafe.sqlite")
    with pytest.raises(ValueError):
        import_release(instance, release_root, release_id="fixture", title="Fixture")
    assert instance.list_releases() == []


def test_http_endpoints_reopen_local_evidence(client):
    assert client.get("/health").status_code == 200
    releases = client.get("/api/releases")
    assert releases.status_code == 200
    overview = client.get("/api/releases/fixture/overview")
    assert overview.status_code == 200 and overview.json["frame_count"] == 7
    listing = client.get("/api/releases/fixture/clips", query_string={"query": "%"})
    assert listing.status_code == 200 and listing.json["total_frames"] == 1
    clip_key = listing.json["items"][0]["clip_key"]
    contact = client.get(f"/api/releases/fixture/clips/{quote(clip_key, safe='')}")
    assert contact.status_code == 200 and contact.json["frame_count"] == 1
    frame = client.get("/api/releases/fixture/frames/zeta")
    assert frame.status_code == 200 and frame.json["frame_id"] == "zeta"
    media = client.get("/media/fixture/zeta")
    assert media.status_code == 200
    assert media.mimetype == "image/jpeg" and media.data == image_bytes("zeta")
    assert client.get("/media/fixture/mu").data == image_bytes("mu")
    assert client.get("/media/fixture/missing").status_code == 404


def test_empty_database_reports_not_ready_and_renders_import_state(tmp_path):
    from silent_storm.app import create_app
    from silent_storm.storage import Store

    application = create_app(store=Store(tmp_path / "empty.sqlite"))
    application.server.config.update(TESTING=True)
    empty_client = application.server.test_client()
    health = empty_client.get("/health")
    assert health.status_code == 503
    assert health.json["ready"] is False
    assert empty_client.get("/api/releases").json == []
    layout = empty_client.get("/_dash-layout")
    assert layout.status_code == 200
    assert "import" in json.dumps(layout.json).lower()
    assert empty_client.get("/api/releases/unknown/overview").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/api/releases/unknown/overview",
        "/api/releases/unknown/clips",
        "/api/releases/unknown/clips/no-clip",
        "/api/releases/unknown/frames/zeta",
        "/media/unknown/zeta",
        "/api/releases/fixture/clips/no-clip",
        "/api/releases/fixture/frames/no-frame",
        "/media/fixture/no-frame",
    ],
)
def test_unknown_records_return_not_found(client, path):
    assert client.get(path).status_code == 404


@pytest.mark.parametrize(
    "query_string",
    [
        {"page": "0"},
        {"page": "-1"},
        {"page": "nonsense"},
        {"page_size": "0"},
        {"page_size": str(MAX_PAGE_SIZE + 1)},
        {"page_size": "1.5"},
        {"query": "x" * 201},
    ],
)
def test_invalid_explorer_paging_returns_client_error(client, query_string):
    assert client.get("/api/releases/fixture/clips", query_string=query_string).status_code == 400


@pytest.mark.parametrize(
    "query_string",
    [
        {"offset": "-1"},
        {"offset": "oops"},
        {"limit": "0"},
        {"limit": str(MAX_PAGE_SIZE + 1)},
    ],
)
def test_invalid_contact_sheet_paging_returns_client_error(client, store, query_string):
    clip_key = store.get_frame("fixture", "zeta")["clip_key"]
    path = f"/api/releases/fixture/clips/{quote(clip_key, safe='')}"
    assert client.get(path, query_string=query_string).status_code == 400


def test_media_resolver_rechecks_symlink_escape_after_import(client, release_root, tmp_path):
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside-root-test-data")
    frame = release_root / "frames" / "different-file-name.jpg"
    frame.unlink()
    frame.symlink_to(outside)
    response = client.get("/media/fixture/zeta")
    assert response.status_code in (400, 404)
    assert b"outside-root-test-data" not in response.data


def _walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _component_document(value):
    return json.loads(json.dumps(value, default=lambda component: component.to_plotly_json()))


def _image_sources(document):
    return [
        node["props"]["src"]
        for node in _walk(document)
        if isinstance(node, dict) and node.get("type") == "Img" and node.get("props", {}).get("src")
    ]


@pytest.mark.parametrize("view", ["clip", "frame", "inspector"])
def test_opted_in_preview_views_construct_supported_images_and_quote_ids(store, view):
    from silent_storm.ui.components import clip_option, evidence_inspector, frame_option

    frame = store.get_frame("fixture", "literal%id")
    clip = store.list_clips("fixture", ExplorerFilters(query="%"))["items"][0]
    render = {
        "clip": lambda reveal: clip_option(clip, "fixture", reveal),
        "frame": lambda reveal: frame_option(frame, "fixture", reveal),
        "inspector": lambda reveal: evidence_inspector(frame, "fixture", reveal),
    }[view]
    hidden = _component_document(render(False))
    assert _image_sources(hidden) == []
    assert "/media/" not in json.dumps(hidden)
    revealed = _component_document(render(True))
    assert _image_sources(revealed) == ["/media/fixture/literal%25id"]


def test_real_dash_reveal_callback_emits_media_only_after_consent(client, dash_app):
    callback_key, metadata = next(
        (key, value)
        for key, value in dash_app.callback_map.items()
        if "ss-selected-clip.options" in key
    )
    values = {
        "ss-release": "fixture",
        "ss-query": "%",
        "ss-week": ALL_FILTER,
        "ss-side": ALL_FILTER,
        "ss-camera": ALL_FILTER,
        "ss-image-type": ALL_FILTER,
        "ss-clip-previous": 0,
        "ss-clip-next": 0,
        "ss-clear-filters": 0,
        "ss-refresh": 0,
        "ss-clip-page": 1,
    }

    def request_reveal(consent):
        current = {**values, "ss-reveal-previews": consent}
        response = client.post(
            "/_dash-update-component",
            json={
                "output": callback_key,
                "outputs": [
                    {"id": output.component_id, "property": output.component_property}
                    for output in metadata["output"]
                ],
                "inputs": [{**item, "value": current[item["id"]]} for item in metadata["inputs"]],
                "state": [{**item, "value": current[item["id"]]} for item in metadata["state"]],
                "changedPropIds": ["ss-reveal-previews.value"],
            },
        )
        assert response.status_code == 200
        options = response.json["response"]["ss-selected-clip"]["options"]
        assert len(options) == 1, (
            "Revealing previews must not replace matching clips with an error."
        )
        return options

    hidden = request_reveal([])
    assert _image_sources(hidden) == [] and "/media/" not in json.dumps(hidden)
    revealed = request_reveal(["reveal"])
    assert _image_sources(revealed) == ["/media/fixture/literal%25id"]
    assert client.get(_image_sources(revealed)[0]).data == image_bytes("literal%id")
    hidden_again = request_reveal([])
    assert _image_sources(hidden_again) == [] and "/media/" not in json.dumps(hidden_again)


def test_ui_labels_publication_and_unknown_order_without_corpus_browser_state(client):
    response = client.get("/_dash-layout")
    assert response.status_code == 200
    document = response.json
    serialized = json.dumps(document).lower()
    assert "exercise week first posted" in serialized
    assert "posting channel side" in serialized
    assert "capture order unknown" in serialized
    assert set(_image_sources(document)) <= {
        "/assets/traced/logos/traced-primary.svg",
        "/assets/traced/logos/traced-reversed.svg",
    }, "Only canonical header logos may load before the reviewer reveals frame previews."
    all_frame_ids = {"zeta", "alpha", "mu", "literal%id", "under_score", "plain", "missing"}
    stores = [
        node
        for node in _walk(document)
        if isinstance(node, dict)
        and node.get("type") == "Store"
        and node.get("namespace") == "dash_core_components"
    ]
    assert stores, "The UI needs small selected-record state, not a browser copy of the corpus."
    for browser_store in stores:
        data = browser_store.get("props", {}).get("data")
        scalar_values = {node for node in _walk(data) if isinstance(node, str)}
        assert not all_frame_ids <= scalar_values
        assert "data:image" not in json.dumps(data).lower()


def test_importing_app_module_does_not_initialize_data(tmp_path):
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", "import silent_storm.app"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []
