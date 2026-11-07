"""Real SQLite checks for the transaction policies retained by shared plumbing."""

import sqlite3

import pytest

from evidence_explorer.claim_check.storage import ClaimCheckStorage
from evidence_explorer.collection.persistence import CollectionRepository
from evidence_explorer.persistence import connection as connection_module
from evidence_explorer.persistence.connection import SQLiteDatabase, sqlite_connection
from evidence_explorer.review.storage import ReviewRepository


def connection_factory(kind, path):
    if kind == "explorer":
        return SQLiteDatabase(path).connection
    if kind == "review":
        return ReviewRepository(path)._connection
    if kind == "claim_check":
        return ClaimCheckStorage(path).connection
    return CollectionRepository(path).connection


@pytest.mark.parametrize("kind", ["explorer", "review", "collection", "claim_check"])
def test_repository_write_policy_commits_and_closes(kind, tmp_path):
    connect = connection_factory(kind, tmp_path / "workspace.sqlite3")
    with connect(write=True) as connection:
        assert connection.in_transaction is (kind != "explorer")
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        expected_timeout = 20000 if kind == "collection" else 10000
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == expected_timeout
        connection.execute("CREATE TABLE observations(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO observations VALUES(1, 'saved')")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with connect() as reopened:
        row = reopened.execute("SELECT * FROM observations").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert dict(row) == {"id": 1, "value": "saved"}


@pytest.mark.parametrize("kind", ["explorer", "review", "collection", "claim_check"])
@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_write_rollback_preserves_saved_rows_for_errors_and_interruptions(kind, failure, tmp_path):
    connect = connection_factory(kind, tmp_path / "workspace.sqlite3")
    with connect(write=True) as connection:
        connection.execute("CREATE TABLE observations(value TEXT)")
        connection.execute("INSERT INTO observations VALUES('baseline')")
    with pytest.raises(failure):
        with connect(write=True) as connection:
            connection.execute("DELETE FROM observations")
            connection.execute("INSERT INTO observations VALUES('uncommitted')")
            raise failure("abort")
    with connect() as connection:
        assert [row[0] for row in connection.execute("SELECT value FROM observations")] == [
            "baseline"
        ]


@pytest.mark.parametrize("kind", ["explorer", "review", "collection", "claim_check"])
def test_read_context_preserves_existing_incidental_write_policy(kind, tmp_path):
    connect = connection_factory(kind, tmp_path / "workspace.sqlite3")
    with connect(write=True) as connection:
        connection.execute("CREATE TABLE observations(value TEXT)")
    with connect() as connection:
        assert not connection.in_transaction
        connection.execute("INSERT INTO observations VALUES('incidental')")
    with connect() as connection:
        count = connection.execute("SELECT count(*) FROM observations").fetchone()[0]
    assert count == (0 if kind == "collection" else 1)


@pytest.mark.parametrize("kind", ["review", "collection", "claim_check"])
def test_immediate_write_context_reserves_writer_before_first_mutation(kind, tmp_path):
    path = tmp_path / "workspace.sqlite3"
    connect = connection_factory(kind, path)
    with connect(write=True):
        contender = sqlite3.connect(path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
        finally:
            contender.close()


def test_explorer_snapshot_is_enforced_read_only_without_creating_parent(tmp_path):
    path = tmp_path / "workspace.sqlite3"
    with SQLiteDatabase(path).connection(write=True) as connection:
        connection.execute("CREATE TABLE observations(value TEXT)")
    snapshot = SQLiteDatabase(path, read_only=True)
    with snapshot.connection() as connection:
        assert connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO observations VALUES('forbidden')")
    with pytest.raises(PermissionError, match="read-only"):
        with snapshot.connection(write=True):
            pytest.fail("No write handle may be exposed")
    missing = tmp_path / "missing" / "snapshot.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        with SQLiteDatabase(missing, read_only=True).connection():
            pytest.fail("No missing snapshot may be created")
    assert not missing.parent.exists()


def test_collection_connection_retains_directory_and_sql_function_hook(tmp_path):
    path = tmp_path / "nested" / "workspace.sqlite3"
    with CollectionRepository(path).connection() as connection:
        assert path.exists()
        assert (
            connection.execute(
                "SELECT observation_time(?)", ("2035-01-02T11:00:00+01:00",)
            ).fetchone()[0]
            == "2035-01-02T10:00:00+00:00"
        )
        with pytest.raises(sqlite3.OperationalError, match="user-defined function"):
            connection.execute("SELECT observation_time(NULL)")


def test_configuration_failure_closes_the_opened_handle(tmp_path, monkeypatch):
    opened = []
    original = sqlite3.connect

    def capture(*args, **kwargs):
        connection = original(*args, **kwargs)
        opened.append(connection)
        return connection

    def reject(_connection):
        raise RuntimeError("configuration failed")

    monkeypatch.setattr(connection_module.sqlite3, "connect", capture)
    with pytest.raises(RuntimeError, match="configuration failed"):
        with sqlite_connection(tmp_path / "workspace.sqlite3", configure=reject):
            pytest.fail("A failed connection must not reach the caller")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
