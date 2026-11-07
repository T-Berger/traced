"""Human review, isolated exclusions and posting-chronology acceptance checks.

All notes and media in these checks are synthetic. No real release database is used.
"""

from __future__ import annotations

import csv
import json
import sqlite3

import pytest

from silent_storm.contracts import (
    MAX_PAGE_SIZE,
    REVIEW_STATUSES,
    ReviewConflictError,
    ReviewNotFoundError,
)

from .conftest import FRAME_FIELDS, file_fingerprints, write_csv


@pytest.fixture
def review_service(store):
    from silent_storm.reviews import ReviewService

    return ReviewService(store)


@pytest.fixture
def baseline(review_service):
    return review_service.ensure_baseline("fixture")


@pytest.fixture
def scenario(review_service, baseline):
    return review_service.create_scenario("fixture", "Independent review", "Fixture reviewer")


def _save(service, scenario_id, **overrides):
    fields = {
        "title": "Synthetic source observation",
        "observation": "The supplied test image is a uniform gray pixel.",
        "interpretation": "No event or identity is inferred.",
        "uncertainty": "Capture time and ordering are not supplied.",
        "rationale": "Inspected the synthetic local frame and supplied metadata.",
        "reviewer": "Fixture reviewer",
        "evidence_ids": ["fixture:zeta"],
        "status": "draft",
    }
    fields.update(overrides)
    return service.save_review(scenario_id, **fields)


def _history(service, review_id):
    result = service.review_history(review_id)
    return {row["revision"]: row for row in result["items"]}


def _items(payload):
    return payload["items"] if isinstance(payload, dict) else payload


def test_saved_reviews_survive_restart_without_mutating_release(
    review_service,
    baseline,
    store,
    release_root,
):
    from silent_storm.reviews import ReviewService
    from silent_storm.storage import Store

    originals = file_fingerprints(release_root)
    saved = _save(review_service, baseline["scenario_id"], status="reviewed")
    restarted = ReviewService(Store(store.path))
    restored = restarted.get_review(saved["review_id"])
    for field in (
        "review_id",
        "scenario_id",
        "release_id",
        "revision",
        "title",
        "observation",
        "interpretation",
        "uncertainty",
        "rationale",
        "reviewer",
        "status",
        "evidence_ids",
    ):
        assert restored[field] == saved[field]
    assert restored["eligibility"]["eligible"] is True
    assert restored["evidence"][0]["frame_id"] == "zeta"
    assert restored["evidence"][0]["release_id"] == "fixture"
    assert restored["evidence"][0]["media_sha256"] is None
    assert file_fingerprints(release_root) == originals
    assert restarted.ensure_baseline("fixture")["scenario_id"] == baseline["scenario_id"]
    assert len(restarted.list_scenarios("fixture")) == 1


def test_four_statuses_append_history_and_lists_use_current_revision(review_service, baseline):
    saved = None
    expected = []
    for status in REVIEW_STATUSES:
        saved = _save(
            review_service,
            baseline["scenario_id"],
            status=status,
            review_id=saved["review_id"] if saved else None,
            expected_revision=saved["revision"] if saved else 0,
            observation=f"Saved synthetic observation in {status} state.",
        )
        expected.append((saved["revision"], status, saved["observation"]))
    history = _history(review_service, saved["review_id"])
    assert len(history) == 4
    assert [
        (revision, history[revision]["status"], history[revision]["observation"])
        for revision, _, _ in expected
    ] == expected
    assert review_service.list_reviews(baseline["scenario_id"])["total"] == 1
    assert (
        review_service.list_reviews(
            baseline["scenario_id"],
            status="needs_review",
        )["total"]
        == 1
    )
    assert (
        review_service.list_reviews(
            baseline["scenario_id"],
            status="reviewed",
        )["total"]
        == 0
    )


