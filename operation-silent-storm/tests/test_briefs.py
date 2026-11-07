from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from test_ingest import frame, release

from silent_storm.briefs import BriefService
from silent_storm.contracts import ReviewConflictError, ReviewNotFoundError
from silent_storm.ingest import import_release
from silent_storm.media import hash_media
from silent_storm.reviews import ReviewService
from silent_storm.storage import Store, fingerprint


@pytest.fixture
def workspace(tmp_path):
    root = release(
        tmp_path,
        frames=[
            frame("a %&:", file="a.jpg"),
            frame("b", video_id="other", clip_id="second"),
            frame("pending", video_id="pending-video"),
            frame("missing", video_id="missing-video"),
        ],
    )
    (root / "frames/missing.jpg").unlink()
    store = Store(tmp_path / "briefs.sqlite")
    import_release(store, root, release_id="fixture", title="Synthetic source label")
    hash_media(store, "fixture", limit=2)
    reviews = ReviewService(store)
    baseline = reviews.ensure_baseline("fixture")
    return {
        "root": root,
        "store": store,
        "reviews": reviews,
        "baseline": baseline,
        "briefs": BriefService(store, reviews),
    }


def note(workspace, **changes):
    values = {
        "title": "Synthetic source observation",
        "observation": "A saved human observation.",
        "interpretation": "No event or identity is inferred.",
        "uncertainty": "Capture time unknown.",
        "rationale": "Inspected the supplied local test record.",
        "reviewer": "Alice",
        "status": "reviewed",
        "evidence_ids": ["fixture:a %&:"],
    }
    scenario_id = changes.pop("scenario_id", workspace["baseline"]["scenario_id"])
    values.update(changes)
    return workspace["reviews"].save_review(scenario_id, **values)


def save_brief(workspace, notes, **changes):
    scenario_id = changes.pop("scenario_id", workspace["baseline"]["scenario_id"])
    preview = workspace["briefs"].preview(scenario_id, [item["review_id"] for item in notes])
    values = {
        "title": "Saved specialist brief",
        "reviewer": "Brief author",
        "source_origin": "synthetic",
        "review_versions": preview["review_versions"],
        "expected_version": preview["expected_version"],
    }
    values.update(changes)
    return workspace["briefs"].create(scenario_id, **values)


def test_constructor_installs_schema_without_creating_scenarios_or_briefs(tmp_path):
    root = release(tmp_path)
    store = Store(tmp_path / "empty-briefs.sqlite")
    import_release(store, root)
    reviews = ReviewService(store)
    BriefService(store, reviews)
    assert reviews.list_scenarios("day-1") == []
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM saved_briefs").fetchone()[0] == 0


def test_snapshot_repository_keeps_insert_in_callers_transaction(workspace, monkeypatch):
    """An error after insertion must leave no partial brief or changed reviews."""
    from silent_storm.brief_repository import BriefRepository

    saved_note = note(workspace)
    scenario_id = workspace["baseline"]["scenario_id"]
    scenario = workspace["reviews"].get_scenario(scenario_id)
    insert = BriefRepository.insert_snapshot

    def fail_after_insert(repository, *args, **kwargs):
        assert repository.connection.in_transaction
        insert(repository, *args, **kwargs)
        raise RuntimeError("synthetic snapshot insertion failure")

    monkeypatch.setattr(BriefRepository, "insert_snapshot", fail_after_insert)
    with pytest.raises(RuntimeError, match="snapshot insertion failure"):
        save_brief(workspace, [saved_note])
    assert workspace["briefs"].list_briefs(scenario_id)["total"] == 0
    assert workspace["reviews"].get_scenario(scenario_id) == scenario
    assert workspace["reviews"].get_review(saved_note["review_id"]) == saved_note


