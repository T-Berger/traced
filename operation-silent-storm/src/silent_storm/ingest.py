"""Atomic, immutable imports of the supplied frame and detection CSV schemas."""

from __future__ import annotations

import csv
import math
import re
import sqlite3
from pathlib import Path

from .media import hash_media as hash_media
from .media import normalize_relative_path, resolve_frame_path, sha256_file
from .storage import Store, canonical_json, fingerprint, make_clip_key, utc_now

FRAME_FIELDS = (
    "frame_id",
    "file",
    "clip_id",
    "video_id",
    "week",
    "side",
    "image_type",
    "from_perspective",
    "to_perspective",
    "camera",
    "width_px",
    "height_px",
)
DETECTION_FIELDS = (
    "frame_id",
    "class",
    "model",
    "confidence",
    "box_x1",
    "box_y1",
    "box_x2",
    "box_y2",
    "track_id",
)


def parse_week(value: str) -> int | None:
    value = str(value).strip()
    if not value:
        return None
    matched = re.fullmatch(r"(?:Week\s+)?(\d+)", value, flags=re.IGNORECASE)
    if not matched:
        raise ValueError(f"Invalid exercise-week label: {value!r}; schema review required.")
    return int(matched.group(1))


def _integer(value: str, name: str) -> int | None:
    if not value.strip():
        return None
    if not re.fullmatch(r"\d+", value.strip()):
        raise ValueError(f"Invalid {name}; expected a nonnegative integer or blank.")
    return int(value)


def _number(value: str, name: str) -> float | None:
    if not value.strip():
        return None
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError(f"Invalid numeric {name}.") from exc
    if not math.isfinite(result):
        raise ValueError(f"Non-finite {name} is not valid supplied metadata.")
    return result


def _rows(path: Path, expected: tuple[str, ...]):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        if len(set(header)) != len(header) or set(header) != set(expected):
            missing = sorted(set(expected) - set(header))
            unknown = sorted(set(header) - set(expected))
            raise ValueError(
                f"Schema review required for {path.name}: missing={missing}, unknown={unknown}."
            )
        while True:
            source_row = reader.line_num + 1
            try:
                raw = next(reader)
            except StopIteration:
                break
            if None in raw or any(value is None for value in raw.values()):
                raise ValueError(f"Malformed CSV row in {path.name} at line {source_row}.")
            yield source_row, raw, {key: value.strip() for key, value in raw.items()}


def _media_path(file: str) -> str:
    normalized = normalize_relative_path(file)
    return normalized if normalized.startswith("frames/") else f"frames/{normalized}"


