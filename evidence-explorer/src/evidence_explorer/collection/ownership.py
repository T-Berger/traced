"""One scheduler owns each canonical source across live controls and saved feeds."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from ..source_identity import canonical_source_key
from .models import CollectionFailure


def canonical_key(platform: str, locator: str) -> str:
    return canonical_source_key(platform, locator)


def _has_table(connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _feed_owner(connection, platform: str, locator: str) -> dict | None:
    if not _has_table(connection, "feed_subscriptions"):
        return None
    target = canonical_key(platform, locator)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(feed_subscriptions)")}
    fields = [
        key
        for key in ("id", "name", "url", "platform", "locator", "canonical_key")
        if key in columns
    ]
    cursor = connection.execute("SELECT " + ",".join(fields) + " FROM feed_subscriptions")
    for values in cursor:
        row = dict(zip(fields, values, strict=True))
        try:
            key = canonical_key(row.get("platform") or "rss", row.get("locator") or row["url"])
        except (ValueError, TypeError, KeyError):
            continue
        if key == target:
            return row
    return None


def find_feed_owner(db_path: Path, platform: str, locator: str) -> dict | None:
    if not Path(db_path).exists():
        return None
    with closing(sqlite3.connect(db_path, timeout=20)) as connection:
        return _feed_owner(connection, platform, locator)


def assert_collection_owner_available(connection, platform: str, locator: str) -> None:
    if _feed_owner(connection, platform, locator) is not None:
        raise CollectionFailure(
            "This source is managed in Saved feeds. "
            "Use its existing controls to avoid a duplicate schedule."
        )


def assert_feed_owner_available(connection, platform: str, locator: str) -> None:
    """Called by saved-feed registration inside its existing write transaction."""
    if not _has_table(connection, "collection_sources"):
        return
    target = canonical_key(platform, locator)
    for row in connection.execute("SELECT config FROM collection_sources"):
        try:
            config = json.loads(row[0])
            key = canonical_key(config["platform"], config["locator"])
        except (ValueError, TypeError, KeyError):
            continue
        if key == target:
            raise CollectionFailure(
                "This source is managed in Live sources. "
                "Use its existing controls to avoid a duplicate schedule."
            )
