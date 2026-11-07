"""Durable feed settings and atomic scheduling on the existing SQLite queue."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from ..persistence.connection import ConnectionFactory
from .contracts import FeedSubscriptionInput, FeedSubscriptionRecord, FeedTickResult
from .ownership import assert_feed_owner_available


def _instant(value: datetime | None = None) -> datetime:
    stamp = value or datetime.now(UTC)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("Scheduler timestamps must include a timezone.")
    return stamp.astimezone(UTC)


def _saved_time(value: str | None, default: datetime) -> datetime:
    try:
        return _instant(datetime.fromisoformat(value)) if value else default
    except ValueError:
        return default


def _has_table(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='feed_subscriptions'"
        ).fetchone()
        is not None
    )


def _record(row: sqlite3.Row) -> FeedSubscriptionRecord:
    values = dict(row)
    values.pop("processed_job_id", None)
    values.pop("refresh_requested", None)
    values["analyze"] = bool(values["analyze"])
    values["enabled"] = bool(values["enabled"])
    values["last_result"] = json.loads(values["last_result"]) if values["last_result"] else None
    return FeedSubscriptionRecord.model_validate(values)


class FeedRepository:
    def __init__(self, connection: ConnectionFactory):
        self._connection = connection

    def list_page(self, limit: int = 30, offset: int = 0) -> list[FeedSubscriptionRecord]:
        with self._connection() as connection:
            if not _has_table(connection):
                return []
            return [
                _record(row)
                for row in connection.execute(
                    "SELECT f.*,j.status AS last_analysis_status,j.error AS last_analysis_error "
                    "FROM feed_subscriptions f LEFT JOIN jobs j ON j.id=f.last_analysis_job_id "
                    "ORDER BY f.id DESC LIMIT ? OFFSET ?",
                    (min(max(int(limit), 0), 200), max(int(offset), 0)),
                )
            ]

    def get(self, feed_id: int) -> FeedSubscriptionRecord | None:
        with self._connection() as connection:
            if not _has_table(connection):
                return None
            row = connection.execute(
                "SELECT f.*,j.status AS last_analysis_status,j.error AS last_analysis_error "
                "FROM feed_subscriptions f LEFT JOIN jobs j ON j.id=f.last_analysis_job_id "
                "WHERE f.id=?",
                (feed_id,),
            ).fetchone()
            return _record(row) if row else None

    def save(
        self,
        request: FeedSubscriptionInput,
        *,
        now: datetime | None = None,
    ) -> FeedSubscriptionRecord:
        request = FeedSubscriptionInput.model_validate(request)
        stamp = _instant(now).isoformat()
        values = {**request.model_dump(), "analyze": int(request.analyze), "stamp": stamp}
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM feed_subscriptions WHERE canonical_key=? OR url=? "
                "ORDER BY id LIMIT 1",
                (request.canonical_key, request.url),
            ).fetchone()
            assert_feed_owner_available(
                connection,
                request.platform,
                request.locator,
                existing_feed_id=previous["id"] if previous else None,
            )
            if previous:
                values["id"] = previous["id"]
                if previous["platform"] == request.platform == "rss":
                    values["url"] = previous["url"]
                    values["locator"] = previous["locator"] or previous["url"]
                connection.execute(
                    "UPDATE feed_subscriptions SET name=:name,url=:url,platform=:platform,"
                    "locator=:locator,canonical_key=:canonical_key,topic=:topic,"
                    "interval_minutes=:interval_minutes,item_limit=:item_limit,"
                    "content_mode=:content_mode,analyze=:analyze,updated_at=:stamp,"
                    "next_refresh_at=CASE WHEN enabled=1 THEN :stamp ELSE next_refresh_at END,"
                    "refresh_requested=CASE WHEN enabled=1 THEN 1 ELSE 0 END WHERE id=:id",
                    values,
                )
            else:
                connection.execute(
                    "INSERT INTO feed_subscriptions(name,url,platform,locator,canonical_key,topic,"
                    "interval_minutes,item_limit,content_mode,analyze,next_refresh_at,"
                    "created_at,updated_at) "
                    "VALUES (:name,:url,:platform,:locator,:canonical_key,:topic,:interval_minutes,"
                    ":item_limit,:content_mode,:analyze,:stamp,:stamp,:stamp)",
                    values,
                )
            row = connection.execute(
                "SELECT * FROM feed_subscriptions WHERE canonical_key=?", (request.canonical_key,)
            ).fetchone()
            return _record(row)

    def set_enabled(
        self,
        feed_id: int,
        enabled: bool,
        *,
        now: datetime | None = None,
    ) -> FeedSubscriptionRecord:
        if not isinstance(enabled, bool):
            raise ValueError("Feed enabled state must be true or false.")
        stamp = _instant(now).isoformat()
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if enabled:
                source = connection.execute(
                    "SELECT * FROM feed_subscriptions WHERE id=?", (feed_id,)
                ).fetchone()
                if source is None:
                    raise ValueError(f"Saved feed {feed_id} does not exist.")
                assert_feed_owner_available(
                    connection,
                    source["platform"],
                    source["locator"] or source["url"],
                    existing_feed_id=feed_id,
                )
            cursor = connection.execute(
                "UPDATE feed_subscriptions SET enabled=?,updated_at=?,refresh_requested=?,"
                "next_refresh_at=CASE WHEN ?=1 THEN ? ELSE next_refresh_at END WHERE id=?",
                (int(enabled), stamp, int(enabled), int(enabled), stamp, feed_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Saved feed {feed_id} does not exist.")
            return _record(
                connection.execute(
                    "SELECT * FROM feed_subscriptions WHERE id=?", (feed_id,)
                ).fetchone()
            )

    @staticmethod
    def _reconcile(
        connection: sqlite3.Connection,
        stamp: datetime,
        *,
        limit: int = 100,
        feed_id: int | None = None,
    ) -> int:
        parameters: list = []
        specific = ""
        if feed_id is not None:
            specific = " AND f.id=?"
            parameters.append(feed_id)
        parameters.append(min(max(int(limit), 0), 100))
        rows = connection.execute(
            "SELECT f.*,j.status AS job_status,j.result AS job_result,j.error AS job_error,"
            "j.finished_at AS job_finished_at,j.updated_at AS job_updated_at "
            "FROM feed_subscriptions f JOIN jobs j ON j.id=f.last_job_id "
            "WHERE j.status IN ('succeeded','failed') "
            "AND f.processed_job_id IS NOT f.last_job_id"
            + specific
            + " ORDER BY COALESCE(j.finished_at,j.updated_at),f.id LIMIT ?",
            parameters,
        ).fetchall()
        for row in rows:
            result, invalid = None, False
            if row["job_result"]:
                try:
                    result = json.loads(row["job_result"])
                    if not isinstance(result, dict):
                        invalid = True
                        result = None
                except (TypeError, ValueError):
                    invalid = True
            failed = row["job_status"] == "failed" or invalid
            failures = row["consecutive_failures"] + 1 if failed else 0
            error = (
                ("Stored refresh result is invalid." if invalid else row["job_error"])
                if failed
                else None
            )
            if (
                not failed
                and result
                and isinstance(result.get("errors"), list)
                and result["errors"]
            ):
                errors = result["errors"]
                first = errors[0]
                detail = (
                    first.get("error", first.get("message", ""))
                    if isinstance(first, dict)
                    else str(first)
                )
                error = f"{len(errors)} article(s) could not be imported. {detail}"[:2000]
            minutes = (
                min(
                    row["interval_minutes"] * 2 ** min(failures - 1, 12),
                    max(row["interval_minutes"], 1440),
                )
                if failed
                else row["interval_minutes"]
            )
            if failed and result:
                retry = result.get("retry_after_seconds")
                if type(retry) is int and retry > 0:
                    minutes = max(minutes, min(retry, 86400) / 60)
            finished = _saved_time(row["job_finished_at"] or row["job_updated_at"], stamp)
            due = max(stamp, finished + timedelta(minutes=minutes))
            if row["refresh_requested"]:
                due = stamp
            analysis_id = result.get("analysis_job_id") if result else None
            if isinstance(analysis_id, bool) or not isinstance(analysis_id, int) or analysis_id < 1:
                analysis_id = None
            if (
                analysis_id is not None
                and not connection.execute(
                    "SELECT 1 FROM jobs WHERE id=?", (analysis_id,)
                ).fetchone()
            ):
                analysis_id = None
            if failed:
                analysis_id = row["last_analysis_job_id"]
            refreshed = row["last_refreshed_at"]
            if not failed and not (result and result.get("skipped")):
                refreshed = finished.isoformat()
            connection.execute(
                "UPDATE feed_subscriptions SET processed_job_id=last_job_id,last_finished_at=?,"
                "last_refreshed_at=?,last_result=?,last_analysis_job_id=?,last_error=?,"
                "consecutive_failures=?,next_refresh_at=?,updated_at=? WHERE id=?",
                (
                    finished.isoformat(),
                    refreshed,
                    json.dumps(result, allow_nan=False) if result is not None else None,
                    analysis_id,
                    error,
                    failures,
                    due.isoformat(),
                    stamp.isoformat(),
                    row["id"],
                ),
            )
        return len(rows)

    def reconcile(self, *, now: datetime | None = None, limit: int = 100) -> int:
        stamp = _instant(now)
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._reconcile(connection, stamp, limit=limit)

    @staticmethod
    def _active_job(connection: sqlite3.Connection, row: sqlite3.Row) -> int | None:
        existing = connection.execute(
            "SELECT id FROM jobs WHERE status IN ('queued','running') "
            "AND (id=? OR dedupe_key=?) ORDER BY id LIMIT 1",
            (row["last_job_id"], f"feed:{row['id']}:refresh"),
        ).fetchone()
        return existing["id"] if existing else None

    @staticmethod
    def _queue(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        stamp: datetime,
        *,
        scheduled: bool,
    ) -> int:
        assert_feed_owner_available(
            connection,
            row["platform"],
            row["locator"] or row["url"],
            existing_feed_id=row["id"],
        )
        active = FeedRepository._active_job(connection, row)
        if active is not None:
            if not scheduled:
                queued = connection.execute(
                    "SELECT payload FROM jobs WHERE id=? AND kind='ingest' AND status='queued'",
                    (active,),
                ).fetchone()
                if queued:
                    try:
                        payload = json.loads(queued["payload"])
                    except (TypeError, ValueError):
                        payload = None
                    if isinstance(payload, dict) and payload.get("scheduled") is True:
                        payload["scheduled"] = False
                        connection.execute(
                            "UPDATE jobs SET payload=?,updated_at=? WHERE id=?",
                            (json.dumps(payload, allow_nan=False), stamp.isoformat(), active),
                        )
            return active
        payload = {
            "source_type": "feed",
            "url": row["url"],
            "topic": row["topic"],
            "limit": row["item_limit"],
            "analyze": bool(row["analyze"]),
            "content_mode": row["content_mode"],
            "feed_id": row["id"],
            "scheduled": scheduled,
            "platform": row["platform"],
            "locator": row["locator"] or row["url"],
            "cursor": row["cursor"],
        }
        # This deliberately uses the existing jobs schema inside the SAME transaction
        # as advancing the feed. Calling JobQueue.enqueue_job would open a second
        # connection and lose atomicity (or deadlock against our write transaction).
        cursor = connection.execute(
            "INSERT INTO jobs(kind,payload,dedupe_key,created_at,updated_at) "
            "VALUES ('ingest',?,?,?,?)",
            (
                json.dumps(payload, allow_nan=False),
                f"feed:{row['id']}:refresh",
                stamp.isoformat(),
                stamp.isoformat(),
            ),
        )
        job_id = int(cursor.lastrowid)
        connection.execute(
            "UPDATE feed_subscriptions SET last_job_id=?,last_queued_at=?,next_refresh_at=?,"
            "refresh_requested=0,updated_at=? WHERE id=?",
            (
                job_id,
                stamp.isoformat(),
                (stamp + timedelta(minutes=row["interval_minutes"])).isoformat(),
                stamp.isoformat(),
                row["id"],
            ),
        )
        return job_id

    def update_cursor(self, feed_id: int, cursor: str) -> None:
        if not isinstance(cursor, str) or len(cursor) > 256:
            raise ValueError("Source cursor must contain at most 256 characters.")
        with self._connection(write=True) as connection:
            connection.execute(
                "UPDATE feed_subscriptions SET cursor=? WHERE id=?", (cursor, feed_id)
            )

    def queue_one(self, feed_id: int, *, now: datetime | None = None) -> int:
        stamp = _instant(now)
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._reconcile(connection, stamp, feed_id=feed_id)
            row = connection.execute(
                "SELECT * FROM feed_subscriptions WHERE id=?", (feed_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Saved feed {feed_id} does not exist.")
            return self._queue(connection, row, stamp, scheduled=False)

    def schedule_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 20,
    ) -> FeedTickResult:
        stamp = _instant(now)
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            reconciled = self._reconcile(connection, stamp)
            rows = connection.execute(
                "SELECT f.* FROM feed_subscriptions f WHERE f.enabled=1 AND f.next_refresh_at<=? "
                "AND (f.last_job_id IS NULL OR f.processed_job_id=f.last_job_id) "
                "AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.status IN ('queued','running') "
                "AND (j.id=f.last_job_id OR j.dedupe_key='feed:'||f.id||':refresh')) "
                "ORDER BY f.next_refresh_at,f.id LIMIT ?",
                (stamp.isoformat(), min(max(int(limit), 0), 20)),
            ).fetchall()
            queued = [self._queue(connection, row, stamp, scheduled=True) for row in rows]
            return FeedTickResult(reconciled=reconciled, queued_job_ids=queued)

    def queue_due(self, *, now: datetime | None = None, limit: int = 20) -> list[int]:
        return self.schedule_due(now=now, limit=limit).queued_job_ids
