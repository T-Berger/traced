import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from swarm_detector.detector import analyze
from swarm_detector.persistence import migrations
from swarm_detector.persistence.migrations import CURRENT_SCHEMA_VERSION, SchemaVersionError
from swarm_detector.sample_data import build_sample
from swarm_detector.serialization import canonical_json, fingerprint
from swarm_detector.service import Workbench
from swarm_detector.storage import Store


@pytest.fixture
def legacy_database(tmp_path):
    """Create a real original-schema file without invoking current Store code."""
    dataset = build_sample()
    run = analyze(dataset, {"randomizations": 2})
    case_id = run["groups"][0]["case_id"]
    review = {
        "run_id": run["run_id"],
        "case_id": case_id,
        "reviewer": "Original reviewer",
        "decision": "Insufficient evidence",
        "reason": "The source context needs inspection.",
        "claim": "",
        "endorsement": "unclear",
        "alignment": "unclear",
        "narrative_notes": "",
        "evidence_refs": [],
        "synthetic": True,
        "updated_at": "2026-10-14T10:00:00+00:00",
    }
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as conn:
        conn.executescript((Path(__file__).parent / "fixtures/unversioned.sql").read_text())
        conn.execute(
            "INSERT INTO datasets VALUES (?,?,?,?)",
            (dataset["dataset_id"], fingerprint(dataset), canonical_json(dataset), "original"),
        )
        conn.execute(
            "INSERT INTO runs VALUES (?,?,?,?)",
            (run["run_id"], dataset["dataset_id"], canonical_json(run), "original"),
        )
        conn.executemany(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
            (
                (
                    dataset["dataset_id"],
                    m["message_key"],
                    m["post_key"],
                    m.get("account_key"),
                    m["channel_key"],
                    m["timestamp_utc"],
                    m["text_original"],
                    canonical_json(m),
                )
                for m in dataset["messages"]
            ),
        )
        conn.executemany(
            "INSERT INTO messages_fts VALUES (?,?,?)",
            (
                (dataset["dataset_id"], m["message_key"], m["text_original"])
                for m in dataset["messages"]
            ),
        )
        conn.executemany(
            "INSERT INTO state VALUES (?,?)",
            (
                ("active_dataset", dataset["dataset_id"]),
                ("active_run", run["run_id"]),
            ),
        )
        conn.execute(
            "INSERT INTO reviews(revision,run_id,case_id,payload,created_at) VALUES (41,?,?,?,?)",
            (run["run_id"], case_id, canonical_json(review), review["updated_at"]),
        )
        conn.execute(
            "INSERT INTO reviews(revision,run_id,case_id,payload,created_at) VALUES (42,?,?,?,?)",
            (run["run_id"], case_id, canonical_json(review), review["updated_at"]),
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    return database, dataset, run, review


def test_unversioned_upgrade_preserves_source_runs_reviews_fts_and_active_state(legacy_database):
    path, dataset, run, review = legacy_database
    workbench = Workbench(path)

    assert workbench.snapshot() == {"dataset": dataset, "run": run}
    assert workbench.overview()["message_count"] == len(dataset["messages"])
    assert workbench.get_review(review["case_id"])["revision"] == 42
    assert [
        r["revision"] for r in workbench.store.review_history(run["run_id"], review["case_id"])
    ] == [41, 42]
    assert workbench.search("fictional")
    exported = json.loads(workbench.export_case(review["case_id"], "json"))
    assert exported["review"]["revision"] == 42
    assert exported["run_id"] == run["run_id"]
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == len(dataset["posts"])
        assert conn.execute("SELECT COUNT(*) FROM pair_events").fetchone()[0] == sum(
            len(p["events"]) for p in run["pairs"]
        )
        assert conn.execute("SELECT payload FROM datasets").fetchone()[0] == canonical_json(dataset)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert Store(path).get_review(run["run_id"], review["case_id"])["revision"] == 42


def test_failed_backfill_rolls_back_ddl_data_and_version(legacy_database, monkeypatch):
    path, dataset, run, _ = legacy_database
    original = migrations.project_run

    def fail_after_partial_projection(conn, record):
        original(conn, record)
        raise RuntimeError("Injected failure after pair and case projection")

    monkeypatch.setattr(migrations, "project_run", fail_after_partial_projection)
    with pytest.raises(RuntimeError, match="Injected failure"):
        Store(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='posts'").fetchone() is None
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchone()
            is None
        )
        assert conn.execute("SELECT payload FROM datasets").fetchone()[0] == canonical_json(dataset)
        assert conn.execute("SELECT payload FROM runs").fetchone()[0] == canonical_json(run)
        assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 2
    monkeypatch.setattr(migrations, "project_run", original)
    assert Store(path).active_run_id() == run["run_id"]


def test_future_schema_is_rejected_without_schema_or_data_changes(legacy_database):
    path, dataset, _, _ = legacy_database
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION + 1}")
        original_schema = conn.execute(
            "SELECT type,name,sql FROM sqlite_master ORDER BY name"
        ).fetchall()
    with pytest.raises(SchemaVersionError, match="newer than supported"):
        Store(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION + 1
        assert (
            conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall()
            == original_schema
        )
        assert conn.execute("SELECT payload FROM datasets").fetchone()[0] == canonical_json(dataset)


def test_concurrent_openers_apply_each_migration_once(legacy_database):
    path, _, run, _ = legacy_database
    with ThreadPoolExecutor(max_workers=2) as pool:
        identities = list(pool.map(lambda _: Store(path).active_run_id(), range(2)))
    assert identities == [run["run_id"], run["run_id"]]
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,)]


def test_known_version_one_upgrades_without_recreating_original_records(legacy_database):
    path, dataset, run, _ = legacy_database
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=1")
    store = Store(path)
    assert store.get_dataset() == dataset
    assert store.get_run() == run
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [(2,)]


def test_old_mismatched_active_run_is_never_presented_as_current(legacy_database):
    path, dataset, run, _ = legacy_database
    other = {**dataset, "dataset_id": "pending-source"}
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO datasets VALUES (?,?,?,?)",
            (other["dataset_id"], fingerprint(other), canonical_json(other), "original"),
        )
        conn.executemany(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
            (
                (
                    other["dataset_id"],
                    m["message_key"],
                    m["post_key"],
                    m.get("account_key"),
                    m["channel_key"],
                    m["timestamp_utc"],
                    m["text_original"],
                    canonical_json(m),
                )
                for m in other["messages"]
            ),
        )
        conn.execute("UPDATE state SET value=? WHERE key='active_dataset'", (other["dataset_id"],))
    workbench = Workbench(path)
    assert workbench.overview()["ready"] is False
    assert workbench.overview()["dataset_id"] == other["dataset_id"]
    assert workbench.overview(run_id=run["run_id"])["dataset_id"] == dataset["dataset_id"]
