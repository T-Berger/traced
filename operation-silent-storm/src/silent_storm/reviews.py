"""Human-authored revisions and isolated source-video exclusion scenarios.

The append-only revision and operation-local transaction patterns follow the
workspace's Swarm storage implementation, without importing its application.
The service owns validation, transaction boundaries, and review decisions. SQL
operations use a connection-bound repository; pure projections consume fetched
evidence. Local review states remain distinct from shared Use/Qualify/Withhold
decisions.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from uuid import uuid4

from .contracts import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    REVIEW_STATUSES,
    UNKNOWN_FILTER,
    ReviewConflictError,
    ReviewNotFoundError,
    normalize_filter,
)
from .ingest import parse_week
from .review_projection import eligibility, review_view
from .review_repository import ReviewRepository
from .storage import Store, fingerprint, utc_now

MAX_SCENARIOS = 48
TEXT_LIMITS = {
    "title": 300,
    "observation": 12000,
    "interpretation": 12000,
    "uncertainty": 6000,
    "rationale": 12000,
    "reviewer": 150,
    "reason": 12000,
}


def qualify_evidence_id(release_id: str, frame_id: str) -> str:
    return f"{release_id}:{frame_id}"


def _text(value: str, field: str, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text.")
    result = value.strip()
    if required and not result:
        raise ValueError(f"Add a nonempty {field}.")
    if len(result) > TEXT_LIMITS.get(field, 300):
        raise ValueError(f"{field} exceeds the supported length.")
    return result


def _page(page: int, page_size: int) -> tuple[int, int]:
    if type(page) is not int or not 1 <= page <= 1_000_000:
        raise ValueError("Page must be a positive integer.")
    if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"Page size must be between 1 and {MAX_PAGE_SIZE}.")
    return page_size, (page - 1) * page_size


def _status(value: str | None) -> str | None:
    value = normalize_filter(value)
    if value is not None and value not in REVIEW_STATUSES:
        raise ValueError("Choose Draft, Reviewed, Rejected, or Needs review.")
    return value


class ReviewService:
    """Manage local review revisions and scenario exclusions through ``Store``.

    Mutations use explicit transactions and version guards. Read methods project
    saved revisions with current evidence eligibility; ``ensure_baseline`` is a
    write-capable operation. Exporters preserve this history as provenance and
    do not synchronize shared-workspace approvals back into these tables.
    """

    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _scenario(conn: sqlite3.Connection, scenario_id: str) -> dict[str, Any]:
        return ReviewRepository(conn).scenario(scenario_id)

    @staticmethod
    def _require_release(conn: sqlite3.Connection, release_id: str) -> None:
        ReviewRepository(conn).require_release(release_id)

    def ensure_baseline(self, release_id: str) -> dict[str, Any]:
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_release(conn, release_id)
            existing = ReviewRepository(conn).baseline_id(release_id)
            if existing is not None:
                return self._scenario(conn, existing)
            scenario_id = "baseline-" + fingerprint({"release_id": release_id})
            ReviewRepository(conn).insert_scenario(
                scenario_id,
                release_id,
                "Baseline",
                is_baseline=True,
                version=0,
                created_at=utc_now(),
                reviewer="system",
            )
            return self._scenario(conn, scenario_id)

    def list_scenarios(self, release_id: str) -> list[dict[str, Any]]:
        with self.store.connection() as conn:
            self._require_release(conn, release_id)
            return ReviewRepository(conn).scenarios(release_id, MAX_SCENARIOS)

    def get_scenario(self, scenario_id: str) -> dict[str, Any]:
        with self.store.connection() as conn:
            return self._scenario(conn, scenario_id)

    @staticmethod
    def _append(
        conn: sqlite3.Connection,
        review_id: str,
        revision: int,
        values: dict,
        evidence: list[tuple[str, str, str]],
        *,
        change_type: str,
        change_reason: str,
    ) -> None:
        """Append through the caller's connection; retain the failure-injection seam."""
        ReviewRepository(conn).append_revision(
            review_id,
            revision,
            values,
            evidence,
            created_at=utc_now(),
            change_type=change_type,
            change_reason=change_reason,
        )

    def _evidence(self, conn: sqlite3.Connection, release_id: str, evidence_ids: list[str]):
        if not isinstance(evidence_ids, list) or len(evidence_ids) > MAX_PAGE_SIZE:
            raise ValueError(f"Evidence must be a list of at most {MAX_PAGE_SIZE} references.")
        prefix, evidence, seen = f"{release_id}:", [], set()
        for evidence_id in evidence_ids:
            if not isinstance(evidence_id, str) or not evidence_id.startswith(prefix):
                raise ReviewNotFoundError("Evidence must be qualified by this scenario's release.")
            frame_id = evidence_id[len(prefix) :]
            if not frame_id or not ReviewRepository(conn).frame_exists(release_id, frame_id):
                raise ReviewNotFoundError(f"Evidence not found: {evidence_id}")
            if evidence_id not in seen:
                evidence.append((release_id, frame_id, evidence_id))
                seen.add(evidence_id)
        return evidence

    def _eligibility(self, conn: sqlite3.Connection, values: dict, scenario_id: str, evidence):
        excluded = ReviewRepository(conn).excluded_videos(scenario_id)
        resolved = [
            (evidence_id, self.store.get_frame(release_id, frame_id))
            for release_id, frame_id, evidence_id in evidence
        ]
        return eligibility(values, resolved, excluded)

    def _render(self, conn: sqlite3.Connection, row: sqlite3.Row | dict) -> dict[str, Any]:
        result = dict(row)
        result["evidence_ids"] = json.loads(result.pop("evidence_ids_json"))
        evidence = self._evidence(conn, result["release_id"], result["evidence_ids"])
        assessment, frames = self._eligibility(conn, result, result["scenario_id"], evidence)
        latest = ReviewRepository(conn).latest_revision(result["review_id"])
        return review_view(result, assessment, frames, latest)

    def create_scenario(
        self,
        release_id: str,
        name: str,
        reviewer: str,
        source_scenario_id: str | None = None,
    ) -> dict[str, Any]:
        name, reviewer = _text(name, "name", True), _text(reviewer, "reviewer", True)
        source_id = source_scenario_id or self.ensure_baseline(release_id)["scenario_id"]
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = self._scenario(conn, source_id)
            if source["release_id"] != release_id:
                raise ValueError("A scenario can only fork another scenario in the same release.")
            count = ReviewRepository(conn).scenario_count(release_id)
            if count >= MAX_SCENARIOS:
                raise ValueError(f"This release supports at most {MAX_SCENARIOS} scenarios.")
            scenario_id = "scenario-" + uuid4().hex
            ReviewRepository(conn).insert_scenario(
                scenario_id,
                release_id,
                name,
                is_baseline=False,
                version=1,
                created_at=utc_now(),
                reviewer=reviewer,
                source_scenario_id=source_id,
                source_scenario_version=source["version"],
            )
            # Copies are new revision-1 notes; immutable origin references preserve lineage.
            for original in ReviewRepository(conn).current_reviews(source_id):
                review_id = "review-" + uuid4().hex
                ReviewRepository(conn).insert_note(
                    review_id,
                    scenario_id,
                    release_id,
                    created_at=utc_now(),
                    origin_review_id=original["review_id"],
                    origin_revision=original["revision"],
                )
                evidence = self._evidence(
                    conn, release_id, json.loads(original["evidence_ids_json"])
                )
                values = dict(original)
                values["reviewer"] = reviewer
                self._append(
                    conn,
                    review_id,
                    1,
                    values,
                    evidence,
                    change_type="fork",
                    change_reason=f"Forked from {source_id} at version {source['version']}.",
                )
            for state in ReviewRepository(conn).source_exclusions(source_id):
                event = ReviewRepository(conn).append_exclusion_event(
                    scenario_id,
                    release_id,
                    state["video_id"],
                    excluded=state["excluded"],
                    reason=f"Inherited source state from {source_id}.",
                    reviewer=reviewer,
                    version=1,
                    created_at=utc_now(),
                    origin_event_id=state["event_id"],
                    inherited=True,
                )
                ReviewRepository(conn).write_exclusion(
                    scenario_id,
                    release_id,
                    state["video_id"],
                    excluded=state["excluded"],
                    event_id=event,
                    inherited=True,
                )
            return self._scenario(conn, scenario_id)

    def save_review(
        self,
        scenario_id: str,
        *,
        title: str,
        observation: str,
        reviewer: str,
        evidence_ids: list[str],
        interpretation: str = "",
        uncertainty: str = "",
        rationale: str = "",
        status: str = "draft",
        review_id: str | None = None,
        expected_revision: int = 0,
    ) -> dict[str, Any]:
        if status not in REVIEW_STATUSES:
            raise ValueError("Choose Draft, Reviewed, Rejected, or Needs review.")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Expected revision must be a nonnegative integer.")
        values = {
            "title": _text(title, "title", True),
            "observation": _text(observation, "observation"),
            "interpretation": _text(interpretation, "interpretation"),
            "uncertainty": _text(uncertainty, "uncertainty"),
            "rationale": _text(rationale, "rationale"),
            "reviewer": _text(reviewer, "reviewer", True),
            "status": status,
        }
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            scenario = self._scenario(conn, scenario_id)
            evidence = self._evidence(conn, scenario["release_id"], evidence_ids)
            if review_id is None:
                if expected_revision != 0:
                    raise ReviewConflictError("A new statement requires expected revision 0.")
                review_id, revision = "review-" + uuid4().hex, 1
            else:
                current = ReviewRepository(conn).current_revision(review_id, scenario_id)
                if current != expected_revision:
                    raise ReviewConflictError(
                        "This statement has a newer saved revision. Reload it."
                    )
                revision = current + 1
            if status == "reviewed":
                eligibility, _ = self._eligibility(conn, values, scenario_id, evidence)
                if not eligibility["eligible"]:
                    raise ValueError("Cannot mark Reviewed: " + " ".join(eligibility["reasons"]))
            if revision == 1:
                ReviewRepository(conn).insert_note(
                    review_id,
                    scenario_id,
                    scenario["release_id"],
                    created_at=utc_now(),
                )
            self._append(
                conn,
                review_id,
                revision,
                values,
                evidence,
                change_type="created" if revision == 1 else "edited",
                change_reason=values["rationale"],
            )
            ReviewRepository(conn).advance_scenario(scenario_id)
            row = ReviewRepository(conn).current_review(review_id)
            return self._render(conn, row)

    def list_reviews(
        self,
        scenario_id: str,
        *,
        status: str | None = None,
        query: str = "",
        week: str | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        limit, offset = _page(page, page_size)
        status = _status(status)
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("Review search is limited to 200 characters.")
        week = normalize_filter(week)
        if week is not None and week != UNKNOWN_FILTER:
            week = parse_week(week)
        with self.store.connection() as conn:
            self._scenario(conn, scenario_id)
            total, rows = ReviewRepository(conn).review_page(
                scenario_id,
                status=status,
                query=query,
                week=week,
                limit=limit,
                offset=offset,
            )
            items = [self._render(conn, row) for row in rows]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def get_review(self, review_id: str) -> dict[str, Any]:
        with self.store.connection() as conn:
            row = ReviewRepository(conn).current_review(review_id)
            if row is None:
                raise ReviewNotFoundError("Review not found.")
            return self._render(conn, row)

    def review_history(
        self,
        review_id: str,
        *,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        limit, offset = _page(page, page_size)
        with self.store.connection() as conn:
            total, rows = ReviewRepository(conn).history_page(review_id, limit, offset)
            items = [self._render(conn, row) for row in rows]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    @staticmethod
    def _require_video(conn: sqlite3.Connection, release_id: str, video_id: str) -> None:
        ReviewRepository(conn).require_video(release_id, video_id)

    @staticmethod
    def _dependencies(conn: sqlite3.Connection, scenario_id: str, video_id: str):
        return ReviewRepository(conn).dependencies(scenario_id, video_id)

    def exclusion_preview(self, scenario_id: str, video_id: str, *, excluded: bool = True):
        if type(excluded) is not bool:
            raise ValueError("Excluded must be true or false.")
        with self.store.connection() as conn:
            scenario = self._scenario(conn, scenario_id)
            self._require_video(conn, scenario["release_id"], video_id)
            repository = ReviewRepository(conn)
            current = repository.is_excluded(scenario_id, video_id)
            total, summaries = repository.dependency_summaries(scenario_id, video_id, MAX_PAGE_SIZE)
        return {
            "scenario_version": scenario["version"],
            "video_id": video_id,
            "excluded": excluded,
            "currently_excluded": current,
            "affected_review_count": total,
            "affected_reviews": summaries,
            "can_apply": not scenario["is_baseline"],
        }

    def set_source_exclusion(
        self,
        scenario_id: str,
        video_id: str,
        *,
        excluded: bool,
        reason: str,
        reviewer: str,
        expected_version: int,
    ) -> dict[str, Any]:
        if type(excluded) is not bool:
            raise ValueError("Excluded must be true or false.")
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("Expected scenario version must be a nonnegative integer.")
        reviewer, reason = _text(reviewer, "reviewer", True), _text(reason, "reason", True)
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            scenario = self._scenario(conn, scenario_id)
            if scenario["is_baseline"]:
                raise ValueError(
                    "Fork the baseline into a separate scenario before excluding a source."
                )
            if scenario["version"] != expected_version:
                raise ReviewConflictError(
                    "The scenario changed after preview. Refresh the preview."
                )
            self._require_video(conn, scenario["release_id"], video_id)
            current = ReviewRepository(conn).is_excluded(scenario_id, video_id)
            if current == excluded:
                raise ValueError(
                    "The source already has this exclusion state. Refresh the preview."
                )
            version = scenario["version"] + 1
            event_id = ReviewRepository(conn).append_exclusion_event(
                scenario_id,
                scenario["release_id"],
                video_id,
                excluded=excluded,
                reason=reason,
                reviewer=reviewer,
                version=version,
                created_at=utc_now(),
            )
            ReviewRepository(conn).write_exclusion(
                scenario_id,
                scenario["release_id"],
                video_id,
                excluded=excluded,
                event_id=event_id,
            )
            affected = 0
            if excluded:
                for original in self._dependencies(conn, scenario_id, video_id).fetchall():
                    values = dict(original)
                    values.update(status="needs_review", reviewer=reviewer)
                    evidence = self._evidence(
                        conn,
                        scenario["release_id"],
                        json.loads(original["evidence_ids_json"]),
                    )
                    self._append(
                        conn,
                        original["review_id"],
                        original["revision"] + 1,
                        values,
                        evidence,
                        change_type="source_excluded",
                        change_reason=reason,
                    )
                    affected += 1
            ReviewRepository(conn).advance_scenario(scenario_id, version)
        return {
            "scenario_version": version,
            "video_id": video_id,
            "excluded": excluded,
            "affected_review_count": affected,
            "event_id": event_id,
        }

    def exclusion_history(
        self,
        scenario_id: str,
        *,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        limit, offset = _page(page, page_size)
        with self.store.connection() as conn:
            self._scenario(conn, scenario_id)
            total, items = ReviewRepository(conn).exclusion_history_page(scenario_id, limit, offset)
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def chronology(self, scenario_id: str, *, unit: str = "videos", status: str | None = None):
        from .chronology import chronology

        return chronology(self.store, scenario_id, unit=unit, status=status)
