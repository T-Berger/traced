"""Already-recorded v5 schemas need a forward upgrade before article writes."""

import hashlib
import sqlite3
from contextlib import closing

import pytest

from evidence_explorer.chunking import chunk_text
from evidence_explorer.db import Repository
from evidence_explorer.persistence.migrations import migrate
from evidence_explorer.persistence.schema import MIGRATIONS


@pytest.mark.parametrize("missing_column", [True, False], ids=["historical-v5", "current-v5"])
def test_v5_upgrade_repairs_missing_content_source_without_changing_saved_data(
    settings, article, missing_column
):
    with closing(sqlite3.connect(settings.db_path)) as connection:
        migrate(connection, "explorer", MIGRATIONS[:5])

    repo = Repository(settings.db_path)
    saved = article.model_copy(
        update={"content_source": None if missing_column else "feed_summary"}
    )
    # Populate the actual historical schema, rather than using today's writer
    # (which also writes source-identity fields added by later migrations).
    values = saved.model_dump()
    values.update(
        content_hash=hashlib.sha256(saved.text.encode()).hexdigest(),
        created_at="2026-11-01T12:00:00+00:00",
        updated_at="2026-11-01T12:00:00+00:00",
    )
    with repo._connection(write=True) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(articles)")}
        legacy_values = {key: value for key, value in values.items() if key in columns}
        article_id = connection.execute(
            f"INSERT INTO articles ({', '.join(legacy_values)}) "
            f"VALUES ({', '.join('?' for _ in legacy_values)})",
            tuple(legacy_values.values()),
        ).lastrowid
    repo.replace_chunks(article_id, chunk_text(saved.text))
    job_id = repo.enqueue_job("analyze", {"mode": "demo"})
    with repo._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO feed_subscriptions(name,url,next_refresh_at,created_at,updated_at) "
            "VALUES ('Saved feed','https://news.example/feed','then','then','then')"
        )
        if missing_column:
            # Reproduce the earlier v5 body, whose journal omitted this column entirely.
            connection.execute("ALTER TABLE articles DROP COLUMN content_source")
        history = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM schema_migrations WHERE component='explorer' ORDER BY version"
            )
        ]
        feed = dict(connection.execute("SELECT * FROM feed_subscriptions").fetchone())
    before = repo.get_article(article_id)

    if missing_column:
        with pytest.raises(sqlite3.OperationalError, match="content_source"):
            repo.upsert_article(saved)

    repo.initialize()
    repo.initialize()
    assert repo.get_article(article_id) == before
    assert repo.get_job(job_id)["status"] == "queued"
    assert repo.keyword_search("bridge")[0]["article_id"] == article_id
    with repo._connection() as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(articles)")]
        assert columns.count("content_source") == 1
        upgraded_feed = dict(connection.execute("SELECT * FROM feed_subscriptions").fetchone())
        assert {key: upgraded_feed[key] for key in feed} == feed
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM schema_migrations WHERE component='explorer' AND version<=5 "
                "ORDER BY version"
            )
        ] == history
        assert (
            connection.execute(
                "SELECT count(*) FROM schema_migrations WHERE component='explorer' AND version=6"
            ).fetchone()[0]
            == 1
        )

    updated = saved.model_copy(update={"content_source": "feed_content"})
    assert repo.upsert_article(updated) == article_id
    assert repo.get_article(article_id)["content_source"] == "feed_content"
