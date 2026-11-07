from __future__ import annotations

import hashlib

import pytest
from test_ingest import frame, release

from silent_storm import media
from silent_storm.ingest import import_release
from silent_storm.media import hash_media, resolve_frame_path
from silent_storm.storage import Store


@pytest.mark.parametrize(
    "relative_path",
    [
        "/tmp/file.jpg",
        "../outside.jpg",
        "frames/../file.jpg",
        "frames/./file.jpg",
        "C:/file.jpg",
        "frames\\file.jpg",
        "frames/file.svg",
        "frames//file.jpg",
    ],
)
def test_resolver_rejects_unsafe_paths(tmp_path, relative_path):
    with pytest.raises(ValueError):
        resolve_frame_path(tmp_path, relative_path)


def test_resolver_distinguishes_valid_missing_and_escaping_symlinks(tmp_path):
    root = release(tmp_path)
    assert resolve_frame_path(root, "frames/a.jpg") == root / "frames/a.jpg"
    with pytest.raises(FileNotFoundError):
        resolve_frame_path(root, "frames/missing.jpg")
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"not part of release")
    (root / "frames/link.jpg").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        resolve_frame_path(root, "frames/link.jpg")


def test_image_alias_cannot_resolve_to_a_non_image_extension(tmp_path):
    root = release(tmp_path)
    target = root / "frames/note.txt"
    target.write_text("synthetic non-image fixture")
    (root / "frames/alias.jpg").symlink_to(target)
    with pytest.raises(ValueError, match="Resolved media target"):
        resolve_frame_path(root, "frames/alias.jpg")


def test_partial_hashing_resumes_and_pending_work_precedes_old_failures(tmp_path):
    root = release(tmp_path, frames=[frame("a-missing"), frame("b"), frame("c")])
    (root / "frames/a-missing.jpg").unlink()
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, root)
    first = hash_media(store, "day-1", limit=1)
    assert first == {"total": 3, "hashed": 0, "pending": 2, "failed": 1, "processed": 1}
    second = hash_media(store, "day-1", limit=1)
    assert second["hashed"] == 1 and second["pending"] == 1
    assert store.get_frame("day-1", "c")["media_sha256"] is None
    result = hash_media(store, "day-1")
    assert result["hashed"] == 2 and result["failed"] == 1
    assert (
        store.get_frame("day-1", "b")["media_sha256"]
        == (store.get_frame("day-1", "c")["media_sha256"])
    )
    (root / "frames/a-missing.jpg").write_bytes(b"recovered source")
    recovered = hash_media(store, "day-1")
    assert recovered["hashed"] == 3 and recovered["failed"] == 0
    assert recovered["processed"] == 1


def test_process_interruption_retains_completed_hashes(tmp_path, monkeypatch):
    root = release(tmp_path, frames=[frame("a"), frame("b"), frame("c")])
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, root)
    original = media.sha256_file
    calls = 0

    def interrupted(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return original(path)

    monkeypatch.setattr(media, "sha256_file", interrupted)
    with pytest.raises(KeyboardInterrupt):
        hash_media(store, "day-1")
    assert Store(store.path).hash_counts("day-1") == {
        "total": 3,
        "hashed": 1,
        "pending": 2,
        "failed": 0,
    }
    monkeypatch.setattr(media, "sha256_file", original)
    assert hash_media(store, "day-1")["processed"] == 2


def test_same_id_in_different_releases_has_independent_byte_identity(tmp_path):
    root1 = release(tmp_path / "one")
    root2 = release(tmp_path / "two")
    (root2 / "frames/a.jpg").write_bytes(b"different supplied bytes")
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, root1, release_id="one")
    import_release(store, root2, release_id="two")
    hash_media(store, "one")
    assert store.get_frame("two", "a")["media_sha256"] is None
    hash_media(store, "two")
    left = store.get_frame("one", "a")["media_sha256"]
    right = store.get_frame("two", "a")["media_sha256"]
    assert left != right
    assert right == hashlib.sha256(b"different supplied bytes").hexdigest()


def test_zero_hash_limit_keeps_every_file_unknown(tmp_path):
    root = release(tmp_path)
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, root)
    assert hash_media(store, "day-1", limit=0) == {
        "total": 1,
        "hashed": 0,
        "pending": 1,
        "failed": 0,
        "processed": 0,
    }