def import_release(
    store: Store,
    dataset_root: str | Path,
    release_id: str = "day-1",
    title: str = "Day 1",
) -> dict:
    if not isinstance(release_id, str) or not release_id.strip():
        raise ValueError("A nonempty release ID is required.")
    root = Path(dataset_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("The dataset root must contain metadata/ and frames/ directories.")
    paths = {name: root / "metadata" / name for name in ("frames.csv", "detections.csv")}
    for path in paths.values():
        if not path.resolve(strict=True).is_relative_to(root):
            raise ValueError("Metadata symlinks must stay within the configured dataset root.")
    digests = {name: sha256_file(path) for name, path in paths.items()}
    metadata_fingerprint = fingerprint({"schema_version": 1, "csv_sha256": digests})
    imported_at = utc_now()
    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT metadata_fingerprint,dataset_root FROM releases WHERE release_id=?",
            (release_id,),
        ).fetchone()
        if existing is not None:
            if existing["metadata_fingerprint"] != metadata_fingerprint:
                raise ValueError(
                    "Release ID already exists with changed metadata; use a new release ID."
                )
            if existing["dataset_root"] != str(root):
                raise ValueError(
                    "The immutable release is already bound to a different dataset root."
                )
            result = store._release(
                conn.execute("SELECT * FROM releases WHERE release_id=?", (release_id,)).fetchone()
            )
            return {
                **result,
                "status": "unchanged",
                "release_status": result["status"],
                "hash_counts": store._hash_counts(conn, release_id),
            }
        conn.execute(
            "INSERT INTO releases VALUES (?,?,?,?,?,?,?,?,?)",
            (
                release_id,
                title,
                str(root),
                metadata_fingerprint,
                digests["frames.csv"],
                digests["detections.csv"],
                imported_at,
                "complete",
                "{}",
            ),
        )
        frame_count = 0
        try:
            for source_row, raw, row in _rows(paths["frames.csv"], FRAME_FIELDS):
                if not row["frame_id"]:
                    raise ValueError(f"Blank frame ID at frames.csv line {source_row}.")
                relative_path = _media_path(row["file"])
                try:
                    resolve_frame_path(root, relative_path)
                    available = 1
                except FileNotFoundError:
                    available = 0
                conn.execute(
                    "INSERT INTO frames (release_id,frame_id,file,clip_key,clip_id,video_id,week,"
                    "side,image_type,from_perspective,to_perspective,camera,width_px,height_px,"
                    "relative_path,source_row,raw_metadata,media_available) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        release_id,
                        row["frame_id"],
                        raw["file"],
                        make_clip_key(row["video_id"], row["clip_id"], row["frame_id"]),
                        row["clip_id"],
                        row["video_id"],
                        parse_week(row["week"]),
                        row["side"],
                        row["image_type"],
                        row["from_perspective"],
                        row["to_perspective"],
                        row["camera"],
                        _integer(row["width_px"], "width_px"),
                        _integer(row["height_px"], "height_px"),
                        relative_path,
                        source_row,
                        canonical_json(raw),
                        available,
                    ),
                )
                frame_count += 1
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                "Duplicate frame ID or invalid frame record; import rolled back."
            ) from exc
        detection_count, orphan_count = 0, 0
        for source_row, raw, row in _rows(paths["detections.csv"], DETECTION_FIELDS):
            if not row["frame_id"]:
                raise ValueError(f"Blank frame ID at detections.csv line {source_row}.")
            confidence = _number(row["confidence"], "confidence")
            if confidence is not None and not 0 <= confidence <= 1:
                raise ValueError("Detector confidence must be between 0 and 1 or blank.")
            frame = conn.execute(
                "SELECT clip_key FROM frames WHERE release_id=? AND frame_id=?",
                (release_id, row["frame_id"]),
            ).fetchone()
            orphan = int(frame is None)
            track_key = (
                fingerprint(
                    {
                        "release_id": release_id,
                        "clip_key": frame["clip_key"],
                        "model": row["model"],
                        "track_id": row["track_id"],
                    }
                )
                if frame is not None and row["track_id"]
                else None
            )
            conn.execute(
                "INSERT INTO detections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    release_id,
                    source_row,
                    row["frame_id"],
                    row["class"],
                    row["model"],
                    confidence,
                    *[
                        _number(row[field], field)
                        for field in ("box_x1", "box_y1", "box_x2", "box_y2")
                    ],
                    row["track_id"],
                    track_key,
                    orphan,
                    canonical_json(raw),
                ),
            )
            detection_count += 1
            orphan_count += orphan
        conn.execute(
            "UPDATE frames SET detection_count=(SELECT COUNT(*) FROM detections d "
            "WHERE d.release_id=frames.release_id AND d.frame_id=frames.frame_id) WHERE release_id=?",
            (release_id,),
        )
        counts = dict(
            conn.execute(
                "SELECT COUNT(*) frame_count,COUNT(DISTINCT clip_key) clip_count,"
                "COUNT(DISTINCT NULLIF(clip_id,'')) supplied_clip_id_count,"
                "COUNT(DISTINCT NULLIF(video_id,'')) video_count,"
                "COALESCE(SUM(media_available=0),0) missing_media_count FROM frames WHERE release_id=?",
                (release_id,),
            ).fetchone()
        )
        if counts["frame_count"] != frame_count:
            raise ValueError("Frame input/import reconciliation failed.")
        counts.update(
            detection_count=detection_count,
            orphan_detection_count=orphan_count,
            duplicate_frame_id_count=0,
        )
        # A changed CSV during import must not commit a misleading input fingerprint.
        if any(sha256_file(paths[name]) != digest for name, digest in digests.items()):
            raise ValueError("Metadata changed during import; import rolled back.")
        conn.execute(
            "UPDATE releases SET counts_json=? WHERE release_id=?",
            (canonical_json(counts), release_id),
        )
    return {
        "status": "imported",
        "release_id": release_id,
        "title": title,
        "dataset_root": str(root),
        "imported_at": imported_at,
        "metadata_fingerprint": metadata_fingerprint,
        "counts": counts,
        **counts,
        "hash_counts": store.hash_counts(release_id),
    }
