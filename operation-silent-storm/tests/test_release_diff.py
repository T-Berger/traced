from __future__ import annotations

import pytest
from test_ingest import frame, release

from silent_storm import media
from silent_storm.contracts import ReviewNotFoundError
from silent_storm.ingest import import_release
from silent_storm.media import hash_media
from silent_storm.release_diff import EXAMPLE_LIMIT, ReleaseDiffService
from silent_storm.reviews import ReviewService
from silent_storm.storage import Store


def comparison_fixture(tmp_path):
    old_rows = [
        frame("changed", clip_id="c-change", video_id="v-change", camera="RGB"),
        frame("gone", clip_id="c-gone", video_id="v-gone"),
        frame("old-a", clip_id="c-reuse", video_id="v-reuse"),
        frame("old-b", clip_id="c-reuse", video_id="v-reuse"),
        frame("same", clip_id="c-stable", video_id="v-stable"),
    ]
    new_rows = [
        frame("changed", clip_id="c-change", video_id="v-change", camera="IR"),
        frame("new-a", clip_id="c-reuse", video_id="v-reuse"),
        frame("new-b", clip_id="c-reuse", video_id="v-reuse"),
        frame("same", clip_id="c-stable", video_id="v-stable"),
        frame("z-pending", clip_id="c-pending", video_id="v-pending"),
    ]
    old = release(tmp_path / "baseline", frames=old_rows)
    new = release(tmp_path / "current", frames=new_rows)
    for root, values in ((old, old_rows), (new, new_rows)):
        for row in values:
            frame_id = row["frame_id"]
            content = (
                b"reused bytes"
                if frame_id in {"old-a", "old-b", "new-a", "new-b"}
                else (frame_id.encode())
            )
            (root / "frames" / row["file"]).write_bytes(content)
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, old, release_id="before", title="Baseline")
    import_release(store, new, release_id="after", title="Current")
    hash_media(store, "before")
    hash_media(store, "after", limit=4)
    return store, ReleaseDiffService(store), old, new


def test_metadata_categories_provenance_and_counting_units_are_separate(tmp_path):
    store, service, _, _ = comparison_fixture(tmp_path)
    result = service.compare("before", "after")
    assert result["summary"] == {
        "added": 3,
        "removed": 3,
        "metadata_changed": 1,
        "unchanged": 1,
        "compared_frame_ids": 8,
    }
    assert result["baseline"]["counts"]["frame_count"] == 5
    assert result["baseline"]["counts"]["clip_count"] == 4
    assert result["baseline"]["counts"]["video_count"] == 4
    assert (
        result["current"]["metadata_fingerprint"]
        == store.get_release("after")["metadata_fingerprint"]
    )
    assert (
        result["baseline"]["frames_csv_sha256"] == store.get_release("before")["frames_csv_sha256"]
    )
    changed = next(item for item in result["items"] if item["frame_id"] == "changed")
    assert changed["metadata_changes"] == [{"field": "camera", "before": "RGB", "after": "IR"}]
    assert changed["before"]["raw_metadata"]["camera"] == "RGB"
    assert changed["after"]["raw_metadata"]["camera"] == "IR"
    assert changed["evidence_refs"] == {"before": "before:changed", "after": "after:changed"}
    assert changed["before"]["source_file"] == "metadata/frames.csv"
    assert changed["before"]["source_row"] == changed["after"]["source_row"] == 2
    assert changed["media_comparison"]["status"] == "same_bytes"


