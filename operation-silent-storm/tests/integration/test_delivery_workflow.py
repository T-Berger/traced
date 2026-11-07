"""End-to-end delivery checks through native shared contracts and package storage."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from urllib.parse import urlencode

import pytest
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.integrations.packages import import_case_package
from evidence_explorer.review.storage import ReviewRepository

from silent_storm import shared_brief
from silent_storm.media import hash_media
from silent_storm.reviews import ReviewService
from silent_storm.shared_brief import build_evidence_bundle
from silent_storm.ui.citation import citation_page


@pytest.fixture
def delivery_case(store, client):
    hash_media(store, "fixture")
    reviews = ReviewService(store)
    scenario = reviews.ensure_baseline("fixture")
    reviewed = reviews.save_review(
        scenario["scenario_id"],
        title="Synthetic gray image",
        observation="A gray pixel is supplied.",
        rationale="Non-graphic automated fixture, not a qualified human judgment.",
        reviewer="Synthetic reviewer",
        evidence_ids=["fixture:zeta", "fixture:alpha"],
        status="reviewed",
    )
    draft = reviews.save_review(
        scenario["scenario_id"],
        title="Unfinished fixture",
        observation="Pending review.",
        reviewer="Synthetic reviewer",
        evidence_ids=["fixture:mu"],
        status="draft",
    )
    return {
        "scenario_id": scenario["scenario_id"],
        "reviewed": reviewed,
        "draft": draft,
        "reviews": reviews,
        "client": client,
    }


def finalize(case):
    client, sid = case["client"], case["scenario_id"]
    response = client.post(
        f"/api/scenarios/{sid}/brief-preview",
        json={
            "review_ids": [case["reviewed"]["review_id"], case["draft"]["review_id"]],
        },
    )
    assert response.status_code == 200, response.json
    preview = response.json
    assert len(preview["included"]) == len(preview["omitted"]) == 1
    response = client.post(
        f"/api/scenarios/{sid}/briefs",
        json={
            "title": "Synthetic cited brief",
            "reviewer": "Synthetic briefing author",
            "review_versions": preview["review_versions"],
            "expected_version": preview["expected_version"],
            "source_origin": "synthetic",
        },
    )
    assert response.status_code == 201, response.json
    return response.json


def test_delivery_snapshot_has_exact_evidence_and_immutable_downloads(delivery_case):
    snapshot = finalize(delivery_case)
    client = delivery_case["client"]
    brief_id = snapshot["brief_id"]
    assert snapshot["included"][0]["revision"] == 1
    assert {item["evidence_id"] for item in snapshot["citations"]} == {
        "fixture:zeta",
        "fixture:alpha",
    }
    for citation in snapshot["citations"]:
        assert citation["source_row"] >= 2
        assert citation["source_file"] == "metadata/frames.csv"
        assert len(citation["media_sha256"]) == 64
        page = client.get(citation["href"])
        assert page.status_code == 200
        assert citation["frame_id"].encode() in page.data
        assert b"freshly checked against the saved citation" in page.data
        assert b'<img src="/media/' not in page.data
    exported = client.get(f"/api/briefs/{brief_id}/export.json")
    assert exported.json == snapshot
    markdown = client.get(f"/api/briefs/{brief_id}/export.md")
    assert markdown.status_code == 200
    assert b"http://localhost/evidence?" in markdown.data
    assert b"attachment" in markdown.headers["Content-Disposition"].encode()
    for citation in snapshot["citations"]:
        assert citation["href"].encode() in markdown.data
        assert citation["media_sha256"].encode() in markdown.data
    reopened = client.post("/api/briefs/reopen", json={"snapshot": exported.json})
    assert reopened.status_code == 200 and reopened.json["valid"]
    tampered = {**snapshot, "title": "Changed after export"}
    assert client.post("/api/briefs/reopen", json={"snapshot": tampered}).status_code == 400


def test_native_package_reopens_into_fresh_shared_workspace(delivery_case, tmp_path):
    snapshot = finalize(delivery_case)
    response = delivery_case["client"].get(f"/api/briefs/{snapshot['brief_id']}/export.zip")
    assert response.status_code == 200, response.json
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        bundle = EvidenceBundle.model_validate_json(archive.read("bundle.json"))
        assert len([r for r in bundle.records if r.kind == "image"]) == 2
        assert len(bundle.findings) == 1
        assert bundle.case.synthetic
        assert bundle.reviews == []
        assert bundle.findings[0].details["specialist_review_status"] == "reviewed"
        for record in bundle.records:
            if record.artifact_ref:
                assert (
                    hashlib.sha256(archive.read(record.artifact_ref)).hexdigest()
                    == record.content_hash
                )
                assert record.metadata["synthetic"] is True
                assert record.metadata["capture_time"] is None
    destination = tmp_path / "new-shared-workspace"
    reopened = import_case_package(response.data, destination / "artifacts")
    repository = ReviewRepository(destination / "review.sqlite")
    repository.initialize()
    repository.import_bundle(reopened)
    saved = repository.get_bundle(reopened.case.id)
    assert saved.fingerprint() == bundle.fingerprint()
    assert not saved.reviews
    assert saved.case.coverage["specialist_snapshot_fingerprint"] == snapshot["fingerprint"]
    assert build_evidence_bundle(snapshot).fingerprint() == saved.fingerprint()


def test_new_review_preserves_old_snapshot_but_blocks_new_package(delivery_case):
    snapshot = finalize(delivery_case)
    client, reviews = delivery_case["client"], delivery_case["reviews"]
    note = delivery_case["reviewed"]
    reviews.save_review(
        delivery_case["scenario_id"],
        review_id=note["review_id"],
        expected_revision=1,
        title=note["title"],
        observation="Different saved wording.",
        rationale="Changed fixture.",
        reviewer="Another synthetic actor",
        evidence_ids=note["evidence_ids"],
        status="reviewed",
    )
    validation = client.get(f"/api/briefs/{snapshot['brief_id']}/validation").json
    assert not validation["valid"] and validation["history_only"]
    assert client.get(f"/api/briefs/{snapshot['brief_id']}").json == snapshot
    assert client.get(f"/api/briefs/{snapshot['brief_id']}/export.json").json == snapshot
    assert client.get(f"/api/briefs/{snapshot['brief_id']}/export.zip").status_code == 409
    assert reviews.review_history(note["review_id"])["total"] == 2


def test_finalization_conflict_and_transport_errors(delivery_case):
    client, sid = delivery_case["client"], delivery_case["scenario_id"]
    assert client.post(f"/api/scenarios/{sid}/brief-preview", data="x").status_code == 415
    assert (
        client.post(
            f"/api/scenarios/{sid}/brief-preview", json={"review_ids": [], "unknown": 1}
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/api/scenarios/{sid}/brief-preview",
            json={"review_ids": []},
            headers={"Origin": "https://other.example"},
        ).status_code
        == 403
    )
    preview = client.post(
        f"/api/scenarios/{sid}/brief-preview",
        json={
            "review_ids": [delivery_case["reviewed"]["review_id"]],
        },
    ).json
    response = client.post(
        f"/api/scenarios/{sid}/briefs",
        json={
            "title": "Stale",
            "reviewer": "Actor",
            "review_versions": preview["review_versions"],
            "expected_version": preview["expected_version"] - 1,
        },
    )
    assert response.status_code == 409
    assert client.get(f"/api/scenarios/{sid}/briefs").json["total"] == 0
    assert client.get("/api/briefs/not-present").status_code == 404
    assert client.post("/api/briefs/reopen", json={"snapshot": []}).status_code == 400
    assert (
        client.post(
            "/api/briefs/reopen", data="x" * (3 * 1024 * 1024 + 1), content_type="application/json"
        ).status_code
        == 413
    )


def test_citation_view_reuses_inspector_and_escapes_supplied_values(store, client):
    frame = store.get_frame("fixture", "zeta")
    frame["raw_metadata"]["side"] = '<img src=x onerror="alert(1)">'
    rendered = citation_page(frame, store.get_release("fixture"))
    assert "&lt;img src=x onerror=" in rendered
    assert "<img src=x onerror=" not in rendered
    assert "Supplied metadata" in rendered and "Local provenance" in rendered
    assert "Capture time / capture order" in rendered
    assert "/media/fixture/zeta" not in rendered
    response = client.get("/evidence?release_id=fixture&frame_id=zeta&reveal=1")
    assert response.status_code == 200
    assert b"/media/fixture/zeta" in response.data
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert client.get("/evidence?release_id=fixture&frame_id=not-present").status_code == 404


def test_frozen_json_is_not_native_case_bundle(delivery_case):
    snapshot = finalize(delivery_case)
    client = delivery_case["client"]
    native = client.get(f"/api/briefs/{snapshot['brief_id']}/bundle.json")
    bundle = EvidenceBundle.model_validate(native.json)
    assert (
        bundle.fingerprint()
        == EvidenceBundle.model_validate_json(json.dumps(native.json)).fingerprint()
    )
    assert bundle.reviews == []
    assert not any(record.source_url for record in bundle.records)


def test_snapshot_citation_never_serves_replaced_image(delivery_case, store):
    snapshot = finalize(delivery_case)
    citation = snapshot["citations"][0]
    client = delivery_case["client"]
    query = urlencode(
        {
            "release_id": citation["release_id"],
            "frame_id": citation["frame_id"],
            "sha256": citation["media_sha256"],
        }
    )
    original = client.get("/evidence-media?" + query)
    assert original.status_code == 200
    assert hashlib.sha256(original.data).hexdigest() == citation["media_sha256"]
    path = Path(store.get_release("fixture")["dataset_root"]) / citation["relative_path"]
    path.write_bytes(original.data[:-1] + bytes([original.data[-1] ^ 1]))
    assert client.get("/evidence-media?" + query).status_code == 404
    page = client.get("/evidence?" + query + "&reveal=1")
    assert b"Saved citation unavailable" in page.data
    assert b'<img src="/evidence-media' not in page.data
    assert client.get(f"/api/briefs/{snapshot['brief_id']}/export.zip").status_code == 409


def test_portable_export_recovers_when_cited_file_disappears_during_read(
    delivery_case, monkeypatch
):
    snapshot = finalize(delivery_case)
    client = delivery_case["client"]
    url = f"/api/briefs/{snapshot['brief_id']}/export"
    resolve = shared_brief.resolve_frame_path
    removed = []

    def remove_after_resolution(root, relative_path):
        path = resolve(root, relative_path)
        original = path.read_bytes()
        path.unlink()
        removed.append((path, original))
        return path

    with monkeypatch.context() as patch:
        patch.setattr(shared_brief, "resolve_frame_path", remove_after_resolution)
        response = client.get(url + ".zip")

    assert response.status_code == 409
    assert b"cited image is unavailable" in response.data
    assert client.get(url + ".json").json == snapshot
    assert len(removed) == 1
    path, original = removed[0]
    path.write_bytes(original)
    assert client.get(url + ".zip").status_code == 200
