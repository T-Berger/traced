"""Saved feeds and live controls keep a single scheduler for each real source."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from evidence_explorer.collection.models import CollectedBatch, CollectionFailure, CollectionSource
from evidence_explorer.collection.ownership import (
    assert_feed_owner_available,
    canonical_key,
    find_feed_owner,
)
from evidence_explorer.collection.persistence import CollectionRepository
from evidence_explorer.collection.runtime import CollectionRuntime
from evidence_explorer.collection.service import CollectionService
from evidence_explorer.review.integrations.sources import SourceItem

FEED = "https://publisher.example/news/rss.xml"
YOUTUBE_CHANNEL = "UC" + "A" * 22
YOUTUBE_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id=" + YOUTUBE_CHANNEL
CLOCK = datetime(2035, 2, 3, 10, tzinfo=UTC)


@pytest.fixture
def desk(tmp_path):
    repository = CollectionRepository(tmp_path / "sources.sqlite3")
    return repository, CollectionService(repository)


def legacy_feed(path, *, url=FEED):
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS feed_subscriptions "
            "(id INTEGER PRIMARY KEY,name TEXT,url TEXT UNIQUE)"
        )
        connection.execute(
            "INSERT INTO feed_subscriptions(id,name,url) VALUES (7,?,?)",
            ("Existing saved feed", url),
        )


def modern_feeds(path):
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE feed_subscriptions "
            "(id INTEGER PRIMARY KEY,name TEXT,url TEXT UNIQUE,platform TEXT,"
            "locator TEXT,canonical_key TEXT,cursor TEXT)"
        )


def feed_rows(path):
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT * FROM feed_subscriptions ORDER BY id").fetchall()


def saved_item(*, channel="existing-channel", label="Existing source"):
    return SourceItem(
        platform="news",
        channel_id=channel,
        channel_name=label,
        native_id="post-1",
        text="A previously collected public announcement remains available for review.",
        source_url="https://publisher.example/announcements/post-1",
        acquired_at=CLOCK.isoformat(),
    )


def test_legacy_saved_feed_blocks_duplicate_live_registration_without_writes(desk):
    repository, service = desk
    legacy_feed(repository.db_path)
    original = feed_rows(repository.db_path)
    with pytest.raises(CollectionFailure, match="Saved feeds"):
        service.add(
            {
                "platform": "news",
                "locator": FEED + "?utm_source=duplicate#latest",
                "label": "Duplicate collection",
            }
        )
    assert repository.sources() == []
    assert repository.jobs() == []
    assert repository.items() == []
    assert feed_rows(repository.db_path) == original
    assert find_feed_owner(repository.db_path, "other", FEED)["id"] == 7


@pytest.mark.parametrize(
    ("platform", "stored_locator", "incoming_locator"),
    [
        ("x", "https://x.com/Sample", "@sample"),
        ("telegram", "https://t.me/s/TelegramTips", "telegramtips"),
        ("reddit", "https://www.reddit.com/r/WorldNews", "r/worldnews"),
    ],
)
def test_modern_saved_source_columns_prevent_alias_duplicates_without_changing_cursor(
    desk, platform, stored_locator, incoming_locator
):
    repository, service = desk
    modern_feeds(repository.db_path)
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute(
            "INSERT INTO feed_subscriptions VALUES (7,?,?,?,?,?,?)",
            (
                "Saved platform source",
                "https://saved.example/compatibility-url",
                platform,
                stored_locator,
                canonical_key(platform, stored_locator),
                "saved-cursor",
            ),
        )
    original = feed_rows(repository.db_path)
    with pytest.raises(CollectionFailure, match="Saved feeds"):
        service.add({"platform": platform, "locator": incoming_locator, "label": "Duplicate"})
    owner = find_feed_owner(repository.db_path, platform, incoming_locator)
    assert owner["id"] == 7
    assert feed_rows(repository.db_path) == original
    assert repository.sources() == []


def test_feed_and_youtube_aliases_share_ownership_across_supported_source_kinds(desk):
    repository, service = desk
    assert canonical_key("rss", FEED) == canonical_key("news", FEED)
    assert canonical_key("other", FEED + "?utm_source=reader") == canonical_key("news", FEED)
    assert canonical_key("youtube", YOUTUBE_CHANNEL) == canonical_key("rss", YOUTUBE_FEED)
    assert canonical_key("youtube", "https://www.youtube.com/channel/" + YOUTUBE_CHANNEL) == (
        canonical_key("news", YOUTUBE_FEED)
    )
    legacy_feed(repository.db_path, url=YOUTUBE_FEED)
    with pytest.raises(CollectionFailure, match="Saved feeds"):
        service.add({"platform": "youtube", "locator": YOUTUBE_CHANNEL, "label": "Videos"})
    assert repository.sources() == []
    assert len(feed_rows(repository.db_path)) == 1


def test_reciprocal_saved_feed_guard_rejects_new_owner_in_existing_write_transaction(desk):
    repository, service = desk
    source_id = service.add({"platform": "news", "locator": FEED, "label": "Live owner"})
    modern_feeds(repository.db_path)
    original = repository.source(source_id).model_dump()
    with pytest.raises(CollectionFailure, match="Live sources"):
        with sqlite3.connect(repository.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO feed_subscriptions VALUES (7,?,?,?,?,?,?)",
                (
                    "Transactional draft",
                    "https://unrelated.example/rss.xml",
                    "rss",
                    "https://unrelated.example/rss.xml",
                    "unrelated",
                    None,
                ),
            )
            assert_feed_owner_available(connection, "rss", FEED + "?utm_source=duplicate")
    assert feed_rows(repository.db_path) == []
    assert repository.source(source_id).model_dump() == original
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert_feed_owner_available(connection, "rss", "https://another.example/rss.xml")


def test_legacy_overlap_preserves_feed_owner_and_stops_duplicate_collection_requests(desk):
    repository, service = desk
    owned = service.add({"platform": "news", "locator": FEED, "label": "Existing source"})
    first = repository.enqueue(owned, now=CLOCK)
    claimed = repository.claim(now=CLOCK)
    assert claimed["id"] == first
    repository.complete(claimed, CollectedBatch(items=(saved_item(),)), now=CLOCK)
    original_items = repository.items(source_id=owned)
    queued_before_overlap = repository.enqueue(owned, now=CLOCK + timedelta(minutes=1))
    # This represents an older database that predates reciprocal registration guards.
    legacy_feed(repository.db_path)
    original_feed = feed_rows(repository.db_path)
    card = next(row for row in service.sources() if row["id"] == owned)
    assert card["managed_by"] == "saved_feed"
    assert card["ready"] is False
    with pytest.raises(CollectionFailure, match="Saved feeds"):
        service.enqueue(owned)
    with pytest.raises(CollectionFailure, match="Saved feeds"):
        service.enable(owned, True)
    service.enable(owned, False)
    other = service.add(
        {
            "platform": "news",
            "locator": "https://another.example/rss.xml",
            "label": "Other source",
        }
    )
    called = []

    def collect(config):
        called.append(config.id)
        return CollectedBatch(items=(saved_item(channel="other-channel", label="Other source"),))

    runtime = CollectionRuntime(repository, collector=collect)
    now = CLOCK + timedelta(minutes=2)
    assert runtime.run_once(now=now)
    failure = next(row for row in repository.jobs() if row["id"] == queued_before_overlap)
    assert failure["status"] == "failed"
    assert "no duplicate request" in failure["error"]
    assert called == []
    assert runtime.run_once(now=now)
    assert called == [other]
    assert runtime.run_once(now=now) is False
    assert repository.items(source_id=owned) == original_items
    assert feed_rows(repository.db_path) == original_feed


def test_root_alias_registration_reuses_existing_id_and_preserves_configuration(desk):
    repository, service = desk
    existing = CollectionSource(
        id="existing-stable-source-id",
        platform="news",
        label="Original live source",
        locator=FEED,
        enabled=False,
        item_limit=7,
        interval_minutes=30,
        cursor="retained-cursor",
    )
    repository.add(existing, now=CLOCK)
    assert (
        service.add(
            {
                "platform": "other",
                "locator": FEED + "?utm_source=reader",
                "label": "New alias label",
            }
        )
        == existing.id
    )
    assert repository.source(existing.id).model_dump() == existing.model_dump()
    x_id = service.add({"platform": "x", "locator": "Sample", "label": "Original X"})
    assert (
        service.add(
            {
                "platform": "x",
                "locator": "https://twitter.com/SAMPLE",
                "label": "X alias",
            }
        )
        == x_id
    )
    youtube_id = service.add(
        {
            "platform": "youtube",
            "locator": YOUTUBE_CHANNEL,
            "label": "Channel videos",
        }
    )
    assert (
        service.add(
            {
                "platform": "news",
                "locator": YOUTUBE_FEED,
                "label": "RSS alias",
            }
        )
        == youtube_id
    )
    assert len(repository.sources()) == 3
