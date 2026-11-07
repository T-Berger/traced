"""Add platform source configuration while preserving existing feed history."""

import sqlite3

from ..source_identity import canonical_source_key

SOURCE_COLUMNS = (
    "source_platform",
    "source_channel_id",
    "source_native_id",
    "source_author_id",
    "acquisition_method",
    "content_scope",
    "source_metadata_json",
)


def platform_feed_sources(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(feed_subscriptions)")}
    for column, definition in (
        ("platform", "TEXT NOT NULL DEFAULT 'rss'"),
        ("locator", "TEXT"),
        ("canonical_key", "TEXT"),
        ("cursor", "TEXT"),
    ):
        if column not in columns:
            connection.execute(f"ALTER TABLE feed_subscriptions ADD COLUMN {column} {definition}")
    seen: dict[str, int] = {}
    rows = connection.execute(
        "SELECT id,url,platform,locator FROM feed_subscriptions ORDER BY id"
    ).fetchall()
    for row in rows:
        locator = row["locator"] or row["url"]
        key = canonical_source_key(row["platform"], locator)
        if key in seen:
            connection.execute(
                "UPDATE feed_subscriptions SET enabled=0,canonical_key=NULL,locator=?,last_error=? "
                "WHERE id=?",
                (locator, f"Duplicate source managed by saved feed #{seen[key]}.", row["id"]),
            )
        else:
            seen[key] = row["id"]
            connection.execute(
                "UPDATE feed_subscriptions SET canonical_key=?,locator=? WHERE id=?",
                (key, locator, row["id"]),
            )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS feed_subscriptions_source_key_idx "
        "ON feed_subscriptions(canonical_key) WHERE canonical_key IS NOT NULL"
    )
    article_columns = {row[1] for row in connection.execute("PRAGMA table_info(articles)")}
    for column in SOURCE_COLUMNS:
        if column not in article_columns:
            connection.execute(f"ALTER TABLE articles ADD COLUMN {column} TEXT")
