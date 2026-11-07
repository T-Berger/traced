"""Additive schema for immutable review history and isolated source scenarios."""

from __future__ import annotations

import sqlite3

REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_scenarios (
    scenario_id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL REFERENCES releases(release_id), name TEXT NOT NULL,
    is_baseline INTEGER NOT NULL CHECK(is_baseline IN (0,1)),
    version INTEGER NOT NULL CHECK(version>=0), created_at TEXT NOT NULL,
    created_by TEXT NOT NULL, source_scenario_id TEXT REFERENCES review_scenarios(scenario_id),
    source_scenario_version INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS one_review_baseline
    ON review_scenarios(release_id) WHERE is_baseline=1;
CREATE TABLE IF NOT EXISTS review_notes (
    review_id TEXT PRIMARY KEY,
    scenario_id TEXT NOT NULL REFERENCES review_scenarios(scenario_id),
    release_id TEXT NOT NULL REFERENCES releases(release_id), created_at TEXT NOT NULL,
    origin_review_id TEXT REFERENCES review_notes(review_id), origin_revision INTEGER
);
CREATE INDEX IF NOT EXISTS notes_scenario ON review_notes(scenario_id,review_id);
CREATE TABLE IF NOT EXISTS review_revisions (
    review_id TEXT NOT NULL REFERENCES review_notes(review_id), revision INTEGER NOT NULL,
    title TEXT NOT NULL, observation TEXT NOT NULL, interpretation TEXT NOT NULL,
    uncertainty TEXT NOT NULL, rationale TEXT NOT NULL, reviewer TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','reviewed','rejected','needs_review')),
    evidence_ids_json TEXT NOT NULL, created_at TEXT NOT NULL,
    change_type TEXT NOT NULL, change_reason TEXT NOT NULL,
    PRIMARY KEY(review_id,revision), CHECK(revision>=1)
);
CREATE TABLE IF NOT EXISTS review_evidence (
    review_id TEXT NOT NULL, revision INTEGER NOT NULL, release_id TEXT NOT NULL,
    frame_id TEXT NOT NULL, evidence_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
    PRIMARY KEY(review_id,revision,release_id,frame_id),
    FOREIGN KEY(review_id,revision) REFERENCES review_revisions(review_id,revision),
    FOREIGN KEY(release_id,frame_id) REFERENCES frames(release_id,frame_id)
);
CREATE INDEX IF NOT EXISTS evidence_frame ON review_evidence(release_id,frame_id,review_id,revision);
CREATE TABLE IF NOT EXISTS source_exclusion_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL REFERENCES review_scenarios(scenario_id),
    release_id TEXT NOT NULL REFERENCES releases(release_id), video_id TEXT NOT NULL,
    excluded INTEGER NOT NULL CHECK(excluded IN (0,1)), reason TEXT NOT NULL,
    reviewer TEXT NOT NULL, scenario_version INTEGER NOT NULL, created_at TEXT NOT NULL,
    origin_event_id INTEGER REFERENCES source_exclusion_events(event_id)
);
CREATE INDEX IF NOT EXISTS exclusion_event_scenario
    ON source_exclusion_events(scenario_id,event_id DESC);
CREATE TABLE IF NOT EXISTS source_exclusions (
    scenario_id TEXT NOT NULL REFERENCES review_scenarios(scenario_id),
    release_id TEXT NOT NULL REFERENCES releases(release_id), video_id TEXT NOT NULL,
    excluded INTEGER NOT NULL CHECK(excluded IN (0,1)),
    event_id INTEGER NOT NULL REFERENCES source_exclusion_events(event_id),
    PRIMARY KEY(scenario_id,video_id)
);
CREATE VIEW IF NOT EXISTS review_current AS
SELECT n.review_id,n.scenario_id,n.release_id,n.origin_review_id,n.origin_revision,
    r.revision,r.title,r.observation,r.interpretation,r.uncertainty,r.rationale,r.reviewer,
    r.status,r.evidence_ids_json,r.created_at,r.change_type,r.change_reason
FROM review_notes n JOIN review_revisions r ON r.review_id=n.review_id
WHERE r.revision=(SELECT MAX(latest.revision) FROM review_revisions latest
    WHERE latest.review_id=n.review_id);
CREATE TRIGGER IF NOT EXISTS immutable_review_revision_update
BEFORE UPDATE ON review_revisions BEGIN SELECT RAISE(ABORT,'Review revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_review_revision_delete
BEFORE DELETE ON review_revisions BEGIN SELECT RAISE(ABORT,'Review revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_review_evidence_update
BEFORE UPDATE ON review_evidence BEGIN SELECT RAISE(ABORT,'Review evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_review_evidence_delete
BEFORE DELETE ON review_evidence BEGIN SELECT RAISE(ABORT,'Review evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_exclusion_event_update
BEFORE UPDATE ON source_exclusion_events
BEGIN SELECT RAISE(ABORT,'Source exclusion events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_exclusion_event_delete
BEFORE DELETE ON source_exclusion_events
BEGIN SELECT RAISE(ABORT,'Source exclusion events are immutable'); END;
"""


def initialize_review_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indexes only; never seed a scenario or review."""
    conn.executescript(REVIEW_SCHEMA)