def test_preview_explicitly_omits_drafts_and_uncomputed_hashes_without_writes(workspace):
    eligible = note(workspace)
    draft = note(workspace, reviewer="Bob", status="draft", evidence_ids=["fixture:b"])
    pending = note(workspace, reviewer="Cara", evidence_ids=["fixture:pending"])
    service, reviews = workspace["briefs"], workspace["reviews"]
    sid = workspace["baseline"]["scenario_id"]
    version = reviews.get_scenario(sid)["version"]
    preview = service.preview(sid, [item["review_id"] for item in (eligible, draft, pending)])
    assert [item["review_id"] for item in preview["included"]] == [eligible["review_id"]]
    omitted = {item["review_id"]: item for item in preview["omitted"]}
    assert "draft" in " ".join(omitted[draft["review_id"]]["reasons"]).lower()
    assert "completed" in " ".join(omitted[pending["review_id"]]["reasons"]).lower()
    assert preview["provisional"] and "rehash" in " ".join(preview["warnings"])
    assert preview["expected_version"] == version
    assert reviews.get_scenario(sid)["version"] == version
    assert all(
        reviews.review_history(item["review_id"])["total"] == 1
        for item in (eligible, draft, pending)
    )
    assert service.list_briefs(sid)["total"] == 0
    assert workspace["store"].get_frame("fixture", "pending")["hash_status"] == "pending"


def test_snapshot_freezes_notes_omissions_hashes_actors_history_and_exact_links(workspace):
    eligible = note(workspace)
    rejected = note(workspace, status="rejected", reviewer="Bob", evidence_ids=["fixture:b"])
    scenario_before = workspace["reviews"].get_scenario(workspace["baseline"]["scenario_id"])
    saved = save_brief(workspace, [eligible, rejected])
    assert saved["schema_version"] == 2
    assert (
        saved["snapshot_kind"] == "saved_specialist_review"
        and saved["source_origin"] == "synthetic"
    )
    assert saved["included"][0]["reviewer"] == "Alice"
    assert {item["reviewer"] for item in saved["selected_reviews"]} == {"Alice", "Bob"}
    assert {item["status"] for item in saved["review_history"]["items"]} == {"reviewed", "rejected"}
    assert saved["omitted"][0]["review_id"] == rejected["review_id"]
    assert len(saved["citations"]) == 1
    citation = saved["citations"][0]
    raw = workspace["store"].get_frame("fixture", "a %&:")
    assert citation["raw_metadata"] == raw["raw_metadata"]
    assert citation["source_file"] == "metadata/frames.csv" and citation["source_row"] == 2
    assert (
        citation["media_sha256"]
        == hashlib.sha256((workspace["root"] / "frames/a.jpg").read_bytes()).hexdigest()
    )
    assert citation["media_size_bytes"] == (workspace["root"] / "frames/a.jpg").stat().st_size
    assert parse_qs(urlsplit(citation["href"]).query) == {
        "release_id": ["fixture"],
        "frame_id": ["a %&:"],
        "sha256": [citation["media_sha256"]],
    }
    assert "a%20%25%26%3A" in citation["href"]
    assert (
        saved["release"]["metadata_fingerprint"]
        == workspace["store"].get_release("fixture")["metadata_fingerprint"]
    )
    assert "dataset_root" not in saved["release"]
    assert (
        fingerprint({key: value for key, value in saved.items() if key != "fingerprint"})
        == saved["fingerprint"]
    )
    assert workspace["reviews"].get_scenario(scenario_before["scenario_id"]) == scenario_before
    assert workspace["reviews"].review_history(eligible["review_id"])["total"] == 1
    assert json.loads(workspace["briefs"].json(saved["brief_id"])) == saved
    markdown = workspace["briefs"].markdown(saved["brief_id"])
    assert f"({citation['href']})" in markdown and "Saved specialist snapshot" in markdown
    assert "Alice" in markdown and "Brief author" in markdown and "Omitted statements" in markdown


def test_saved_content_survives_restart_and_database_rejects_mutation(workspace):
    saved = save_brief(workspace, [note(workspace)])
    reopened_store = Store(workspace["store"].path)
    reopened = BriefService(reopened_store, ReviewService(reopened_store))
    assert reopened.get_brief(saved["brief_id"]) == saved
    assert reopened.validate_brief(saved["brief_id"])["valid"] is True
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with reopened_store.connection() as connection:
            connection.execute(
                "UPDATE saved_briefs SET title='changed' WHERE brief_id=?", (saved["brief_id"],)
            )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with reopened_store.connection() as connection:
            connection.execute("DELETE FROM saved_briefs WHERE brief_id=?", (saved["brief_id"],))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with reopened_store.connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO saved_briefs "
                "SELECT brief_id,scenario_id,release_id,scenario_version,'replaced',reviewer,"
                "created_at,source_origin,snapshot_json,fingerprint FROM saved_briefs "
                "WHERE brief_id=?",
                (saved["brief_id"],),
            )
    result = reopened.get_brief(saved["brief_id"])
    result["included"][0]["title"] = "Caller-side mutation"
    assert reopened.get_brief(saved["brief_id"]) == saved


