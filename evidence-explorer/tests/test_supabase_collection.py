"""Read-only database collection retains exact rows and explicit bounded coverage."""

import hashlib
import json

import psycopg
import pytest

from evidence_explorer.collection.models import CollectionFailure, CollectionSource
from evidence_explorer.collection.supabase import (
    MAX_ROW_BYTES,
    collect_supabase,
    configuration_status,
    inspect_table,
    normalize_locator,
)


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    for name in (
        "HOST",
        "PORT",
        "DATABASE",
        "USER",
        "PASSWORD",
        "PROJECT_REF",
        "ID_COLUMN",
        "TEXT_COLUMN",
        "TITLE_COLUMN",
        "URL_COLUMN",
        "PUBLISHED_AT_COLUMN",
        "AUTHOR_COLUMN",
    ):
        monkeypatch.delenv("SUPABASE_" + name, raising=False)
    monkeypatch.setenv("SUPABASE_HOST", "db.fixtureproject.supabase.co")
    monkeypatch.setenv("SUPABASE_PASSWORD", "private-fixture-password")


def source(**changes):
    return CollectionSource(
        id="database-source",
        platform="supabase",
        label="Saved database sample",
        locator="public.content_items",
        **changes,
    )


def encoded(**changes):
    return json.dumps(
        {"id": "row-1", "content": "The bridge inspection is scheduled.", **changes},
        ensure_ascii=False,
    )


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class Database:
    def __init__(self, rows=(), *, columns=None, unique=True):
        self.rows = rows
        self.columns = (
            columns
            if columns is not None
            else [
                {"column_name": "id", "data_type": "text", "is_nullable": "NO"},
                {"column_name": "content", "data_type": "text", "is_nullable": "YES"},
            ]
        )
        self.unique = unique
        self.settings = None
        self.queries = []
        self.read_only = False
        self.isolation_level = None
        self.closed = False

    def connect(self, **kwargs):
        self.settings = kwargs
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def execute(self, query, parameters=()):
        assert self.read_only is True
        assert self.isolation_level == psycopg.IsolationLevel.REPEATABLE_READ
        query = query if isinstance(query, str) else query.as_string()
        self.queries.append((query, parameters))
        if query == "SET LOCAL statement_timeout = '10s'":
            return Result([])
        assert query.startswith("SELECT ")
        if "information_schema.columns" in query:
            return Result(self.columns)
        if "pg_catalog.pg_index" in query:
            return Result([{"unique_id": self.unique}])
        assert "left(to_jsonb(t)::text" in query
        return Result([{"row_json": row} for row in self.rows])


def optional_columns(*names):
    return Database().columns + [
        {"column_name": name, "data_type": "text", "is_nullable": "YES"} for name in names
    ]


def test_configuration_status_is_offline_and_does_not_disclose_secrets(monkeypatch):
    status = configuration_status()
    assert status["ready"] is True
    assert "not been checked" in status["message"]
    assert "private-fixture-password" not in str(status)
    monkeypatch.delenv("SUPABASE_PASSWORD")
    assert configuration_status()["ready"] is False


