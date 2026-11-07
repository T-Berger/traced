"""Reciprocal, transaction-local ownership checks against live collection sources."""

import sqlite3

from ..source_identity import canonical_source_key


def assert_feed_owner_available(
    connection: sqlite3.Connection,
    platform: str,
    locator: str,
    *,
    existing_feed_id: int | None = None,
) -> None:
    key = canonical_source_key(platform, locator)
    # An established saved feed remains the owner of a historical cross-store overlap.
    owner = connection.execute(
        "SELECT id FROM feed_subscriptions WHERE canonical_key=? ORDER BY id LIMIT 1", (key,)
    ).fetchone()
    if owner is not None:
        if existing_feed_id is None or owner["id"] == existing_feed_id:
            return
        raise ValueError(f"This source is managed by saved feed #{owner['id']}.")
    from ..collection.ownership import assert_feed_owner_available as check_collection

    check_collection(connection, platform, locator)