@pytest.mark.parametrize("stale_scenario", [True, False])
def test_finalization_checks_scenario_and_each_selected_revision_atomically(
    workspace, stale_scenario
):
    original = note(workspace)
    service, reviews, sid = (
        workspace["briefs"],
        workspace["reviews"],
        workspace["baseline"]["scenario_id"],
    )
    preview = service.preview(sid, [original["review_id"]])
    note(
        workspace,
        review_id=original["review_id"],
        expected_revision=original["revision"],
        reviewer="Bob",
    )
    expected = (
        preview["expected_version"] if stale_scenario else reviews.get_scenario(sid)["version"]
    )
    with pytest.raises(ReviewConflictError):
        service.create(
            sid,
            title="Stale snapshot",
            reviewer="Author",
            review_versions=preview["review_versions"],
            expected_version=expected,
        )
    assert service.list_briefs(sid)["total"] == 0
    assert reviews.review_history(original["review_id"])["total"] == 2


def test_no_eligible_selection_creates_no_snapshot_or_hash_job_changes(workspace):
    pending = note(workspace, evidence_ids=["fixture:pending"])
    counts = workspace["store"].hash_counts("fixture")
    with pytest.raises(ValueError, match="No selected statement"):
        save_brief(workspace, [pending])
    assert workspace["briefs"].list_briefs(workspace["baseline"]["scenario_id"])["total"] == 0
    assert workspace["store"].hash_counts("fixture") == counts


def test_selection_bounds_are_rejected_instead_of_truncating(workspace, tmp_path):
    service, sid = workspace["briefs"], workspace["baseline"]["scenario_id"]
    with pytest.raises(ValueError, match="12"):
        service.preview(sid, [f"unknown-{index}" for index in range(13)])
    rows = [frame(f"frame-{index}") for index in range(13)]
    root = release(tmp_path / "many", frames=rows)
    store = Store(tmp_path / "many.sqlite")
    import_release(store, root)
    hash_media(store, "day-1")
    reviews = ReviewService(store)
    baseline = reviews.ensure_baseline("day-1")
    many = reviews.save_review(
        baseline["scenario_id"],
        title="Many references",
        observation="Synthetic references.",
        reviewer="Author",
        rationale="Synthetic test records.",
        status="reviewed",
        evidence_ids=[f"day-1:frame-{index}" for index in range(13)],
    )
    with pytest.raises(ValueError, match="unique evidence frames"):
        BriefService(store, reviews).preview(baseline["scenario_id"], [many["review_id"]])
    other = workspace["reviews"].create_scenario("fixture", "Separate", "Author")
    with pytest.raises(ReviewNotFoundError):
        service.preview(other["scenario_id"], [note(workspace)["review_id"]])


@pytest.mark.parametrize("origin", ["Synthetic", "real-world", "", None, True])
def test_origin_is_explicit_and_strictly_validated(workspace, origin):
    with pytest.raises(ValueError, match="origin"):
        save_brief(workspace, [note(workspace)], source_origin=origin)


def test_source_title_never_guesses_synthetic_origin(workspace):
    saved = save_brief(workspace, [note(workspace)], source_origin="unspecified")
    assert saved["source_origin"] == "unspecified"


def test_newer_review_makes_saved_snapshot_historical_without_rewriting_it(workspace):
    original = note(workspace)
    saved = save_brief(workspace, [original])
    note(
        workspace,
        review_id=original["review_id"],
        expected_revision=1,
        observation="New manually saved wording.",
        reviewer="Bob",
    )
    checked = workspace["briefs"].validate_brief(saved["brief_id"])
    assert not checked["valid"] and checked["history_only"]
    assert checked["notes"][0]["current_revision"] == 2
    assert "newer" in " ".join(checked["notes"][0]["reasons"]).lower()
    assert checked["brief"] == saved and workspace["briefs"].get_brief(saved["brief_id"]) == saved


