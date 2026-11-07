"""Reversible scenario edits with optimistic concurrency and atomic audit events."""

from __future__ import annotations

import json
from uuid import uuid4

from ..models import ScenarioState
from .common import (
    ConnectionFactory,
    VersionConflictError,
    case_identity,
    json_text,
    load_scenario,
    required_text,
    utc_now,
)
from .history import append_event


class ScenarioRepository:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def create(
        self,
        case_id: str,
        base_run_id: str,
        reviewer: str = "Analyst",
        *,
        assessment_kind: str = "scenario",
    ) -> ScenarioState:
        reviewer = required_text(reviewer, "Reviewer")
        if assessment_kind not in {"baseline", "scenario"}:
            raise ValueError("Choose baseline or scenario assessment kind.")
        with self._connect(write=True) as connection:
            case_identity(connection, case_id)
            if (
                connection.execute(
                    "SELECT 1 FROM review_runs WHERE case_id = ? AND run_id = ?",
                    (case_id, base_run_id),
                ).fetchone()
                is None
            ):
                raise ValueError(f"Run {base_run_id} does not belong to case {case_id}.")
            if assessment_kind == "baseline":
                existing = connection.execute(
                    "SELECT scenario_id FROM review_events WHERE case_id = ? "
                    "AND event_type = 'scenario_created' "
                    "AND json_extract(payload_json, '$.assessment_kind') = 'baseline' "
                    "AND json_extract(payload_json, '$.base_run_id') = ? ORDER BY rowid LIMIT 1",
                    (case_id, base_run_id),
                ).fetchone()
                if existing is not None:
                    return load_scenario(connection, existing["scenario_id"])
            scenario_id = str(uuid4())
            now = utc_now()
            connection.execute(
                "INSERT INTO review_scenarios "
                "(id, case_id, base_run_id, version, excluded_record_ids_json, reviewer, "
                "created_at, updated_at) VALUES (?, ?, ?, 1, '[]', ?, ?, ?)",
                (scenario_id, case_id, base_run_id, reviewer, now, now),
            )
            append_event(
                connection,
                case_id=case_id,
                scenario_id=scenario_id,
                event_type="scenario_created",
                reviewer=reviewer,
                payload={
                    "base_run_id": base_run_id,
                    "scenario_version": 1,
                    "assessment_kind": assessment_kind,
                },
                created_at=now,
            )
            return load_scenario(connection, scenario_id)

    def get(self, scenario_id: str) -> ScenarioState:
        with self._connect() as connection:
            return load_scenario(connection, scenario_id)

    def list_states(self, case_id: str) -> list[tuple[ScenarioState, str]]:
        with self._connect() as connection:
            case_identity(connection, case_id)
            result = []
            for row in connection.execute(
                "SELECT s.*, COALESCE(json_extract(e.payload_json, '$.assessment_kind'), "
                "'scenario') AS assessment_kind FROM review_scenarios AS s "
                "LEFT JOIN review_events AS e ON e.scenario_id = s.id "
                "AND e.event_type = 'scenario_created' WHERE s.case_id = ? ORDER BY s.rowid",
                (case_id,),
            ):
                value = dict(row)
                kind = value.pop("assessment_kind")
                value["excluded_record_ids"] = json.loads(value.pop("excluded_record_ids_json"))
                result.append((ScenarioState.model_validate(value), kind))
            return result

    def get_kind(self, scenario_id: str) -> str:
        with self._connect() as connection:
            load_scenario(connection, scenario_id)
            row = connection.execute(
                "SELECT json_extract(payload_json, '$.assessment_kind') AS assessment_kind "
                "FROM review_events WHERE scenario_id = ? AND event_type = 'scenario_created' "
                "ORDER BY rowid LIMIT 1",
                (scenario_id,),
            ).fetchone()
            if row is None:
                return "scenario"
            return row["assessment_kind"] or "scenario"

    def set_exclusion(
        self,
        scenario_id: str,
        record_id: str,
        excluded: bool,
        reason: str,
        expected_version: int,
    ) -> ScenarioState:
        if not isinstance(excluded, bool):
            raise ValueError("excluded must be a boolean.")
        with self._connect(write=True) as connection:
            scenario = load_scenario(connection, scenario_id)
            if scenario.version != expected_version:
                raise VersionConflictError(
                    f"Scenario changed: expected version {expected_version}, "
                    f"found {scenario.version}. Reload the scenario and try again."
                )
            if (
                connection.execute(
                    "SELECT 1 FROM review_record_ids WHERE case_id = ? AND record_id = ?",
                    (scenario.case_id, record_id),
                ).fetchone()
                is None
            ):
                raise ValueError(f"Record {record_id} does not belong to this scenario's case.")
            exclusions = set(scenario.excluded_record_ids)
            if (record_id in exclusions) == excluded:
                return scenario
            if excluded:
                exclusions.add(record_id)
            else:
                exclusions.remove(record_id)
            version = scenario.version + 1
            now = utc_now()
            connection.execute(
                "UPDATE review_scenarios SET excluded_record_ids_json = ?, version = ?, "
                "updated_at = ? WHERE id = ?",
                (json_text(sorted(exclusions)), version, now, scenario_id),
            )
            append_event(
                connection,
                case_id=scenario.case_id,
                scenario_id=scenario_id,
                event_type="exclusion_changed",
                reviewer=scenario.reviewer,
                payload={
                    "record_id": record_id,
                    "excluded": excluded,
                    "reason": reason,
                    "scenario_version": version,
                },
                created_at=now,
            )
            return load_scenario(connection, scenario_id)