def test_stale_revision_cannot_overwrite_another_reviewers_save(review_service, baseline, store):
    from silent_storm.reviews import ReviewService
    from silent_storm.storage import Store

    first = _save(review_service, baseline["scenario_id"])
    other = ReviewService(Store(store.path))
    current = _save(
        other,
        baseline["scenario_id"],
        review_id=first["review_id"],
        expected_revision=first["revision"],
        observation="The second reviewer saved this wording.",
    )
    with pytest.raises(ReviewConflictError):
        _save(
            review_service,
            baseline["scenario_id"],
            review_id=first["review_id"],
            expected_revision=first["revision"],
            observation="Stale wording must not replace it.",
        )
    assert review_service.get_review(first["review_id"])["observation"] == current["observation"]
    assert review_service.review_history(first["review_id"])["total"] == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "approved"},
        {"reviewer": ""},
        {"evidence_ids": ["zeta"]},
    ],
)
def test_invalid_review_inputs_do_not_create_notes(review_service, baseline, overrides):
    with pytest.raises((ValueError, ReviewNotFoundError)):
        _save(review_service, baseline["scenario_id"], **overrides)
    assert review_service.list_reviews(baseline["scenario_id"])["total"] == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"observation": ""},
        {"rationale": ""},
        {"evidence_ids": []},
        {"evidence_ids": ["fixture:missing"]},
    ],
)
def test_reviewed_requires_observation_rationale_and_live_evidence(
    review_service,
    baseline,
    overrides,
):
    with pytest.raises(ValueError):
        _save(review_service, baseline["scenario_id"], status="reviewed", **overrides)
    assert review_service.list_reviews(baseline["scenario_id"])["total"] == 0


def test_drafts_allow_no_evidence_but_unknown_references_never_become_valid(
    review_service,
    baseline,
):
    empty = _save(review_service, baseline["scenario_id"], evidence_ids=[])
    unavailable = _save(
        review_service,
        baseline["scenario_id"],
        evidence_ids=["fixture:missing"],
    )
    assert empty["eligibility"]["eligible"] is False
    assert unavailable["eligibility"]["eligible"] is False
    with pytest.raises(ReviewNotFoundError):
        _save(review_service, baseline["scenario_id"], evidence_ids=["fixture:orphan"])
    assert review_service.list_reviews(baseline["scenario_id"])["total"] == 2


def test_fork_copies_current_notes_and_preserves_origin_and_source_history(
    review_service,
    baseline,
):
    original = _save(review_service, baseline["scenario_id"])
    current = _save(
        review_service,
        baseline["scenario_id"],
        review_id=original["review_id"],
        expected_revision=original["revision"],
        status="reviewed",
        title="Current baseline wording",
    )
    source = review_service.get_scenario(baseline["scenario_id"])
    fork = review_service.create_scenario("fixture", "Forked assessment", "Fork reviewer")
    clone = review_service.list_reviews(fork["scenario_id"])["items"][0]
    assert clone["review_id"] != current["review_id"]
    assert clone["origin_review_id"] == current["review_id"]
    assert clone["origin_revision"] == current["revision"]
    assert clone["title"] == current["title"] and clone["status"] == "reviewed"
    assert clone["revision"] == 1
    assert fork["source_scenario_id"] == baseline["scenario_id"]
    assert fork["source_scenario_version"] == source["version"]
    _save(
        review_service,
        fork["scenario_id"],
        review_id=clone["review_id"],
        expected_revision=clone["revision"],
        title="Only the fork changed",
    )
    assert review_service.get_review(current["review_id"])["title"] == "Current baseline wording"
    assert review_service.review_history(current["review_id"])["total"] == 2
    assert review_service.review_history(clone["review_id"])["total"] == 2


def test_exclusion_preview_is_read_only_and_reports_every_dependent_review(
    review_service,
    scenario,
):
    sid = scenario["scenario_id"]
    dependent = [
        _save(review_service, sid, status=status, evidence_ids=["fixture:zeta", "fixture:alpha"])
        for status in ("draft", "reviewed", "rejected")
    ]
    _save(review_service, sid, evidence_ids=["fixture:mu"])
    before = review_service.get_scenario(sid)["version"]
    preview = review_service.exclusion_preview(sid, "videoA")
    assert preview["scenario_version"] == before
    assert preview["affected_review_count"] == 3
    assert len(preview["affected_reviews"]) <= MAX_PAGE_SIZE
    assert preview["currently_excluded"] is False
    assert review_service.get_scenario(sid)["version"] == before
    assert review_service.exclusion_history(sid)["total"] == 0
    assert all(review_service.review_history(note["review_id"])["total"] == 1 for note in dependent)


