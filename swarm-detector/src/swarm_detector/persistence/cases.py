"""Paged case/source queries; never decode dataset or analysis payloads."""

from __future__ import annotations

import sqlite3

from ..models import AccountActivity, Case, Channel, Message, Page, Post
from .database import Database
from .paging import DEFAULT_PAGE_SIZE, page_bounds

_CASE_SELECT = """SELECT c.payload,
    COALESCE((SELECT json_extract(r.payload,'$.decision') FROM reviews r
        WHERE r.run_id=c.run_id AND r.case_id=c.case_id
        ORDER BY r.revision DESC LIMIT 1),'Not reviewed') AS review_status,
    (SELECT COUNT(*) FROM case_accounts a WHERE a.run_id=c.run_id
        AND a.case_id=c.case_id AND a.is_core=1) AS account_count
    FROM cases c"""


def _case(row: sqlite3.Row) -> Case:
    return Case.model_validate_json(row["payload"]).model_copy(
        update={"review_status": row["review_status"], "account_count": row["account_count"]}
    )


class CaseRepository:
    def __init__(self, database: Database):
        self.database = database

    def get(self, run_id: str, case_id: str) -> Case | None:
        with self.database.connection() as conn:
            row = conn.execute(
                _CASE_SELECT + " WHERE c.run_id=? AND c.case_id=?", (run_id, case_id)
            ).fetchone()
        return _case(row) if row else None

    def page(
        self,
        run_id: str,
        query: str = "",
        kind: str = "all",
        status: str = "all",
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> Page[Case]:
        limit, offset = page_bounds(limit, offset)
        where = "c.run_id=?"
        parameters: list = [run_id]
        if query:
            where += " AND instr(c.search_text,?)>0"
            parameters.append(query.casefold())
        if kind not in ("all", "", None):
            where += " AND c.kind=?"
            parameters.append(kind)
        if status not in ("all", "", None):
            # The latest review is selected once per case; revisions remain append-only.
            comparison = "=" if status in ("unreviewed", "Not reviewed") else "<>"
            if status in ("unreviewed", "Not reviewed", "reviewed"):
                where += f" AND review_status {comparison} 'Not reviewed'"
            else:
                where += " AND review_status=?"
                parameters.append(status)
        selection = _CASE_SELECT + " WHERE " + where
        with self.database.connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM (" + selection + ")", parameters).fetchone()[
                0
            ]
            rows = conn.execute(
                selection + " ORDER BY c.ordinal,c.case_id LIMIT ? OFFSET ?",
                [*parameters, limit, offset],
            )
            return Page[Case](
                items=tuple(_case(row) for row in rows), total=total, limit=limit, offset=offset
            )

    def messages(
        self,
        run_id: str,
        case_id: str,
        dataset_id: str,
        *,
        context: bool = False,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> Page[Message]:
        limit, offset = page_bounds(limit, offset)
        if context:
            scope = """FROM messages m JOIN case_posts c ON c.post_key=m.post_key
                WHERE c.run_id=? AND c.case_id=? AND m.dataset_id=?"""
        else:
            scope = """FROM messages m JOIN case_accounts c ON c.account_key=m.account_key
                WHERE c.run_id=? AND c.case_id=? AND m.dataset_id=? AND c.is_core=1"""
        parameters = (run_id, case_id, dataset_id)
        with self.database.connection() as conn:
            total = conn.execute("SELECT COUNT(*) " + scope, parameters).fetchone()[0]
            rows = conn.execute(
                "SELECT m.payload "
                + scope
                + " ORDER BY m.timestamp_utc,m.message_key LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            )
            return Page[Message](
                items=tuple(Message.model_validate_json(row[0]) for row in rows),
                total=total,
                limit=limit,
                offset=offset,
            )

    def posts(
        self,
        run_id: str,
        case_id: str,
        dataset_id: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> Page[Post]:
        limit, offset = page_bounds(limit, offset)
        scope = """FROM posts p JOIN case_posts c ON c.post_key=p.post_key
            WHERE c.run_id=? AND c.case_id=? AND p.dataset_id=?"""
        parameters = (run_id, case_id, dataset_id)
        with self.database.connection() as conn:
            total = conn.execute("SELECT COUNT(*) " + scope, parameters).fetchone()[0]
            rows = conn.execute(
                "SELECT p.payload "
                + scope
                + " ORDER BY p.timestamp_utc,p.post_key LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            )
            return Page[Post](
                items=tuple(Post.model_validate_json(row[0]) for row in rows),
                total=total,
                limit=limit,
                offset=offset,
            )

    def thread_participation_counts(
        self, run_id: str, case_id: str, dataset_id: str
    ) -> tuple[int, int]:
        """Count core/all comments in declared evidence threads only.

        Projected ``declared=0`` posts preserve broader account context without
        changing the denominator of participation in the selected case threads.
        """
        with self.database.connection() as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(a.account_key IS NOT NULL),0),COUNT(*) FROM messages m
                JOIN case_posts p ON p.post_key=m.post_key
                LEFT JOIN case_accounts a ON a.run_id=p.run_id AND a.case_id=p.case_id
                    AND a.account_key=m.account_key AND a.is_core=1
                WHERE p.run_id=? AND p.case_id=? AND p.declared=1 AND m.dataset_id=?""",
                (run_id, case_id, dataset_id),
            ).fetchone()
        return row[0], row[1]

    def channels(
        self,
        run_id: str,
        case_id: str,
        dataset_id: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> Page[Channel]:
        limit, offset = page_bounds(limit, offset)
        scope = """FROM channels ch WHERE ch.dataset_id=? AND EXISTS (
            SELECT 1 FROM case_posts cp JOIN posts p ON p.post_key=cp.post_key
            WHERE cp.run_id=? AND cp.case_id=? AND p.dataset_id=ch.dataset_id
            AND p.channel_key=ch.channel_key)"""
        parameters = (dataset_id, run_id, case_id)
        with self.database.connection() as conn:
            total = conn.execute("SELECT COUNT(*) " + scope, parameters).fetchone()[0]
            rows = conn.execute(
                "SELECT ch.payload " + scope + " ORDER BY ch.ordinal LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            )
            return Page[Channel](
                items=tuple(Channel.model_validate_json(row[0]) for row in rows),
                total=total,
                limit=limit,
                offset=offset,
            )

    def accounts(
        self,
        run_id: str,
        case_id: str,
        dataset_id: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> Page[AccountActivity]:
        limit, offset = page_bounds(limit, offset)
        with self.database.connection() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM case_accounts WHERE run_id=? AND case_id=?",
                (run_id, case_id),
            ).fetchone()[0]
            rows = conn.execute(
                """SELECT a.account_key,a.is_core AS core,
                (SELECT COUNT(*) FROM messages m WHERE m.dataset_id=?
                    AND m.account_key=a.account_key) AS comment_count
                FROM case_accounts a WHERE a.run_id=? AND a.case_id=?
                ORDER BY a.is_core DESC,a.account_key LIMIT ? OFFSET ?""",
                (dataset_id, run_id, case_id, limit, offset),
            )
            return Page[AccountActivity](
                items=tuple(AccountActivity.model_validate(dict(row)) for row in rows),
                total=total,
                limit=limit,
                offset=offset,
            )

    def has_public_call(self, run_id: str, case_id: str, dataset_id: str) -> bool:
        with self.database.connection() as conn:
            return (
                conn.execute(
                    """SELECT EXISTS(SELECT 1 FROM case_posts c JOIN posts p ON p.post_key=c.post_key
                WHERE c.run_id=? AND c.case_id=? AND p.dataset_id=?
                AND instr(json_extract(p.payload,'$.text_original'),'PUBLIC CALL')>0)""",
                    (run_id, case_id, dataset_id),
                ).fetchone()[0]
                == 1
            )

    def validate_evidence_refs(
        self, run_id: str, case_id: str, dataset_id: str, refs: tuple[str, ...]
    ) -> None:
        with self.database.connection() as conn:
            for ref in refs:
                row = conn.execute(
                    """SELECT 1 FROM messages m JOIN case_accounts a ON a.account_key=m.account_key
                    WHERE a.run_id=? AND a.case_id=? AND a.is_core=1 AND m.dataset_id=?
                    AND m.message_key=? LIMIT 1""",
                    (run_id, case_id, dataset_id, ref),
                ).fetchone()
                if row is None:
                    raise ValueError("Narrative evidence must refer to comments in this case.")
