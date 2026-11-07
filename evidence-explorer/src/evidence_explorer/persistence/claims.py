"""Focused claims persistence with validated internal records."""

import json
import sqlite3
from typing import Any

from ..claim_excerpts import build_claim_excerpts
from ..claim_selection import CLAIMS_POLICY_VERSION
from ..grounding import validate_extractive_claims
from ..models import ClaimInput
from ..records import ClaimRecord, VerifiedClaimRecord
from .connection import ConnectionFactory


class ClaimRepository:
    def __init__(self, connection: ConnectionFactory):
        self._connection = connection

    # A missing pointer preserves custom/demo imports, except the deprecated
    # free-form Ollama v1 policy. A stale/malformed pointer never resurrects history.
    _ACTIVE_JOIN = (
        " LEFT JOIN metadata active ON active.key = 'claims:' || c.article_id || ':active' "
    )
    _POINTER_MATCH = (
        "CASE WHEN json_valid(active.value) THEN "
        "json_extract(active.value, '$.content_hash') = a.content_hash "
        "AND json_extract(active.value, '$.model_key') = c.model_key ELSE 0 END"
    )
    _ACTIVE_WHERE = (
        "(c.model_key NOT LIKE 'ollama:%@%:v1' AND (active.key IS NULL OR " + _POINTER_MATCH + "))"
    )

    @classmethod
    def _query(
        cls, article_id: int | None, limit: int | None, *, active_only: bool, verified: bool
    ) -> tuple[str, tuple[Any, ...]]:
        query = (
            "SELECT c.*, a.title AS article_title, a.publisher, a.url, a.is_sample, "
            "a.published_at, a.topic FROM claims c JOIN articles a ON a.id=c.article_id "
        )
        if active_only:
            query += cls._ACTIVE_JOIN
        if verified:
            query += " JOIN chunks p ON p.id=c.chunk_id AND p.article_id=c.article_id "
        conditions = []
        parameters: tuple[Any, ...] = ()
        if article_id is not None:
            conditions.append("c.article_id = ?")
            parameters = (article_id,)
        if active_only:
            conditions.append(cls._ACTIVE_WHERE)
        if verified:
            conditions.extend(
                ("length(c.quote)>0", "instr(p.text,c.quote)>0", "instr(a.text,c.quote)>0")
            )
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY c.article_id, c.id"
        if limit is not None:
            if not 1 <= limit <= 500:
                raise ValueError("Claim pages require a limit from 1 to 500.")
            query += " LIMIT ?"
            parameters += (limit,)
        return query, parameters

    def _verified_page(self, limit: int, *, active_only: bool) -> list[VerifiedClaimRecord]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Claim pages require a limit from 1 to 500.")
        query, parameters = self._query(None, limit, active_only=active_only, verified=True)
        with self._connection() as connection:
            return [
                VerifiedClaimRecord.model_validate(dict(row))
                for row in connection.execute(query, parameters)
            ]

    def verified_page(self, *, limit: int = 500) -> list[VerifiedClaimRecord]:
        """Historical quote matches; use verified_active_page for current displays."""
        return self._verified_page(limit, active_only=False)

    def verified_active_page(self, *, limit: int = 500) -> list[VerifiedClaimRecord]:
        """Current source-matched results without loading source bodies into Python."""
        return self._verified_page(limit, active_only=True)

    def count_active(self) -> int:
        with self._connection() as connection:
            return connection.execute(
                "SELECT count(*) FROM claims c JOIN articles a ON a.id=c.article_id "
                + self._ACTIVE_JOIN
                + " WHERE "
                + self._ACTIVE_WHERE
            ).fetchone()[0]

    def active_model_keys(self) -> set[str]:
        """Retain successful empty-extraction provenance in default exports."""
        with self._connection() as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT CASE WHEN json_valid(active.value) THEN "
                    "json_extract(active.value, '$.model_key') END AS model_key "
                    "FROM articles a JOIN metadata active "
                    "ON active.key = 'claims:' || a.id || ':active' "
                    "WHERE CASE WHEN json_valid(active.value) THEN "
                    "json_extract(active.value, '$.content_hash') = a.content_hash "
                    "AND json_extract(active.value, '$.model_key') NOT LIKE 'ollama:%@%:v1' "
                    "ELSE 0 END"
                )
                if isinstance(row[0], str) and row[0]
            }

    @staticmethod
    def _publish_active(
        connection: sqlite3.Connection, article_id: int, model_key: str, content_hash: str
    ) -> None:
        key = f"claims:{article_id}:active"
        value = {"model_key": model_key, "content_hash": content_hash}
        previous = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        if previous is not None:
            try:
                if json.loads(previous["value"]) == value:
                    return
            except (ValueError, TypeError):
                pass  # A successfully validated result can repair a malformed pointer.
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def activate_cached_result(
        self, article_id: int, model_key: str, *, expected_content_hash: str, cache_key: str
    ) -> None:
        """Select a completed cached result without rewriting its evidence or history."""
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            article = connection.execute(
                "SELECT content_hash FROM articles WHERE id = ?", (article_id,)
            ).fetchone()
            if article is None:
                raise ValueError(f"Article {article_id} does not exist.")
            if article["content_hash"] != expected_content_hash:
                raise ValueError("The article changed during extraction. Submit analysis again.")
            expected_key = f"claims:{article_id}:{article['content_hash']}:{model_key}"
            if cache_key != expected_key:
                raise ValueError("Claim completion marker does not match the current source/model.")
            completed = connection.execute(
                "SELECT value FROM metadata WHERE key = ?", (cache_key,)
            ).fetchone()
            try:
                is_complete = completed is not None and json.loads(completed["value"]) == "done"
            except (ValueError, TypeError):
                is_complete = False
            if not is_complete:
                raise ValueError("Cached analysis has no successful completion marker.")
            self._publish_active(connection, article_id, model_key, article["content_hash"])

    def replace_claims(
        self,
        article_id: int,
        claims: list[ClaimInput],
        model_key: str,
        *,
        expected_content_hash: str | None = None,
        cache_key: str | None = None,
        cache_diagnostics: dict[str, int] | None = None,
    ) -> int:
        """Replace one model's claims and optionally commit its completion marker.

        The expected hash prevents a delayed model response from being attached to
        an edited source. Empty valid extractions can still be marked complete.
        """
        rejected = (cache_diagnostics or {}).get("rejected_claims", 0)
        if type(rejected) is not int or rejected < 0:
            raise ValueError("Rejected claim count must be a nonnegative integer.")
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            article = connection.execute(
                "SELECT content_hash, text FROM articles WHERE id = ?", (article_id,)
            ).fetchone()
            if article is None:
                raise ValueError(f"Article {article_id} does not exist.")
            if (
                expected_content_hash is not None
                and expected_content_hash != article["content_hash"]
            ):
                raise ValueError("The article changed during extraction. Submit analysis again.")
            if cache_key is not None:
                expected_key = f"claims:{article_id}:{article['content_hash']}:{model_key}"
                if cache_key != expected_key:
                    raise ValueError(
                        "Claim completion marker does not match the current source/model."
                    )
            saved_chunks = [
                dict(row)
                for row in connection.execute(
                    "SELECT id, text, start_char, end_char FROM chunks "
                    "WHERE article_id = ? ORDER BY ordinal",
                    (article_id,),
                )
            ]
            chunks = {row["id"]: row["text"] for row in saved_chunks}
            if any(claim.chunk_id not in chunks for claim in claims):
                raise ValueError("Every claim must refer to a chunk from its own article.")
            if any(claim.quote not in chunks[claim.chunk_id] for claim in claims):
                raise ValueError("Every claim quote must occur verbatim in its supporting passage.")
            if model_key.endswith(":" + CLAIMS_POLICY_VERSION):
                claims = validate_extractive_claims(
                    claims,
                    saved_chunks,
                    candidates=build_claim_excerpts({"text": article["text"]}, saved_chunks),
                )
            connection.execute(
                "DELETE FROM claims WHERE article_id = ? AND model_key = ?", (article_id, model_key)
            )
            connection.executemany(
                "INSERT INTO claims(article_id, chunk_id, claim_text, quote, subject, "
                "attribution, event_time, location, model_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        article_id,
                        item.chunk_id,
                        item.claim_text,
                        item.quote,
                        item.subject,
                        item.attribution,
                        item.event_time,
                        item.location,
                        model_key,
                    )
                    for item in claims
                ],
            )
            if cache_key is not None:
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (cache_key, json.dumps("done")),
                )
                # Publishing the active result and its completion marker belongs
                # to the same transaction as all claim inserts/deletes. An empty
                # successful result intentionally hides earlier model outputs.
                self._publish_active(connection, article_id, model_key, article["content_hash"])
                if cache_diagnostics is not None:
                    connection.execute(
                        "INSERT INTO metadata(key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (f"{cache_key}:quality", json.dumps({"rejected_claims": rejected})),
                    )
        return len(claims)

    def _list_claims(
        self, article_id: int | None, limit: int | None, *, active_only: bool
    ) -> list[ClaimRecord]:
        query, parameters = self._query(article_id, limit, active_only=active_only, verified=False)
        with self._connection() as connection:
            return [
                ClaimRecord.model_validate(dict(row))
                for row in connection.execute(query, parameters)
            ]

    def list_claims(
        self, article_id: int | None = None, *, limit: int | None = None
    ) -> list[ClaimRecord]:
        """Read retained model history; this does not choose an active extractor."""
        return self._list_claims(article_id, limit, active_only=False)

    def list_active_claims(
        self, article_id: int | None = None, *, limit: int | None = None
    ) -> list[ClaimRecord]:
        """Current successful results, or all legacy rows if no pointer exists."""
        return self._list_claims(article_id, limit, active_only=True)
