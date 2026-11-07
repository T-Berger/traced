from __future__ import annotations

import os

import pytest
from test_ingest import frame, release

from silent_storm.config import AppConfig
from silent_storm.contracts import ExplorerFilters
from silent_storm.ingest import import_release
from silent_storm.media import hash_media
from silent_storm.storage import Store, canonical_json, fingerprint, make_clip_key


def test_fingerprints_are_deterministic_without_erasing_unicode():
    assert canonical_json({"b": "ü", "a": 1}) == '{"a":1,"b":"ü"}'
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    assert make_clip_key("video-a", "same") != make_clip_key("video-b", "same")
    assert make_clip_key("video", "", "a") != make_clip_key("video", "", "b")


def test_default_database_matches_application_config_without_import_side_effects(
    tmp_path, monkeypatch
):
    target = tmp_path / "configured" / "archive.sqlite"
    monkeypatch.setenv("SILENT_STORM_DB", str(target))
    assert AppConfig.from_env().db_path == target
    assert not target.exists()
    assert Store().path == target


def test_backup_is_reopenable_and_never_overwrites(tmp_path):
    root = release(tmp_path)
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, root)
    destination = tmp_path / "backup.sqlite"
    store.backup(destination)
    reopened = Store(destination)
    assert reopened.get_frame("day-1", "a")["raw_metadata"]["week"] == "Week 11"
    with pytest.raises(ValueError, match="new file"):
        store.backup(destination)
    with pytest.raises(ValueError, match="new file"):
        store.backup(store.path)


def test_pending_hashes_are_unknown_after_reopening(tmp_path):
    root = release(tmp_path)
    path = tmp_path / "archive.sqlite"
    import_release(Store(path), root)
    reopened = Store(path)
    assert reopened.hash_counts("day-1") == {
        "total": 1,
        "hashed": 0,
        "pending": 1,
        "failed": 0,
    }
    assert reopened.get_frame("day-1", "a")["media_sha256"] is None


def test_contact_sheet_limit_is_bounded_and_offset_is_validated(tmp_path):
    root = release(tmp_path, frames=[frame("a"), frame("b")])
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, root)
    clip = store.list_clips("day-1", ExplorerFilters())["items"][0]
    result = store.get_clip("day-1", clip["clip_key"], limit=500)
    assert result["limit"] == 48
    assert [item["frame_id"] for item in result["frames"]] == ["a", "b"]
    with pytest.raises(ValueError):
        store.get_clip("day-1", clip["clip_key"], offset=-1)
    assert store.get_clip("day-1", "unknown") is None


def test_unknown_releases_have_explicit_lookup_behavior(tmp_path):
    store = Store(tmp_path / "archive.sqlite")
    assert store.get_release("missing") is None
    assert store.get_frame("missing", "a") is None
    with pytest.raises(KeyError, match="Unknown release"):
        store.overview("missing")


def test_selected_reads_recheck_deleted_media_and_recovery_without_mutating_manifest(tmp_path):
    root = release(tmp_path)
    store = Store(tmp_path / "archive.sqlite")
    report = import_release(store, root)
    before = store.get_frame("day-1", "a")
    assert before["media_available"] is True
    (root / "frames/a.jpg").unlink()
    assert store.get_frame("day-1", "a")["media_available"] is False
    clip = store.get_clip("day-1", before["clip_key"])
    assert clip["frames"][0]["media_available"] is False
    (root / "frames/a.jpg").write_bytes(b"restored fixture")
    assert store.get_frame("day-1", "a")["media_available"] is True
    assert store.get_clip("day-1", before["clip_key"])["frames"][0]["media_available"] is True
    assert store.get_release("day-1")["metadata_fingerprint"] == report["metadata_fingerprint"]
    assert store.get_release("day-1")["missing_media_count"] == 0
    assert store.get_frame("day-1", "a")["raw_metadata"] == before["raw_metadata"]


def test_selected_reads_preserve_recorded_hash_but_clear_current_hash_when_media_changes(tmp_path):
    root = release(tmp_path)
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, root)
    hash_media(store, "day-1")
    initial = store.get_frame("day-1", "a")
    recorded = initial["media_sha256"]
    assert recorded is not None and initial["hash_stat_matches"] is True
    assert initial["hash_verification"] == "recorded_stat_match"
    path = root / "frames/a.jpg"
    path.write_bytes(b"changed bytes")
    changed = store.get_frame("day-1", "a")
    assert changed["media_available"] is True
    assert changed["media_sha256"] is None
    assert changed["recorded_media_sha256"] == recorded
    assert changed["hash_verification"] == "media_changed"
    assert changed["hash_stat_matches"] is False
    path.unlink()
    missing = store.get_frame("day-1", "a")
    assert missing["media_sha256"] is None
    assert missing["recorded_media_sha256"] == recorded
    assert missing["hash_verification"] == "media_unavailable"
    # A newly created file with matching bytes still needs byte verification;
    # changing its timestamp makes that freshness distinction deterministic.
    path.write_bytes(b"synthetic checksum fixture")
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    restored = store.get_frame("day-1", "a")
    assert restored["media_available"] is True
    assert restored["media_sha256"] is None
    assert restored["recorded_media_sha256"] == recorded
    # Reads never invent a new pending job or rewrite saved checksum history.
    assert store.hash_counts("day-1")["hashed"] == 1
    assert restored["hash_status"] == "hashed"
