"""Paged pair evidence, case-level aggregates, shared content, and literal search."""

from __future__ import annotations

from ..models import (
    CaseEvent,
    ContentMatches,
    Message,
    Page,
    Pair,
    PairEvent,
    SharedLink,
    SharedText,
)
from .database import Database
from .paging import DEFAULT_PAGE_SIZE, page_bounds

_PAIR_SCOPE = """FROM pairs p
    JOIN case_accounts a ON a.run_id=p.run_id AND a.account_key=p.account_a AND a.is_core=1
    JOIN case_accounts b ON b.run_id=p.run_id AND b.case_id=a.case_id
        AND b.account_key=p.account_b AND b.is_core=1
    WHERE p.run_id=? AND a.case_id=?"""


class EvidenceRepository:
    def __init__(self, database: Database):
        self.database = database

    def pairs(
        self, run_id: str, case_id: str, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> Page[Pair]:
        limit, offset = page_bounds(limit, offset)
        with self.database.connection() as conn:
            total = conn.execute("SELECT COUNT(*) " + _PAIR_SCOPE, (run_id, case_id)).fetchone()[0]
            rows = conn.execute(
                "SELECT p.payload " + _PAIR_SCOPE + " ORDER BY p.ordinal LIMIT ? OFFSET ?",
                (run_id, case_id, limit, offset),
            )
            return Page[Pair](
                items=tuple(Pair.model_validate_json(row[0]) for row in rows),
                total=total,
                limit=limit,
                offset=offset,
            )

    def events(
        self, run_id: str, pair_key: str, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> Page[PairEvent]:
        limit, offset = page_bounds(limit, offset)
        with self.database.connection() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM pair_events WHERE run_id=? AND pair_key=?", (run_id, pair_key)
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT payload FROM pair_events WHERE run_id=? AND pair_key=? "
                "ORDER BY ordinal LIMIT ? OFFSET ?",
                (run_id, pair_key, limit, offset),
            )
            return Page[PairEvent](
                items=tuple(PairEvent.model_validate_json(row[0]) for row in rows),
                total=total,
                limit=limit,
                offset=offset,
            )

    def case_events(
        self,
        run_id: str,
        case_id: str,
        *,
        pair_limit: int = DEFAULT_PAGE_SIZE,
        pair_offset: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> Page[CaseEvent]:
        """One global event budget across the selected pair page, not per pair."""
        pair_limit, pair_offset = page_bounds(pair_limit, pair_offset)
        limit, offset = page_bounds(limit, offset)
        selected = (
            "SELECT p.pair_key,p.ordinal " + _PAIR_SCOPE + " ORDER BY p.ordinal LIMIT ? OFFSET ?"
        )
        scope = (
            "FROM pair_events e JOIN (" + selected + ") s ON s.pair_key=e.pair_key WHERE e.run_id=?"
        )
        parameters = (run_id, case_id, pair_limit, pair_offset, run_id)
        with self.database.connection() as conn:
            total = conn.execute("SELECT COUNT(*) " + scope, parameters).fetchone()[0]
            rows = conn.execute(
                "SELECT e.pair_key,e.payload "
                + scope
                + " ORDER BY s.ordinal,e.ordinal LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            )
            return Page[CaseEvent](
                items=tuple(
                    CaseEvent(pair_key=row[0], event=PairEvent.model_validate_json(row[1]))
                    for row in rows
                ),
                total=total,
                limit=limit,
                offset=offset,
            )

    def pair_in_case(self, run_id: str, case_id: str, pair_key: str) -> bool:
        with self.database.connection() as conn:
            return (
                conn.execute(
                    "SELECT 1 " + _PAIR_SCOPE + " AND p.pair_key=? LIMIT 1",
                    (run_id, case_id, pair_key),
                ).fetchone()
                is not None
            )

    def retained_count(self, run_id: str, case_id: str) -> int:
        with self.database.connection() as conn:
            return conn.execute(
                "SELECT COUNT(*) " + _PAIR_SCOPE + " AND p.retained=1", (run_id, case_id)
            ).fetchone()[0]

    def sensitivity(self, run_id: str, case_id: str, precision: float, minimum: int) -> list[dict]:
        result = []
        with self.database.connection() as conn:
            for window in (30, 120, 300):
                if window < precision:
                    continue
                row = conn.execute(
                    "SELECT COUNT(*),COALESCE(SUM(COALESCE(json_extract(p.payload,?),0)>=?),0) "
                    + _PAIR_SCOPE,
                    (f'$.sensitivity."{window}"', minimum, run_id, case_id),
                ).fetchone()
                result.append(
                    {
                        "window_seconds": window,
                        "pairs_with_repeated_support": row[1],
                        "pair_count": row[0],
                    }
                )
        return result

    def strongest_family(self, run_id: str, case_id: str) -> str | None:
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT json_extract(e.payload,'$.family_key') AS family,COUNT(*) AS weight "
                + _PAIR_SCOPE.replace(
                    "WHERE",
                    "JOIN pair_events e ON e.run_id=p.run_id AND e.pair_key=p.pair_key WHERE",
                    1,
                )
                + " AND p.retained=1 GROUP BY family ORDER BY weight DESC,family LIMIT 1",
                (run_id, case_id),
            ).fetchone()
        return row[0] if row else None

    def search(self, dataset_id: str, query: str, limit: int = 30) -> tuple[Message, ...]:
        limit, _ = page_bounds(limit, 0)
        tokens = query.strip().split()[:12]
        if not tokens:
            return ()
        fts_query = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        with self.database.connection() as conn:
            rows = conn.execute(
                """SELECT m.payload FROM messages_fts f JOIN messages m
                ON m.dataset_id=f.dataset_id AND m.message_key=f.message_key
                WHERE messages_fts MATCH ? AND f.dataset_id=? ORDER BY rank LIMIT ?""",
                (fts_query, dataset_id, limit),
            )
            return tuple(Message.model_validate_json(row[0]) for row in rows)

    def content_matches(self, run_id: str, case_id: str, dataset_id: str) -> ContentMatches:
        """Aggregate the full case in SQLite; cap displayed groups and references."""
        scope = """JOIN messages m ON m.dataset_id=x.dataset_id AND m.message_key=x.message_key
            JOIN case_accounts a ON a.account_key=m.account_key AND a.is_core=1
            WHERE a.run_id=? AND a.case_id=? AND x.dataset_id=?"""
        parameters = (run_id, case_id, dataset_id)
        links = []
        texts = []
        with self.database.connection() as conn:
            shared_urls = conn.execute(
                "SELECT x.url,COUNT(DISTINCT m.account_key) AS accounts,COUNT(*) AS messages "
                "FROM message_urls x " + scope + " GROUP BY x.url HAVING accounts>1 "
                "ORDER BY accounts DESC,x.url LIMIT 20",
                parameters,
            ).fetchall()
            for row in shared_urls:
                refs = conn.execute(
                    "SELECT m.message_key FROM message_urls x "
                    + scope
                    + " AND x.url=? ORDER BY m.timestamp_utc,m.message_key LIMIT 200",
                    (*parameters, row["url"]),
                ).fetchall()
                accounts = conn.execute(
                    "SELECT DISTINCT m.account_key FROM message_urls x "
                    + scope
                    + " AND x.url=? ORDER BY m.account_key LIMIT 500",
                    (*parameters, row["url"]),
                ).fetchall()
                count = conn.execute(
                    "SELECT COUNT(*) FROM message_urls WHERE dataset_id=? AND url=?",
                    (dataset_id, row["url"]),
                ).fetchone()[0]
                links.append(
                    SharedLink(
                        url=row["url"],
                        accounts=row["accounts"],
                        dataset_count=count,
                        account_keys=tuple(item[0] for item in accounts),
                        message_keys=tuple(item[0] for item in refs),
                        message_count=row["messages"],
                        truncated=row["messages"] > 200 or row["accounts"] > 500,
                    )
                )
            shared_texts = conn.execute(
                "SELECT x.normalized_text,COUNT(DISTINCT m.account_key) AS accounts,"
                "COUNT(*) AS messages,MIN(x.ordinal) AS first_ordinal FROM message_content x "
                + scope
                + " AND x.normalized_text<>'' GROUP BY x.normalized_text HAVING accounts>1 "
                "ORDER BY first_ordinal LIMIT 20",
                parameters,
            ).fetchall()
            for row in shared_texts:
                refs = conn.execute(
                    "SELECT m.message_key,m.text_original FROM message_content x "
                    + scope
                    + " AND x.normalized_text=? ORDER BY x.ordinal LIMIT 200",
                    (*parameters, row["normalized_text"]),
                ).fetchall()
                texts.append(
                    SharedText(
                        text=refs[0]["text_original"],
                        accounts=row["accounts"],
                        message_keys=tuple(item[0] for item in refs),
                        message_count=row["messages"],
                        truncated=row["messages"] > 200,
                    )
                )
        return ContentMatches(links=tuple(links), texts=tuple(texts))