def test_exclusion_atomically_marks_dependents_and_restore_requires_explicit_review(
    review_service,
    scenario,
    baseline,
):
    sid = scenario["scenario_id"]
    baseline_note = _save(review_service, baseline["scenario_id"], status="reviewed")
    dependent = [
        _save(review_service, sid, status=status, evidence_ids=["fixture:zeta", "fixture:alpha"])
        for status in ("draft", "reviewed", "rejected")
    ]
    unrelated = _save(review_service, sid, status="reviewed", evidence_ids=["fixture:mu"])
    before = review_service.exclusion_preview(sid, "videoA")
    result = review_service.set_source_exclusion(
        sid,
        "videoA",
        excluded=True,
        reason="Synthetic source withdrawn for rechecking.",
        reviewer="Exclusion reviewer",
        expected_version=before["scenario_version"],
    )
    assert result["affected_review_count"] == 3
    assert result["scenario_version"] == before["scenario_version"] + 1
    for original in dependent:
        current = review_service.get_review(original["review_id"])
        assert current["status"] == "needs_review"
        assert current["revision"] == original["revision"] + 1
        assert current["reviewer"] == "Exclusion reviewer"
        assert current["change_type"] == "source_excluded"
        assert current["change_reason"] == "Synthetic source withdrawn for rechecking."
        assert current["eligibility"]["eligible"] is False
        for field in ("observation", "interpretation", "uncertainty", "rationale", "evidence_ids"):
            assert current[field] == original[field]
        historical = _history(review_service, original["review_id"])[original["revision"]]
        for field in (
            "revision",
            "status",
            "observation",
            "interpretation",
            "uncertainty",
            "rationale",
            "reviewer",
            "evidence_ids",
            "created_at",
        ):
            assert historical[field] == original[field]
    assert review_service.get_review(unrelated["review_id"])["status"] == "reviewed"
    assert review_service.get_review(baseline_note["review_id"])["status"] == "reviewed"
    events = review_service.exclusion_history(sid)["items"]
    assert events[0]["reason"] == "Synthetic source withdrawn for rechecking."
    assert events[0]["reviewer"] == "Exclusion reviewer"

    preview = review_service.exclusion_preview(sid, "videoA", excluded=False)
    review_service.set_source_exclusion(
        sid,
        "videoA",
        excluded=False,
        reason="Synthetic source restored.",
        reviewer="Restoration reviewer",
        expected_version=preview["scenario_version"],
    )
    for note in dependent:
        assert review_service.get_review(note["review_id"])["status"] == "needs_review"
        assert review_service.review_history(note["review_id"])["total"] == 2
    current = review_service.get_review(dependent[0]["review_id"])
    rereviewed = _save(
        review_service,
        sid,
        review_id=current["review_id"],
        expected_revision=current["revision"],
        status="reviewed",
    )
    assert rereviewed["eligibility"]["eligible"] is True
    assert review_service.exclusion_history(sid)["total"] == 2


def test_baseline_exclusion_is_protected_and_invalid_requests_are_atomic(
    review_service,
    baseline,
    scenario,
):
    baseline_before = review_service.get_scenario(baseline["scenario_id"])["version"]
    with pytest.raises(ValueError):
        review_service.set_source_exclusion(
            baseline["scenario_id"],
            "videoA",
            excluded=True,
            reason="Attempted baseline change",
            reviewer="Fixture reviewer",
            expected_version=baseline_before,
        )
    assert review_service.get_scenario(baseline["scenario_id"])["version"] == baseline_before
    assert review_service.exclusion_history(baseline["scenario_id"])["total"] == 0
    sid = scenario["scenario_id"]
    before = review_service.get_scenario(sid)["version"]
    for invalid in ({"reason": ""}, {"reviewer": ""}, {"excluded": "true"}):
        fields = {"excluded": True, "reason": "Synthetic reason", "reviewer": "Fixture reviewer"}
        fields.update(invalid)
        with pytest.raises(ValueError):
            review_service.set_source_exclusion(sid, "videoA", expected_version=before, **fields)
    assert review_service.get_scenario(sid)["version"] == before
    assert review_service.exclusion_history(sid)["total"] == 0