def test_exclusion_and_restoration_never_reapprove_a_frozen_snapshot(workspace):
    original = note(workspace)
    reviews = workspace["reviews"]
    fork = reviews.create_scenario("fixture", "Independent check", "Fork reviewer")
    clone = reviews.list_reviews(fork["scenario_id"])["items"][0]
    saved = save_brief(workspace, [clone], scenario_id=fork["scenario_id"])
    assert saved["included"][0]["origin"]["reviewer"] == original["reviewer"]
    reviews.set_source_exclusion(
        fork["scenario_id"],
        "video",
        excluded=True,
        reason="Source under rechecking",
        reviewer="Bob",
        expected_version=fork["version"],
    )
    assert workspace["briefs"].validate_brief(saved["brief_id"])["history_only"]
    current = reviews.get_scenario(fork["scenario_id"])
    reviews.set_source_exclusion(
        fork["scenario_id"],
        "video",
        excluded=False,
        reason="Restored source",
        reviewer="Cara",
        expected_version=current["version"],
    )
    assert reviews.get_review(clone["review_id"])["status"] == "needs_review"
    assert workspace["briefs"].validate_brief(saved["brief_id"])["history_only"]
    assert workspace["briefs"].get_brief(saved["brief_id"]) == saved
    assert reviews.review_history(original["review_id"])["total"] == 1


def test_finalization_detects_changed_bytes_even_with_same_size_and_mtime(workspace):
    item = note(workspace)
    path = workspace["root"] / "frames/a.jpg"
    before = path.stat()
    recorded = workspace["store"].get_frame("fixture", "a %&:")["media_sha256"]
    path.write_bytes(b"X" * before.st_size)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert workspace["store"].get_frame("fixture", "a %&:")["media_sha256"] == recorded
    with pytest.raises(ValueError, match="No selected statement"):
        save_brief(workspace, [item])
    assert workspace["store"].get_frame("fixture", "a %&:")["recorded_media_sha256"] == recorded


def test_fresh_proof_accepts_unchanged_bytes_after_a_metadata_only_touch(workspace):
    item = note(workspace)
    path = workspace["root"] / "frames/a.jpg"
    before = path.stat()
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
    assert workspace["store"].get_frame("fixture", "a %&:")["hash_stat_matches"] is False
    saved = save_brief(workspace, [item])
    assert workspace["briefs"].validate_brief(saved["brief_id"])["valid"]


@pytest.mark.parametrize("change", ["bytes", "missing", "escape"])
def test_validation_reports_current_media_failure_without_mutating_saved_content(
    workspace, tmp_path, change
):
    saved = save_brief(workspace, [note(workspace)])
    path = workspace["root"] / "frames/a.jpg"
    if change == "bytes":
        before = path.stat()
        path.write_bytes(b"X" * before.st_size)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif change == "missing":
        path.unlink()
    else:
        outside = tmp_path / "outside.jpg"
        outside.write_bytes(b"outside synthetic bytes")
        path.unlink()
        path.symlink_to(outside)
    result = workspace["briefs"].validate_brief(saved["brief_id"])
    assert not result["valid"] and result["history_only"] and result["reasons"]
    assert result["citations"][0]["valid"] is False
    assert result["brief"] == saved
    assert str(tmp_path) not in json.dumps(result)


def test_file_and_total_byte_limits_are_checked_before_any_hash_read(workspace, monkeypatch):
    import silent_storm.briefs as module

    first = note(workspace)
    second = note(workspace, evidence_ids=["fixture:b"])
    size = (workspace["root"] / "frames/a.jpg").stat().st_size

    def forbidden_read(*args, **kwargs):
        raise AssertionError("Preflight limits must fail before media hashing.")

    monkeypatch.setattr(module, "_fresh_digest", forbidden_read)
    monkeypatch.setattr(module, "MAX_SELECTED_MEDIA_BYTES", size * 2 - 1)
    with pytest.raises(ValueError, match="60 MiB"):
        save_brief(workspace, [first, second])
    monkeypatch.setattr(module, "MAX_SELECTED_MEDIA_BYTES", 60 * 1024 * 1024)
    monkeypatch.setattr(module, "MAX_MEDIA_FILE_BYTES", size - 1)
    with pytest.raises(ValueError, match="No selected statement"):
        save_brief(workspace, [first])


