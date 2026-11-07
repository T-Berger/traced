from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from test_ingest import frame, release

from silent_storm.contracts import ReviewConflictError
from silent_storm.ingest import import_release
from silent_storm.reviews import ReviewService, qualify_evidence_id
from silent_storm.storage import Store


def setup_service(tmp_path):
    root = release(tmp_path, frames=[frame("a"), frame("b", video_id="other")])
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, root)
    service = ReviewService(store)
    baseline = service.ensure_baseline("day-1")
    return store, service, baseline


def save(service, scenario_id, **changes):
    values = dict(
        title="Supplied frame review",
        observation="A reviewer-authored observation.",
        reviewer="Analyst A",
        evidence_ids=["day-1:a"],
        rationale="Supporting local source inspected.",
        status="reviewed",
    )
    values.update(changes)
    return service.save_review(scenario_id, **values)


def test_simultaneous_edits_only_one_expected_revision_wins(tmp_path):
    store, service, baseline = setup_service(tmp_path)
    first = save(service, baseline["scenario_id"])
    barrier = Barrier(2)

    def attempt(actor):
        barrier.wait()
        try:
            return save(
                service,
                baseline["scenario_id"],
                review_id=first["review_id"],
                expected_revision=1,
                reviewer=actor,
            )
        except ReviewConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["Analyst B", "Analyst C"]))
    assert results.count("conflict") == 1
    assert service.get_review(first["review_id"])["revision"] == 2
    assert service.get_scenario(baseline["scenario_id"])["version"] == 2
    history = ReviewService(Store(store.path)).review_history(first["review_id"])
    assert history["total"] == 2
    assert history["items"][1]["reviewer"] == "Analyst A"


def test_exclusion_failure_rolls_back_event_all_revisions_and_scenario_version(
    tmp_path, monkeypatch
):
    _, service, baseline = setup_service(tmp_path)
    save(service, baseline["scenario_id"])
    save(service, baseline["scenario_id"], title="Second dependent statement")
    scenario = service.create_scenario("day-1", "Sensitivity check", "Analyst B")
    original = service._append
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic interrupted invalidation")
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_append", fail_second)
    with pytest.raises(RuntimeError, match="synthetic"):
        service.set_source_exclusion(
            scenario["scenario_id"],
            "video",
            excluded=True,
            reason="Source under review",
            reviewer="Analyst B",
            expected_version=scenario["version"],
        )
    assert service.get_scenario(scenario["scenario_id"])["version"] == scenario["version"]
    assert service.exclusion_history(scenario["scenario_id"])["total"] == 0
    assert not service.exclusion_preview(scenario["scenario_id"], "video")["currently_excluded"]
    notes = service.list_reviews(scenario["scenario_id"])["items"]
    assert all(note["status"] == "reviewed" and note["revision"] == 1 for note in notes)


def test_database_protects_immutable_revision_and_evidence_history(tmp_path):
    store, service, baseline = setup_service(tmp_path)
    note = save(service, baseline["scenario_id"])
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store.connection() as conn:
            conn.execute(
                "UPDATE review_revisions SET observation='overwritten' WHERE review_id=?",
                (note["review_id"],),
            )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store.connection() as conn:
            conn.execute("DELETE FROM review_evidence WHERE review_id=?", (note["review_id"],))
    assert service.get_review(note["review_id"])["observation"] == note["observation"]


def test_projection_failure_rolls_back_saved_revision_and_scenario_version(tmp_path, monkeypatch):
    """Returning a view is part of the mutation's transaction, including failures."""
    _, service, baseline = setup_service(tmp_path)
    original = save(service, baseline["scenario_id"])
    version = service.get_scenario(baseline["scenario_id"])["version"]

    def unavailable_projection(*_args):
        raise RuntimeError("synthetic projection failure")

    with monkeypatch.context() as patch:
        patch.setattr(service, "_render", unavailable_projection)
        with pytest.raises(RuntimeError, match="projection failure"):
            save(
                service,
                baseline["scenario_id"],
                review_id=original["review_id"],
                expected_revision=original["revision"],
                observation="This revision must roll back with its failed projection.",
            )
    assert service.get_scenario(baseline["scenario_id"])["version"] == version
    assert service.get_review(original["review_id"]) == original
    assert service.review_history(original["review_id"])["total"] == 1


def test_excluding_same_video_id_never_leaks_between_releases(tmp_path):
    store, service, baseline = setup_service(tmp_path / "first")
    save(service, baseline["scenario_id"])
    other_root = release(tmp_path / "second")
    import_release(store, other_root, release_id="second")
    second = service.ensure_baseline("second")
    other_note = save(service, second["scenario_id"], evidence_ids=["second:a"])
    scenario = service.create_scenario("day-1", "First release check", "Analyst B")
    service.set_source_exclusion(
        scenario["scenario_id"],
        "video",
        excluded=True,
        reason="Review source context",
        reviewer="Analyst B",
        expected_version=scenario["version"],
    )
    assert service.get_review(other_note["review_id"])["status"] == "reviewed"
    assert service.get_review(other_note["review_id"])["eligibility"]["eligible"]
    assert service.get_scenario(second["scenario_id"])["version"] == 1


def test_forking_an_exclusion_preserves_origin_event_and_source_history(tmp_path):
    _, service, baseline = setup_service(tmp_path)
    original = save(service, baseline["scenario_id"])
    first = service.create_scenario("day-1", "First scenario", "Analyst B")
    event = service.set_source_exclusion(
        first["scenario_id"],
        "video",
        excluded=True,
        reason="Check source context",
        reviewer="Analyst B",
        expected_version=first["version"],
    )
    source_note = service.list_reviews(first["scenario_id"])["items"][0]
    second = service.create_scenario(
        "day-1", "Second scenario", "Analyst C", source_scenario_id=first["scenario_id"]
    )
    copied = service.list_reviews(second["scenario_id"])["items"][0]
    assert copied["origin_review_id"] == source_note["review_id"]
    assert copied["origin_revision"] == source_note["revision"]
    assert (
        service.exclusion_history(second["scenario_id"])["items"][0]["origin_event_id"]
        == (event["event_id"])
    )
    assert service.exclusion_history(first["scenario_id"])["total"] == 1
    assert service.get_review(original["review_id"])["revision"] == 1


def test_release_qualified_ids_support_colons_without_ambiguous_splitting(tmp_path):
    root = release(tmp_path, frames=[frame("frame:one")])
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, root, release_id="release:one")
    service = ReviewService(store)
    scenario = service.ensure_baseline("release:one")
    evidence_id = qualify_evidence_id("release:one", "frame:one")
    note = save(service, scenario["scenario_id"], evidence_ids=[evidence_id])
    assert note["evidence_ids"] == ["release:one:frame:one"]
    assert note["evidence"][0]["frame_id"] == "frame:one"