def test_cross_id_reuse_is_many_to_many_and_distinct_from_metadata_categories(tmp_path):
    _, service, _, _ = comparison_fixture(tmp_path)
    result = service.compare("before", "after", category="added")
    hashes = result["hash_comparison"]
    assert all(item["category"] == "added" for item in result["items"])
    assert hashes["recorded_candidate_pair_count_across_ids"] == 4
    assert hashes["recorded_candidate_frame_counts"] == {"baseline": 2, "current": 2}
    assert hashes["stat_matched_pair_count"] == 4
    assert {
        (item["baseline_frame_id"], item["current_frame_id"])
        for item in hashes["stat_checked_examples"]
    } == {
        ("old-a", "new-a"),
        ("old-a", "new-b"),
        ("old-b", "new-a"),
        ("old-b", "new-b"),
    }
    assert hashes["recorded_coverage"]["current"] == {
        "total": 5,
        "hashed": 4,
        "pending": 1,
        "failed": 0,
    }
    pending = next(item for item in result["items"] if item["frame_id"] == "z-pending")
    assert pending["after"]["media_sha256"] is None
    assert pending["media_comparison"]["status"] == "one_sided"
    assert "does not" in pending["media_comparison"]["note"]


def test_stale_missing_failed_and_pending_media_are_unresolved(tmp_path):
    rows = [frame(key) for key in ("failed", "missing", "stale", "z-pending")]
    old = release(tmp_path / "old", frames=rows)
    new = release(tmp_path / "new", frames=rows)
    (new / "frames/failed.jpg").unlink()
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, old, release_id="old")
    import_release(store, new, release_id="new")
    hash_media(store, "old")
    hash_media(store, "new", limit=3)
    (new / "frames/failed.jpg").write_bytes(b"synthetic checksum fixture")
    (new / "frames/missing.jpg").unlink()
    (new / "frames/stale.jpg").write_bytes(b"changed source bytes")
    result = ReleaseDiffService(store).compare("old", "new")
    states = {
        item["frame_id"]: item["media_comparison"]["after_hash_state"] for item in result["items"]
    }
    assert states == {
        "failed": "failed",
        "missing": "missing",
        "stale": "stale",
        "z-pending": "pending",
    }
    assert all(item["category"] == "unchanged" for item in result["items"])
    assert all(item["media_comparison"]["status"] == "unresolved" for item in result["items"])
    assert result["hash_comparison"]["stat_matched_pair_count"] == 0
    assert result["hash_comparison"]["recorded_candidate_pair_count_across_ids"] == 6
    assert result["hash_comparison"]["unresolved_example_pair_count"] == 6


def test_same_frame_id_with_different_bytes_is_still_metadata_unchanged(tmp_path):
    old, new = release(tmp_path / "old"), release(tmp_path / "new")
    (new / "frames/a.jpg").write_bytes(b"different bytes")
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, old, release_id="old")
    import_release(store, new, release_id="new")
    hash_media(store, "old")
    hash_media(store, "new")
    item = ReleaseDiffService(store).compare("old", "new")["items"][0]
    assert item["category"] == "unchanged"
    assert item["metadata_changes"] == []
    assert item["media_comparison"]["status"] == "different_bytes"


def test_csv_row_reordering_changes_provenance_but_not_supplied_metadata(tmp_path):
    old = release(tmp_path / "old", frames=[frame("a"), frame("b")])
    new = release(tmp_path / "new", frames=[frame("b"), frame("a")])
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, old, release_id="old")
    import_release(store, new, release_id="new")
    result = ReleaseDiffService(store).compare("old", "new")
    assert result["summary"]["unchanged"] == 2
    item = result["items"][0]
    assert item["before"]["source_row"] == 2
    assert item["after"]["source_row"] == 3
    assert item["metadata_changes"] == []


def test_pagination_is_stable_and_cannot_invent_unchanged_category_matches(tmp_path):
    _, service, _, _ = comparison_fixture(tmp_path)
    first = service.compare("before", "after", category="added", page_size=2)
    second = service.compare("before", "after", category="added", page=2, page_size=2)
    assert first["total"] == second["total"] == 3
    assert [item["frame_id"] for item in first["items"]] == ["new-a", "new-b"]
    assert [item["frame_id"] for item in second["items"]] == ["z-pending"]
    assert service.compare("before", "after", category="added", page=3)["items"] == []