def test_preview_version_rejects_exclusion_when_new_dependent_note_was_saved(
    review_service,
    scenario,
):
    sid = scenario["scenario_id"]
    preview = review_service.exclusion_preview(sid, "videoA")
    note = _save(review_service, sid, status="reviewed")
    with pytest.raises(ReviewConflictError):
        review_service.set_source_exclusion(
            sid,
            "videoA",
            excluded=True,
            reason="Stale preview",
            reviewer="Fixture reviewer",
            expected_version=preview["scenario_version"],
        )
    assert review_service.get_review(note["review_id"])["status"] == "reviewed"
    assert review_service.review_history(note["review_id"])["total"] == 1
    assert review_service.exclusion_history(sid)["total"] == 0


def test_bounded_preview_does_not_limit_atomic_exclusion_fanout(review_service, scenario, baseline):
    sid = scenario["scenario_id"]
    source_note = _save(review_service, baseline["scenario_id"], status="reviewed")
    notes = [
        _save(review_service, sid, title=f"Synthetic dependent {number}", status="reviewed")
        for number in range(MAX_PAGE_SIZE + 1)
    ]
    preview = review_service.exclusion_preview(sid, "videoA")
    assert preview["affected_review_count"] == MAX_PAGE_SIZE + 1
    assert len(preview["affected_reviews"]) <= MAX_PAGE_SIZE
    assert all(
        review_service.get_review(note["review_id"])["status"] == "reviewed" for note in notes
    )
    result = review_service.set_source_exclusion(
        sid,
        "videoA",
        excluded=True,
        reason="Synthetic all-dependent exclusion",
        reviewer="Fanout reviewer",
        expected_version=preview["scenario_version"],
    )
    assert result["affected_review_count"] == MAX_PAGE_SIZE + 1
    for note in notes:
        current = review_service.get_review(note["review_id"])
        assert current["status"] == "needs_review" and current["revision"] == 2
    assert review_service.exclusion_history(sid)["total"] == 1
    assert review_service.get_review(source_note["review_id"])["status"] == "reviewed"
    assert review_service.review_history(source_note["review_id"])["total"] == 1


def test_mid_exclusion_storage_failure_rolls_back_event_and_every_revision(
    review_service,
    scenario,
    baseline,
    store,
):
    sid = scenario["scenario_id"]
    protected = _save(review_service, baseline["scenario_id"], status="reviewed")
    notes = [_save(review_service, sid, status="reviewed") for _ in range(3)]
    preview = review_service.exclusion_preview(sid, "videoA")
    # A controlled storage fault after one staged invalidation tests the transaction boundary.
    with store.connection() as connection:
        connection.executescript("""
            CREATE TRIGGER synthetic_exclusion_write_failure
            BEFORE INSERT ON review_revisions
            WHEN NEW.change_type='source_excluded' AND
                (SELECT COUNT(*) FROM review_revisions WHERE change_type='source_excluded') >= 1
            BEGIN SELECT RAISE(ABORT,'Synthetic exclusion write fault'); END;
        """)
    with pytest.raises(sqlite3.IntegrityError, match="Synthetic exclusion write fault"):
        review_service.set_source_exclusion(
            sid,
            "videoA",
            excluded=True,
            reason="Synthetic transaction failure",
            reviewer="Fixture reviewer",
            expected_version=preview["scenario_version"],
        )
    after = review_service.exclusion_preview(sid, "videoA")
    assert after["scenario_version"] == preview["scenario_version"]
    assert after["currently_excluded"] is False
    assert review_service.exclusion_history(sid)["total"] == 0
    for note in notes:
        current = review_service.get_review(note["review_id"])
        assert current["status"] == "reviewed" and current["revision"] == 1
        assert review_service.review_history(note["review_id"])["total"] == 1
    assert review_service.get_review(protected["review_id"])["status"] == "reviewed"


def test_fork_from_excluded_scenario_preserves_its_snapshot_without_leaking_to_baseline(
    review_service,
    scenario,
    baseline,
):
    sid = scenario["scenario_id"]
    note = _save(review_service, sid, status="reviewed")
    preview = review_service.exclusion_preview(sid, "videoA")
    review_service.set_source_exclusion(
        sid,
        "videoA",
        excluded=True,
        reason="Synthetic scenario exclusion",
        reviewer="Fixture reviewer",
        expected_version=preview["scenario_version"],
    )
    fork = review_service.create_scenario(
        "fixture",
        "Fork of excluded scenario",
        "Fork reviewer",
        source_scenario_id=sid,
    )
    assert review_service.exclusion_preview(fork["scenario_id"], "videoA")["currently_excluded"]
    assert not review_service.exclusion_preview(
        baseline["scenario_id"],
        "videoA",
    )["currently_excluded"]
    clone = review_service.list_reviews(fork["scenario_id"])["items"][0]
    assert clone["origin_review_id"] == note["review_id"] and clone["origin_revision"] == 2
    assert clone["status"] == "needs_review"


