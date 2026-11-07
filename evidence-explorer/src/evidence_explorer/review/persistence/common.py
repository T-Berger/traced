"""Connection contracts and small validated lookups shared by review repositories."""

from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from ..contracts import Finding
from ..models import ReviewJob, ScenarioState


class ConnectionFactory(Protocol):
    def __call__(self, *, write: bool = False) -> AbstractContextManager[sqlite3.Connection]: ...


class VersionConflictError(ValueError):
    """The analyst edited an older version of a review scenario or case."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def required_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty.")
    return value.strip()


@dataclass(frozen=True, slots=True)
class CaseIdentity:
    case_id: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ReviewContext:
    case: CaseIdentity
    scenario: ScenarioState
    finding: Finding


def case_identity(connection: sqlite3.Connection, case_id: str) -> CaseIdentity:
    row = connection.execute(
        "SELECT id, fingerprint FROM review_cases WHERE id = ?", (case_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Unknown case: {case_id}")
    return CaseIdentity(case_id=row["id"], fingerprint=row["fingerprint"])


def load_scenario(connection: sqlite3.Connection, scenario_id: str) -> ScenarioState:
    row = connection.execute(
        "SELECT * FROM review_scenarios WHERE id = ?", (scenario_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Unknown scenario: {scenario_id}")
    value = dict(row)
    value["excluded_record_ids"] = json.loads(value.pop("excluded_record_ids_json"))
    return ScenarioState.model_validate(value)


def load_finding(connection: sqlite3.Connection, case_id: str, finding_id: str) -> Finding:
    row = connection.execute(
        "SELECT finding_json FROM review_findings WHERE case_id = ? AND finding_id = ?",
        (case_id, finding_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"Finding {finding_id} does not belong to case {case_id}.")
    return Finding.model_validate_json(row["finding_json"])


def review_context(
    connection: sqlite3.Connection, case_id: str, finding_id: str, scenario_id: str
) -> ReviewContext:
    case = case_identity(connection, case_id)
    scenario = load_scenario(connection, scenario_id)
    if scenario.case_id != case_id:
        raise ValueError("Scenario does not belong to the specified case.")
    finding = load_finding(connection, case_id, finding_id)
    if finding.run_id != scenario.base_run_id:
        raise ValueError("Finding does not belong to the scenario's baseline run.")
    return ReviewContext(case=case, scenario=scenario, finding=finding)


def job_from_row(row: sqlite3.Row) -> ReviewJob:
    value = dict(row)
    value["payload"] = json.loads(value.pop("payload_json"))
    raw_result = value.pop("result_json")
    value["result"] = json.loads(raw_result) if raw_result is not None else None
    return ReviewJob.model_validate(value)
