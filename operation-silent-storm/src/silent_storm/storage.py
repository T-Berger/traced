"""Specialist SQLite lifecycle and bounded queries for supplied archive metadata.

Canonical JSON, fingerprints, and operation-local connection patterns are adapted
from Swarm and maintained locally; no Swarm modules are imported. Metadata and
specialist review tables live in this separate store while media bytes stay on
disk. Shared Evidence Trail cases are handed off by ``shared_brief``, not merged
into this database.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import MAX_PAGE_SIZE, UNKNOWN_FILTER, ExplorerFilters, normalize_filter


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def make_clip_key(video_id: str, clip_id: str, frame_id: str = "") -> str:
    """Unknown clip membership must not merge unrelated frames into one clip."""
    value = {"video_id": video_id, "clip_id": clip_id}
    if not clip_id:
        value["unknown_clip_frame_id"] = frame_id
    return fingerprint(value)


def default_db_path() -> Path:
    from .config import AppConfig

    return AppConfig.from_env().db_path


SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    release_id TEXT PRIMARY KEY, title TEXT NOT NULL, dataset_root TEXT NOT NULL,
    metadata_fingerprint TEXT NOT NULL, frames_csv_sha256 TEXT NOT NULL,
    detections_csv_sha256 TEXT NOT NULL, imported_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status='complete'), counts_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS frames (
    release_id TEXT NOT NULL REFERENCES releases(release_id), frame_id TEXT NOT NULL,
    file TEXT NOT NULL, clip_key TEXT NOT NULL, clip_id TEXT NOT NULL, video_id TEXT NOT NULL,
    week INTEGER, side TEXT NOT NULL, image_type TEXT NOT NULL,
    from_perspective TEXT NOT NULL, to_perspective TEXT NOT NULL, camera TEXT NOT NULL,
    width_px INTEGER, height_px INTEGER, relative_path TEXT NOT NULL,
    source_row INTEGER NOT NULL, raw_metadata TEXT NOT NULL,
    media_available INTEGER NOT NULL CHECK(media_available IN (0,1)),
    detection_count INTEGER NOT NULL DEFAULT 0, media_sha256 TEXT,
    hash_status TEXT NOT NULL DEFAULT 'pending' CHECK(hash_status IN ('pending','hashed','failed')),
    hash_error TEXT, hash_checked_at TEXT, media_size_bytes INTEGER, media_mtime_ns INTEGER,
    PRIMARY KEY(release_id,frame_id)
);
CREATE INDEX IF NOT EXISTS frames_clip ON frames(release_id,clip_key,frame_id);
CREATE INDEX IF NOT EXISTS frames_week ON frames(release_id,week);
CREATE INDEX IF NOT EXISTS frames_filters ON frames(release_id,side,camera,image_type);
CREATE INDEX IF NOT EXISTS frames_hash_status ON frames(release_id,hash_status,frame_id);
CREATE INDEX IF NOT EXISTS frames_media_hash ON frames(media_sha256) WHERE media_sha256 IS NOT NULL;
CREATE TABLE IF NOT EXISTS detections (
    release_id TEXT NOT NULL REFERENCES releases(release_id), source_row INTEGER NOT NULL,
    frame_id TEXT NOT NULL, class TEXT NOT NULL, model TEXT NOT NULL, confidence REAL,
    box_x1 REAL, box_y1 REAL, box_x2 REAL, box_y2 REAL, track_id TEXT NOT NULL,
    track_key TEXT, orphan INTEGER NOT NULL CHECK(orphan IN (0,1)), raw_metadata TEXT NOT NULL,
    PRIMARY KEY(release_id,source_row)
);
CREATE INDEX IF NOT EXISTS detections_frame ON detections(release_id,frame_id,source_row);
"""


