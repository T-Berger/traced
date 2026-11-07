from __future__ import annotations

import csv
from pathlib import Path

import pytest

from silent_storm.ingest import DETECTION_FIELDS, FRAME_FIELDS, import_release
from silent_storm.storage import Store


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def frame(frame_id="a", **changes):
    row = dict.fromkeys(FRAME_FIELDS, "")
    row.update(
        frame_id=frame_id,
        file=f"{frame_id}.jpg",
        clip_id="clip",
        video_id="video",
        week="Week 11",
        width_px="1",
        height_px="1",
    )
    row.update(changes)
    return row


def release(
    tmp_path: Path, frames: list[dict] | None = None, detections: list[dict] | None = None
) -> Path:
    root = tmp_path / "release"
    (root / "metadata").mkdir(parents=True)
    (root / "frames").mkdir()
    frames = frames if frames is not None else [frame()]
    for row in frames:
        (root / "frames" / row["file"]).write_bytes(b"synthetic checksum fixture")
    write_csv(root / "metadata/frames.csv", FRAME_FIELDS, frames)
    write_csv(root / "metadata/detections.csv", DETECTION_FIELDS, detections or [])
    return root


def test_import_preserves_sources_orphans_and_is_idempotent(tmp_path):
    detection = dict.fromkeys(DETECTION_FIELDS, "")
    detection.update(frame_id="orphan", confidence="0.5", **{"class": "unclear"})
    root = release(tmp_path, detections=[detection])
    original = (root / "metadata/frames.csv").read_bytes()
    store = Store(tmp_path / "archive.sqlite")
    report = import_release(store, root)
    assert report["frame_count"] == 1
    assert report["detection_count"] == report["orphan_detection_count"] == 1
    item = store.get_frame("day-1", "a")
    assert item["week"] == 11
    assert item["raw_metadata"]["week"] == "Week 11"
    assert item["relative_path"] == "frames/a.jpg"
    assert item["source_row"] == 2
    assert item["media_sha256"] is None
    assert import_release(store, root)["status"] == "unchanged"
    assert len(store.list_releases()) == 1
    assert (root / "metadata/frames.csv").read_bytes() == original
    assert store.get_detections("day-1", "orphan")[0]["unverified"] is True


def test_changed_metadata_cannot_replace_an_immutable_release(tmp_path):
    root = release(tmp_path)
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, root)
    write_csv(root / "metadata/frames.csv", FRAME_FIELDS, [frame(week="Week 12")])
    with pytest.raises(ValueError, match="changed metadata"):
        import_release(store, root)
    assert store.get_frame("day-1", "a")["week"] == 11
    assert len(store.list_releases()) == 1


def test_duplicate_frame_ids_roll_back_the_entire_import(tmp_path):
    root = release(tmp_path, frames=[frame(), frame()])
    store = Store(tmp_path / "archive.sqlite")
    with pytest.raises(ValueError, match="Duplicate frame ID"):
        import_release(store, root)
    assert store.list_releases() == []
    assert store.get_frame("day-1", "a") is None


@pytest.mark.parametrize(
    "changes",
    [
        {"week": "September"},
        {"width_px": "nan"},
        {"file": "../outside.jpg"},
    ],
)
def test_invalid_typed_or_path_metadata_is_atomic(tmp_path, changes):
    root = release(tmp_path)
    write_csv(root / "metadata/frames.csv", FRAME_FIELDS, [frame(), frame("b", **changes)])
    store = Store(tmp_path / "archive.sqlite")
    with pytest.raises(ValueError):
        import_release(store, root)
    assert store.get_release() is None
    assert store.get_frame("day-1", "a") is None


def test_schema_drift_requires_review_instead_of_mapping_unknown_columns(tmp_path):
    root = release(tmp_path)
    write_csv(
        root / "metadata/frames.csv",
        (*FRAME_FIELDS, "capture_time"),
        [{**frame(), "capture_time": "not supplied by original schema"}],
    )
    store = Store(tmp_path / "archive.sqlite")
    with pytest.raises(ValueError, match="Schema review required"):
        import_release(store, root)
    assert store.get_release() is None


def test_csv_provenance_uses_the_physical_start_line(tmp_path):
    root = release(tmp_path, frames=[frame(camera="synthetic\nlabel"), frame("b")])
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, root)
    assert store.get_frame("day-1", "a")["source_row"] == 2
    assert store.get_frame("day-1", "b")["source_row"] == 4


def test_unknown_dimensions_and_week_stay_unknown(tmp_path):
    root = release(tmp_path, frames=[frame(week="", width_px="", height_px="")])
    store = Store(tmp_path / "archive.sqlite")
    import_release(store, root)
    item = store.get_frame("day-1", "a")
    assert item["week"] is item["width_px"] is item["height_px"] is None
