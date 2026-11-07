"""Focused delivery callback regressions using non-graphic, isolated releases."""

from __future__ import annotations

import base64
import json

import pytest
from integration.conftest import release_root

from silent_storm.app import create_app
from silent_storm.briefs import BriefService
from silent_storm.ingest import import_release
from silent_storm.media import hash_media
from silent_storm.reviews import ReviewService
from silent_storm.storage import Store
from silent_storm.ui.delivery import citation_links


class Desk:
    def __init__(self, app):
        self.app, self.client, self.state = app, app.server.test_client(), {}
        self._walk(self.client.get("/_dash-layout").get_json())

    def _walk(self, node):
        if isinstance(node, dict):
            props = node.get("props", {})
            if isinstance(props.get("id"), str):
                self.state.update({(props["id"], key): value for key, value in props.items()})
            for value in node.values():
                self._walk(value)
        elif isinstance(node, list):
            for value in node:
                self._walk(value)

    def put(self, component, value, prop="value"):
        self.state[(component, prop)] = value

    def get(self, component, prop="value"):
        return self.state.get((component, prop))

    def call(self, name, changed):
        key, spec = next(
            (key, spec)
            for key, spec in self.app.callback_map.items()
            if getattr(spec.get("callback"), "__wrapped__", lambda: None).__name__ == name
        )
        outputs = spec["output"]
        multi = isinstance(outputs, list)
        result_spec = (
            [
                {"id": item.component_id, "property": item.component_property}
                for item in outputs
                if multi
            ]
            if multi
            else {"id": outputs.component_id, "property": outputs.component_property}
        )
        response = self.client.post(
            "/_dash-update-component",
            json={
                "output": key,
                "outputs": result_spec,
                "inputs": [
                    dict(item, value=self.state.get((item["id"], item["property"])))
                    for item in spec["inputs"]
                ],
                "state": [
                    dict(item, value=self.state.get((item["id"], item["property"])))
                    for item in spec["state"]
                ],
                "changedPropIds": [changed],
            },
        )
        assert response.status_code in (200, 204), response.get_data(as_text=True)
        result = response.get_json()["response"] if response.status_code == 200 else {}
        for component, props in result.items():
            self.state.update({(component, prop): value for prop, value in props.items()})
        return result


@pytest.fixture
def delivery_desk(tmp_path):
    root = release_root.__wrapped__(tmp_path)
    store = Store(tmp_path / "delivery.sqlite")
    import_release(store, root, release_id="fixture", title="Synthetic delivery fixture")
    hash_media(store, "fixture")
    reviews = ReviewService(store)
    scenario = reviews.ensure_baseline("fixture")
    fields = dict(
        title="Saved synthetic observation",
        observation="A uniform gray test pixel.",
        rationale="Inspected the synthetic record.",
        reviewer="Alice",
        evidence_ids=["fixture:zeta"],
    )
    included = reviews.save_review(scenario["scenario_id"], status="reviewed", **fields)
    omitted = reviews.save_review(scenario["scenario_id"], status="draft", **fields)
    briefs = BriefService(store, reviews)
    desk = Desk(create_app(store=store, review_service=reviews, delivery_service=briefs))
    desk.put("ss-release", "fixture")
    desk.put("ss-d-scenario", scenario["scenario_id"])
    desk.put("ss-d-notes", [included["review_id"], omitted["review_id"]])
    desk.put("ss-d-title", "Synthetic cited brief")
    desk.put("ss-d-author", "Bob")
    desk.put("ss-d-origin", "synthetic")
    return desk, store, reviews, briefs, scenario, included


def _preview(desk):
    desk.put("ss-d-preview", 1, "n_clicks")
    return desk.call("delivery_preview", "ss-d-preview.n_clicks")


def _finalize(desk):
    desk.put("ss-d-finalize", 1, "n_clicks")
    desk.call("delivery_finalize", "ss-d-finalize.n_clicks")
    return desk.get("ss-d-created-brief", "data")


def test_single_release_state_and_layout_do_not_create_delivery_data(delivery_desk):
    desk, _store, reviews, briefs, scenario, _note = delivery_desk
    before = reviews.list_scenarios("fixture")
    assert desk.client.get("/_dash-layout").status_code == 200
    assert reviews.list_scenarios("fixture") == before
    assert briefs.list_briefs(scenario["scenario_id"])["total"] == 0
    result = desk.call("delivery_releases", "ss-release.value")
    assert result["ss-d-baseline"]["disabled"] is True
    assert result["ss-d-current"]["disabled"] is True
    assert "Two completed releases" in result["ss-d-release-status"]["children"]