class Store:
    """Open the local metadata store and initialize archive/review schemas.

    Construction creates directories, enables WAL, and applies review-schema
    initialization. ``connection`` commits successful operations and rolls back
    failures; review and brief services also execute their own SQL through it.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path).expanduser().resolve() if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            from .review_schema import initialize_review_schema

            initialize_review_schema(conn)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _release(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        counts = json.loads(result.pop("counts_json"))
        result["counts"] = counts
        result.update(counts)
        return result

    def list_releases(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM releases WHERE status='complete' "
                "ORDER BY imported_at DESC,release_id LIMIT 1000"
            ).fetchall()
        return [self._release(row) for row in rows]

    def get_release(self, release_id: str | None = None) -> dict[str, Any] | None:
        with self.connection() as conn:
            if release_id is None:
                row = conn.execute(
                    "SELECT * FROM releases WHERE status='complete' "
                    "ORDER BY imported_at DESC,release_id LIMIT 1"
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM releases WHERE release_id=? AND status='complete'", (release_id,)
                ).fetchone()
        return self._release(row)

    @staticmethod
    def _hash_counts(conn: sqlite3.Connection, release_id: str) -> dict[str, int]:
        counts = {"total": 0, "hashed": 0, "pending": 0, "failed": 0}
        for row in conn.execute(
            "SELECT hash_status,COUNT(*) n FROM frames WHERE release_id=? GROUP BY hash_status",
            (release_id,),
        ):
            counts[row["hash_status"]] = row["n"]
            counts["total"] += row["n"]
        return counts

    def hash_counts(self, release_id: str) -> dict[str, int]:
        with self.connection() as conn:
            return self._hash_counts(conn, release_id)

    def overview(self, release_id: str) -> dict[str, Any]:
        release = self.get_release(release_id)
        if release is None:
            raise KeyError(f"Unknown release: {release_id}")
        options: dict[str, list[str]] = {}
        with self.connection() as conn:
            for field in ("week", "side", "camera", "image_type"):
                rows = conn.execute(
                    f"SELECT DISTINCT {field} value FROM frames WHERE release_id=? "
                    f"ORDER BY {field}",
                    (release_id,),
                ).fetchall()
                options[field] = [
                    UNKNOWN_FILTER
                    if row["value"] in (None, "")
                    else (f"Week {row['value']}" if field == "week" else str(row["value"]))
                    for row in rows
                ]
            counts = conn.execute(
                "SELECT COUNT(*) frame_count,COUNT(DISTINCT clip_key) clip_count,"
                "COUNT(DISTINCT NULLIF(video_id,'')) video_count,"
                "COALESCE(SUM(media_available=0),0) missing_media_count "
                "FROM frames WHERE release_id=?",
                (release_id,),
            ).fetchone()
            detection_count = conn.execute(
                "SELECT COUNT(*) FROM detections WHERE release_id=?", (release_id,)
            ).fetchone()[0]
            hashes = self._hash_counts(conn, release_id)
        return {
            "release_id": release_id,
            "title": release["title"],
            **dict(counts),
            "detection_count": detection_count,
            "hash_counts": hashes,
            "filter_options": options,
            "orphan_detection_count": release["counts"].get("orphan_detection_count", 0),
        }

    @staticmethod
    def _where(release_id: str, filters: ExplorerFilters) -> tuple[str, list[Any]]:
        clauses, args = ["release_id=?"], [release_id]
        for field in ("week", "side", "camera", "image_type"):
            value = normalize_filter(getattr(filters, field))
            if value is None:
                continue
            if value == UNKNOWN_FILTER:
                clauses.append(f"{field} IS NULL" if field == "week" else f"{field}=''")
            elif field == "week":
                from .ingest import parse_week

                clauses.append("week=?")
                args.append(parse_week(value))
            else:
                clauses.append(f"{field}=?")
                args.append(value)
        query = filters.query.strip()
        if query:
            clauses.append(
                "(instr(lower(frame_id),lower(?))>0 OR instr(lower(clip_id),lower(?))>0 "
                "OR instr(lower(video_id),lower(?))>0)"
            )
            args.extend([query] * 3)
        return " AND ".join(clauses), args

    def list_clips(self, release_id: str, filters: ExplorerFilters) -> dict[str, Any]:
        where, args = self._where(release_id, filters)
        with self.connection() as conn:
            counts = conn.execute(
                f"SELECT COUNT(DISTINCT clip_key) total,COUNT(*) total_frames FROM frames WHERE {where}",
                args,
            ).fetchone()
            rows = conn.execute(
                f"""WITH matched AS (
                    SELECT clip_key,COUNT(*) matched_frame_count,
                    MIN(frame_id) representative_frame_id FROM frames WHERE {where} GROUP BY clip_key
                ) SELECT f.clip_key,MIN(f.clip_id) clip_id,MIN(f.video_id) video_id,
                    COUNT(*) frame_count,m.matched_frame_count,m.representative_frame_id,
                    json_group_array(DISTINCT f.week) weeks,
                    json_group_array(DISTINCT f.side) sides,
                    json_group_array(DISTINCT f.camera) cameras
                FROM frames f JOIN matched m ON f.clip_key=m.clip_key
                WHERE f.release_id=? GROUP BY f.clip_key
                ORDER BY clip_id,video_id,f.clip_key LIMIT ? OFFSET ?""",
                [*args, release_id, filters.page_size, (filters.page - 1) * filters.page_size],
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            for field in ("weeks", "sides", "cameras"):
                item[field] = sorted(
                    json.loads(item[field]), key=lambda value: (value is None, value)
                )
            items.append(item)
        return {
            "items": items,
            **dict(counts),
            "page": filters.page,
            "page_size": filters.page_size,
        }

    @staticmethod
    def _frame(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["raw_metadata"] = json.loads(result["raw_metadata"])
        result["recorded_media_available"] = bool(result["media_available"])
        result["source_file"] = "metadata/frames.csv"
        root = result.pop("dataset_root")
        from .media import resolve_frame_path

        stat = None
        try:
            path = resolve_frame_path(root, result["relative_path"])
            stat = path.stat()
            result["media_available"] = True
            result["media_availability_error"] = None
        except (OSError, ValueError) as exc:
            result["media_available"] = False
            result["media_availability_error"] = (
                f"Media unavailable ({type(exc).__name__})"
                if isinstance(exc, OSError)
                else str(exc)
            )
        # Hash status belongs to the persisted checksum job. Do not turn a read
        # into a write or pretend that a changed file is queued for that job.
        recorded = result["media_sha256"]
        result["recorded_media_sha256"] = recorded
        if recorded is None:
            result["hash_stat_matches"] = None
            result["hash_verification"] = "not_computed"
        else:
            matches = stat is not None and (
                stat.st_size == result["media_size_bytes"]
                and stat.st_mtime_ns == result["media_mtime_ns"]
            )
            result["hash_stat_matches"] = matches
            result["hash_verification"] = (
                "recorded_stat_match"
                if matches
                else ("media_unavailable" if stat is None else "media_changed")
            )
            if not matches:
                result["media_sha256"] = None
        return result

    def get_clip(self, release_id: str, clip_key: str, offset: int = 0, limit: int = 12):
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("Contact-sheet offset must be a nonnegative integer.")
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("Contact-sheet limit must be a positive integer.")
        limit = min(limit, MAX_PAGE_SIZE)
        with self.connection() as conn:
            group = conn.execute(
                "SELECT clip_key,MIN(clip_id) clip_id,MIN(video_id) video_id,COUNT(*) frame_count "
                "FROM frames WHERE release_id=? AND clip_key=? GROUP BY clip_key",
                (release_id, clip_key),
            ).fetchone()
            if group is None:
                return None
            rows = conn.execute(
                "SELECT f.*,r.dataset_root FROM frames f "
                "JOIN releases r ON r.release_id=f.release_id "
                "WHERE f.release_id=? AND f.clip_key=? "
                "ORDER BY f.frame_id LIMIT ? OFFSET ?",
                (release_id, clip_key, limit, offset),
            ).fetchall()
        return {
            **dict(group),
            "offset": offset,
            "limit": limit,
            "frames": [self._frame(row) for row in rows],
        }

    def get_frame(self, release_id: str, frame_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT f.*,r.dataset_root FROM frames f "
                "JOIN releases r ON r.release_id=f.release_id "
                "WHERE f.release_id=? AND f.frame_id=?",
                (release_id, frame_id),
            ).fetchone()
        return self._frame(row)

    def get_detections(self, release_id: str, frame_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM detections WHERE release_id=? AND frame_id=? "
                "ORDER BY source_row LIMIT ?",
                (release_id, frame_id, MAX_PAGE_SIZE),
            ).fetchall()
        results = []
        for row in rows:
            value = dict(row)
            value["raw_metadata"] = json.loads(value["raw_metadata"])
            value.update(source_file="metadata/detections.csv", unverified=True)
            value["orphan"] = bool(value["orphan"])
            results.append(value)
        return results

    def backup(self, destination: str | Path) -> Path:
        path = Path(destination).expanduser().resolve()
        if path == self.path or path.exists():
            raise ValueError("Backup destination must be a new file, different from the database.")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb"):
            pass
        try:
            with self.connection() as source:
                target = sqlite3.connect(path)
                try:
                    source.backup(target)
                finally:
                    target.close()
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path