def test_saved_history_is_bounded_and_truncation_is_declared(workspace):
    current = note(workspace)
    for index in range(50):
        current = note(
            workspace,
            review_id=current["review_id"],
            expected_revision=current["revision"],
            observation=f"Synthetic revision {index}.",
            reviewer=f"Actor {index}",
        )
    saved = save_brief(workspace, [current])
    assert saved["review_history"]["total"] == 51
    assert len(saved["review_history"]["items"]) == 48 and saved["review_history"]["truncated"]
    assert saved["selected_reviews"][0]["revision"] == 51
    assert workspace["reviews"].review_history(current["review_id"])["total"] == 51


def test_oversized_pretty_json_cannot_create_a_snapshot_that_will_not_reopen(workspace):
    current = note(workspace)
    for _ in range(14):
        current = note(
            workspace,
            review_id=current["review_id"],
            expected_revision=current["revision"],
            observation="🧪" * 12000,
            interpretation="🧪" * 12000,
            uncertainty="🧪" * 6000,
            rationale="🧪" * 12000,
        )
    with pytest.raises(ValueError, match="2 MiB"):
        save_brief(workspace, [current])
    assert workspace["briefs"].list_briefs(workspace["baseline"]["scenario_id"])["total"] == 0


def test_unavailable_origin_is_explicit_without_inventing_an_origin_revision(workspace):
    original = note(workspace)
    fork = workspace["reviews"].create_scenario("fixture", "Origin check", "Fork reviewer")
    clone = workspace["reviews"].list_reviews(fork["scenario_id"])["items"][0]
    with workspace["store"].connection() as connection:
        connection.execute(
            "UPDATE review_notes SET origin_revision=999 WHERE review_id=?", (clone["review_id"],)
        )
    clone = workspace["reviews"].get_review(clone["review_id"])
    saved = save_brief(workspace, [clone], scenario_id=fork["scenario_id"])
    origin = saved["included"][0]["origin"]
    assert origin["available"] is False and origin["review_id"] == original["review_id"]
    assert "unavailable" in origin["reason"]


def test_brief_listing_is_bounded_and_unknown_ids_are_not_found(workspace):
    item = note(workspace)
    saved = [save_brief(workspace, [item], title=f"Snapshot {index}") for index in range(3)]
    sid = workspace["baseline"]["scenario_id"]
    first = workspace["briefs"].list_briefs(sid, page=1, page_size=2)
    second = workspace["briefs"].list_briefs(sid, page=2, page_size=2)
    assert first["total"] == second["total"] == 3
    assert {item["brief_id"] for item in first["items"] + second["items"]} == {
        item["brief_id"] for item in saved
    }
    with pytest.raises(ValueError):
        workspace["briefs"].list_briefs(sid, page_size=13)
    with pytest.raises(ReviewNotFoundError):
        workspace["briefs"].get_brief("missing")


def test_validity_check_detects_scenario_change_during_review_reads(workspace, monkeypatch):
    original = note(workspace)
    saved = save_brief(workspace, [original])
    get_review = workspace["reviews"].get_review
    changed = False

    def concurrent_change(review_id):
        nonlocal changed
        current = get_review(review_id)
        if not changed:
            changed = True
            note(workspace, title="Concurrent unrelated draft", status="draft")
        return current

    monkeypatch.setattr(workspace["reviews"], "get_review", concurrent_change)
    result = workspace["briefs"].validate_brief(saved["brief_id"])
    assert result["history_only"] and not result["valid"]
    assert "during validity" in " ".join(result["reasons"])


