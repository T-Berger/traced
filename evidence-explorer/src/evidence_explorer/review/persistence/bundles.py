"""Immutable bundle imports plus indexed, bounded reads of individual objects."""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import Any

from ..contracts import EvidenceBundle, Finding, Run
from ..models import CaseMetadata, CaseSummary
from .common import ConnectionFactory, case_identity, json_text, load_finding, utc_now

SUMMARY_COLUMNS = (
    "id, id AS case_id, title, fingerprint, record_count, finding_count, run_count, created_at"
)


def merge_bundle(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Append immutable objects, retaining objects absent from the new import."""
    collections = {
        "records": "id",
        "fragments": "id",
        "runs": "id",
        "findings": "snapshot_id",
        "links": "id",
        "reviews": "id",
        "scenarios": "id",
    }
    merged = dict(existing)
    for key, value in incoming.items():
        if key in collections:
            continue
        if key in existing and json_text(existing[key]) != json_text(value):
            raise ValueError(f"Immutable bundle metadata changed: {key}.")
        merged[key] = value
    for name, identity_key in collections.items():
        items = list(existing.get(name, []))
        by_id = {item[identity_key]: item for item in items}
        for item in incoming.get(name, []):
            object_id = item[identity_key]
            if object_id in by_id:
                if json_text(by_id[object_id]) != json_text(item):
                    raise ValueError(
                        f"Immutable {name} ID {object_id} already has different content."
                    )
            else:
                items.append(item)
                by_id[object_id] = item
        merged[name] = items
    return merged


class BundleRepository:
    def __init__(self, connect: ConnectionFactory) -> None:
        self._connect = connect

    def preview_import(self, bundle: EvidenceBundle) -> str:
        """Validate a prospective merge without changing saved evidence."""
        with self._connect() as connection:
            previous = connection.execute(
                "SELECT fingerprint, bundle_json FROM review_cases WHERE id = ?", (bundle.case.id,)
            ).fetchone()
            if previous is None:
                return "new"
            merged = merge_bundle(
                json.loads(previous["bundle_json"]), bundle.model_dump(mode="json")
            )
            fingerprint = EvidenceBundle.model_validate(merged).fingerprint()
            return "existing" if fingerprint == previous["fingerprint"] else "update"

    def import_bundle(self, bundle: EvidenceBundle) -> str:
        bundle = EvidenceBundle.model_validate(bundle)
        case_id = bundle.case.id
        fingerprint = bundle.fingerprint()
        with self._connect(write=True) as connection:
            previous = connection.execute(
                "SELECT fingerprint FROM review_cases WHERE id = ?", (case_id,)
            ).fetchone()
            if previous is not None and previous["fingerprint"] == fingerprint:
                return case_id
            payload = bundle.model_dump(mode="json")
            if previous is not None:
                stored = connection.execute(
                    "SELECT bundle_json FROM review_cases WHERE id = ?", (case_id,)
                ).fetchone()
                payload = merge_bundle(json.loads(stored["bundle_json"]), payload)
                bundle = EvidenceBundle.model_validate(payload)
                fingerprint = bundle.fingerprint()
                if previous["fingerprint"] == fingerprint:
                    return case_id
                connection.execute(
                    "UPDATE review_cases SET fingerprint = ?, bundle_json = ?, case_json = ?, "
                    "record_count = ?, finding_count = ?, run_count = ? WHERE id = ?",
                    (
                        fingerprint,
                        json_text(payload),
                        json_text(payload["case"]),
                        len(bundle.records),
                        len(bundle.findings),
                        len(bundle.runs),
                        case_id,
                    ),
                )
            else:
                connection.execute(
                    "INSERT INTO review_cases "
                    "(id, fingerprint, bundle_json, case_json, title, record_count, finding_count, "
                    "run_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        case_id,
                        fingerprint,
                        json_text(payload),
                        json_text(payload["case"]),
                        bundle.case.title,
                        len(bundle.records),
                        len(bundle.findings),
                        len(bundle.runs),
                        utc_now(),
                    ),
                )
            # These are lookup projections, not another copy of saved source text.
            connection.executemany(
                "INSERT OR IGNORE INTO review_record_ids(case_id, record_id) VALUES (?, ?)",
                ((case_id, record.id) for record in bundle.records),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO review_fragment_records(case_id, fragment_id, record_id) "
                "VALUES (?, ?, ?)",
                ((case_id, fragment.id, fragment.record_id) for fragment in bundle.fragments),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO review_runs(case_id, run_id, ordinal, run_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    (case_id, run.id, index, run.model_dump_json())
                    for index, run in enumerate(bundle.runs)
                ),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO review_findings(case_id, finding_id, run_id, finding_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    (case_id, finding.snapshot_id, finding.run_id, finding.model_dump_json())
                    for finding in bundle.findings
                ),
            )
        return case_id

    def list_cases(self, *, limit: int | None = None, offset: int = 0) -> list[CaseSummary]:
        if offset < 0 or (limit is not None and limit < 1):
            raise ValueError("Case pagination requires a positive limit and nonnegative offset.")
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {SUMMARY_COLUMNS} FROM review_cases ORDER BY created_at, id "
                "LIMIT ? OFFSET ?",
                (limit if limit is not None else -1, offset),
            )
            return [CaseSummary.model_validate(dict(row)) for row in rows]

    def get_metadata(self, case_id: str) -> CaseMetadata:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {SUMMARY_COLUMNS}, case_json FROM review_cases WHERE id = ?", (case_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown case: {case_id}")
            value = dict(row)
            value["case"] = json.loads(value.pop("case_json"))
            return CaseMetadata.model_validate(value)

    def get_fingerprint(self, case_id: str) -> str:
        with self._connect() as connection:
            return case_identity(connection, case_id).fingerprint

    def get_snapshot(self, case_id: str) -> tuple[EvidenceBundle, str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT bundle_json, fingerprint FROM review_cases WHERE id = ?", (case_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown case: {case_id}")
            return EvidenceBundle.model_validate_json(row["bundle_json"]), row["fingerprint"]

    def get_finding(self, case_id: str, finding_id: str) -> Finding:
        with self._connect() as connection:
            case_identity(connection, case_id)
            return load_finding(connection, case_id, finding_id)

    def list_runs(self, case_id: str) -> list[Run]:
        with self._connect() as connection:
            case_identity(connection, case_id)
            return [
                Run.model_validate_json(row["run_json"])
                for row in connection.execute(
                    "SELECT run_json FROM review_runs WHERE case_id = ? ORDER BY ordinal",
                    (case_id,),
                )
            ]

    def has_record(self, case_id: str, record_id: str) -> bool:
        with self._connect() as connection:
            case_identity(connection, case_id)
            return (
                connection.execute(
                    "SELECT 1 FROM review_record_ids WHERE case_id = ? AND record_id = ?",
                    (case_id, record_id),
                ).fetchone()
                is not None
            )

    def get_fragment_records(self, case_id: str, fragment_ids: Collection[str]) -> dict[str, str]:
        with self._connect() as connection:
            case_identity(connection, case_id)
            if not fragment_ids:
                return {}
            return {
                row["fragment_id"]: row["record_id"]
                for row in connection.execute(
                    "SELECT fragment_id, record_id FROM review_fragment_records WHERE case_id = ? "
                    "AND fragment_id IN (SELECT value FROM json_each(?))",
                    (case_id, json_text(sorted(set(fragment_ids)))),
                )
            }

    def get_swarm_baseline_run_id(self, case_id: str) -> str | None:
        """Read recomputation capability without decoding the saved dataset in Python."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT json_extract(bundle_json, '$.swarm_context.baseline_run_id') AS run_id "
                "FROM review_cases WHERE id = ?",
                (case_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown case: {case_id}")
            run_id = row["run_id"]
            if run_id is not None and not isinstance(run_id, str):
                raise ValueError("The saved Swarm baseline run must be an identifier.")
            return run_id
