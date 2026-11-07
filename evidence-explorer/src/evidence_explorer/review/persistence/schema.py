"""Review schema history, including safe adoption of pre-journal databases."""

import sqlite3

from ...persistence.migrations import Migration, execute_script

INITIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_cases (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    bundle_json TEXT NOT NULL,
    title TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    finding_count INTEGER NOT NULL,
    run_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_scenarios (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES review_cases(id),
    base_run_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    excluded_record_ids_json TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS review_scenarios_case_idx
    ON review_scenarios(case_id);
CREATE TABLE IF NOT EXISTS review_events (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES review_cases(id),
    scenario_id TEXT REFERENCES review_scenarios(id),
    event_type TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS review_events_case_idx
    ON review_events(case_id, created_at);
CREATE TABLE IF NOT EXISTS review_decisions (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES review_cases(id),
    finding_id TEXT NOT NULL,
    scenario_id TEXT NOT NULL REFERENCES review_scenarios(id),
    scenario_version INTEGER NOT NULL,
    reviewer TEXT NOT NULL,
    disposition TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_jobs (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES review_cases(id),
    finding_id TEXT NOT NULL,
    scenario_id TEXT NOT NULL REFERENCES review_scenarios(id),
    mode TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'completed', 'failed')
    ),
    payload_json TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS review_jobs_status_idx
    ON review_jobs(status, created_at);
"""


def _initial_schema(connection: sqlite3.Connection) -> None:
    execute_script(connection, INITIAL_SCHEMA)


def _pin_decision_fingerprints(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(review_decisions)")}
    if "case_fingerprint" not in columns:
        # Older decisions did not record this identity; keep their provenance unknown.
        connection.execute("ALTER TABLE review_decisions ADD COLUMN case_fingerprint TEXT")


def _index_bundle_objects(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(review_cases)")}
    if "case_json" not in columns:
        connection.execute("ALTER TABLE review_cases ADD COLUMN case_json TEXT")
    execute_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS review_record_ids (
            case_id TEXT NOT NULL REFERENCES review_cases(id) ON DELETE CASCADE,
            record_id TEXT NOT NULL,
            PRIMARY KEY(case_id, record_id)
        );
        CREATE TABLE IF NOT EXISTS review_runs (
            case_id TEXT NOT NULL REFERENCES review_cases(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            run_json TEXT NOT NULL,
            PRIMARY KEY(case_id, run_id)
        );
        CREATE TABLE IF NOT EXISTS review_fragment_records (
            case_id TEXT NOT NULL REFERENCES review_cases(id) ON DELETE CASCADE,
            fragment_id TEXT NOT NULL,
            record_id TEXT NOT NULL,
            PRIMARY KEY(case_id, fragment_id),
            FOREIGN KEY(case_id, record_id) REFERENCES review_record_ids(case_id, record_id)
        );
        CREATE TABLE IF NOT EXISTS review_findings (
            case_id TEXT NOT NULL REFERENCES review_cases(id) ON DELETE CASCADE,
            finding_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            finding_json TEXT NOT NULL,
            PRIMARY KEY(case_id, finding_id)
        );
        CREATE INDEX IF NOT EXISTS review_findings_run_idx
            ON review_findings(case_id, run_id);
        CREATE INDEX IF NOT EXISTS review_cases_created_idx
            ON review_cases(created_at, id);
        CREATE INDEX IF NOT EXISTS review_jobs_scenario_version_idx
            ON review_jobs(scenario_id, mode, json_extract(payload_json, '$.scenario_version'));
        UPDATE review_cases SET case_json = json_extract(bundle_json, '$.case')
            WHERE case_json IS NULL;
        INSERT OR IGNORE INTO review_record_ids(case_id, record_id)
            SELECT c.id, json_extract(item.value, '$.id')
            FROM review_cases AS c, json_each(c.bundle_json, '$.records') AS item;
        INSERT OR IGNORE INTO review_runs(case_id, run_id, ordinal, run_json)
            SELECT c.id, json_extract(item.value, '$.id'), CAST(item.key AS INTEGER), item.value
            FROM review_cases AS c, json_each(c.bundle_json, '$.runs') AS item;
        INSERT OR IGNORE INTO review_fragment_records(case_id, fragment_id, record_id)
            SELECT c.id, json_extract(item.value, '$.id'),
                   json_extract(item.value, '$.record_id')
            FROM review_cases AS c, json_each(c.bundle_json, '$.fragments') AS item;
        INSERT OR IGNORE INTO review_findings(case_id, finding_id, run_id, finding_json)
            SELECT c.id, json_extract(item.value, '$.snapshot_id'),
                   json_extract(item.value, '$.run_id'), item.value
            FROM review_cases AS c, json_each(c.bundle_json, '$.findings') AS item;
    """,
    )


MIGRATIONS = (
    Migration(1, "initial_review_tables", _initial_schema),
    Migration(2, "pin_decision_fingerprints", _pin_decision_fingerprints),
    Migration(3, "index_bundle_objects", _index_bundle_objects),
)
