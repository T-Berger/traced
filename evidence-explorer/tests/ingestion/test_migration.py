"""A pre-ingestion database upgrades without losing its saved source records."""

import hashlib
import sqlite3
from datetime import UTC, datetime

from evidence_explorer.db import Repository
from evidence_explorer.persistence.migrations import migrate
from evidence_explorer.persistence.schema import MIGRATIONS


def test_ingestion_migration_preserves_legacy_sources_and_adds_provenance(settings, article):
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).isoformat()
    original_hash = hashlib.sha256(article.text.encode()).hexdigest()
    with sqlite3.connect(settings.db_path) as connection:
        migrate(connection, "explorer", MIGRATIONS[:2])
        connection.execute(
            "INSERT INTO articles(title,publisher,url,published_at,text,language,topic,"
            "is_sample,content_hash,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                article.title,
                article.publisher,
                article.url,
                article.published_at,
                article.text,
                article.language,
                article.topic,
                article.is_sample,
                original_hash,
                timestamp,
                timestamp,
            ),
        )
    before = settings.db_path.read_bytes()
    snapshot = Repository(settings.db_path, read_only=True)
    snapshot.initialize()
    summary = snapshot.collection().articles[0]
    assert summary.requested_url is None
    assert summary.retrieved_at is None
    assert "text" not in summary.model_dump()
    assert settings.db_path.read_bytes() == before

    repo = Repository(settings.db_path)
    repo.initialize()
    previous = repo.list_articles()[0]
    assert previous["text"] == article.text
    assert previous["content_hash"] == original_hash
    assert previous["retrieved_at"] is None
    assert previous["requested_url"] is None
    assert previous["author"] is None

    enriched = article.model_copy(
        update={
            "requested_url": article.url + "?utm_source=feed",
            "final_url": article.url,
            "feed_url": "https://bulletin.example/feed.xml",
            "retrieved_at": timestamp,
            "author": "Alex Example",
            "publication_source": "article",
        }
    )
    assert repo.upsert_article(enriched) == previous["id"]
    saved = repo.get_article(previous["id"])
    assert saved["content_hash"] == original_hash
    assert saved["author"] == "Alex Example"
    assert saved["feed_url"] == "https://bulletin.example/feed.xml"
    assert saved["retrieved_at"] == timestamp
    with sqlite3.connect(settings.db_path) as connection:
        version = connection.execute(
            "SELECT max(version) FROM schema_migrations WHERE component='explorer'"
        ).fetchone()[0]
    assert version == MIGRATIONS[-1].version
