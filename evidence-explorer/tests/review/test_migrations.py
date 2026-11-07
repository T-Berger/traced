"""Real review-schema upgrades adopt legacy data and remain atomic and component-scoped."""

import json
import sqlite3

import pytest

from evidence_explorer.persistence.migrations import SchemaVersionError, execute_script
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.persistence import schema
from evidence_explorer.review.storage import ReviewRepository


def legacy_database(path, *, valid_bundle=True):
    bundle = load_demo_bundle()
    finding = bundle.findings[-1]
    now = "2026-11-06T18:00:00+00:00"
    with sqlite3.connect(path) as connection:
        execute_script(connection, schema.INITIAL_SCHEMA)
        connection.execute(
            "INSERT INTO review_cases VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bundle.case.id,
                bundle.fingerprint(),
                bundle.model_dump_json() if valid_bundle else "{",
                bundle.case.title,
                len(bundle.records),
                len(bundle.findings),
                len(bundle.runs),
                now,
            ),
        )
        connection.execute(
            "INSERT INTO review_scenarios VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy-scenario", bundle.case.id, finding.run_id, 1, "[]", "Mina", now, now),
        )
        payload = {
            "case_id": bundle.case.id,
            "case_fingerprint": bundle.fingerprint(),
            "finding_id": finding.snapshot_id,
            "finding_snapshot": finding.model_dump(mode="json"),
            "scenario_id": "legacy-scenario",
            "scenario_version": 1,
            "excluded_record_ids": [],
            "base_run_id": finding.run_id,
            "mode": "demo",
        }
        connection.execute(
            "INSERT INTO review_jobs(id, case_id, finding_id, scenario_id, mode, status, "
            "payload_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-job",
                bundle.case.id,
                finding.snapshot_id,
                "legacy-scenario",
                "demo",
                "queued",
                json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO review_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-decision",
                bundle.case.id,
                finding.snapshot_id,
                "legacy-scenario",
                1,
                "Mina",
                "qualify",
                "Original rationale remains",
                now,
            ),
        )
    return bundle, payload


def test_legacy_upgrade_preserves_jobs_decisions_and_builds_object_projections(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    bundle, payload = legacy_database(path)
    repository = ReviewRepository(path)
    repository.initialize()
    assert repository.get_job("legacy-job")["payload"] == payload
    assert repository.get_scenario("legacy-scenario")["version"] == 1
    assert repository.get_case_metadata(bundle.case.id).case == bundle.case
    assert (
        repository.get_finding(bundle.case.id, bundle.findings[-1].snapshot_id)
        == bundle.findings[-1]
    )
    assert repository.get_fragment_records(bundle.case.id, [bundle.fragments[0].id]) == {
        bundle.fragments[0].id: bundle.fragments[0].record_id
    }
    assert repository.list_runs(bundle.case.id) == bundle.runs
    assert (
        repository.enqueue_job(
            bundle.case.id, bundle.findings[-1].snapshot_id, "legacy-scenario", "demo"
        )["id"]
        == "legacy-job"
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT reason, case_fingerprint FROM review_decisions WHERE id = 'legacy-decision'"
        ).fetchone() == ("Original rationale remains", None)


def test_replaying_review_migrations_does_not_rewrite_history_or_data(tmp_path):
    repository = ReviewRepository(tmp_path / "review.sqlite3")
    repository.initialize()
    repository.import_bundle(load_demo_bundle())
    with sqlite3.connect(repository.db_path) as connection:
        history = connection.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
        original_bundle = connection.execute("SELECT bundle_json FROM review_cases").fetchone()[0]
    repository.initialize()
    with sqlite3.connect(repository.db_path) as connection:
        assert (
            connection.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
            == history
        )
        assert (
            connection.execute("SELECT bundle_json FROM review_cases").fetchone()[0]
            == original_bundle
        )
        assert [(row[0], row[1]) for row in history] == [
            ("review", migration.version) for migration in schema.MIGRATIONS
        ]


def test_failed_backfill_rolls_back_schema_columns_indexes_and_history(tmp_path):
    path = tmp_path / "invalid-legacy.sqlite3"
    legacy_database(path, valid_bundle=False)
    repository = ReviewRepository(path)
    with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
        repository.initialize()
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert "schema_migrations" not in tables
        assert "review_record_ids" not in tables
        columns = {row[1] for row in connection.execute("PRAGMA table_info(review_decisions)")}
        assert "case_fingerprint" not in columns
        assert connection.execute("SELECT bundle_json FROM review_cases").fetchone()[0] == "{"


def test_review_rejects_future_versions_without_changing_the_database(tmp_path):
    repository = ReviewRepository(tmp_path / "review.sqlite3")
    repository.initialize()
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
            ("review", len(schema.MIGRATIONS) + 1, "future-change", "2027-01-01"),
        )
        before = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
    with pytest.raises(SchemaVersionError, match="review schema"):
        repository.initialize()
    with sqlite3.connect(repository.db_path) as connection:
        assert (
            connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
            == before
        )


def test_review_history_is_independent_of_the_explorer_component(tmp_path):
    repository = ReviewRepository(tmp_path / "shared.sqlite3")
    repository.initialize()
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
            ("explorer", 1000, "unrelated-component-history", "2027-01-01"),
        )
    repository.initialize()
    with sqlite3.connect(repository.db_path) as connection:
        assert (
            connection.execute(
                "SELECT version FROM schema_migrations WHERE component = 'explorer'"
            ).fetchone()[0]
            == 1000
        )
