"""Persist direct-collection sources, jobs, and source-specific item observations.

This repository uses the configured workspace SQLite file and shared connection
helper with its own transaction policy and ``collection_schema`` migrations. It
does not use Explorer's article queue. Provider I/O belongs to ``CollectionRuntime``;
saved observations support immutable case projection through the collection service.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from ..persistence.connection import ConnectionPolicy, sqlite_connection
from ..source_contracts import SourceItem
from .models import CollectedBatch, CollectionFailure, CollectionSource


def instant(now: datetime | None = None) -> datetime:
    stamp = now or datetime.now(UTC)
    if stamp.tzinfo is None:
        raise ValueError("Collection clocks require timezone-aware values.")
    return stamp.astimezone(UTC)


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def observation_time(value) -> str:
    """Normalize known capture times; unknown history cannot establish recency."""
    try:
        if not isinstance(value, str):
            raise ValueError
        return instant(datetime.fromisoformat(value)).isoformat()
    except (ValueError, TypeError, OverflowError):
        raise CollectionFailure(
            "A saved source observation has a missing or invalid capture time. "
            "Review its history or collect that source again before creating a case."
        ) from None


def _source_summary(row) -> dict:
    return {
        **json.loads(row["config"]),
        "enabled": bool(row["enabled"]),
        "next_due_at": row["next_due_at"],
        "last_collected_at": row["last_collected_at"],
        "last_status": row["job_status"] or row["last_status"] or "idle",
        "last_error": row["last_error"],
        "retry_after_at": row["retry_after_at"],
        "item_count": row["item_count"],
        "unattributed_count": row["unattributed_count"],
        "job_id": row["last_job_id"],
    }


class CollectionRepository:
    """Own collection tables without fetching sources or creating review cases.

    ``initialize`` applies collection migrations explicitly. Write contexts begin
    an immediate transaction; each connection registers the observation-time SQL
    function needed for saved-source version ordering.
    """

    def __init__(self, path: Path):
        self.db_path = Path(path)

    @contextmanager
    def connection(self, *, write=False):
        """Open a short connection and commit or roll back requested writes.

        The parent directory is created even for a read context. ``write=False``
        skips the explicit write transaction; it is not a read-only SQLite handle.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        def configure(connection: sqlite3.Connection) -> None:
            connection.create_function("observation_time", 1, observation_time, deterministic=True)

        with sqlite_connection(
            self.db_path,
            write=write,
            policy=ConnectionPolicy(
                timeout=20,
                busy_timeout_ms=None,
                immediate_writes=True,
                commit_read_contexts=False,
            ),
            configure=configure,
        ) as connection:
            yield connection

    def initialize(self) -> None:
        with self.connection(write=True) as connection:
            statements = (
                "CREATE TABLE IF NOT EXISTS collection_schema(version INTEGER PRIMARY KEY)",
                "CREATE TABLE IF NOT EXISTS collection_sources("
                "id TEXT PRIMARY KEY,config TEXT NOT NULL,enabled INTEGER NOT NULL,"
                "next_due_at TEXT NOT NULL,last_collected_at TEXT,last_status TEXT,"
                "last_error TEXT,last_job_id TEXT,failures INTEGER NOT NULL DEFAULT 0)",
                "CREATE TABLE IF NOT EXISTS collection_jobs("
                "id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES collection_sources(id),"
                "config TEXT NOT NULL,status TEXT NOT NULL,scheduled INTEGER NOT NULL,"
                "attempt INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,"
                "started_at TEXT,finished_at TEXT,error TEXT,result TEXT)",
                "CREATE UNIQUE INDEX IF NOT EXISTS collection_active_job "
                "ON collection_jobs(source_id) WHERE status IN ('queued','running')",
                "CREATE INDEX IF NOT EXISTS collection_job_queue "
                "ON collection_jobs(status,created_at,id)",
                "CREATE TABLE IF NOT EXISTS collection_items("
                "item_key TEXT PRIMARY KEY,payload TEXT NOT NULL,semantic_hash TEXT NOT NULL,"
                "first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS collection_source_items("
                "source_id TEXT NOT NULL REFERENCES collection_sources(id),"
                "item_key TEXT NOT NULL REFERENCES collection_items(item_key),"
                "PRIMARY KEY(source_id,item_key))",
            )
            for statement in statements:
                connection.execute(statement)
            versions = {
                row[0] for row in connection.execute("SELECT version FROM collection_schema")
            }
            if versions - {1, 2, 3}:
                raise ValueError("This collection database uses a newer schema version.")
            connection.execute("INSERT OR IGNORE INTO collection_schema VALUES (1)")
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(collection_sources)")
            }
            if "retry_after_at" not in columns:
                connection.execute("ALTER TABLE collection_sources ADD COLUMN retry_after_at TEXT")
            connection.execute("INSERT OR IGNORE INTO collection_schema VALUES (2)")
            membership_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(collection_source_items)")
            }
            # Older links identify a source/item association, but cannot establish
            # which global revision that source observed. Never reconstruct it.
            for name in ("payload", "semantic_hash", "first_seen_at", "last_seen_at"):
                if name not in membership_columns:
                    connection.execute(
                        "ALTER TABLE collection_source_items ADD COLUMN " + name + " TEXT"
                    )
            connection.execute("INSERT OR IGNORE INTO collection_schema VALUES (3)")

    def add(self, source: CollectionSource, *, now=None) -> str:
        from .ownership import assert_collection_owner_available, canonical_key

        with self.connection(write=True) as connection:
            assert_collection_owner_available(connection, source.platform, source.locator)
            key = canonical_key(source.platform, source.locator)
            for row in connection.execute("SELECT id,config FROM collection_sources"):
                existing = json.loads(row["config"])
                if canonical_key(existing["platform"], existing["locator"]) == key:
                    return row["id"]
            connection.execute(
                "INSERT OR IGNORE INTO collection_sources(id,config,enabled,next_due_at) "
                "VALUES (?,?,?,?)",
                (
                    source.id,
                    source.model_dump_json(),
                    int(source.enabled),
                    instant(now).isoformat(),
                ),
            )
        return source.id

    def source(self, source_id: str) -> CollectionSource:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT config,enabled FROM collection_sources WHERE id=?", (source_id,)
            ).fetchone()
        if row is None:
            raise ValueError("This live source is no longer available.")
        return CollectionSource.model_validate(
            {**json.loads(row["config"]), "enabled": bool(row["enabled"])}
        )

    def sources(self) -> list[dict]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT s.*,j.status AS job_status,"
                "(SELECT COUNT(*) FROM collection_source_items i "
                "WHERE i.source_id=s.id) AS item_count,"
                "(SELECT COUNT(*) FROM collection_source_items i "
                "WHERE i.source_id=s.id AND i.payload IS NULL) AS unattributed_count "
                "FROM collection_sources s LEFT JOIN collection_jobs j ON j.id=s.last_job_id "
                "ORDER BY s.rowid LIMIT 100"
            ).fetchall()
        return [_source_summary(row) for row in rows]

    def scope_snapshot(self, source_ids: list[str], limit: int = 2000) -> dict:
        """Read selected source coverage and bounded union items in one SQLite snapshot."""
        if (
            not isinstance(source_ids, list)
            or not 1 <= len(source_ids) <= 100
            or any(not isinstance(source_id, str) or not source_id for source_id in source_ids)
        ):
            raise CollectionFailure("Choose at least one saved source for the new case.")
        if len(set(source_ids)) != len(source_ids):
            raise CollectionFailure("Choose each saved source only once.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 2000:
            raise CollectionFailure("A case can include between 1 and 2,000 saved records.")
        selected = sorted(source_ids)
        placeholders = ",".join("?" for _ in selected)
        versions = (
            "WITH observed AS (SELECT source_id,item_key,payload,"
            "observation_time(last_seen_at) AS last_seen_at "
            "FROM collection_source_items "
            f"WHERE source_id IN ({placeholders}) AND payload IS NOT NULL),"
            "ranked AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY item_key "
            "ORDER BY last_seen_at DESC,source_id) AS version_rank FROM observed),"
            "winners AS (SELECT * FROM ranked WHERE version_rank=1) "
        )
        chosen = (
            versions + ",chosen AS (SELECT * FROM winners "
            "ORDER BY last_seen_at DESC,item_key LIMIT ?) "
        )
        with self.connection() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT s.*,j.status AS job_status,"
                "(SELECT COUNT(*) FROM collection_source_items si "
                "WHERE si.source_id=s.id) AS item_count,"
                "(SELECT COUNT(*) FROM collection_source_items si "
                "WHERE si.source_id=s.id AND si.payload IS NULL) AS unattributed_count "
                "FROM collection_sources s LEFT JOIN collection_jobs j ON j.id=s.last_job_id "
                f"WHERE s.id IN ({placeholders}) ORDER BY s.id",
                selected,
            ).fetchall()
            if {row["id"] for row in rows} != set(selected):
                raise CollectionFailure(
                    "A selected source is no longer available. Choose sources again."
                )
            observation_digest = hashlib.sha256()
            observation_manifest = []
            for observation in connection.execute(
                "SELECT source_id,item_key,payload,semantic_hash,first_seen_at,last_seen_at "
                "FROM collection_source_items "
                f"WHERE source_id IN ({placeholders}) ORDER BY source_id,item_key",
                selected,
            ):
                first, last, payload_hash, content_hash = None, None, None, None
                if observation["payload"] is not None:
                    first = observation_time(observation["first_seen_at"])
                    last = observation_time(observation["last_seen_at"])
                    if first > last:
                        raise CollectionFailure(
                            "A saved source observation has inconsistent capture times. "
                            "Review its history or collect that source again."
                        )
                    payload_hash = hashlib.sha256(
                        observation["payload"].encode("utf-8")
                    ).hexdigest()
                    content_hash = hashlib.sha256(
                        json.loads(observation["payload"])["text"].encode("utf-8")
                    ).hexdigest()
                observation_manifest.append(
                    {
                        "source_id": observation["source_id"],
                        "item_key": observation["item_key"],
                        "payload_hash": payload_hash,
                        "semantic_hash": observation["semantic_hash"],
                        "content_hash": content_hash,
                        "first_observed_at": first,
                        "observed_at": last,
                        "attribution": "observed" if payload_hash else "unknown",
                    }
                )
                # Hash even observations omitted by the item limit or version
                # policy, including unknown historical associations.
                observation_digest.update(encode(dict(observation)).encode("utf-8"))
                observation_digest.update(b"\n")
            total = connection.execute(
                "SELECT COUNT(DISTINCT item_key) FROM collection_source_items "
                f"WHERE source_id IN ({placeholders}) AND payload IS NOT NULL",
                selected,
            ).fetchone()[0]
            items = {
                row["item_key"]: json.loads(row["payload"])
                for row in connection.execute(
                    chosen + "SELECT item_key,payload FROM chosen "
                    "ORDER BY last_seen_at DESC,item_key",
                    [*selected, limit],
                )
            }
            membership = {source_id: set() for source_id in selected}
            for row in connection.execute(
                chosen + "SELECT o.source_id,o.item_key FROM observed o "
                "JOIN chosen c ON c.item_key=o.item_key AND c.payload=o.payload",
                [*selected, limit],
            ):
                membership[row["source_id"]].add(row["item_key"])
            omissions = {source_id: [] for source_id in selected}
            for observation in connection.execute(
                versions + "SELECT o.*,w.source_id AS included_source_id,"
                "w.last_seen_at AS included_observed_at,w.payload AS included_payload "
                "FROM observed o JOIN winners w ON w.item_key=o.item_key "
                "WHERE o.payload<>w.payload ORDER BY o.source_id,o.item_key",
                selected,
            ):
                payload = json.loads(observation["payload"])
                preferred_payload = json.loads(observation["included_payload"])
                time_tied = observation["last_seen_at"] == observation["included_observed_at"]
                omissions[observation["source_id"]].append(
                    {
                        "item_key": observation["item_key"],
                        "channel_id": payload["channel_id"],
                        "native_id": payload["native_id"],
                        "observed_at": observation["last_seen_at"],
                        "payload_hash": hashlib.sha256(
                            observation["payload"].encode("utf-8")
                        ).hexdigest(),
                        "content_hash": hashlib.sha256(payload["text"].encode("utf-8")).hexdigest(),
                        "included_source_id": observation["included_source_id"],
                        "included_observed_at": observation["included_observed_at"],
                        "included_in_case": observation["item_key"] in items,
                        "included_payload_hash": hashlib.sha256(
                            observation["included_payload"].encode("utf-8")
                        ).hexdigest(),
                        "included_content_hash": hashlib.sha256(
                            preferred_payload["text"].encode("utf-8")
                        ).hexdigest(),
                        "observation_time_tied": time_tied,
                        "reason": (
                            "A different saved version from another selected source wins "
                            "by latest observation time, then source ID for ties. "
                            "Payload differences may be text or acquisition metadata; they "
                            "are not independent corroboration. Omitted content is not "
                            "included in the case package."
                            + (
                                " Capture times are equal: source ID is a selection convention, "
                                "not proof of the newest platform edit."
                                if time_tied
                                else ""
                            )
                        ),
                    }
                )
            summaries = []
            for row in rows:
                included = membership[row["id"]]
                content_scopes = {}
                for key in included:
                    scope = (items[key].get("provenance") or {}).get("content_scope", "unknown")
                    content_scopes[scope] = content_scopes.get(scope, 0) + 1
                summaries.append(
                    {
                        **_source_summary(row),
                        "included_count": len(included),
                        "omitted_version_count": len(omissions[row["id"]]),
                        "version_omissions": omissions[row["id"]],
                        "content_scopes": content_scopes,
                        "unknown_publication_times": sum(
                            items[key].get("published_at") is None for key in included
                        ),
                    }
                )
        return {
            "version_policy": "latest_selected_observation_then_source_id",
            "observations_fingerprint": observation_digest.hexdigest(),
            "observation_manifest": observation_manifest,
            "source_ids": selected,
            "sources": summaries,
            "items": list(items.values()),
            "total_items": total,
            "truncated": total > len(items),
        }

    def set_enabled(self, source_id: str, enabled: bool, *, now=None) -> None:
        with self.connection(write=True) as connection:
            if not connection.execute(
                "UPDATE collection_sources SET enabled=?,next_due_at="
                "CASE WHEN retry_after_at>? THEN retry_after_at ELSE ? END WHERE id=?",
                (int(enabled), instant(now).isoformat(), instant(now).isoformat(), source_id),
            ).rowcount:
                raise ValueError("This live source is no longer available.")

    @staticmethod
    def _queue(connection, source_id: str, scheduled: bool, stamp: str) -> str:
        from .ownership import assert_collection_owner_available

        row = connection.execute(
            "SELECT * FROM collection_sources WHERE id=?", (source_id,)
        ).fetchone()
        if row is None:
            raise ValueError("This live source is no longer available.")
        config = json.loads(row["config"])
        assert_collection_owner_available(connection, config["platform"], config["locator"])
        if row["retry_after_at"] and row["retry_after_at"] > stamp:
            raise CollectionFailure(
                "This source is rate limited. Retry after " + row["retry_after_at"] + "."
            )
        active = connection.execute(
            "SELECT id FROM collection_jobs WHERE source_id=? AND status IN ('queued','running')",
            (source_id,),
        ).fetchone()
        if active:
            if not scheduled:
                connection.execute(
                    "UPDATE collection_jobs SET scheduled=0 WHERE id=? AND status='queued'",
                    (active[0],),
                )
            return active[0]
        job_id = str(uuid4())
        connection.execute(
            "INSERT INTO collection_jobs(id,source_id,config,status,scheduled,created_at) "
            "VALUES (?,?,?,'queued',?,?)",
            (job_id, source_id, row["config"], int(scheduled), stamp),
        )
        connection.execute(
            "UPDATE collection_sources SET last_job_id=?,last_error=NULL WHERE id=?",
            (job_id, source_id),
        )
        return job_id

    def enqueue(self, source_id: str, *, now=None) -> str:
        with self.connection(write=True) as connection:
            return self._queue(connection, source_id, False, instant(now).isoformat())

    def schedule(self, *, now=None, limit=20, source_ids: list[str] | None = None) -> int:
        stamp = instant(now).isoformat()
        if source_ids == []:
            return 0
        selected = (
            "AND id IN (" + ",".join("?" for _ in source_ids) + ") "
            if source_ids is not None
            else ""
        )
        parameters = [stamp, *(source_ids or []), limit]
        with self.connection(write=True) as connection:
            due = connection.execute(
                "SELECT id FROM collection_sources WHERE enabled=1 AND next_due_at<=? "
                "AND (retry_after_at IS NULL OR retry_after_at<=?) "
                + selected
                + "AND NOT EXISTS(SELECT 1 FROM collection_jobs j "
                "WHERE j.source_id=collection_sources.id "
                "AND j.status IN ('queued','running')) ORDER BY next_due_at,id LIMIT ?",
                [stamp, *parameters],
            ).fetchall()
            queued = 0
            for row in due:
                from .ownership import _feed_owner

                source = json.loads(
                    connection.execute(
                        "SELECT config FROM collection_sources WHERE id=?", (row[0],)
                    ).fetchone()[0]
                )
                if _feed_owner(connection, source["platform"], source["locator"]):
                    continue
                self._queue(connection, row[0], True, stamp)
                queued += 1
        return queued

    def recover(self) -> None:
        """Called only while holding the shared worker lock after process startup."""
        with self.connection(write=True) as connection:
            connection.execute("UPDATE collection_jobs SET status='queued' WHERE status='running'")

    def claim(self, *, now=None) -> dict | None:
        stamp = instant(now).isoformat()
        with self.connection(write=True) as connection:
            connection.execute(
                "UPDATE collection_jobs SET status='cancelled',finished_at=? WHERE status='queued' "
                "AND scheduled=1 AND source_id IN "
                "(SELECT id FROM collection_sources WHERE enabled=0)",
                (stamp,),
            )
            row = connection.execute(
                "SELECT * FROM collection_jobs WHERE status='queued' ORDER BY created_at,id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE collection_jobs SET status='running',attempt=attempt+1,started_at=? "
                "WHERE id=?",
                (stamp, row["id"]),
            )
        return {**dict(row), "status": "running", "attempt": row["attempt"] + 1}

    def complete(self, job: dict, batch: CollectedBatch, *, now=None) -> dict:
        stamp = instant(now)
        counts = {"new": 0, "updated": 0, "unchanged": 0, "warnings": list(batch.warnings)}
        source = CollectionSource.model_validate_json(job["config"])
        if len(batch.items) > source.item_limit:
            raise ValueError("Collector returned more items than requested.")
        items = [SourceItem.model_validate(item) for item in batch.items]
        if any(item.platform != source.platform for item in items):
            raise CollectionFailure("The collector returned records for a different platform.")
        identities = {(item.platform, item.channel_id, item.native_id) for item in items}
        if len(identities) != len(items):
            raise CollectionFailure("The collector returned duplicate source identities.")
        with self.connection(write=True) as connection:
            self._require_attempt(connection, job)
            previous_capture = connection.execute(
                "SELECT last_collected_at FROM collection_sources WHERE id=?", (source.id,)
            ).fetchone()[0]
            if previous_capture is not None and stamp.isoformat() < observation_time(
                previous_capture
            ):
                raise CollectionFailure(
                    "This collection completion predates a saved source observation. "
                    "The newer saved evidence was preserved."
                )
            for item in items:
                payload = item.model_dump(mode="json")
                key = hashlib.sha256(
                    encode([item.platform, item.channel_id, item.native_id]).encode()
                ).hexdigest()
                semantic = {key: value for key, value in payload.items() if key != "acquired_at"}
                digest = hashlib.sha256(encode(semantic).encode()).hexdigest()
                previous_observation = connection.execute(
                    "SELECT last_seen_at FROM collection_source_items "
                    "WHERE source_id=? AND item_key=? AND payload IS NOT NULL",
                    (source.id, key),
                ).fetchone()
                if previous_observation is not None and stamp.isoformat() < observation_time(
                    previous_observation[0]
                ):
                    raise CollectionFailure(
                        "This collection completion predates a saved source observation. "
                        "The newer saved evidence was preserved."
                    )
                old = connection.execute(
                    "SELECT semantic_hash FROM collection_items WHERE item_key=?", (key,)
                ).fetchone()
                state = "new" if old is None else "unchanged" if old[0] == digest else "updated"
                counts[state] += 1
                if state == "unchanged":
                    connection.execute(
                        "UPDATE collection_items SET last_seen_at=? WHERE item_key=?",
                        (stamp.isoformat(), key),
                    )
                else:
                    connection.execute(
                        "INSERT INTO collection_items VALUES (?,?,?,?,?) "
                        "ON CONFLICT(item_key) DO UPDATE SET payload=excluded.payload,"
                        "semantic_hash=excluded.semantic_hash,last_seen_at=excluded.last_seen_at",
                        (key, encode(payload), digest, stamp.isoformat(), stamp.isoformat()),
                    )
                connection.execute(
                    "INSERT INTO collection_source_items "
                    "(source_id,item_key,payload,semantic_hash,first_seen_at,last_seen_at) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(source_id,item_key) DO UPDATE SET "
                    "payload=CASE WHEN semantic_hash=excluded.semantic_hash THEN payload "
                    "ELSE excluded.payload END,semantic_hash=excluded.semantic_hash,"
                    "first_seen_at=COALESCE(first_seen_at,excluded.first_seen_at),"
                    "last_seen_at=excluded.last_seen_at",
                    (source.id, key, encode(payload), digest, stamp.isoformat(), stamp.isoformat()),
                )
            current = json.loads(
                connection.execute(
                    "SELECT config FROM collection_sources WHERE id=?", (source.id,)
                ).fetchone()[0]
            )
            if batch.cursor is not None:
                current["cursor"] = batch.cursor
            connection.execute(
                "UPDATE collection_jobs SET status='succeeded',finished_at=?,result=?,error=NULL "
                "WHERE id=?",
                (stamp.isoformat(), encode(counts), job["id"]),
            )
            connection.execute(
                "UPDATE collection_sources SET config=?,last_collected_at=?,"
                "last_status='succeeded',"
                "last_error=?,failures=0,next_due_at=?,retry_after_at=NULL WHERE id=?",
                (
                    encode(current),
                    stamp.isoformat(),
                    "; ".join(batch.warnings)[:1000] or None,
                    (stamp + timedelta(minutes=source.interval_minutes)).isoformat(),
                    source.id,
                ),
            )
        return counts

    @staticmethod
    def _require_attempt(connection, job):
        row = connection.execute(
            "SELECT status,attempt FROM collection_jobs WHERE id=?", (job["id"],)
        ).fetchone()
        if row is None or row["status"] != "running" or row["attempt"] != job["attempt"]:
            raise ValueError("This collection attempt was superseded.")

    def fail(self, job: dict, message: str, *, retry_after_seconds=None, now=None) -> None:
        stamp = instant(now)
        source = CollectionSource.model_validate_json(job["config"])
        with self.connection(write=True) as connection:
            self._require_attempt(connection, job)
            failures = (
                connection.execute(
                    "SELECT failures FROM collection_sources WHERE id=?", (source.id,)
                ).fetchone()[0]
                + 1
            )
            delay = min(source.interval_minutes * 60 * 2 ** min(failures - 1, 7), 86400)
            delay = max(delay, min(retry_after_seconds or 0, 86400))
            connection.execute(
                "UPDATE collection_jobs SET status='failed',finished_at=?,error=? WHERE id=?",
                (stamp.isoformat(), message[:1000], job["id"]),
            )
            connection.execute(
                "UPDATE collection_sources SET last_status='failed',last_error=?,failures=?,"
                "next_due_at=?,retry_after_at=? WHERE id=?",
                (
                    message[:1000],
                    failures,
                    (stamp + timedelta(seconds=delay)).isoformat(),
                    (stamp + timedelta(seconds=min(retry_after_seconds, 86400))).isoformat()
                    if retry_after_seconds
                    else None,
                    source.id,
                ),
            )

    def jobs(self, limit=20) -> list[dict]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id,source_id,status,created_at,started_at,finished_at,error,result "
                "FROM collection_jobs "
                "ORDER BY created_at DESC,id LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {**dict(row), "result": json.loads(row["result"]) if row["result"] else None}
            for row in rows
        ]

    def items(self, limit=2000, *, source_id: str | None = None) -> list[dict]:
        limit = min(max(int(limit), 1), 2000)
        with self.connection() as connection:
            connection.execute("BEGIN")
            if source_id is None:
                rows = connection.execute(
                    "SELECT payload FROM collection_items "
                    "ORDER BY last_seen_at DESC,item_key LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                for row in connection.execute(
                    "SELECT first_seen_at,last_seen_at FROM collection_source_items "
                    "WHERE source_id=? AND payload IS NOT NULL",
                    (source_id,),
                ):
                    if observation_time(row[0]) > observation_time(row[1]):
                        raise CollectionFailure(
                            "A saved source observation has inconsistent capture times."
                        )
                rows = connection.execute(
                    "SELECT payload FROM collection_source_items "
                    "WHERE source_id=? AND payload IS NOT NULL "
                    "ORDER BY observation_time(last_seen_at) DESC,item_key LIMIT ?",
                    (source_id, limit),
                ).fetchall()
        return [json.loads(row[0]) for row in rows]