def test_release_qualified_evidence_and_review_edits_cannot_cross_scenarios(
    review_service,
    scenario,
    store,
    release_root,
):
    from silent_storm.ingest import import_release

    import_release(store, release_root, release_id="other", title="Other synthetic release")
    other = review_service.create_scenario("other", "Other release review", "Fixture reviewer")
    foreign = _save(review_service, other["scenario_id"], evidence_ids=["other:zeta"])
    with pytest.raises(ReviewNotFoundError):
        _save(review_service, scenario["scenario_id"], evidence_ids=["other:zeta"])
    with pytest.raises(ReviewNotFoundError):
        _save(
            review_service,
            scenario["scenario_id"],
            review_id=foreign["review_id"],
            expected_revision=foreign["revision"],
        )
    assert review_service.list_reviews(scenario["scenario_id"])["total"] == 0
    assert review_service.list_reviews(other["scenario_id"])["total"] == 1
    assert review_service.review_history(foreign["review_id"])["total"] == 1


@pytest.fixture
def gap_service(tmp_path, release_root):
    from silent_storm.ingest import import_release
    from silent_storm.reviews import ReviewService
    from silent_storm.storage import Store

    metadata = release_root / "metadata" / "frames.csv"
    with metadata.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        row["week"] = {"12": "14", "13": "16", "14": "16"}.get(row["week"], row["week"])
    write_csv(metadata, FRAME_FIELDS, rows)
    instance = Store(tmp_path / "gap-review.sqlite")
    import_release(instance, release_root, release_id="gap", title="Synthetic gap coverage")
    return ReviewService(instance)


def test_chronology_deduplicates_units_and_notes_and_preserves_unknown_and_zero_bins(gap_service):
    service = gap_service
    sid = service.ensure_baseline("gap")["scenario_id"]
    first = _save(service, sid, evidence_ids=["gap:zeta", "gap:alpha"])
    _save(
        service,
        sid,
        status="reviewed",
        evidence_ids=["gap:zeta", "gap:alpha"],
        review_id=first["review_id"],
        expected_revision=first["revision"],
    )
    _save(service, sid, evidence_ids=["gap:alpha", "gap:mu"])
    _save(service, sid, status="needs_review", evidence_ids=["gap:literal%id"])
    chronology = service.chronology(sid)
    assert chronology["unit"] == "videos"
    assert chronology["x_label"] == "Exercise week first posted"
    bins = {row["week"]: row for row in chronology["bins"]}
    assert set(bins) == {11, 12, 13, 14, 15, 16, None}
    assert bins[11]["count"] == bins[11]["video_count"] == 1
    assert bins[11]["frame_count"] == 2 and bins[11]["clip_count"] == 1
    assert bins[11]["review_count"] == 2
    assert bins[14]["review_count"] == 1
    assert bins[None]["count"] == 1 and bins[None]["review_count"] == 1
    assert sum(row["count"] for row in bins.values()) == 6
    for week in (12, 13, 15):
        assert all(
            bins[week][field] == 0
            for field in (
                "count",
                "frame_count",
                "clip_count",
                "video_count",
                "review_count",
            )
        )
    for row in bins.values():
        assert not {"event_date", "capture_time", "capture_timestamp", "date"} & row.keys()
    frames = {row["week"]: row for row in service.chronology(sid, unit="frames")["bins"]}
    assert frames[11]["count"] == 2 and sum(row["count"] for row in frames.values()) == 7
    reviewed = {row["week"]: row for row in service.chronology(sid, status="reviewed")["bins"]}
    assert reviewed[11]["review_count"] == 1 and reviewed[14]["review_count"] == 0
    assert all(reviewed[week]["count"] == row["count"] for week, row in bins.items())


