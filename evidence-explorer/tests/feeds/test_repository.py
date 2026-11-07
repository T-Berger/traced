"""Saved settings, atomic refresh requests, and compatibility with older snapshots."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from evidence_explorer.db import Repository
from evidence_explorer.feeds import FeedSubscriptionInput, tick_feeds

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)


def request(**changes):
    return FeedSubscriptionInput.model_validate(
        {
            "name": "City news",
            "url": "https://news.example/feed",
            "analyze": False,
            **changes,
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"interval_minutes": True},
        {"interval_minutes": 4},
        {"interval_minutes": 10081},
        {"interval_minutes": 60.0},
        {"item_limit": 0},
        {"item_limit": 21},
        {"analyze": "false"},
        {"content_mode": "automatic"},
        {"url": "http://127.1/feed"},
        {"name": "  "},
    ],
)
def test_invalid_saved_settings_are_rejected(changes):
    with pytest.raises(ValidationError):
        request(**changes)


def test_save_normalizes_upserts_and_preserves_pause(repo):
    first = repo.feeds.save(request(url="https://NEWS.example/feed?utm_source=rss"), now=NOW)
    assert first.url == "https://news.example/feed"
    paused = repo.feeds.set_enabled(first.id, False, now=NOW)
    updated = repo.feeds.save(
        request(name="Renamed", interval_minutes=30), now=NOW + timedelta(hours=1)
    )
    assert updated.id == first.id
    assert updated.name == "Renamed"
    assert updated.enabled is False
    assert updated.next_refresh_at == paused.next_refresh_at
    assert len(repo.feeds.list_page()) == 1
    resumed = repo.feeds.set_enabled(first.id, True, now=NOW + timedelta(hours=2))
    assert resumed.next_refresh_at == (NOW + timedelta(hours=2)).isoformat()
    assert not repo.feeds.list_page(limit=0)


def test_manual_refresh_is_atomic_across_competing_callers(repo):
    feed = repo.feeds.save(request(), now=NOW)
    with ThreadPoolExecutor(max_workers=6) as workers:
        ids = list(workers.map(lambda _: repo.feeds.queue_one(feed.id, now=NOW), range(12)))
    assert len(set(ids)) == 1
    assert repo.stats()["jobs"] == 1
    job = repo.get_job(ids[0])
    assert job["payload"]["feed_id"] == feed.id
    assert job["payload"]["scheduled"] is False
    assert job["payload"]["analyze"] is False


def test_manual_refresh_upgrades_queued_scheduled_job_while_paused(repo):
    feed = repo.feeds.save(request(), now=NOW)
    job_id = tick_feeds(repo, now=NOW).queued_job_ids[0]
    assert repo.get_job(job_id)["payload"]["scheduled"] is True
    repo.feeds.set_enabled(feed.id, False, now=NOW)
    assert repo.feeds.queue_one(feed.id, now=NOW) == job_id
    assert repo.get_job(job_id)["payload"]["scheduled"] is False
    assert not repo.feeds.queue_due(now=NOW + timedelta(days=1))


def test_older_read_only_snapshot_lists_no_feeds_without_migration(repo, tmp_path):
    snapshot_path = repo.backup(tmp_path / "old.sqlite3")
    with sqlite3.connect(snapshot_path) as connection:
        connection.execute("DROP TABLE feed_subscriptions")
        connection.execute(
            "DELETE FROM schema_migrations WHERE component='explorer' AND version>=5"
        )
    snapshot = Repository(snapshot_path, read_only=True)
    snapshot.initialize()
    assert snapshot.feeds.list_page() == []
    assert snapshot.feeds.get(1) is None
    assert tick_feeds(snapshot, now=NOW).queued_job_ids == []
    with pytest.raises(PermissionError):
        snapshot.feeds.save(request(), now=NOW)
    with sqlite3.connect(snapshot_path) as connection:
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='feed_subscriptions'"
        ).fetchone()


def test_schema_adds_content_source_without_changing_existing_article(repo, article):
    article_id = repo.upsert_article(article)
    repo.initialize()
    assert repo.get_article(article_id)["text"] == article.text
    with repo._connection() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(articles)")}
        assert "content_source" in columns
