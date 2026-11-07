"""Bounded, read-only Postgres snapshots with server-side Supabase credentials.

Database rows are captured exactly as JSON alongside explicitly mapped text. A
database insertion time is never inferred to be an original publication time.
The existing collection repository owns persistence, jobs, and review snapshots.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..source_contracts import SourceItem
from .models import CollectedBatch, CollectionFailure, CollectionSource

MAX_ROW_BYTES = 100_000
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
_HOST = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?\Z")
_PROJECT = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
_DIRECT_HOST = re.compile(r"db\.([a-z0-9-]+)\.supabase\.co\Z")
_FAILURE = (
    "Supabase collection could not complete. Check the connection, table access, and "
    "column settings; no database rows were changed."
)


@dataclass(frozen=True)
class _Settings:
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)
    project_ref: str
    id_column: str
    text_column: str
    title_column: str | None
    url_column: str | None
    published_at_column: str | None
    author_column: str | None


def _column(name: str, default: str = "") -> str | None:
    value = os.environ.get(name, default).strip()
    if not value:
        return None
    if not _IDENTIFIER.fullmatch(value):
        raise CollectionFailure("Supabase column settings must be plain SQL identifiers.")
    return value


def _settings() -> _Settings:
    host = os.environ.get("SUPABASE_HOST", "").strip().lower()
    password = os.environ.get("SUPABASE_PASSWORD", "")
    if not host or not password:
        raise CollectionFailure("Configure SUPABASE_HOST and SUPABASE_PASSWORD on the server.")
    if not _HOST.fullmatch(host) or ".." in host or any(ord(char) < 32 for char in password):
        raise CollectionFailure("Check the server-side Supabase host and password settings.")
    project = os.environ.get("SUPABASE_PROJECT_REF", "").strip()
    direct = _DIRECT_HOST.fullmatch(host)
    if direct:
        if project and project != direct.group(1):
            raise CollectionFailure("SUPABASE_PROJECT_REF must match the direct database host.")
        project = direct.group(1)
    if not _PROJECT.fullmatch(project):
        raise CollectionFailure("Set SUPABASE_PROJECT_REF explicitly when using a pooler host.")
    try:
        port = int(os.environ.get("SUPABASE_PORT", "5432"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise CollectionFailure("SUPABASE_PORT must be an integer between 1 and 65535.") from None
    database = os.environ.get("SUPABASE_DATABASE", "postgres")
    user = os.environ.get("SUPABASE_USER", "postgres")
    if any(
        not value or len(value.encode("utf-8")) > 63 or any(ord(char) < 32 for char in value)
        for value in (database, user)
    ):
        raise CollectionFailure("Check the server-side Supabase database and user settings.")
    id_column = _column("SUPABASE_ID_COLUMN", "id")
    text_column = _column("SUPABASE_TEXT_COLUMN", "content")
    if id_column is None or text_column is None:
        raise CollectionFailure("Configure a Supabase row ID column and content text column.")
    return _Settings(
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
        project_ref=project,
        id_column=id_column,
        text_column=text_column,
        title_column=_column("SUPABASE_TITLE_COLUMN"),
        url_column=_column("SUPABASE_URL_COLUMN"),
        published_at_column=_column("SUPABASE_PUBLISHED_AT_COLUMN"),
        author_column=_column("SUPABASE_AUTHOR_COLUMN"),
    )


def configuration_status() -> dict:
    """Describe configuration presence without connecting or disclosing credentials."""
    try:
        _settings()
    except CollectionFailure as exc:
        return {"ready": False, "message": str(exc)}
    return {
        "ready": True,
        "message": "Supabase configured; read-only table access has not been checked.",
    }


def normalize_locator(locator: str) -> str:
    """Accept table names only; connection strings never enter source configuration."""
    parts = locator.strip().split(".")
    if len(parts) == 1:
        parts.insert(0, "public")
    if len(parts) != 2 or not all(_IDENTIFIER.fullmatch(part) for part in parts):
        raise CollectionFailure("Use a Supabase table name such as public.content_items.")
    return ".".join(parts)


@contextmanager
def _connection(settings: _Settings, connect):
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        raise CollectionFailure("Install the Supabase connector's psycopg dependency.") from None
    try:
        with (connect or psycopg.connect)(
            host=settings.host,
            port=settings.port,
            dbname=settings.database,
            user=settings.user,
            password=settings.password,
            sslmode="require",
            connect_timeout=8,
            options="-c statement_timeout=10000 -c default_transaction_read_only=on",
            row_factory=dict_row,
        ) as connection:
            connection.read_only = True
            connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            # Poolers may not forward arbitrary startup options to the backend.
            connection.execute("SET LOCAL statement_timeout = '10s'")
            yield connection
    except CollectionFailure:
        raise
    except Exception:
        # Driver errors can contain credentials, connection strings, and row values.
        raise CollectionFailure(_FAILURE) from None


def _columns(connection, schema: str, table: str) -> list[dict]:
    columns = connection.execute(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_catalog = current_database() AND table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position",
        (schema, table),
    ).fetchall()
    if not columns:
        raise CollectionFailure("The Supabase table is missing or its columns are not accessible.")
    return [dict(column) for column in columns]


def inspect_table(locator: str = "public.content_items", *, connect=None) -> list[dict]:
    """Read schema metadata only; this neither reads content nor creates local evidence."""
    schema, table = normalize_locator(locator).split(".")
    settings = _settings()
    with _connection(settings, connect) as connection:
        return _columns(connection, schema, table)


def _validate_mapping(connection, columns: list[dict], settings: _Settings, schema, table):
    available = {column["column_name"]: column for column in columns}
    mapped = (
        settings.id_column,
        settings.text_column,
        settings.title_column,
        settings.url_column,
        settings.published_at_column,
        settings.author_column,
    )
    if any(name is not None and name not in available for name in mapped):
        raise CollectionFailure(
            "A configured Supabase column is missing. Check the table's column mapping."
        )
    unique = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_index AS i "
        "JOIN pg_catalog.pg_class AS c ON c.oid = i.indrelid "
        "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
        "JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.oid "
        "AND a.attnum = i.indkey[0] "
        "WHERE n.nspname = %s AND c.relname = %s AND a.attname = %s "
        "AND i.indisunique AND i.indisvalid AND i.indnkeyatts = 1 "
        "AND i.indpred IS NULL AND i.indexprs IS NULL) AS unique_id",
        (schema, table, settings.id_column),
    ).fetchone()
    if available[settings.id_column]["is_nullable"] != "NO" or not unique["unique_id"]:
        raise CollectionFailure(
            "The Supabase ID column must have a single-column unique index and disallow nulls."
        )


def _optional_text(row: dict, column: str | None) -> str | None:
    value = row.get(column) if column else None
    if value is not None and not isinstance(value, str):
        raise CollectionFailure("A mapped Supabase text field contains a non-text value.")
    return value


def _item(raw: str, settings: _Settings, schema, table, acquired_at: str) -> SourceItem:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_ROW_BYTES:
        raise CollectionFailure("A Supabase row exceeds the 100,000-byte snapshot limit.")
    try:
        row = json.loads(raw)
    except (ValueError, RecursionError):
        raise CollectionFailure("A Supabase row could not be preserved as complete JSON.") from None
    if not isinstance(row, dict):
        raise CollectionFailure("A Supabase row must be a JSON object.")
    native = row.get(settings.id_column)
    if not isinstance(native, (int, str)) or isinstance(native, bool):
        raise CollectionFailure("A Supabase row has no usable integer or text identifier.")
    content = row.get(settings.text_column)
    if not isinstance(content, str) or not content.strip():
        raise CollectionFailure("A Supabase row has no text in the configured content column.")
    published_raw = _optional_text(row, settings.published_at_column)
    published_at = None
    if published_raw:
        try:
            parsed = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                published_at = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
        except (ValueError, OverflowError):
            pass
    author = _optional_text(row, settings.author_column)
    return SourceItem(
        platform="supabase",
        channel_id=f"{settings.project_ref}/{settings.database}/{schema}.{table}",
        channel_name=f"Supabase · {schema}.{table}",
        native_id=str(native),
        text=content,
        title=_optional_text(row, settings.title_column),
        source_url=_optional_text(row, settings.url_column),
        published_at=published_at,
        acquired_at=acquired_at,
        provenance={
            "method": "supabase_postgres",
            "content_scope": "database_row",
            "author_label": author,
            "published_at_raw": published_raw,
            "database_row": {
                "project_ref": settings.project_ref,
                "database": settings.database,
                "schema_name": schema,
                "table_name": table,
                "id_column": settings.id_column,
                "text_column": settings.text_column,
                "row_json": raw,
                "row_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            },
        },
    )


def collect_supabase(source: CollectionSource, *, connect=None) -> CollectedBatch:
    """Capture one ID-ordered sample in a single repeatable, read-only transaction."""
    if source.platform != "supabase":
        raise CollectionFailure("Choose the Supabase platform for database collection.")
    schema, table = normalize_locator(source.locator).split(".")
    settings = _settings()
    with _connection(settings, connect) as connection:
        from psycopg import sql

        _validate_mapping(connection, _columns(connection, schema, table), settings, schema, table)
        rows = connection.execute(
            sql.SQL(
                "SELECT left(to_jsonb(t)::text, %s) AS row_json FROM {} AS t "
                "ORDER BY {} DESC LIMIT %s"
            ).format(sql.Identifier(schema, table), sql.Identifier("t", settings.id_column)),
            (MAX_ROW_BYTES + 1, source.item_limit + 1),
        ).fetchall()
        acquired_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        items = tuple(
            _item(row["row_json"], settings, schema, table, acquired_at)
            for row in rows[: source.item_limit]
        )
        if len({item.native_id for item in items}) != len(items):
            raise CollectionFailure("The Supabase sample contains duplicate row identifiers.")
        warnings = [
            "Bounded database snapshot ordered by row ID, not publication time. "
            "Historical completeness and original source coverage are unknown."
        ]
        if len(rows) > source.item_limit:
            warnings.append("Additional database rows were omitted by the collection limit.")
        if any(item.provenance.published_at_raw and item.published_at is None for item in items):
            warnings.append(
                "Unusable or timezone-free publication values remain unknown; raw values are saved."
            )
        return CollectedBatch(items=items, warnings=tuple(warnings))
