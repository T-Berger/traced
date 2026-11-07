"""Migration history, atomic upgrades, and old snapshots are part of the storage API."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from evidence_explorer.db import Repository
from evidence_explorer.persistence.connection import enable_wal
from evidence_explorer.persistence.migrations import (
    Migration,
    SchemaVersionError,
    check_schema,
    execute_script,
    migrate,
)
from evidence_explorer.persistence.schema import MIGRATIONS, SCHEMA_SQL


def legacy_article(path, article):
    """Create the original schema without using future repository write behavior."""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(SCHEMA_SQL)
        connection.execute(
            "INSERT INTO articles(id,title,publisher,url,published_at,text,language,topic,"
            "is_sample,content_hash,created_at,updated_at) VALUES (1,?,?,?,?,?,?,?,?,?,?,?)",
            (
                article.title,
                article.publisher,
                article.url,
                article.published_at,
                article.text,
                article.language,
                article.topic,
                int(article.is_sample),
                hashlib.sha256(article.text.encode()).hexdigest(),
                "now",
                "now",
            ),
        )
    return 1


def test_unversioned_explorer_upgrade_preserves_source_fts_jobs_and_metadata(settings, article):
    article_id = legacy_article(settings.db_path, article)
    legacy = Repository(settings.db_path)
    from evidence_explorer.chunking import chunk_text

    legacy.replace_chunks(article_id, chunk_text(article.text))
    job_id = legacy.enqueue_job("analyze", {"mode": "demo"})
    legacy.set_meta("preserved", {"reason": "existing investigation"})
    upgraded = Repository(settings.db_path)
    upgraded.initialize()
    upgraded.initialize()
    assert upgraded.get_article(article_id)["text"] == article.text
    assert upgraded.keyword_search("bridge")[0]["article_id"] == article_id
    assert upgraded.get_job(job_id)["status"] == "queued"
    assert upgraded.get_meta("preserved") == {"reason": "existing investigation"}
    with closing(sqlite3.connect(settings.db_path)) as connection:
        assert check_schema(connection, "explorer", MIGRATIONS) == len(MIGRATIONS)
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE component='explorer'"
        ).fetchone()[0] == len(MIGRATIONS)


def test_failure_rolls_back_ddl_rows_and_journal(tmp_path):
    with closing(sqlite3.connect(tmp_path / "atomic.db")) as connection:

        def first(database):
            execute_script(
                database, "CREATE TABLE proof(value TEXT); INSERT INTO proof VALUES ('x');"
            )

        def broken(database):
            database.execute("ALTER TABLE proof ADD COLUMN uncommitted TEXT")
            raise RuntimeError("Simulated interrupted upgrade")

        with pytest.raises(RuntimeError, match="interrupted"):
            migrate(
                connection, "example", (Migration(1, "first", first), Migration(2, "bad", broken))
            )
        assert not connection.in_transaction
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
        )
        migrate(connection, "example", (Migration(1, "first", first),))
        assert connection.execute("SELECT value FROM proof").fetchall() == [("x",)]


def test_future_schema_is_rejected_without_touching_existing_records(repo, settings):
    future = len(MIGRATIONS) + 1
    with closing(sqlite3.connect(settings.db_path)) as connection, connection:
        connection.execute(
            "INSERT INTO schema_migrations VALUES ('explorer', ?, 'future', 'unknown')", (future,)
        )
    with pytest.raises(SchemaVersionError, match="Unsupported"):
        Repository(settings.db_path).initialize()
    with closing(sqlite3.connect(settings.db_path)) as connection:
        assert connection.execute(
            "SELECT name FROM schema_migrations WHERE component='explorer' AND version=?", (future,)
        ).fetchone() == ("future",)


def test_read_only_legacy_snapshot_stays_unversioned(settings, article):
    legacy_article(settings.db_path, article)
    before = settings.db_path.read_bytes()
    snapshot = Repository(settings.db_path, read_only=True)
    snapshot.initialize()
    assert snapshot.get_article(1)["text"] == article.text
    assert settings.db_path.read_bytes() == before
    with closing(sqlite3.connect(settings.db_path)) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='schema_migrations'"
            ).fetchone()
            is None
        )


def test_components_have_independent_version_histories(tmp_path):
    with closing(sqlite3.connect(tmp_path / "shared.db")) as connection:
        migration = Migration(1, "first", lambda db: db.execute("SELECT 1"))
        migrate(connection, "explorer", (migration,))
        migrate(connection, "review", (migration,))
        assert check_schema(connection, "explorer", (migration,)) == 1
        assert check_schema(connection, "review", (migration,)) == 1


def test_concurrent_initializers_apply_each_migration_once(settings):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: Repository(settings.db_path).initialize(), range(4)))
    with closing(sqlite3.connect(settings.db_path)) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in range(1, len(MIGRATIONS) + 1)]


def test_sql_triggers_do_not_break_atomic_script_execution(tmp_path):
    with closing(sqlite3.connect(tmp_path / "triggers.db")) as connection:
        migrate(connection, "explorer", MIGRATIONS)
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'chunks_%'"
            ).fetchone()[0]
            == 3
        )


def test_migrations_reject_an_owned_transaction(tmp_path):
    with closing(sqlite3.connect(tmp_path / "owned.db")) as connection:
        connection.execute("BEGIN")
        with pytest.raises(ValueError, match="existing transaction"):
            migrate(connection, "explorer", MIGRATIONS)
        assert connection.in_transaction


def test_upgrade_quarantines_invalid_legacy_completed_jobs_without_losing_original_json(
    settings, article
):
    legacy_article(settings.db_path, article)
    with closing(sqlite3.connect(settings.db_path)) as connection, connection:
        connection.execute(
            "INSERT INTO jobs(kind,payload,status,result,created_at,updated_at) "
            "VALUES ('analyze','[]','succeeded','[1, 2]','then','then')"
        )
    repo = Repository(settings.db_path)
    repo.initialize()
    job = repo.get_job(1)
    assert job["status"] == "failed"
    assert job["payload"]["invalid_persisted_payload_json"] == "[]"
    assert job["payload"]["invalid_persisted_result_json"] == "[1, 2]"


def test_wal_transition_retries_sqlite_busy_errors(tmp_path, monkeypatch):
    attempts = []
    with closing(sqlite3.connect(tmp_path / "wal.db")) as connection:

        class BusyTransition:
            def execute(self, statement):
                if statement == "PRAGMA journal_mode=WAL":
                    attempts.append(statement)
                    if len(attempts) < 3:
                        error = sqlite3.OperationalError("database is locked")
                        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                        raise error
                return connection.execute(statement)

        monkeypatch.setattr("evidence_explorer.persistence.connection.time.sleep", lambda _: None)
        enable_wal(BusyTransition())
        assert len(attempts) == 3
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