def test_chronology_exclusion_is_a_labeled_subset_and_does_not_rewrite_supplied_counts(
    review_service,
    scenario,
):
    sid = scenario["scenario_id"]
    _save(review_service, sid, status="reviewed", evidence_ids=["fixture:zeta", "fixture:alpha"])
    preview = review_service.exclusion_preview(sid, "videoA")
    review_service.set_source_exclusion(
        sid,
        "videoA",
        excluded=True,
        reason="Synthetic source exclusion",
        reviewer="Fixture reviewer",
        expected_version=preview["scenario_version"],
    )
    videos = {row["week"]: row for row in review_service.chronology(sid)["bins"]}
    frames = {row["week"]: row for row in review_service.chronology(sid, unit="frames")["bins"]}
    assert videos[11]["count"] == videos[11]["excluded_count"] == 1
    assert videos[11]["excluded_video_count"] == 1
    assert videos[11]["review_count"] == videos[11]["needs_review_count"] == 1
    assert frames[11]["count"] == frames[11]["excluded_count"] == 2
    assert sum(row["count"] for row in videos.values()) == 6


def test_scenario_get_routes_are_read_only_before_explicit_baseline_creation(
    client, review_service
):
    assert review_service.list_scenarios("fixture") == []
    response = client.get("/api/releases/fixture/scenarios")
    assert response.status_code == 200 and _items(response.json) == []
    assert review_service.list_scenarios("fixture") == []


def test_http_review_history_conflict_and_exclusion_workflow(client, review_service, scenario):
    sid = scenario["scenario_id"]
    payload = {
        "title": "HTTP synthetic observation",
        "observation": "A gray test pixel is supplied.",
        "rationale": "Inspected the synthetic local record.",
        "reviewer": "API reviewer",
        "evidence_ids": ["fixture:zeta", "fixture:alpha"],
        "status": "reviewed",
    }
    saved_response = client.post(f"/api/scenarios/{sid}/reviews", json=payload)
    assert saved_response.status_code in (200, 201)
    saved = saved_response.json
    review_id = saved["review_id"]
    assert client.get(f"/api/reviews/{review_id}").json["status"] == "reviewed"
    revision = client.post(
        f"/api/scenarios/{sid}/reviews",
        json={
            **payload,
            "review_id": review_id,
            "expected_revision": saved["revision"],
            "observation": "Revised wording preserves the same synthetic evidence.",
        },
    )
    assert revision.status_code in (200, 201)
    stale = client.post(
        f"/api/scenarios/{sid}/reviews",
        json={
            **payload,
            "review_id": review_id,
            "expected_revision": saved["revision"],
        },
    )
    assert stale.status_code == 409
    assert client.get(f"/api/reviews/{review_id}/history").json["total"] == 2
    preview = client.get(
        f"/api/scenarios/{sid}/exclusions/preview",
        query_string={"video_id": "videoA"},
    ).json
    excluded = client.post(
        f"/api/scenarios/{sid}/exclusions",
        json={
            "video_id": "videoA",
            "excluded": True,
            "reason": "API synthetic source exclusion",
            "reviewer": "API reviewer",
            "expected_version": preview["scenario_version"],
        },
    )
    assert excluded.status_code in (200, 201)
    assert client.get(f"/api/reviews/{review_id}").json["status"] == "needs_review"
    assert client.get(f"/api/scenarios/{sid}/exclusions").json["total"] == 1
    assert client.get(f"/api/scenarios/{sid}/chronology").json["unit"] == "videos"
    assert review_service.review_history(review_id)["total"] == 3


@pytest.mark.parametrize("suffix", ["reviews", "exclusions"])
def test_review_write_routes_require_json(client, scenario, suffix):
    response = client.post(
        f"/api/scenarios/{scenario['scenario_id']}/{suffix}",
        data="not-json",
        content_type="text/plain",
    )
    assert response.status_code == 415


def test_scenario_creation_route_requires_json(client):
    assert (
        client.post(
            "/api/releases/fixture/scenarios",
            data="not-json",
            content_type="text/plain",
        ).status_code
        == 415
    )


def test_cross_origin_mutation_is_rejected_without_saving_review(client, scenario, review_service):
    sid = scenario["scenario_id"]
    response = client.post(
        f"/api/scenarios/{sid}/reviews",
        json={
            "title": "Untrusted origin",
            "observation": "Synthetic request only.",
            "reviewer": "Fixture reviewer",
            "evidence_ids": [],
        },
        headers={"Origin": "https://untrusted.example"},
    )
    assert response.status_code == 403
    assert review_service.list_reviews(sid)["total"] == 0