def test_comparison_checks_bounded_media_examples_and_never_hashes_or_mutates(
    tmp_path, monkeypatch
):
    rows = [frame(f"f{index:03}") for index in range(110)]
    old = release(tmp_path / "old", frames=rows)
    new = release(tmp_path / "new", frames=rows)
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, old, release_id="old")
    import_release(store, new, release_id="new")
    hash_media(store, "old")
    hash_media(store, "new")
    before = store.get_release("old")
    seen = set()
    original = store.get_frame

    def counted(release_id, frame_id):
        assert (release_id, frame_id) not in seen
        seen.add((release_id, frame_id))
        return original(release_id, frame_id)

    def forbidden_hash(*_args, **_kwargs):
        raise AssertionError("Release comparison must not freshly hash the corpus")

    monkeypatch.setattr(store, "get_frame", counted)
    monkeypatch.setattr(media, "sha256_file", forbidden_hash)
    result = ReleaseDiffService(store).compare("old", "new", page_size=12)
    assert result["summary"]["unchanged"] == 110
    assert len(result["items"]) == 12
    assert len(result["hash_comparison"]["stat_checked_examples"]) <= EXAMPLE_LIMIT
    assert len(seen) <= 24 + 2 * EXAMPLE_LIMIT
    assert result["hash_comparison"]["unchecked_record_count"] > 0
    assert store.get_release("old") == before
    assert store.hash_counts("old") == {"total": 110, "hashed": 110, "pending": 0, "failed": 0}
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM review_notes").fetchone()[0] == 0


def test_checksum_committed_during_compare_only_appears_in_next_read_snapshot(
    tmp_path, monkeypatch
):
    old = release(tmp_path / "old", frames=[frame("original")])
    new = release(tmp_path / "new", frames=[frame("reused")])
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, old, release_id="old")
    import_release(store, new, release_id="new")
    hash_media(store, "old")

    reviews = ReviewService(store)
    baseline = reviews.ensure_baseline("old")
    saved = reviews.save_review(
        baseline["scenario_id"],
        title="Synthetic baseline note",
        observation="This fixture supplies a saved frame record.",
        reviewer="Fixture reviewer",
        rationale="Record inspection for the synthetic test.",
        evidence_ids=["old:original"],
        status="reviewed",
    )
    before = reviews.review_history(saved["review_id"])
    service = ReleaseDiffService(store)
    original_count = service._candidate_counts

    def commit_after_coverage(connection, baseline_id, current_id):
        assert hash_media(store, current_id)["hashed"] == 1
        return original_count(connection, baseline_id, current_id)

    with monkeypatch.context() as patch:
        patch.setattr(service, "_candidate_counts", commit_after_coverage)
        result = service.compare("old", "new")["hash_comparison"]
    assert result["recorded_coverage"]["current"]["pending"] == 1
    assert result["recorded_candidate_pair_count_across_ids"] == 0
    assert result["stat_checked_examples"] == []

    refreshed = service.compare("old", "new")["hash_comparison"]
    assert refreshed["recorded_coverage"]["current"]["hashed"] == 1
    assert refreshed["recorded_candidate_pair_count_across_ids"] == 1
    assert refreshed["stat_matched_pair_count"] == 1
    assert reviews.review_history(saved["review_id"]) == before


@pytest.mark.parametrize(
    "kwargs",
    [
        {"category": "added' OR 1=1"},
        {"page": 0},
        {"page": True},
        {"page_size": 49},
    ],
)
def test_invalid_category_or_pagination_is_rejected(tmp_path, kwargs):
    _, service, _, _ = comparison_fixture(tmp_path)
    with pytest.raises(ValueError):
        service.compare("before", "after", **kwargs)


def test_comparison_rejects_same_unknown_and_invalid_releases(tmp_path):
    store = Store(tmp_path / "archive.sqlite")
    service = ReleaseDiffService(store)
    with pytest.raises(ValueError, match="different releases"):
        service.compare("same", "same")
    with pytest.raises(ReviewNotFoundError):
        service.compare("missing", "other")
    with pytest.raises(ValueError):
        service.compare("", "other")
