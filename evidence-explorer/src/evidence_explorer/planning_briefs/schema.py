"""Planning-only tables, versioned independently in the shared migration journal."""

import sqlite3

from ..persistence.migrations import Migration, execute_script

COMPONENT = "monitoring_briefs"


def _initial_schema(connection: sqlite3.Connection) -> None:
    execute_script(
        connection,
        """
        CREATE TABLE monitoring_brief_heads (
            brief_id TEXT PRIMARY KEY,
            head_revision_id TEXT REFERENCES monitoring_brief_revisions(revision_id)
                DEFERRABLE INITIALLY DEFERRED
        );
        CREATE TABLE monitoring_brief_revisions (
            revision_id TEXT PRIMARY KEY,
            brief_id TEXT NOT NULL REFERENCES monitoring_brief_heads(brief_id),
            number INTEGER NOT NULL CHECK(number > 0),
            parent_revision_id TEXT REFERENCES monitoring_brief_revisions(revision_id),
            case_id TEXT,
            revision_json TEXT NOT NULL,
            local INTEGER NOT NULL CHECK(local IN (0,1)),
            UNIQUE(brief_id, number)
        );
        CREATE INDEX monitoring_brief_case_history
            ON monitoring_brief_revisions(case_id,brief_id,number);
        CREATE TABLE monitoring_brief_confirmations (
            confirmation_id TEXT PRIMARY KEY,
            revision_id TEXT NOT NULL REFERENCES monitoring_brief_revisions(revision_id),
            confirmation_json TEXT NOT NULL,
            local INTEGER NOT NULL CHECK(local IN (0,1))
        );
        CREATE UNIQUE INDEX monitoring_brief_local_confirmation
            ON monitoring_brief_confirmations(revision_id) WHERE local=1;
        CREATE TABLE monitoring_brief_active_cases (
            case_id TEXT PRIMARY KEY REFERENCES review_cases(id),
            brief_id TEXT NOT NULL REFERENCES monitoring_brief_heads(brief_id)
        );
        CREATE TRIGGER monitoring_brief_revisions_no_update
            BEFORE UPDATE ON monitoring_brief_revisions
            BEGIN SELECT RAISE(ABORT,'Planning revisions are immutable'); END;
        CREATE TRIGGER monitoring_brief_revisions_no_delete
            BEFORE DELETE ON monitoring_brief_revisions
            BEGIN SELECT RAISE(ABORT,'Planning revisions are immutable'); END;
        CREATE TRIGGER monitoring_brief_confirmations_no_update
            BEFORE UPDATE ON monitoring_brief_confirmations
            BEGIN SELECT RAISE(ABORT,'Planning confirmations are immutable'); END;
        CREATE TRIGGER monitoring_brief_confirmations_no_delete
            BEFORE DELETE ON monitoring_brief_confirmations
            BEGIN SELECT RAISE(ABORT,'Planning confirmations are immutable'); END;
        """,
    )


MIGRATIONS = (Migration(1, "immutable_planning_brief_history", _initial_schema),)
