"""Frozen-brief SQL and bounded provenance reads on a caller-owned connection.

The application service owns the write lock, revision guards, media checks, and
snapshot fingerprint. Repository methods neither commit nor re-open storage,
so history capture and insertion use the same transaction selected by the caller.
"""

from __future__ import annotations

import json
import sqlite3

from .brief_schema import initialize_brief_schema


class BriefRepository:
    """Persist snapshots and capture their bounded historical context."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def initialize(self) -> None:
        initialize_brief_schema(self.connection)

    def origin(self, note: dict):
        origin_id = note.get("origin_review_id")
        if not origin_id:
            return None
        row = self.connection.execute(
            "SELECT n.scenario_id,n.release_id,r.* FROM review_notes n JOIN review_revisions r "
            "ON r.review_id=n.review_id WHERE n.review_id=? AND r.revision=?",
            (origin_id, note["origin_revision"]),
        ).fetchone()
        if row is None:
            return {
                "review_id": origin_id,
                "revision": note["origin_revision"],
                "available": False,
                "reason": "Origin review revision unavailable.",
            }
        result = dict(row)
        result["evidence_ids"] = json.loads(result.pop("evidence_ids_json"))
        return {**result, "available": True}

    def lineage(self, scenario: dict, limit: int):
        items, visited, current = [], set(), scenario
        total, exact, unavailable = 0, True, False
        while current is not None and len(visited) < 128:
            identity = current["scenario_id"]
            if identity in visited:
                unavailable, exact = True, False
                break
            visited.add(identity)
            total += 1
            if len(items) < limit:
                items.append({**current, "available": True})
            parent = current.get("source_scenario_id")
            if not parent:
                current = None
                break
            row = self.connection.execute(
                "SELECT * FROM review_scenarios WHERE scenario_id=?", (parent,)
            ).fetchone()
            if row is None:
                total += 1
                unavailable = True
                if len(items) < limit:
                    items.append(
                        {
                            "scenario_id": parent,
                            "available": False,
                            "reason": "Origin scenario unavailable.",
                        }
                    )
                current = None
                break
            current = dict(row)
            current["is_baseline"] = bool(current["is_baseline"])
        if current is not None:
            exact = False
        return {
            "items": items,
            "total": total,
            "total_is_exact": exact,
            "truncated": total > len(items) or not exact,
            "origin_unavailable": unavailable,
        }

    def events(self, scenario_id: str, limit: int):
        total = self.connection.execute(
            "SELECT COUNT(*) FROM source_exclusion_events WHERE scenario_id=?",
            (scenario_id,),
        ).fetchone()[0]
        rows = self.connection.execute(
            "SELECT * FROM source_exclusion_events WHERE scenario_id=? ORDER BY event_id DESC LIMIT ?",
            (scenario_id, limit),
        ).fetchall()
        return {
            "items": [{**dict(row), "excluded": bool(row["excluded"])} for row in rows],
            "total": total,
            "truncated": total > len(rows),
        }

    def history(self, review_ids: list[str], limit: int):
        placeholders = ",".join("?" for _ in review_ids)
        total = self.connection.execute(
            f"SELECT COUNT(*) FROM review_revisions WHERE review_id IN ({placeholders})",
            review_ids,
        ).fetchone()[0]
        rows = self.connection.execute(
            "SELECT n.scenario_id,n.release_id,n.origin_review_id,n.origin_revision,r.* "
            "FROM review_notes n JOIN review_revisions r ON r.review_id=n.review_id "
            f"WHERE n.review_id IN ({placeholders}) "
            "ORDER BY r.created_at DESC,r.review_id,r.revision DESC LIMIT ?",
            [*review_ids, limit],
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["evidence_ids"] = json.loads(item.pop("evidence_ids_json"))
            items.append(item)
        return {
            "items": items,
            "total": total,
            "truncated": total > len(items),
            "limit": limit,
        }

    def insert_snapshot(
        self, payload: dict, snapshot_json: str, digest: str, *, scenario_id: str
    ) -> None:
        self.connection.execute(
            "INSERT INTO saved_briefs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                payload["brief_id"],
                scenario_id,
                payload["scenario"]["release_id"],
                payload["scenario"]["version"],
                payload["title"],
                payload["reviewer"],
                payload["created_at"],
                payload["source_origin"],
                snapshot_json,
                digest,
            ),
        )

    def snapshot(self, brief_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT snapshot_json,fingerprint FROM saved_briefs WHERE brief_id=?",
            (brief_id,),
        ).fetchone()

    def page(self, scenario_id: str, limit: int, offset: int):
        total = self.connection.execute(
            "SELECT COUNT(*) FROM saved_briefs WHERE scenario_id=?",
            (scenario_id,),
        ).fetchone()[0]
        rows = self.connection.execute(
            "SELECT brief_id,scenario_id,release_id,scenario_version,title,reviewer,created_at,"
            "source_origin,fingerprint FROM saved_briefs WHERE scenario_id=? "
            "ORDER BY created_at DESC,brief_id LIMIT ? OFFSET ?",
            (scenario_id, limit, offset),
        ).fetchall()
        return total, [dict(row) for row in rows]
