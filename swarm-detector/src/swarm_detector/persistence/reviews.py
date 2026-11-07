"""Append-only analyst decisions and revision history."""

from __future__ import annotations

from ..models import Review
from ..serialization import canonical_json, utc_now
from .database import Database


class ReviewRepository:
    def __init__(self, database: Database):
        self.database = database

    def save(self, review: Review, expected_revision: int | None = None) -> Review:
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValueError("Expected review revision must be a nonnegative integer.")
        review = review.model_copy(update={"updated_at": utc_now()})
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT COALESCE(MAX(revision),0) FROM reviews WHERE run_id=? AND case_id=?",
                (review.run_id, review.case_id),
            ).fetchone()[0]
            if expected_revision is not None and expected_revision != current:
                raise ValueError(
                    "This review changed since you opened it. Reload the saved review before saving; your draft has not been overwritten."
                )
            cursor = conn.execute(
                "INSERT INTO reviews(run_id,case_id,payload,created_at) VALUES (?,?,?,?)",
                (
                    review.run_id,
                    review.case_id,
                    canonical_json(review.to_record()),
                    review.updated_at,
                ),
            )
            return review.model_copy(update={"revision": cursor.lastrowid})

    def revision(self, run_id: str, case_id: str, revision: int) -> Review | None:
        """Zero pins an unreviewed case; positive values address immutable revisions."""
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("Review revision must be a nonnegative integer.")
        if revision == 0:
            return None
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT revision,payload FROM reviews WHERE run_id=? AND case_id=? AND revision=?",
                (run_id, case_id, revision),
            ).fetchone()
        if row is None:
            raise ValueError("The selected review revision does not belong to this case and run.")
        return Review.model_validate_json(row["payload"]).model_copy(
            update={"revision": row["revision"]}
        )

    def latest(self, run_id: str, case_id: str) -> Review | None:
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT revision,payload FROM reviews WHERE run_id=? AND case_id=? "
                "ORDER BY revision DESC LIMIT 1",
                (run_id, case_id),
            ).fetchone()
        if row is None:
            return None
        return Review.model_validate_json(row["payload"]).model_copy(
            update={"revision": row["revision"]}
        )

    def history(self, run_id: str, case_id: str) -> tuple[Review, ...]:
        with self.database.connection() as conn:
            rows = conn.execute(
                "SELECT revision,payload FROM reviews WHERE run_id=? AND case_id=? "
                "ORDER BY revision",
                (run_id, case_id),
            )
            return tuple(
                Review.model_validate_json(row["payload"]).model_copy(
                    update={"revision": row["revision"]}
                )
                for row in rows
            )
