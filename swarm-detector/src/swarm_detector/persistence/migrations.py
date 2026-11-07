"""Transactional, versioned upgrades, including the original unversioned database."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from ..models import AnalysisRun, Dataset
from ..serialization import utc_now
from .projections import project_dataset, project_run

CURRENT_SCHEMA_VERSION = 2


class SchemaVersionError(ValueError):
    """The database requires a newer application or has an unsupported schema."""


LEGACY_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS datasets (
        dataset_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
        payload TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
        payload TEXT NOT NULL, created_at TEXT NOT NULL)""",
    "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS messages (
        dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id), message_key TEXT NOT NULL,
        post_key TEXT NOT NULL, account_key TEXT, channel_key TEXT NOT NULL,
        timestamp_utc TEXT NOT NULL, text_original TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY (dataset_id, message_key))""",
    "CREATE INDEX IF NOT EXISTS idx_message_post ON messages(dataset_id, post_key)",
    "CREATE INDEX IF NOT EXISTS idx_message_account ON messages(dataset_id, account_key)",
    """CREATE TABLE IF NOT EXISTS reviews (
        revision INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES runs(run_id), case_id TEXT NOT NULL,
        payload TEXT NOT NULL, created_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_review_case ON reviews(run_id, case_id, revision DESC)",
    """CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
        dataset_id UNINDEXED, message_key UNINDEXED, text_original, tokenize='unicode61')""",
)

NORMALIZED_SCHEMA = (
    """CREATE TABLE dataset_info (
        dataset_id TEXT PRIMARY KEY REFERENCES datasets(dataset_id), payload TEXT NOT NULL,
        channel_count INTEGER NOT NULL, post_count INTEGER NOT NULL,
        message_count INTEGER NOT NULL, account_count INTEGER NOT NULL, synthetic INTEGER NOT NULL)""",
    """CREATE TABLE channels (
        dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id), channel_key TEXT NOT NULL,
        ordinal INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(dataset_id,channel_key))""",
    """CREATE TABLE posts (
        dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id), post_key TEXT NOT NULL,
        channel_key TEXT NOT NULL, post_family_key TEXT NOT NULL, timestamp_utc TEXT NOT NULL,
        ordinal INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(dataset_id,post_key))""",
    "CREATE INDEX idx_post_order ON posts(dataset_id,timestamp_utc,post_key)",
    """CREATE TABLE dataset_comparisons (
        dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id), case_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(dataset_id,case_id))""",
    """CREATE TABLE message_content (
        dataset_id TEXT NOT NULL, message_key TEXT NOT NULL,
        ordinal INTEGER NOT NULL, normalized_text TEXT NOT NULL,
        PRIMARY KEY(dataset_id,message_key),
        FOREIGN KEY(dataset_id,message_key) REFERENCES messages(dataset_id,message_key))""",
    "CREATE INDEX idx_content_text ON message_content(dataset_id,normalized_text)",
    """CREATE TABLE message_urls (
        dataset_id TEXT NOT NULL, message_key TEXT NOT NULL, url TEXT NOT NULL,
        PRIMARY KEY(dataset_id,message_key,url),
        FOREIGN KEY(dataset_id,message_key) REFERENCES messages(dataset_id,message_key))""",
    "CREATE INDEX idx_content_url ON message_urls(dataset_id,url)",
    """CREATE TABLE run_info (
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id), dataset_id TEXT NOT NULL,
        payload TEXT NOT NULL, candidate_count INTEGER NOT NULL)""",
    """CREATE TABLE cases (
        run_id TEXT NOT NULL REFERENCES runs(run_id), case_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
        search_text TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id,case_id))""",
    "CREATE INDEX idx_case_order ON cases(run_id,kind,ordinal)",
    """CREATE TABLE case_accounts (
        run_id TEXT NOT NULL, case_id TEXT NOT NULL, account_key TEXT NOT NULL,
        is_core INTEGER NOT NULL, PRIMARY KEY(run_id,case_id,account_key),
        FOREIGN KEY(run_id,case_id) REFERENCES cases(run_id,case_id))""",
    """CREATE TABLE case_posts (
        run_id TEXT NOT NULL, case_id TEXT NOT NULL, post_key TEXT NOT NULL,
        declared INTEGER NOT NULL, PRIMARY KEY(run_id,case_id,post_key),
        FOREIGN KEY(run_id,case_id) REFERENCES cases(run_id,case_id))""",
    """CREATE TABLE pairs (
        run_id TEXT NOT NULL REFERENCES runs(run_id), pair_key TEXT NOT NULL,
        account_a TEXT NOT NULL, account_b TEXT NOT NULL, ordinal INTEGER NOT NULL,
        retained INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id,pair_key))""",
    "CREATE INDEX idx_pair_accounts ON pairs(run_id,account_a,account_b)",
    """CREATE TABLE pair_events (
        run_id TEXT NOT NULL, pair_key TEXT NOT NULL, ordinal INTEGER NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(run_id,pair_key,ordinal),
        FOREIGN KEY(run_id,pair_key) REFERENCES pairs(run_id,pair_key))""",
    "CREATE INDEX idx_message_order ON messages(dataset_id,timestamp_utc,message_key)",
)


def _initial_schema(conn: sqlite3.Connection) -> None:
    for statement in LEGACY_SCHEMA:
        conn.execute(statement)


def _normalized_read_tables(conn: sqlite3.Connection) -> None:
    for statement in NORMALIZED_SCHEMA:
        conn.execute(statement)
    # Only upgrades materialize legacy payloads, one dataset/run at a time.
    # The original immutable payloads, review revisions and FTS rows remain untouched.
    for row in conn.execute("SELECT payload FROM datasets ORDER BY rowid"):
        project_dataset(conn, Dataset.model_validate_json(row[0]))
    for row in conn.execute("SELECT payload FROM runs ORDER BY rowid"):
        project_run(conn, AnalysisRun.model_validate_json(row[0]))


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _initial_schema,
    2: _normalized_read_tables,
}


def migrate(conn: sqlite3.Connection) -> None:
    """Serialize upgrades and commit schema, backfill and version together."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > CURRENT_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Database schema {version} is newer than supported schema "
                f"{CURRENT_SCHEMA_VERSION}; use a newer application."
            )
        if version < 0:
            raise SchemaVersionError(f"Unsupported database schema: {version}")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for target in range(version + 1, CURRENT_SCHEMA_VERSION + 1):
            MIGRATIONS[target](conn)
            conn.execute("INSERT INTO schema_migrations VALUES (?,?)", (target, utc_now()))
            conn.execute(f"PRAGMA user_version={target}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
