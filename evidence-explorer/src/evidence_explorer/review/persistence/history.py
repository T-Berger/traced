"""Reviewer decisions and their append-only audit events."""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from uuid import uuid4

from ..models import AuditEvent, ReviewDecision
from .common import (
    ConnectionFactory,
    VersionConflictError,
    case_identity,
    json_text,
    required_text,
    review_context,
    utc_now,
)


def append_event(
    connection: sqlite3.Connection,
    *,
    case_id: str,
    scenario_id: str,
    event_type: str,
    reviewer: str,
    payload: dict[str, Any],
    created_at: str,
) -> None:
    connection.execute(
        "INSERT INTO review_events "
        "(id, case_id, scenario_id, event_type, reviewer, payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(uuid4()), case_id, scenario_id, event_type, reviewer, json_text(payload), created_at),
    )


class ReviewHistoryRepository:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def list_events(
        self, case_id: str, *, limit: int | None = None, offset: int = 0
    ) -> list[AuditEvent]:
        if offset < 0 or (limit is not None and limit < 1):
            raise ValueError("Audit pagination requires a positive limit and nonnegative offset.")
        with self._connect() as connection:
            case_identity(connection, case_id)
            result = []
            for row in connection.execute(
                "SELECT * FROM review_events WHERE case_id = ? ORDER BY rowid LIMIT ? OFFSET ?",
                (case_id, limit if limit is not None else -1, offset),
            ):
                event = dict(row)
                event["payload"] = json.loads(event.pop("payload_json"))
                result.append(AuditEvent.model_validate(event))
            return result

    def save_review(
        self,
        case_id: str,
        finding_id: str,
        scenario_id: str,
        reviewer: str,
        disposition: str,
        reason: str,
        *,
        expected_version: int | None = None,
        expected_fingerprint: str | None = None,
    ) -> ReviewDecision:
        reviewer = required_text(reviewer, "Reviewer")
        disposition = required_text(disposition, "Disposition")
        reason = required_text(reason, "Reason")
        with self._connect(write=True) as connection:
            context = review_context(connection, case_id, finding_id, scenario_id)
            if expected_version is not None and context.scenario.version != expected_version:
                raise VersionConflictError(
                    f"Scenario changed: expected version {expected_version}, "
                    f"found {context.scenario.version}. Reload before saving the review."
                )
            if (
                expected_fingerprint is not None
                and context.case.fingerprint != expected_fingerprint
            ):
                raise VersionConflictError(
                    "Investigation changed since this view was opened. "
                    "Reload before saving the review."
                )
            review = ReviewDecision.model_validate(
                {
                    "id": str(uuid4()),
                    "case_id": case_id,
                    "finding_id": finding_id,
                    "scenario_id": scenario_id,
                    "scenario_version": context.scenario.version,
                    "case_fingerprint": context.case.fingerprint,
                    "reviewer": reviewer,
                    "disposition": disposition,
                    "reason": reason,
                    "created_at": utc_now(),
                }
            )
            values = review.model_dump(mode="json")
            connection.execute(
                "INSERT INTO review_decisions "
                "(id, case_id, finding_id, scenario_id, scenario_version, case_fingerprint, "
                "reviewer, disposition, reason, created_at) VALUES "
                "(:id, :case_id, :finding_id, :scenario_id, :scenario_version, :case_fingerprint, "
                ":reviewer, :disposition, :reason, :created_at)",
                values,
            )
            append_event(
                connection,
                case_id=case_id,
                scenario_id=scenario_id,
                event_type="finding_reviewed",
                reviewer=reviewer,
                payload=values,
                created_at=review.created_at,
            )
            return review
