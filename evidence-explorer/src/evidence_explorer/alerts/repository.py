"""Read-only access to labeled analytics in PostgreSQL (``analytics_*`` tables)."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

DEFAULT_DSN = "postgresql://localhost:5432/disinfo"
REQUIRED_TABLES = ("analytics_content_labels", "content_items", "cases")


def analytics_dsn() -> str:
    return os.getenv("TRACED_ANALYTICS_DATABASE_URL", DEFAULT_DSN)


class AnalyticsUnavailable(RuntimeError):
    """The analytics database or its labeled tables cannot be read."""


@dataclass
class CaseData:
    case: dict[str, Any]
    total_posts: int
    labels: list[dict[str, Any]]
    references: dict[str, dict[str, Any]]
    last_run: dict[str, Any] | None
    bursts: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, dict[str, Any]] = field(default_factory=dict)


class AnalyticsRepository:
    """Every connection is read-only with a short statement timeout; results are cached briefly."""

    def __init__(self, dsn: str | None = None, *, connect=None, ttl_seconds: float = 30.0):
        self.dsn = dsn or analytics_dsn()
        self._connect = connect
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, CaseData]] = {}
        self._lock = threading.Lock()

    def _connection(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - psycopg is a declared dependency
            raise AnalyticsUnavailable("PostgreSQL support is not installed.") from exc
        connect = self._connect or psycopg.connect
        try:
            conn = connect(self.dsn, connect_timeout=3, row_factory=dict_row)
        except Exception as exc:
            raise AnalyticsUnavailable(
                "Cannot reach the analytics database. Check TRACED_ANALYTICS_DATABASE_URL "
                "and that PostgreSQL is running."
            ) from exc
        conn.read_only = True
        conn.execute("SET statement_timeout = '15s'")
        return conn

    def list_cases(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            present = {
                r["table_name"]
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = ANY(%s)",
                    (list(REQUIRED_TABLES),),
                )
            }
            if set(REQUIRED_TABLES) - present:
                raise AnalyticsUnavailable(
                    "The analytics tables are not in this database yet. Run the content-analytics "
                    "classifier first."
                )
            return conn.execute(
                """SELECT c.id::text AS id, c.slug, c.subject_name, c.subject_role,
                          count(l.content_item_id) AS labeled
                   FROM cases c LEFT JOIN analytics_content_labels l ON l.case_id = c.id
                   GROUP BY c.id ORDER BY count(l.content_item_id) DESC, c.created_at"""
            ).fetchall()

    def case_data(self, case_id: str, *, refresh: bool = False) -> CaseData:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(case_id)
            if hit and not refresh and now - hit[0] < self._ttl:
                return hit[1]
        data = self._load(case_id)
        with self._lock:
            self._cache[case_id] = (now, data)
        return data

    def case_snapshot(self, case_id: str, item_ids: list[str]) -> CaseData:
        """Capture fresh labels, case-scoped references and selected raw records together."""
        return self._load(case_id, capture_ids=item_ids)

    def _load(self, case_id: str, *, capture_ids: list[str] | None = None) -> CaseData:
        with self._connection() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            case = conn.execute(
                """SELECT id::text AS id, slug, subject_name, subject_role, description,
                          window_start, window_end FROM cases WHERE id::text = %s""",
                (case_id,),
            ).fetchone()
            if not case:
                raise AnalyticsUnavailable("The selected analytics case no longer exists.")
            total = conn.execute(
                "SELECT count(*) AS n FROM content_items WHERE case_id::text = %s", (case_id,)
            ).fetchone()["n"]
            labels = conn.execute(
                """
                SELECT l.content_item_id::text AS id, l.case_id::text AS case_id,
                       l.attribution, l.posted_at, l.status,
                       l.disinformation, l.racism, l.hate_speech, l.harassment, l.harassment_volume,
                       l.disinfo_verdict, l.claim_summary, l.hate_speech_basis, l.racism_target,
                       l.harassment_target, l.rationale, l.signals, l.model, l.prompt_version,
                       l.classified_at,
                       coalesce(l.reference_item_ids::text[], '{}') AS reference_ids,
                       l.author_id::text AS author_id,
                       c.platform::text AS platform, c.platform_item_id, c.text, c.url,
                       c.archive_url,
                       c.content_sha256, c.collected_at, c.is_reply, c.view_count, c.like_count,
                       c.repost_count, c.reply_count, c.lang,
                       -- Export-time hash of the saved jsonb text; not a collection-time hash.
                       encode(sha256(convert_to(c.raw::text, 'UTF8')), 'hex') AS capture_sha256,
                       a.handle, a.display_name, a.followers_count, a.is_verified,
                       a.platform_user_id
                FROM analytics_content_labels l
                JOIN content_items c ON c.id = l.content_item_id AND c.case_id = l.case_id
                LEFT JOIN authors a ON a.id = l.author_id
                WHERE l.case_id::text = %s
                ORDER BY l.posted_at
                """,
                (case_id,),
            ).fetchall()
            label_ids = [row["id"] for row in labels]
            if len(label_ids) != len(set(label_ids)):
                raise AnalyticsUnavailable(
                    "Multiple labels share a record ID; select an unambiguous saved run first."
                )
            ref_ids = sorted({r for row in labels for r in row["reference_ids"]})
            references = {}
            if ref_ids:
                for r in conn.execute(
                    """SELECT id::text AS id, case_id::text AS case_id,
                              attribution::text AS attribution, posted_at, text, url,
                              raw::text AS raw,
                              encode(sha256(convert_to(raw::text, 'UTF8')), 'hex')
                                  AS capture_sha256
                       FROM content_items WHERE case_id::text = %s AND id::text = ANY(%s)""",
                    (case_id, ref_ids),
                ):
                    references[r["id"]] = r
            last_run = None
            if _table_exists(conn, "analytics_runs"):
                last_run = conn.execute(
                    """SELECT model, effort, prompt_version, started_at, finished_at, status,
                              items_classified, items_failed
                       FROM analytics_runs WHERE case_id::text = %s
                       ORDER BY started_at DESC LIMIT 1""",
                    (case_id,),
                ).fetchone()
            bursts = []
            if _table_exists(conn, "analytics_harassment_bursts"):
                bursts = conn.execute(
                    """SELECT author_handle, target, pattern, window_start, window_end, post_count,
                              reply_count FROM analytics_harassment_bursts
                       WHERE case_id::text = %s ORDER BY post_count DESC, window_start LIMIT 50""",
                    (case_id,),
                ).fetchall()
            provenance = self._provenance(conn, capture_ids or [], case_id=case_id)
        return CaseData(
            case=case,
            total_posts=total,
            labels=labels,
            references=references,
            last_run=last_run,
            bursts=bursts,
            provenance=provenance,
        )

    def captures(self, item_ids: list[str]) -> dict[str, str]:
        """The captured platform records, as stored (hash input for capture_sha256)."""
        return {k: v["raw"] for k, v in self.provenance(item_ids).items()}

    def provenance(self, item_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Native capture plus the collection run that acquired it (chain of custody)."""
        if not item_ids:
            return {}
        with self._connection() as conn:
            return self._provenance(conn, item_ids)

    @staticmethod
    def _provenance(
        conn, item_ids: list[str], *, case_id: str | None = None
    ) -> dict[str, dict[str, Any]]:
        if not item_ids:
            return {}
        has_runs = _table_exists(conn, "collection_runs")
        run_cols = (
            "r.id::text AS run_id, r.tier, r.actor_id, r.query, r.status AS run_status, "
            "r.started_at AS run_started_at, r.finished_at AS run_finished_at, "
            "r.items_returned"
            if has_runs
            else "NULL AS run_id"
        )
        join = "LEFT JOIN collection_runs r ON r.id = c.collection_run_id" if has_runs else ""
        scope = " AND c.case_id::text = %s" if case_id is not None else ""
        params = (list(item_ids), case_id) if case_id is not None else (list(item_ids),)
        rows = conn.execute(
            f"""SELECT c.id::text AS id, c.raw::text AS raw, {run_cols}
                FROM content_items c {join} WHERE c.id::text = ANY(%s){scope}""",
            params,
        ).fetchall()
        out = {}
        for r in rows:
            raw = r.pop("raw")
            out[r.pop("id")] = {"raw": raw, "run": r if r.get("run_id") else None}
        return out


def _table_exists(conn, name: str) -> bool:
    return bool(
        conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"public.{name}",)).fetchone()[
            "ok"
        ]
    )
