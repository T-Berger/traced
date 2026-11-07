"""Bounded comparisons of immutable supplied metadata and recorded media hashes."""

from __future__ import annotations

from collections import deque
from typing import Any

from .contracts import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, ReviewNotFoundError
from .reviews import qualify_evidence_id
from .storage import Store

CATEGORIES = ("added", "removed", "metadata_changed", "unchanged")
EXAMPLE_LIMIT = MAX_PAGE_SIZE
MATCHES_PER_CURRENT_EXAMPLE = 4

COMPARISON = """
WITH compared AS (
    SELECT c.frame_id,
        CASE WHEN b.frame_id IS NULL THEN 'added'
             WHEN b.raw_metadata<>c.raw_metadata THEN 'metadata_changed'
             ELSE 'unchanged' END category
    FROM frames c LEFT JOIN frames b ON b.release_id=? AND b.frame_id=c.frame_id
    WHERE c.release_id=?
    UNION ALL
    SELECT b.frame_id,'removed' category FROM frames b
    WHERE b.release_id=? AND NOT EXISTS (
        SELECT 1 FROM frames c WHERE c.release_id=? AND c.frame_id=b.frame_id
    )
)
"""


def _release_argument(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("Choose a nonempty release ID of at most 200 characters.")
    if any(ord(character) < 32 for character in value):
        raise ValueError("Release IDs cannot contain control characters.")
    return value


def _hash_state(frame: dict | None) -> str:
    if frame is None:
        return "absent"
    if not frame["media_available"]:
        return "missing"
    if frame["hash_status"] == "pending":
        return "pending"
    if frame["hash_status"] == "failed":
        return "failed"
    if frame.get("media_sha256") and frame.get("hash_stat_matches") is True:
        return "recorded_hash_and_stat_match"
    return "stale"


class ReleaseDiffService:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _descriptor(release: dict) -> dict[str, Any]:
        return {
            key: release[key]
            for key in (
                "release_id",
                "title",
                "metadata_fingerprint",
                "frames_csv_sha256",
                "detections_csv_sha256",
                "counts",
            )
        }

    @staticmethod
    def _media_comparison(before: dict | None, after: dict | None) -> dict[str, str]:
        states = {"before_hash_state": _hash_state(before), "after_hash_state": _hash_state(after)}
        if before is None or after is None:
            return {
                **states,
                "status": "one_sided",
                "basis": "metadata_membership_only",
                "note": "Added or removed metadata does not establish new or lost media bytes.",
            }
        left, right = before.get("media_sha256"), after.get("media_sha256")
        if (
            states["before_hash_state"]
            == states["after_hash_state"]
            == "recorded_hash_and_stat_match"
        ):
            return {
                **states,
                "status": "same_bytes" if left == right else "different_bytes",
                "basis": "completed_recorded_sha256_with_matching_current_size_mtime",
                "note": "Recorded byte checksums compared; current size/mtime matches their records. "
                "This comparison did not freshly hash file bytes.",
            }
        return {
            **states,
            "status": "unresolved",
            "basis": "incomplete_current_hash_evidence",
            "note": "Pending, failed, stale, or missing media cannot establish byte identity.",
        }

    @staticmethod
    def _candidate_counts(conn, baseline_id: str, current_id: str) -> dict[str, int]:
        # Aggregate hash groups rather than materializing a potentially quadratic
        # many-to-many join. Same-ID pairs are excluded from cross-ID reuse counts.
        row = conn.execute(
            """WITH b AS (
                SELECT media_sha256 digest,COUNT(*) n FROM frames
                WHERE release_id=? AND hash_status='hashed' AND media_sha256 IS NOT NULL
                GROUP BY media_sha256
            ), c AS (
                SELECT media_sha256 digest,COUNT(*) n FROM frames
                WHERE release_id=? AND hash_status='hashed' AND media_sha256 IS NOT NULL
                GROUP BY media_sha256
            ), same_id AS (
                SELECT f.media_sha256 digest,COUNT(*) n FROM frames f JOIN frames g ON
                    g.frame_id=f.frame_id AND g.media_sha256=f.media_sha256
                WHERE f.release_id=? AND g.release_id=? AND f.hash_status='hashed'
                    AND g.hash_status='hashed' AND f.media_sha256 IS NOT NULL
                GROUP BY f.media_sha256
            ) SELECT
                COALESCE(SUM(b.n*c.n-COALESCE(s.n,0)),0) pair_count,
                COALESCE(SUM(CASE WHEN b.n*c.n>COALESCE(s.n,0) THEN 1 ELSE 0 END),0) hash_groups,
                COALESCE(SUM(c.n-CASE WHEN b.n=1 THEN COALESCE(s.n,0) ELSE 0 END),0) current_frames,
                COALESCE(SUM(b.n-CASE WHEN c.n=1 THEN COALESCE(s.n,0) ELSE 0 END),0) baseline_frames
            FROM b JOIN c ON c.digest=b.digest LEFT JOIN same_id s ON s.digest=b.digest""",
            (baseline_id, current_id, baseline_id, current_id),
        ).fetchone()
        return dict(row)

    @staticmethod
    def _candidate_examples(conn, baseline_id: str, current_id: str, preferred: list[str]):
        candidates = []
        eligibility = (
            "c.release_id=? AND c.hash_status='hashed' AND c.media_sha256 IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM frames b WHERE b.release_id=? AND b.hash_status='hashed' "
            "AND b.media_sha256=c.media_sha256 AND b.frame_id<>c.frame_id)"
        )
        if preferred:
            placeholders = ",".join("?" for _ in preferred)
            candidates.extend(
                conn.execute(
                    "SELECT c.frame_id,c.media_sha256 FROM frames c WHERE "
                    + eligibility
                    + f" AND c.frame_id IN ({placeholders}) ORDER BY c.frame_id LIMIT ?",
                    [current_id, baseline_id, *preferred, EXAMPLE_LIMIT],
                ).fetchall()
            )
        remaining = EXAMPLE_LIMIT - len(candidates)
        if remaining:
            skip = [row["frame_id"] for row in candidates]
            condition = ""
            if skip:
                condition = " AND c.frame_id NOT IN (" + ",".join("?" for _ in skip) + ")"
            candidates.extend(
                conn.execute(
                    "SELECT c.frame_id,c.media_sha256 FROM frames c WHERE "
                    + eligibility
                    + condition
                    + " ORDER BY c.frame_id LIMIT ?",
                    [current_id, baseline_id, *skip, remaining],
                ).fetchall()
            )
        # Round robin makes examples useful across several current IDs instead of
        # allowing one large duplicate family to occupy every displayed example.
        queues = []
        for current in candidates:
            matches = conn.execute(
                "SELECT frame_id FROM frames WHERE release_id=? AND hash_status='hashed' "
                "AND media_sha256=? AND frame_id<>? ORDER BY frame_id LIMIT ?",
                (
                    baseline_id,
                    current["media_sha256"],
                    current["frame_id"],
                    MATCHES_PER_CURRENT_EXAMPLE,
                ),
            ).fetchall()
            queues.append(
                deque(
                    (row["frame_id"], current["frame_id"], current["media_sha256"])
                    for row in matches
                )
            )
        examples = []
        while len(examples) < EXAMPLE_LIMIT and any(queues):
            for queue in queues:
                if queue and len(examples) < EXAMPLE_LIMIT:
                    examples.append(queue.popleft())
        return examples

    def compare(
        self,
        baseline_release_id: str,
        current_release_id: str,
        *,
        category: str = "all",
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        baseline_id = _release_argument(baseline_release_id)
        current_id = _release_argument(current_release_id)
        if baseline_id == current_id:
            raise ValueError("Choose two different releases.")
        if category not in ("all", *CATEGORIES):
            raise ValueError("Unknown metadata comparison category.")
        if type(page) is not int or not 1 <= page <= 1_000_000:
            raise ValueError("Page must be a positive integer.")
        if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"Page size must be between 1 and {MAX_PAGE_SIZE}.")
        baseline, current = self.store.get_release(baseline_id), self.store.get_release(current_id)
        if baseline is None or current is None:
            raise ReviewNotFoundError("Comparison release not found.")
        params = (baseline_id, current_id, baseline_id, current_id)
        summary = dict.fromkeys(CATEGORIES, 0)
        with self.store.connection() as conn:
            # Keep coverage, candidate counts and the sampled pairs on one SQLite
            # snapshot while checksum jobs can commit on other connections.
            conn.execute("BEGIN")
            for row in conn.execute(
                COMPARISON + "SELECT category,COUNT(*) n FROM compared GROUP BY category", params
            ):
                summary[row["category"]] = row["n"]
            summary["compared_frame_ids"] = sum(summary.values())
            condition, category_args = (
                ("", []) if category == "all" else ("WHERE category=?", [category])
            )
            rows = conn.execute(
                COMPARISON + f"SELECT frame_id,category FROM compared {condition} "
                "ORDER BY frame_id LIMIT ? OFFSET ?",
                [*params, *category_args, page_size, (page - 1) * page_size],
            ).fetchall()
            coverage = {
                "baseline": self.store._hash_counts(conn, baseline_id),
                "current": self.store._hash_counts(conn, current_id),
            }
            candidate_counts = self._candidate_counts(conn, baseline_id, current_id)
            preferred = [row["frame_id"] for row in rows if row["category"] != "removed"]
            pairs = (
                self._candidate_examples(conn, baseline_id, current_id, preferred)
                if candidate_counts["pair_count"]
                else []
            )

        cache: dict[tuple[str, str], dict | None] = {}

        def frame(release_id: str, frame_id: str):
            key = (release_id, frame_id)
            if key not in cache:
                cache[key] = self.store.get_frame(release_id, frame_id)
            return cache[key]

        items = []
        for row in rows:
            before = None if row["category"] == "added" else frame(baseline_id, row["frame_id"])
            after = None if row["category"] == "removed" else frame(current_id, row["frame_id"])
            old, new = (before or {}).get("raw_metadata", {}), (after or {}).get("raw_metadata", {})
            changes = [
                {"field": field, "before": old.get(field), "after": new.get(field)}
                for field in sorted(set(old) | set(new))
                if old.get(field) != new.get(field)
            ]
            items.append(
                {
                    "frame_id": row["frame_id"],
                    "category": row["category"],
                    "before": before,
                    "after": after,
                    "metadata_changes": changes,
                    "evidence_refs": {
                        "before": qualify_evidence_id(baseline_id, row["frame_id"])
                        if before
                        else None,
                        "after": qualify_evidence_id(current_id, row["frame_id"])
                        if after
                        else None,
                    },
                    "media_comparison": self._media_comparison(before, after),
                }
            )
        examples = []
        for before_id, after_id, recorded_digest in pairs:
            before, after = frame(baseline_id, before_id), frame(current_id, after_id)
            matched = (
                _hash_state(before) == _hash_state(after) == "recorded_hash_and_stat_match"
                and before["media_sha256"] == after["media_sha256"] == recorded_digest
            )
            examples.append(
                {
                    "baseline_evidence_id": qualify_evidence_id(baseline_id, before_id),
                    "current_evidence_id": qualify_evidence_id(current_id, after_id),
                    "baseline_frame_id": before_id,
                    "current_frame_id": after_id,
                    "recorded_sha256": recorded_digest,
                    "status": "recorded_hash_and_stat_match" if matched else "unresolved",
                    "baseline_hash_state": _hash_state(before),
                    "current_hash_state": _hash_state(after),
                    "note": "Recorded completed checksums match and current size/mtime matches their records; "
                    "no fresh byte hashing was performed."
                    if matched
                    else "A candidate's media is missing, stale, or no longer has a usable recorded hash.",
                }
            )
        total = summary["compared_frame_ids"] if category == "all" else summary[category]
        return {
            "baseline": self._descriptor(baseline),
            "current": self._descriptor(current),
            "summary": summary,
            "category": category,
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "correspondence_note": "Metadata correspondence uses exact frame IDs only. It does not "
            "establish matching media, events, or transferable review approval.",
            "hash_comparison": {
                "recorded_coverage": coverage,
                "recorded_candidate_pair_count_across_ids": candidate_counts["pair_count"],
                "recorded_candidate_hash_group_count": candidate_counts["hash_groups"],
                "recorded_candidate_frame_counts": {
                    "baseline": candidate_counts["baseline_frames"],
                    "current": candidate_counts["current_frames"],
                },
                "stat_checked_examples": examples,
                "example_limit": EXAMPLE_LIMIT,
                "stat_checked_pair_count": len(examples),
                "stat_matched_pair_count": sum(
                    item["status"] == "recorded_hash_and_stat_match" for item in examples
                ),
                "unresolved_example_pair_count": sum(
                    item["status"] == "unresolved" for item in examples
                ),
                "stat_checked_record_count": len(cache),
                "unchecked_record_count": max(
                    0, coverage["baseline"]["total"] + coverage["current"]["total"] - len(cache)
                ),
                "scope_note": "Recorded hash coverage and candidate matches come from one database snapshot. "
                "The comparison checks current file size and modification time for the selected records "
                "and a bounded set of candidate pairs. Unchecked records and absent matches leave reuse "
                "unresolved. Brief finalization verifies selected bytes.",
            },
        }
