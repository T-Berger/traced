"""Additive saved-feed tables; imported by the Explorer migration registry."""

import sqlite3

from ..persistence.migrations import execute_script


def saved_feed_subscriptions(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(articles)")}
    if "content_source" not in columns:
        connection.execute("ALTER TABLE articles ADD COLUMN content_source TEXT")
    execute_script(
        connection,
        """
        CREATE TABLE IF NOT EXISTS feed_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            topic TEXT NOT NULL DEFAULT 'General',
            interval_minutes INTEGER NOT NULL DEFAULT 60
                CHECK(interval_minutes BETWEEN 5 AND 10080),
            item_limit INTEGER NOT NULL DEFAULT 5 CHECK(item_limit BETWEEN 1 AND 20),
            content_mode TEXT NOT NULL DEFAULT 'article_links'
                CHECK(content_mode IN ('article_links', 'feed_content')),
            analyze INTEGER NOT NULL DEFAULT 1 CHECK(analyze IN (0,1)),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            next_refresh_at TEXT NOT NULL,
            last_queued_at TEXT,
            last_finished_at TEXT,
            last_refreshed_at TEXT,
            last_job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
            last_result TEXT,
            last_analysis_job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
            last_error TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            processed_job_id INTEGER,
            refresh_requested INTEGER NOT NULL DEFAULT 1 CHECK(refresh_requested IN (0,1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS feed_subscriptions_due_idx
            ON feed_subscriptions(enabled, next_refresh_at, id);
    """,
    )