@pytest.mark.parametrize(
    "path",
    [
        "/api/scenarios/no-scenario",
        "/api/scenarios/no-scenario/reviews",
        "/api/scenarios/no-scenario/exclusions",
        "/api/scenarios/no-scenario/chronology",
        "/api/reviews/no-review",
        "/api/reviews/no-review/history",
        "/api/releases/no-release/scenarios",
    ],
)
def test_review_routes_return_not_found_for_unknown_records(client, path):
    assert client.get(path).status_code == 404


def test_http_scenario_creation_and_invalid_filters(client):
    created = client.post(
        "/api/releases/fixture/scenarios",
        json={
            "name": "API fork",
            "reviewer": "API reviewer",
        },
    )
    assert created.status_code in (200, 201)
    sid = created.json["scenario_id"]
    assert client.get(f"/api/scenarios/{sid}").json["name"] == "API fork"
    for path, query in (
        (f"/api/scenarios/{sid}/reviews", {"status": "approved"}),
        (f"/api/scenarios/{sid}/reviews", {"page": "0"}),
        (f"/api/scenarios/{sid}/chronology", {"unit": "events"}),
        (f"/api/scenarios/{sid}/exclusions/preview", {"video_id": "videoA", "excluded": "maybe"}),
    ):
        assert client.get(path, query_string=query).status_code == 400


def _post_dash_callback(client, application, target_id, trigger, values):
    trigger_id = trigger.rsplit(".", 1)[0]
    candidates = []
    for key, metadata in application.callback_map.items():
        outputs = metadata["output"]
        outputs = outputs if isinstance(outputs, list) else [outputs]
        if any(output.component_id == target_id for output in outputs) and any(
            item["id"] == trigger_id for item in metadata["inputs"]
        ):
            candidates.append((key, metadata, outputs))
    assert len(candidates) == 1, "The visible action must have one unambiguous callback."
    key, metadata, outputs = candidates[0]
    output_specs = [
        {"id": output.component_id, "property": output.component_property} for output in outputs
    ]
    result = client.post(
        "/_dash-update-component",
        json={
            "output": key,
            "outputs": output_specs if isinstance(metadata["output"], list) else output_specs[0],
            "inputs": [{**item, "value": values.get(item["id"])} for item in metadata["inputs"]],
            "state": [{**item, "value": values.get(item["id"])} for item in metadata["state"]],
            "changedPropIds": [trigger],
        },
    )
    assert result.status_code == 200, result.get_data(as_text=True)
    return result.json["response"]


def _editor_state(scenario_id, review_id, reviewer):
    return {
        "ss-release": "fixture",
        "ss-r-scenario": scenario_id,
        "ss-r-review-select": review_id,
        "ss-r-reviewer": reviewer,
        "ss-r-editor-ref": None,
        "ss-selected-frame": "zeta",
        "ss-r-new": 0,
        "ss-review-from-frame": 0,
        "ss-r-reload": 0,
    }


def _save_state(scenario_id, review_id, revision, reviewer):
    return {
        "ss-r-save": 1,
        "ss-release": "fixture",
        "ss-r-scenario": scenario_id,
        "ss-r-editor-ref": {
            "review_id": review_id,
            "revision": revision,
            "scenario_id": scenario_id,
            "release_id": "fixture",
        },
        "ss-r-title": "Synthetic Bob revision",
        "ss-r-observation": "Bob's synthetic wording.",
        "ss-r-interpretation": "No event is inferred.",
        "ss-r-uncertainty": "Capture time unknown.",
        "ss-r-rationale": "Bob checked the supplied test frame.",
        "ss-r-reviewer": reviewer,
        "ss-r-status": "reviewed",
        "ss-r-evidence": ["fixture:zeta"],
        "ss-r-refresh": 0,
    }