@pytest.mark.parametrize("change", ["missing", "same_size_and_mtime"])
def test_file_changed_after_hashing_is_conflict_not_partial_save(workspace, monkeypatch, change):
    import silent_storm.briefs as module

    item = note(workspace)
    verify = module._fresh_digest

    def change_after_proof(*args, **kwargs):
        proof = verify(*args, **kwargs)
        path = proof["path"]
        if change == "missing":
            path.unlink()
        else:
            before = path.stat()
            path.write_bytes(b"X" * before.st_size)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        return proof

    monkeypatch.setattr(module, "_fresh_digest", change_after_proof)
    with pytest.raises(ReviewConflictError, match="media changed"):
        save_brief(workspace, [item])
    assert workspace["briefs"].list_briefs(workspace["baseline"]["scenario_id"])["total"] == 0


def test_validation_rechecks_shared_citation_and_flags_only_dependent_notes(workspace, monkeypatch):
    import silent_storm.briefs as module

    first = note(workspace, title="Only A")
    second = note(workspace, title="Only B", evidence_ids=["fixture:b"])
    shared = note(workspace, title="A and B", evidence_ids=["fixture:a %&:", "fixture:b"])
    saved = save_brief(workspace, [first, second, shared])
    assert [citation["evidence_id"] for citation in saved["citations"]] == [
        "fixture:a %&:",
        "fixture:b",
    ]
    verify = module._fresh_digest
    path = workspace["root"] / "frames/a.jpg"

    def rewrite_first_during_second(root, relative_path, *args, **kwargs):
        if relative_path == "frames/b.jpg":
            before = path.stat()
            path.write_bytes(b"X" * before.st_size)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        return verify(root, relative_path, *args, **kwargs)

    monkeypatch.setattr(module, "_fresh_digest", rewrite_first_during_second)
    result = workspace["briefs"].validate_brief(saved["brief_id"])
    assert result["history_only"] and result["brief"] == saved
    assert {row["evidence_id"]: row["valid"] for row in result["citations"]} == {
        "fixture:a %&:": False,
        "fixture:b": True,
    }
    assert {row["review_id"]: row["valid"] for row in result["notes"]} == {
        first["review_id"]: False,
        second["review_id"]: True,
        shared["review_id"]: False,
    }


def test_validation_compares_frozen_evidence_even_when_revision_number_matches(
    workspace, monkeypatch
):
    item = note(workspace)
    saved = save_brief(workspace, [item])
    get_review = workspace["reviews"].get_review

    def same_number_different_evidence(review_id):
        return {**get_review(review_id), "evidence_ids": ["fixture:b"]}

    monkeypatch.setattr(workspace["reviews"], "get_review", same_number_different_evidence)
    result = workspace["briefs"].validate_brief(saved["brief_id"])
    assert result["history_only"] and result["brief"] == saved
    assert result["citations"][0]["valid"]
    assert result["notes"][0]["current_revision"] == item["revision"]
    assert "content differs" in " ".join(result["notes"][0]["reasons"])


def test_legacy_snapshot_without_history_remains_inspectable_and_unchanged(workspace):
    from silent_storm.storage import canonical_json

    saved = save_brief(workspace, [note(workspace)])
    legacy = {
        key: value for key, value in saved.items() if key not in ("fingerprint", "review_history")
    }
    legacy["schema_version"] = 1
    legacy["brief_id"] = "legacy-without-history"
    digest = fingerprint(legacy)
    with workspace["store"].connection() as connection:
        connection.execute(
            "INSERT INTO saved_briefs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                legacy["brief_id"],
                legacy["scenario"]["scenario_id"],
                legacy["release"]["release_id"],
                legacy["scenario"]["version"],
                legacy["title"],
                legacy["reviewer"],
                legacy["created_at"],
                legacy["source_origin"],
                canonical_json(legacy),
                digest,
            ),
        )
    before = workspace["briefs"].get_brief(legacy["brief_id"])
    validity = workspace["briefs"].validate_brief(legacy["brief_id"])
    assert validity["valid"] and validity["brief"] == before
    markdown = workspace["briefs"].markdown(legacy["brief_id"])
    assert "history was not included" in markdown and "extent is unknown" in markdown
    assert workspace["briefs"].get_brief(legacy["brief_id"]) == before
    assert "review_history" not in before