@pytest.mark.parametrize(
    "name,value",
    [
        ("SUPABASE_PORT", "0"),
        ("SUPABASE_PORT", "not-a-port"),
        ("SUPABASE_HOST", "postgresql://user:secret@example.com/postgres"),
        ("SUPABASE_HOST", "a..example.com"),
        ("SUPABASE_PROJECT_REF", "different-project"),
        ("SUPABASE_TEXT_COLUMN", "content; DROP TABLE content_items"),
        ("SUPABASE_ID_COLUMN", ""),
    ],
)
def test_invalid_settings_are_unavailable_before_connecting(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    assert configuration_status()["ready"] is False
    db = Database()
    with pytest.raises(CollectionFailure):
        collect_supabase(source(), connect=db.connect)
    assert db.settings is None


def test_pooler_requires_explicit_project_identity(monkeypatch):
    monkeypatch.setenv("SUPABASE_HOST", "aws-0-region.pooler.supabase.com")
    assert configuration_status()["ready"] is False
    monkeypatch.setenv("SUPABASE_PROJECT_REF", "fixtureproject")
    monkeypatch.setenv("SUPABASE_USER", "postgres.fixtureproject")
    db = Database([encoded()])
    batch = collect_supabase(source(), connect=db.connect)
    assert batch.items[0].channel_id == "fixtureproject/postgres/public.content_items"
    assert db.settings["user"] == "postgres.fixtureproject"


@pytest.mark.parametrize(
    "locator",
    [
        "postgresql://user:password@example.com/postgres",
        "public.content_items;DELETE",
        "public.content_items.extra",
        'public."content_items"',
        "public.*",
        "public.content_items WHERE true",
        "",
        "public..content_items",
    ],
)
def test_locator_rejects_connection_strings_and_sql(locator):
    with pytest.raises(CollectionFailure):
        normalize_locator(locator)


def test_plain_table_names_are_canonicalized():
    assert normalize_locator(" content_items ") == "public.content_items"
    assert normalize_locator("archive.ContentItems") == "archive.ContentItems"


def test_schema_inspection_does_not_read_rows_and_uses_readonly_tls_connection():
    db = Database()
    columns = inspect_table(connect=db.connect)
    assert columns == db.columns
    assert len(db.queries) == 2
    assert db.queries[0] == ("SET LOCAL statement_timeout = '10s'", ())
    assert db.queries[1][1] == ("public", "content_items")
    assert db.settings["sslmode"] == "require"
    assert db.settings["connect_timeout"] == 8
    assert "statement_timeout=10000" in db.settings["options"]
    assert "default_transaction_read_only=on" in db.settings["options"]
    assert db.closed is True


def test_bounded_snapshot_retains_exact_json_and_never_infers_publication():
    raw = encoded(
        platform="telegram",
        created_at="2026-11-01T11:00:00Z",
        metadata={"origin": "unverified", "unknown": None},
    )
    db = Database([raw, encoded(id="row-2")])
    batch = collect_supabase(source(item_limit=1), connect=db.connect)
    assert len(batch.items) == 1
    item = batch.items[0]
    assert item.platform == "supabase"
    assert item.published_at is None
    assert item.author_id is None
    assert item.source_url is None
    assert item.provenance.method == "supabase_postgres"
    assert item.provenance.content_scope == "database_row"
    assert item.provenance.database_row.row_json == raw
    assert item.provenance.database_row.row_sha256 == hashlib.sha256(raw.encode()).hexdigest()
    assert json.loads(item.provenance.database_row.row_json)["platform"] == "telegram"
    assert batch.cursor is None
    assert any("omitted" in warning for warning in batch.warnings)
    assert any("completeness" in warning for warning in batch.warnings)
    query, values = db.queries[-1]
    assert 'FROM "public"."content_items" AS t' in query
    assert 'ORDER BY "t"."id" DESC LIMIT %s' in query
    assert values == (MAX_ROW_BYTES + 1, 2)
    assert "private-fixture-password" not in str(batch) + str(db.queries)


def test_explicit_optional_mapping_preserves_raw_aware_publication(monkeypatch):
    for variable, column in (
        ("TITLE", "heading"),
        ("URL", "permalink"),
        ("PUBLISHED_AT", "first_posted"),
        ("AUTHOR", "author"),
    ):
        monkeypatch.setenv(f"SUPABASE_{variable}_COLUMN", column)
    raw = encoded(
        heading="Bridge update",
        permalink="https://publisher.example/news/1",
        first_posted="2026-11-01T12:00:00+02:00",
        author="public-account",
    )
    db = Database([raw], columns=optional_columns("heading", "permalink", "first_posted", "author"))
    item = collect_supabase(source(), connect=db.connect).items[0]
    assert item.title == "Bridge update"
    assert item.source_url == "https://publisher.example/news/1"
    assert item.author_id is None
    assert item.provenance.author_label == "public-account"
    assert item.published_at == "2026-11-01T10:00:00Z"
    assert item.provenance.published_at_raw == "2026-11-01T12:00:00+02:00"


@pytest.mark.parametrize("value", ["2026-11-01T12:00:00", "2026-11-01", "unknown"])
def test_unusable_publication_stays_unknown_with_original_value(monkeypatch, value):
    monkeypatch.setenv("SUPABASE_PUBLISHED_AT_COLUMN", "first_posted")
    db = Database([encoded(first_posted=value)], columns=optional_columns("first_posted"))
    batch = collect_supabase(source(), connect=db.connect)
    assert batch.items[0].published_at is None
    assert batch.items[0].provenance.published_at_raw == value
    assert any("remain unknown" in warning for warning in batch.warnings)


def test_missing_column_prevents_row_query(monkeypatch):
    monkeypatch.setenv("SUPABASE_TEXT_COLUMN", "missing")
    db = Database()
    with pytest.raises(CollectionFailure, match="column is missing"):
        collect_supabase(source(), connect=db.connect)
    assert len(db.queries) == 2


def test_missing_table_is_safe():
    db = Database(columns=[])
    with pytest.raises(CollectionFailure, match="missing or"):
        inspect_table(connect=db.connect)


@pytest.mark.parametrize("unique,nullable", [(False, "NO"), (True, "YES")])
def test_identity_requires_non_null_unique_key(unique, nullable):
    db = Database(unique=unique)
    db.columns[0]["is_nullable"] = nullable
    with pytest.raises(CollectionFailure, match="unique index"):
        collect_supabase(source(), connect=db.connect)
    assert len(db.queries) == 3
    query, parameters = db.queries[-1]
    assert "i.indnkeyatts = 1" in query
    assert "i.indpred IS NULL" in query
    assert "i.indexprs IS NULL" in query
    assert parameters == ("public", "content_items", "id")


@pytest.mark.parametrize(
    "raw",
    [
        '{"id":',
        "[]",
        encoded(id=None),
        encoded(id=True),
        encoded(content=None),
        encoded(content=" "),
        "x" * (MAX_ROW_BYTES + 1),
        encoded(extra="é" * (MAX_ROW_BYTES // 2)),
    ],
)
def test_unusable_or_oversized_rows_fail_without_returning_partial_evidence(raw):
    db = Database([encoded(), raw])
    with pytest.raises(CollectionFailure):
        collect_supabase(source(), connect=db.connect)
    assert db.closed is True


def test_duplicate_ids_fail_instead_of_merging_rows():
    db = Database([encoded(), encoded(content="Changed text with duplicate ID")])
    with pytest.raises(CollectionFailure, match="duplicate row identifiers"):
        collect_supabase(source(), connect=db.connect)


def test_driver_failures_are_redacted():
    def fail(**kwargs):
        raise RuntimeError("private-fixture-password private-connection-response")

    with pytest.raises(CollectionFailure) as caught:
        collect_supabase(source(), connect=fail)
    assert "private-" not in str(caught.value)
    assert "no database rows were changed" in str(caught.value)


def test_invalid_mapped_values_are_redacted(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL_COLUMN", "url")
    db = Database(
        [encoded(url="postgresql://private-fixture-password@localhost")],
        columns=optional_columns("url"),
    )
    with pytest.raises(CollectionFailure) as caught:
        collect_supabase(source(), connect=db.connect)
    assert "private-fixture-password" not in str(caught.value)


def test_changed_row_metadata_changes_snapshot_even_when_mapped_text_is_unchanged():
    first = collect_supabase(source(), connect=Database([encoded(metadata="first")]).connect)
    second = collect_supabase(source(), connect=Database([encoded(metadata="second")]).connect)
    assert first.items[0].text == second.items[0].text
    assert first.items[0].native_id == second.items[0].native_id
    assert (
        first.items[0].provenance.database_row.row_sha256
        != second.items[0].provenance.database_row.row_sha256
    )


def test_empty_table_is_explicit_empty_sample():
    batch = collect_supabase(source(), connect=Database().connect)
    assert batch.items == ()
    assert any("completeness" in warning for warning in batch.warnings)