def test_dash_loading_and_reloading_alice_note_preserves_bob_then_saves_bob_history(
    client,
    dash_app,
    review_service,
    baseline,
):
    sid = baseline["scenario_id"]
    alice = _save(review_service, sid, reviewer="Alice", status="reviewed")
    state = _editor_state(sid, alice["review_id"], "Bob")
    loaded = _post_dash_callback(
        client,
        dash_app,
        "ss-r-title",
        "ss-r-review-select.value",
        state,
    )
    assert loaded["ss-r-reviewer"]["value"] == "Bob"
    state.update({"ss-r-editor-ref": loaded["ss-r-editor-ref"]["data"], "ss-r-reload": 1})
    reloaded = _post_dash_callback(client, dash_app, "ss-r-title", "ss-r-reload.n_clicks", state)
    assert reloaded["ss-r-reviewer"]["value"] == "Bob"
    saved = _post_dash_callback(
        client,
        dash_app,
        "ss-r-title",
        "ss-r-save.n_clicks",
        _save_state(
            sid, alice["review_id"], reloaded["ss-r-editor-ref"]["data"]["revision"], "Bob"
        ),
    )
    assert saved["ss-r-reviewer"]["value"] == "Bob"
    current = review_service.get_review(alice["review_id"])
    assert current["reviewer"] == "Bob" and current["observation"] == "Bob's synthetic wording."
    history = _history(review_service, alice["review_id"])
    assert history[alice["revision"]]["reviewer"] == "Alice"
    assert history[alice["revision"]]["observation"] == alice["observation"]
    rendered = _post_dash_callback(
        client,
        dash_app,
        "ss-r-history",
        "ss-r-editor-ref.data",
        {
            "ss-r-editor-ref": saved["ss-r-editor-ref"]["data"],
            "ss-r-refresh": 1,
            "ss-r-history-previous": 0,
            "ss-r-history-next": 0,
            "ss-r-history-page": 1,
        },
    )
    assert "Alice" in json.dumps(rendered) and "Bob" in json.dumps(rendered)


def test_dash_loading_historical_note_does_not_fill_an_intentionally_blank_actor(
    client,
    dash_app,
    review_service,
    baseline,
):
    alice = _save(review_service, baseline["scenario_id"], reviewer="Alice")
    loaded = _post_dash_callback(
        client,
        dash_app,
        "ss-r-title",
        "ss-r-review-select.value",
        _editor_state(baseline["scenario_id"], alice["review_id"], ""),
    )
    assert loaded["ss-r-reviewer"]["value"] == ""


def test_dash_stale_save_keeps_unsaved_editor_fields_and_does_not_overwrite_current_note(
    client,
    dash_app,
    review_service,
    baseline,
):
    sid = baseline["scenario_id"]
    original = _save(review_service, sid, reviewer="Alice")
    current = _save(
        review_service,
        sid,
        review_id=original["review_id"],
        expected_revision=original["revision"],
        reviewer="Other reviewer",
        observation="The newer saved wording must survive.",
    )
    response = _post_dash_callback(
        client,
        dash_app,
        "ss-r-title",
        "ss-r-save.n_clicks",
        _save_state(sid, original["review_id"], original["revision"], "Bob"),
    )
    assert "retained" in json.dumps(response).lower()
    for field in ("title", "observation", "interpretation", "uncertainty", "rationale", "reviewer"):
        assert f"ss-r-{field}" not in response
    assert "ss-r-editor-ref" not in response
    assert review_service.get_review(original["review_id"])["observation"] == current["observation"]
    assert review_service.review_history(original["review_id"])["total"] == 2


def _json_image_sources(value):
    if isinstance(value, dict):
        if value.get("type") == "Img" and value.get("props", {}).get("src"):
            yield value["props"]["src"]
        for child in value.values():
            yield from _json_image_sources(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_image_sources(child)


def test_dash_linked_evidence_uses_shared_explicit_preview_consent(client, dash_app):
    values = {
        "ss-r-linked-frame": "fixture:literal%id",
        "ss-release": "fixture",
        "ss-reveal-previews": [],
    }
    hidden = _post_dash_callback(
        client,
        dash_app,
        "ss-r-linked-inspector",
        "ss-reveal-previews.value",
        values,
    )
    assert list(_json_image_sources(hidden)) == [] and "/media/" not in json.dumps(hidden)
    revealed = _post_dash_callback(
        client,
        dash_app,
        "ss-r-linked-inspector",
        "ss-reveal-previews.value",
        {**values, "ss-reveal-previews": ["reveal"]},
    )
    assert list(_json_image_sources(revealed)) == ["/media/fixture/literal%25id"]
