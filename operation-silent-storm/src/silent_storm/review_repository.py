"""Review SQL operations bound to a caller-owned SQLite connection.

The service owns transaction boundaries, validation, IDs, timestamps, and review
decisions. These queries neither open connections nor commit, so a scenario edit,
its revision history, and its eligibility projection remain one atomic operation.
"""

from __future__ import annotations

import sqlite3

from .contracts import UNKNOWN_FILTER, ReviewNotFoundError
from .storage import canonical_json


class ReviewRepository:
    """Read and append specialist review records within the supplied transaction."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def scenario(self, scenario_id: str) -> dict:
        row = self.connection.execute(
            "SELECT * FROM review_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise ReviewNotFoundError("Scenario not found.")
        return {**dict(row), "is_baseline": bool(row["is_baseline"])}

    def require_release(self, release_id: str) -> None:
        if (
            self.connection.execute(
                "SELECT 1 FROM releases WHERE release_id=? AND status='complete'", (release_id,)
            ).fetchone()
            is None
        ):
            raise ReviewNotFoundError("Release not found.")

    def baseline_id(self, release_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT scenario_id FROM review_scenarios WHERE release_id=? AND is_baseline=1",
            (release_id,),
        ).fetchone()
        return row["scenario_id"] if row else None

    def insert_scenario(
        self,
        scenario_id: str,
        release_id: str,
        name: str,
        *,
        is_baseline: bool,
        version: int,
        created_at: str,
        reviewer: str,
        source_scenario_id: str | None = None,
        source_scenario_version: int | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO review_scenarios VALUES (?,?,?,?,?,?,?,?,?)",
            (
                scenario_id,
                release_id,
                name,
                int(is_baseline),
                version,
                created_at,
                reviewer,
                source_scenario_id,
                source_scenario_version,
            ),
        )

    def scenario_count(self, release_id: str) -> int:
        return self.connection.execute(
            "SELECT COUNT(*) FROM review_scenarios WHERE release_id=?", (release_id,)
        ).fetchone()[0]

    def scenarios(self, release_id: str, limit: int) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM review_scenarios WHERE release_id=? "
            "ORDER BY is_baseline DESC,created_at DESC,scenario_id LIMIT ?",
            (release_id, limit),
        ).fetchall()
        return [{**dict(row), "is_baseline": bool(row["is_baseline"])} for row in rows]

    def insert_note(
        self,
        review_id: str,
        scenario_id: str,
        release_id: str,
        *,
        created_at: str,
        origin_review_id: str | None = None,
        origin_revision: int | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO review_notes VALUES (?,?,?,?,?,?)",
            (review_id, scenario_id, release_id, created_at, origin_review_id, origin_revision),
        )

    def append_revision(
        self,
        review_id: str,
        revision: int,
        values: dict,
        evidence: list[tuple[str, str, str]],
        *,
        created_at: str,
        change_type: str,
        change_reason: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO review_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                review_id,
                revision,
                values["title"],
                values["observation"],
                values["interpretation"],
                values["uncertainty"],
                values["rationale"],
                values["reviewer"],
                values["status"],
                canonical_json([item[2] for item in evidence]),
                created_at,
                change_type,
                change_reason,
            ),
        )
        self.connection.executemany(
            "INSERT INTO review_evidence VALUES (?,?,?,?,?,?)",
            [
                (review_id, revision, release, frame, evidence_id, ordinal)
                for ordinal, (release, frame, evidence_id) in enumerate(evidence)
            ],
        )

    def frame_exists(self, release_id: str, frame_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM frames WHERE release_id=? AND frame_id=?", (release_id, frame_id)
            ).fetchone()
            is not None
        )

    def excluded_videos(self, scenario_id: str) -> set[str]:
        return {
            row[0]
            for row in self.connection.execute(
                "SELECT video_id FROM source_exclusions WHERE scenario_id=? AND excluded=1",
                (scenario_id,),
            )
        }

    def latest_revision(self, review_id: str) -> int:
        return self.connection.execute(
            "SELECT MAX(revision) FROM review_revisions WHERE review_id=?", (review_id,)
        ).fetchone()[0]

    def current_reviews(self, scenario_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM review_current WHERE scenario_id=? ORDER BY review_id", (scenario_id,)
        ).fetchall()

    def current_review(self, review_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM review_current WHERE review_id=?", (review_id,)
        ).fetchone()

    def current_revision(self, review_id: str, scenario_id: str) -> int:
        row = self.connection.execute(
            "SELECT revision FROM review_current WHERE review_id=? AND scenario_id=?",
            (review_id, scenario_id),
        ).fetchone()
        if row is None:
            raise ReviewNotFoundError("Review not found in the selected scenario.")
        return row["revision"]

    def advance_scenario(self, scenario_id: str, version: int | None = None) -> None:
        if version is None:
            self.connection.execute(
                "UPDATE review_scenarios SET version=version+1 WHERE scenario_id=?", (scenario_id,)
            )
        else:
            self.connection.execute(
                "UPDATE review_scenarios SET version=? WHERE scenario_id=?", (version, scenario_id)
            )

    def review_page(
        self,
        scenario_id: str,
        *,
        status: str | None,
        query: str,
        week: int | str | None,
        limit: int,
        offset: int,
    ) -> tuple[int, list[sqlite3.Row]]:
        clauses, values = ["c.scenario_id=?"], [scenario_id]
        if status is not None:
            clauses.append("c.status=?")
            values.append(status)
        if query.strip():
            clauses.append(
                "(instr(lower(c.title),lower(?))>0 OR instr(lower(c.observation),lower(?))>0)"
            )
            values.extend([query.strip()] * 2)
        if week is not None:
            condition = "f.week IS NULL" if week == UNKNOWN_FILTER else "f.week=?"
            exists = (
                "EXISTS (SELECT 1 FROM review_evidence e JOIN frames f ON "
                "f.release_id=e.release_id AND f.frame_id=e.frame_id WHERE "
                "e.review_id=c.review_id AND e.revision=c.revision AND " + condition + ")"
            )
            if week == UNKNOWN_FILTER:
                exists = (
                    "(" + exists + " OR NOT EXISTS (SELECT 1 FROM review_evidence empty "
                    "WHERE empty.review_id=c.review_id AND empty.revision=c.revision))"
                )
            clauses.append(exists)
            if week != UNKNOWN_FILTER:
                values.append(week)
        where = " AND ".join(clauses)
        total = self.connection.execute(
            f"SELECT COUNT(*) FROM review_current c WHERE {where}", values
        ).fetchone()[0]
        rows = self.connection.execute(
            f"SELECT c.* FROM review_current c WHERE {where} "
            "ORDER BY c.created_at DESC,c.review_id LIMIT ? OFFSET ?",
            [*values, limit, offset],
        ).fetchall()
        return total, rows

    def history_page(
        self, review_id: str, limit: int, offset: int
    ) -> tuple[int, list[sqlite3.Row]]:
        if (
            self.connection.execute(
                "SELECT 1 FROM review_notes WHERE review_id=?", (review_id,)
            ).fetchone()
            is None
        ):
            raise ReviewNotFoundError("Review not found.")
        total = self.connection.execute(
            "SELECT COUNT(*) FROM review_revisions WHERE review_id=?", (review_id,)
        ).fetchone()[0]
        rows = self.connection.execute(
            "SELECT n.scenario_id,n.release_id,n.origin_review_id,n.origin_revision,r.* "
            "FROM review_notes n JOIN review_revisions r ON r.review_id=n.review_id "
            "WHERE n.review_id=? ORDER BY r.revision DESC LIMIT ? OFFSET ?",
            (review_id, limit, offset),
        ).fetchall()
        return total, rows

    def require_video(self, release_id: str, video_id: str) -> None:
        if (
            not isinstance(video_id, str)
            or not video_id
            or self.connection.execute(
                "SELECT 1 FROM frames WHERE release_id=? AND video_id=? LIMIT 1",
                (release_id, video_id),
            ).fetchone()
            is None
        ):
            raise ReviewNotFoundError("Source video not found in this release.")

    def source_exclusions(self, scenario_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM source_exclusions WHERE scenario_id=?", (scenario_id,)
        ).fetchall()

    def is_excluded(self, scenario_id: str, video_id: str) -> bool:
        row = self.connection.execute(
            "SELECT excluded FROM source_exclusions WHERE scenario_id=? AND video_id=?",
            (scenario_id, video_id),
        ).fetchone()
        return bool(row[0]) if row else False

    def append_exclusion_event(
        self,
        scenario_id: str,
        release_id: str,
        video_id: str,
        *,
        excluded: bool,
        reason: str,
        reviewer: str,
        version: int,
        created_at: str,
        inherited: bool = False,
        origin_event_id: int | None = None,
    ) -> int:
        values = (
            scenario_id,
            release_id,
            video_id,
            int(excluded),
            reason,
            reviewer,
            version,
            created_at,
        )
        if inherited:
            sql = (
                "INSERT INTO source_exclusion_events "
                "(scenario_id,release_id,video_id,excluded,reason,reviewer,scenario_version,"
                "created_at,origin_event_id) VALUES (?,?,?,?,?,?,?,?,?)"
            )
            values += (origin_event_id,)
        else:
            sql = (
                "INSERT INTO source_exclusion_events "
                "(scenario_id,release_id,video_id,excluded,reason,reviewer,scenario_version,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)"
            )
        return self.connection.execute(sql, values).lastrowid

    def write_exclusion(
        self,
        scenario_id: str,
        release_id: str,
        video_id: str,
        *,
        excluded: bool,
        event_id: int,
        inherited: bool = False,
    ) -> None:
        sql = "INSERT INTO source_exclusions VALUES (?,?,?,?,?)"
        if not inherited:
            sql += (
                " ON CONFLICT(scenario_id,video_id) "
                "DO UPDATE SET excluded=excluded.excluded,event_id=excluded.event_id"
            )
        self.connection.execute(sql, (scenario_id, release_id, video_id, int(excluded), event_id))

    def dependencies(self, scenario_id: str, video_id: str) -> sqlite3.Cursor:
        return self.connection.execute(
            "SELECT DISTINCT c.* FROM review_current c JOIN review_evidence e ON "
            "e.review_id=c.review_id AND e.revision=c.revision JOIN frames f ON "
            "f.release_id=e.release_id AND f.frame_id=e.frame_id "
            "WHERE c.scenario_id=? AND f.video_id=? ORDER BY c.review_id",
            (scenario_id, video_id),
        )

    def dependency_summaries(
        self,
        scenario_id: str,
        video_id: str,
        limit: int,
    ) -> tuple[int, list[dict]]:
        joins = (
            "FROM review_current c JOIN review_evidence e ON "
            "e.review_id=c.review_id AND e.revision=c.revision JOIN frames f ON "
            "f.release_id=e.release_id AND f.frame_id=e.frame_id "
            "WHERE c.scenario_id=? AND f.video_id=?"
        )
        total = self.connection.execute(
            "SELECT COUNT(DISTINCT c.review_id) " + joins, (scenario_id, video_id)
        ).fetchone()[0]
        rows = self.connection.execute(
            "SELECT DISTINCT c.review_id,c.revision,c.title,c.status "
            + joins
            + " ORDER BY c.review_id LIMIT ?",
            (scenario_id, video_id, limit),
        ).fetchall()
        return total, [dict(row) for row in rows]

    def exclusion_history_page(
        self,
        scenario_id: str,
        limit: int,
        offset: int,
    ) -> tuple[int, list[dict]]:
        total = self.connection.execute(
            "SELECT COUNT(*) FROM source_exclusion_events WHERE scenario_id=?", (scenario_id,)
        ).fetchone()[0]
        rows = self.connection.execute(
            "SELECT * FROM source_exclusion_events WHERE scenario_id=? "
            "ORDER BY event_id DESC LIMIT ? OFFSET ?",
            (scenario_id, limit, offset),
        ).fetchall()
        return total, [{**dict(row), "excluded": bool(row["excluded"])} for row in rows]
