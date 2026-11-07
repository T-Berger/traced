"""Consistent read-only review snapshots for preparing evidence exports.

Every repository read shares one explicit SQLite transaction. No migrations,
collection initialization, model calls, or source requests occur here. Rendering
and artifact packaging should use detached values after leaving this context.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .workspace import WorkspaceRepository, WorkspaceService


class _SnapshotRepository(WorkspaceRepository):
    def __init__(self, original: WorkspaceRepository, connection: sqlite3.Connection) -> None:
        self._snapshot_connection = connection
        self._snapshot_active = True
        super().__init__(original.db_path)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if not self._snapshot_active:
            raise RuntimeError("The review snapshot has been released.")
        if write:
            raise PermissionError("Review export snapshots are read-only.")
        yield self._snapshot_connection


@contextmanager
def workspace_snapshot(service: WorkspaceService) -> Iterator[WorkspaceService]:
    """Yield review/export operations bound to one consistent database snapshot.

    A nested call shares its enclosing snapshot. The returned service deliberately
    has no collection service: only review reads and artifact operations belong
    in this boundary, and callers must capture detached values before it closes.
    """
    if isinstance(service.repository, _SnapshotRepository):
        if not service.repository._snapshot_active:
            raise RuntimeError("The review snapshot has been released.")
        yield service
        return
    with service.repository._connection() as connection:
        if connection.in_transaction:
            raise RuntimeError("A fresh database connection is required for a review snapshot.")
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        repository = _SnapshotRepository(service.repository, connection)
        # Avoid WorkspaceService.__init__, which constructs unrelated collection services.
        snapshot = object.__new__(WorkspaceService)
        snapshot.repository = repository
        snapshot.settings = service.settings
        snapshot.artifact_root = service.artifact_root
        try:
            yield snapshot
        finally:
            repository._snapshot_active = False