def test_preview_finalizes_only_cited_eligible_notes_with_explicit_origin(delivery_desk):
    desk, _store, _reviews, briefs, _scenario, _note = delivery_desk
    preview = _preview(desk)
    assert desk.get("ss-d-finalize", "disabled") is False
    assert "1 included" in json.dumps(preview) and "1 omitted" in json.dumps(preview)
    brief_id = _finalize(desk)
    frozen = briefs.get_brief(brief_id)
    assert frozen["source_origin"] == "synthetic"
    assert frozen["reviewer"] == "Bob"
    assert len(frozen["included"]) == len(frozen["omitted"]) == 1
    desk.put("ss-d-snapshot", brief_id)
    view = desk.call("delivery_snapshot_view", "ss-d-snapshot.value")
    rendered = json.dumps(view)
    assert "sha256=" in rendered and frozen["citations"][0]["media_sha256"] in rendered
    assert f"/api/briefs/{brief_id}/export.zip" in rendered


def test_stale_preview_preserves_title_author_origin_and_requires_new_preview(delivery_desk):
    desk, _store, reviews, briefs, scenario, note = delivery_desk
    _preview(desk)
    reviews.save_review(
        scenario["scenario_id"],
        title=note["title"],
        observation="A newer saved wording.",
        reviewer="Carol",
        evidence_ids=note["evidence_ids"],
        rationale="New review.",
        status="reviewed",
        review_id=note["review_id"],
        expected_revision=note["revision"],
    )
    assert _finalize(desk) is None
    assert desk.get("ss-d-title") == "Synthetic cited brief"
    assert desk.get("ss-d-author") == "Bob" and desk.get("ss-d-origin") == "synthetic"
    assert desk.get("ss-d-preview-token", "data") is None
    assert desk.get("ss-d-finalize", "disabled") is True
    assert briefs.list_briefs(scenario["scenario_id"])["total"] == 0


def test_historical_snapshot_keeps_frozen_downloads_but_hides_package(delivery_desk):
    desk, _store, reviews, briefs, scenario, note = delivery_desk
    newer = reviews.save_review(
        scenario["scenario_id"],
        title=note["title"],
        observation="Reviewed wording captured at finalization.",
        reviewer="Bob",
        evidence_ids=note["evidence_ids"],
        rationale="Updated before finalization.",
        status="reviewed",
        review_id=note["review_id"],
        expected_revision=note["revision"],
    )
    _preview(desk)
    brief_id = _finalize(desk)
    reviews.save_review(
        scenario["scenario_id"],
        title=note["title"],
        observation="Later wording.",
        reviewer="Carol",
        evidence_ids=note["evidence_ids"],
        rationale="Later review.",
        status="draft",
        review_id=note["review_id"],
        expected_revision=newer["revision"],
    )
    desk.put("ss-d-snapshot", brief_id)
    rendered = json.dumps(desk.call("delivery_snapshot_view", "ss-d-snapshot.value"))
    assert "Historical snapshot" in rendered
    assert f"/api/briefs/{brief_id}/export.md" in rendered
    assert f"/api/briefs/{brief_id}/export.json" in rendered
    assert f"/api/briefs/{brief_id}/export.zip" not in rendered
    assert "shared evidence package unavailable" in rendered.lower()
    assert "Saved review history" in rendered
    assert note["observation"] in rendered
    assert newer["observation"] in rendered
    assert "3 of 3 revisions captured at finalization" in rendered
    assert "Later wording." not in rendered
    assert briefs.get_brief(brief_id)["included"][0]["revision"] == newer["revision"]


def test_reopen_accepts_only_exact_saved_json_without_importing_rows(delivery_desk):
    desk, _store, reviews, briefs, scenario, _note = delivery_desk
    _preview(desk)
    brief_id = _finalize(desk)
    snapshot = briefs.get_brief(brief_id)
    before = reviews.list_reviews(scenario["scenario_id"])["total"]
    contents = (
        "data:application/json;base64," + base64.b64encode(json.dumps(snapshot).encode()).decode()
    )
    desk.put("ss-d-reopen", contents, "contents")
    result = desk.call("delivery_reopen", "ss-d-reopen.contents")
    assert result["ss-d-created-brief"]["data"] == brief_id
    snapshot["title"] = "Tampered title"
    desk.put(
        "ss-d-reopen",
        "data:application/json;base64," + base64.b64encode(json.dumps(snapshot).encode()).decode(),
        "contents",
    )
    result = desk.call("delivery_reopen", "ss-d-reopen.contents")
    assert "ss-d-created-brief" not in result
    assert "Snapshot not reopened" in json.dumps(result)
    assert reviews.list_reviews(scenario["scenario_id"])["total"] == before
    assert briefs.list_briefs(scenario["scenario_id"])["total"] == 1


def test_frozen_citations_ignore_supplied_href_and_bind_exact_checksum():
    node = citation_links(
        {
            "citations": [
                {
                    "release_id": "r / 1",
                    "frame_id": "f%1",
                    "media_sha256": "a" * 64,
                    "href": "javascript:alert(1)",
                }
            ]
        },
        frozen=True,
    )
    link = node.children[0]
    assert link.href.startswith("/evidence?")
    assert "r%20%2F%201" in link.href and "f%251" in link.href
    assert "sha256=" + "a" * 64 in link.href
    assert "javascript" not in link.href
