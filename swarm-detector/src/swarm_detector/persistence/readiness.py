"""Readiness aggregates without decoding any corpus or full run payload."""

from __future__ import annotations

from ..readiness import ReadinessFacts, normalized_time
from .database import Database


class ReadinessRepository:
    def __init__(self, database: Database):
        self.database = database

    def facts(self, dataset_id: str, run_id: str | None) -> ReadinessFacts:
        with self.database.connection() as conn:
            counts = conn.execute(
                "SELECT post_count,message_count,channel_count FROM dataset_info WHERE dataset_id=?",
                (dataset_id,),
            ).fetchone()
            if counts is None:
                raise ValueError("Dataset was not found.")
            identities = conn.execute(
                """SELECT COUNT(DISTINCT CASE WHEN account_key IS NOT NULL AND trim(account_key)<>''
                    AND COALESCE(json_extract(payload,'$.sender_type'),'user')='user'
                    THEN account_key END), COALESCE(SUM(account_key IS NULL OR trim(account_key)=''
                    OR COALESCE(json_extract(payload,'$.sender_type'),'user')<>'user'),0)
                FROM messages WHERE dataset_id=?""",
                (dataset_id,),
            ).fetchone()
            timeline = (
                "SELECT timestamp_utc FROM posts WHERE dataset_id=? UNION ALL "
                "SELECT timestamp_utc FROM messages WHERE dataset_id=?"
            )
            dates = conn.execute(
                "SELECT COUNT(*),COALESCE(SUM(julianday(timestamp_utc) IS NULL),0) FROM ("
                + timeline
                + ")",
                (dataset_id, dataset_id),
            ).fetchone()
            start = conn.execute(
                "SELECT timestamp_utc FROM (" + timeline + ") "
                "WHERE julianday(timestamp_utc) IS NOT NULL "
                "ORDER BY julianday(timestamp_utc) LIMIT 1",
                (dataset_id, dataset_id),
            ).fetchone()
            end = conn.execute(
                "SELECT timestamp_utc FROM (" + timeline + ") "
                "WHERE julianday(timestamp_utc) IS NOT NULL "
                "ORDER BY julianday(timestamp_utc) DESC LIMIT 1",
                (dataset_id, dataset_id),
            ).fetchone()
            invalid_posts = conn.execute(
                """SELECT COUNT(*) FROM posts p LEFT JOIN channels c
                ON c.dataset_id=p.dataset_id AND c.channel_key=p.channel_key
                WHERE p.dataset_id=? AND c.channel_key IS NULL""",
                (dataset_id,),
            ).fetchone()[0]
            mapping = conn.execute(
                """SELECT COALESCE(SUM(p.post_key IS NULL OR m.channel_key<>p.channel_key),0),
                COALESCE(SUM(p.post_key IS NOT NULL AND m.channel_key=p.channel_key
                    AND julianday(m.timestamp_utc)<julianday(p.timestamp_utc)),0)
                FROM messages m LEFT JOIN posts p ON p.dataset_id=m.dataset_id AND p.post_key=m.post_key
                WHERE m.dataset_id=?""",
                (dataset_id,),
            ).fetchone()
            missing_refs = conn.execute(
                "SELECT COUNT(*) FROM (SELECT payload FROM posts WHERE dataset_id=? "
                "UNION ALL SELECT payload FROM messages WHERE dataset_id=?) "
                "WHERE trim(COALESCE(json_extract(payload,'$.source_ref'),''))=''",
                (dataset_id, dataset_id),
            ).fetchone()[0]
            events = conn.execute(
                """SELECT COUNT(*),COALESCE(SUM(m.message_key IS NULL OR m.post_key<>e.post_key),0)
                FROM (SELECT json_extract(payload,'$.message_a') AS message_key,
                    json_extract(payload,'$.post_key') AS post_key FROM pair_events WHERE run_id=?
                UNION ALL SELECT json_extract(payload,'$.message_b'),json_extract(payload,'$.post_key')
                    FROM pair_events WHERE run_id=?) e
                LEFT JOIN messages m ON m.dataset_id=? AND m.message_key=e.message_key""",
                (run_id, run_id, dataset_id),
            ).fetchone()
        return ReadinessFacts(
            post_count=counts[0],
            message_count=counts[1],
            channel_count=counts[2],
            account_count=identities[0],
            unknown_accounts=identities[1],
            valid_timestamps=dates[0] - dates[1],
            invalid_timestamps=dates[1],
            observation_start=normalized_time(start[0]) if start else None,
            observation_end=normalized_time(end[0]) if end else None,
            invalid_posts=invalid_posts,
            invalid_links=mapping[0],
            predating=mapping[1],
            duplicate_keys=False,
            missing_source_refs=missing_refs,
            checked_event_refs=events[0],
            unresolved_event_refs=events[1],
        )
