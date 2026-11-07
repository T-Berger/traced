"""Initial Explorer database schema, including full-text indexes and job state."""

import sqlite3

from ..feeds.schema import saved_feed_subscriptions
from ..feeds.schema_platforms import platform_feed_sources
from .jobs import normalize_invalid_jobs
from .migrations import Migration, execute_script

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    published_at TEXT NOT NULL,
    text TEXT NOT NULL,
    language TEXT NOT NULL,
    topic TEXT NOT NULL,
    is_sample INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    start_char INTEGER NOT NULL,
    end_char INTEGER NOT NULL,
    UNIQUE(article_id, ordinal)
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS chunks_insert AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_delete AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text)
    VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_update AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text)
    VALUES ('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    model_key TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    PRIMARY KEY(chunk_id, model_key)
);
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    claim_text TEXT NOT NULL,
    quote TEXT NOT NULL,
    subject TEXT NOT NULL,
    attribution TEXT NOT NULL,
    event_time TEXT,
    location TEXT,
    model_key TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS claims_article_idx ON claims(article_id);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    dedupe_key TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status, id);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_active_dedupe_idx
ON jobs(dedupe_key)
WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running');
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _initial_schema(connection: sqlite3.Connection) -> None:
    execute_script(connection, SCHEMA_SQL)


def _paged_query_indexes(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE INDEX IF NOT EXISTS embeddings_model_chunk_idx ON embeddings(model_key, chunk_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS articles_published_idx ON articles(published_at DESC, id DESC)"
    )


def _news_provenance(connection: sqlite3.Connection) -> None:
    existing = {row[1] for row in connection.execute("PRAGMA table_info(articles)")}
    for column in (
        "requested_url",
        "final_url",
        "feed_url",
        "retrieved_at",
        "author",
        "publication_source",
    ):
        if column not in existing:
            connection.execute(f"ALTER TABLE articles ADD COLUMN {column} TEXT")


def _article_content_source(connection: sqlite3.Connection) -> None:
    """Repair v5 databases created before content_source was added to that migration."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(articles)")}
    if "content_source" not in columns:
        connection.execute("ALTER TABLE articles ADD COLUMN content_source TEXT")


MIGRATIONS = (
    Migration(1, "initial_explorer_schema", _initial_schema),
    Migration(2, "paged_query_indexes", _paged_query_indexes),
    Migration(3, "news_ingestion_provenance", _news_provenance),
    Migration(4, "quarantine_invalid_legacy_jobs", normalize_invalid_jobs),
    Migration(5, "saved_feed_subscriptions", saved_feed_subscriptions),
    Migration(6, "ensure_article_content_source", _article_content_source),
    Migration(7, "platform_feed_sources", platform_feed_sources),
)
