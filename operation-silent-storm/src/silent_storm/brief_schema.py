"""Additive persistence for immutable, human-authored specialist snapshots."""

from __future__ import annotations

import sqlite3

BRIEF_SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_briefs (
    brief_id TEXT PRIMARY KEY,
    scenario_id TEXT NOT NULL REFERENCES review_scenarios(scenario_id),
    release_id TEXT NOT NULL REFERENCES releases(release_id),
    scenario_version INTEGER NOT NULL CHECK(scenario_version>=0),
    title TEXT NOT NULL, reviewer TEXT NOT NULL, created_at TEXT NOT NULL,
    source_origin TEXT NOT NULL CHECK(source_origin IN ('unspecified','real','synthetic')),
    snapshot_json TEXT NOT NULL, fingerprint TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS briefs_scenario
    ON saved_briefs(scenario_id,created_at DESC,brief_id);
CREATE TRIGGER IF NOT EXISTS immutable_saved_brief_replace
BEFORE INSERT ON saved_briefs
WHEN EXISTS (SELECT 1 FROM saved_briefs WHERE brief_id=NEW.brief_id)
BEGIN SELECT RAISE(ABORT,'Saved briefs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_saved_brief_update
BEFORE UPDATE ON saved_briefs BEGIN SELECT RAISE(ABORT,'Saved briefs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_saved_brief_delete
BEFORE DELETE ON saved_briefs BEGIN SELECT RAISE(ABORT,'Saved briefs are immutable'); END;
"""


def initialize_brief_schema(connection: sqlite3.Connection) -> None:
    """Install only tables/indexes/triggers; never create scenarios or snapshots."""
    connection.executescript(BRIEF_SCHEMA)
