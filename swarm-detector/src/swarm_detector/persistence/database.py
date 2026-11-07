"""Connection lifecycle, schema upgrades, and consistent backups."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .migrations import migrate


def default_db_path() -> Path:
    if os.environ.get("SWARM_DB_PATH"):
        return Path(os.environ["SWARM_DB_PATH"]).expanduser().resolve()
    return Path(os.environ.get("SWARM_DATA_DIR", "data")).expanduser().resolve() / "swarm.db"


class Database:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path).expanduser().resolve() if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            migrate(conn)
            conn.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def state(self, key: str) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def backup(self, destination: str | Path) -> Path:
        path = Path(destination).expanduser().resolve()
        if path == self.path or path.exists():
            raise ValueError("Backup destination must be a new file, different from the database.")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb"):
            pass
        try:
            with self.connection() as source:
                target = sqlite3.connect(path)
                try:
                    source.backup(target)
                finally:
                    target.close()
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path
