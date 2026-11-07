"""Transactional, component-scoped SQLite migrations for the shared database.

Explorer and review tables can share a file and evolve independently. A legacy
database is adopted by idempotent initial migrations; read-only snapshots are
checked without adding a journal or changing the source file.
"""

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime


class SchemaVersionError(RuntimeError):
    """The database cannot be safely opened by this application version."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    upgrade: Callable[[sqlite3.Connection], None]


def execute_script(connection: sqlite3.Connection, sql: str) -> None:
    """Execute SQL, including triggers, without executescript's implicit commit."""
    pending = ""
    for character in sql:
        pending += character
        if character == ";" and sqlite3.complete_statement(pending):
            connection.execute(pending)
            pending = ""
    if pending.strip():
        connection.execute(pending)


def _validate_migrations(migrations: Sequence[Migration]) -> None:
    if [migration.version for migration in migrations] != list(range(1, len(migrations) + 1)):
        raise ValueError("Migrations must have contiguous versions starting at 1.")
    if any(not migration.name.strip() for migration in migrations):
        raise ValueError("Migration names must not be empty.")


def check_schema(
    connection: sqlite3.Connection,
    component: str,
    migrations: Sequence[Migration],
    *,
    allow_legacy: bool = True,
) -> int:
    """Validate the recorded history and return its version without writing."""
    _validate_migrations(migrations)
    journal = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    rows = (
        connection.execute(
            "SELECT version, name FROM schema_migrations WHERE component=? ORDER BY version",
            (component,),
        ).fetchall()
        if journal
        else []
    )
    if not rows:
        if not allow_legacy:
            raise SchemaVersionError(f"The {component} schema has no migration history.")
        return 0
    versions = [row[0] for row in rows]
    if versions != list(range(1, len(rows) + 1)) or versions[-1] > len(migrations):
        raise SchemaVersionError(
            f"Unsupported or incomplete {component} schema history: {versions}."
        )
    for (version, name), migration in zip(rows, migrations, strict=False):
        if name != migration.name:
            raise SchemaVersionError(f"Unexpected {component} migration {version}: {name}.")
    return versions[-1]


def migrate(
    connection: sqlite3.Connection, component: str, migrations: Sequence[Migration]
) -> None:
    """Apply every pending migration in one atomic transaction, serialized by SQLite."""
    _validate_migrations(migrations)
    if not component.strip():
        raise ValueError("A migration component is required.")
    if connection.in_transaction:
        raise ValueError("Migrations require a connection outside an existing transaction.")
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = check_schema(connection, component, migrations)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "component TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL, "
            "applied_at TEXT NOT NULL, PRIMARY KEY(component, version))"
        )
        for migration in migrations[current:]:
            migration.upgrade(connection)
            connection.execute(
                "INSERT INTO schema_migrations(component, version, name, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (component, migration.version, migration.name, datetime.now(UTC).isoformat()),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
