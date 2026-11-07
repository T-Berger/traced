"""Connection ownership and transaction helpers shared by focused repositories."""

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol


class ConnectionFactory(Protocol):
    def __call__(self, *, write: bool = False) -> AbstractContextManager[sqlite3.Connection]: ...


@dataclass(frozen=True)
class ConnectionPolicy:
    """Retain each repository's existing timeout and transaction semantics."""

    timeout: float = 10
    busy_timeout_ms: int | None = 10000
    immediate_writes: bool = False
    commit_read_contexts: bool = True


_DEFAULT_POLICY = ConnectionPolicy()


@contextmanager
def sqlite_connection(
    path: Path,
    *,
    write: bool = False,
    read_only: bool = False,
    policy: ConnectionPolicy = _DEFAULT_POLICY,
    configure: Callable[[sqlite3.Connection], None] | None = None,
) -> Iterator[sqlite3.Connection]:
    """Configure, finish, and close one connection without owning schema changes.

    Callers retain path normalization and directory creation. ``configure`` adds
    repository-specific SQL functions before foreign keys and timeout settings.
    A read context may commit incidental writes only when its policy allows it;
    only ``read_only=True`` opens an enforced read-only SQLite handle.
    """
    if write and read_only:
        raise PermissionError("This snapshot is read-only.")
    connection = (
        sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=policy.timeout)
        if read_only
        else sqlite3.connect(path, timeout=policy.timeout)
    )
    try:
        connection.row_factory = sqlite3.Row
        if configure is not None:
            configure(connection)
        connection.execute("PRAGMA foreign_keys = ON")
        if policy.busy_timeout_ms is not None:
            connection.execute(f"PRAGMA busy_timeout = {policy.busy_timeout_ms:d}")
        transaction = connection if write or policy.commit_read_contexts else nullcontext()
        with transaction:
            if write and policy.immediate_writes:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
    finally:
        connection.close()


def now() -> str:
    return datetime.now(UTC).isoformat()


def enable_wal(connection: sqlite3.Connection, *, timeout: float = 10.0) -> None:
    """Retry journal transitions, for which SQLite may bypass its busy timeout."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
                return
            if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal":
                return
            raise sqlite3.OperationalError("Could not enable SQLite WAL mode.")
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
            if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def invalidate_claim_cache(connection: sqlite3.Connection, article_id: int) -> None:
    prefix = f"claims:{article_id}:"
    connection.execute("DELETE FROM metadata WHERE substr(key, 1, ?) = ?", (len(prefix), prefix))


class SQLiteDatabase:
    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = Path(path).expanduser().resolve()
        self.read_only = read_only

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with sqlite_connection(self.path, write=write, read_only=self.read_only) as connection:
            yield connection
