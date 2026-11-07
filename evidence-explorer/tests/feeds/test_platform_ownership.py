"""One canonical source has one scheduler, even across both source configuration UIs."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest

from evidence_explorer.collection.models import CollectionFailure
from evidence_explorer.collection.persistence import CollectionRepository
from evidence_explorer.collection.service import CollectionService
from evidence_explorer.db import Repository
from evidence_explorer.persistence.migrations import migrate
from evidence_explorer.persistence.schema import MIGRATIONS
from evidence_explorer.service import ExplorerService
from evidence_explorer.source_identity import canonical_source_key
from evidence_explorer.worker import run_worker

CHANNEL = "UC1234567890123456789012"


def live_service(settings):
    return CollectionService(CollectionRepository(settings.db_path))


@pytest.mark.parametrize(
    ("platform", "locator", "live_platform", "live_locator"),
    [
        ("telegram", "@Example", "telegram", "https://t.me/s/example"),
        ("youtube", CHANNEL, "youtube", f"https://www.youtube.com/channel/{CHANNEL}"),
        (
            "rss",
            "https://news.example/feed.xml?utm_source=a",
            "news",
            "https://news.example/feed.xml",
        ),
        (
            "rss",
            "https://social.example/profile.rss",
            "other",
            "https://social.example/profile.rss",
        ),
    ],
)
def test_existing_feed_blocks_duplicate_live_registration(
    settings, platform, locator, live_platform, live_locator
):
    explorer = ExplorerService(settings)
    feed = explorer.save_feed("Source", locator, platform=platform, analyze=False)
    service = live_service(settings)
    with pytest.raises(CollectionFailure, match="Saved feeds"):
        service.add({"platform": live_platform, "label": "Duplicate", "locator": live_locator})
    assert not service.repository.sources()
    assert explorer.repo.feeds.get(feed["id"]).enabled


def test_existing_live_source_blocks_new_feed_even_when_paused(settings):
    service = live_service(settings)
    service.add({"platform": "telegram", "label": "Source", "locator": "example", "enabled": False})
    explorer = ExplorerService(settings)
    with pytest.raises(ValueError, match="Live sources"):
        explorer.save_feed("Duplicate", platform="telegram", locator="https://t.me/example")
    assert explorer.list_feeds() == []
    assert explorer.repo.jobs.list_records() == []


def test_competing_registrations_cannot_create_two_pollers(settings):
    explorer = ExplorerService(settings)
    service = live_service(settings)
    barrier = Barrier(2)

    def save_feed():
        barrier.wait()
        try:
            explorer.save_feed("Source", platform="telegram", locator="example", analyze=False)
            return "feed"
        except ValueError:
            return "refused"

    def save_live():
        barrier.wait()
        try:
            service.add({"platform": "telegram", "label": "Source", "locator": "example"})
            return "live"
        except ValueError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save_feed), pool.submit(save_live)]
        outcomes = [future.result() for future in futures]
    assert outcomes.count("refused") == 1
    assert len(explorer.list_feeds()) + len(service.repository.sources()) == 1


def test_platform_aliases_share_one_record_and_one_active_job(settings):
    explorer = ExplorerService(settings)
    first = explorer.save_feed("Source", platform="telegram", locator="@Example", analyze=False)
    alias = explorer.save_feed(
        "Source renamed", platform="telegram", locator="https://t.me/s/example"
    )
    assert alias["id"] == first["id"]
    job_id = explorer.enqueue_feed_refresh(first["id"])
    assert explorer.enqueue_feed_refresh(alias["id"]) == job_id
    assert explorer.repo.jobs.claim_record().id == job_id
    assert explorer.repo.feeds.queue_due() == []
    assert len(explorer.repo.jobs.list_records()) == 1


def test_youtube_rss_and_provider_share_canonical_identity(settings):
    assert canonical_source_key(
        "rss", f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}"
    ) == canonical_source_key("youtube", CHANNEL)
    assert canonical_source_key(
        "other", f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={CHANNEL}"
    ) == canonical_source_key("youtube", CHANNEL)
    explorer = ExplorerService(settings)
    explorer.save_feed(
        "Existing video RSS",
        f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}",
        analyze=False,
    )
    with pytest.raises(CollectionFailure, match="Saved feeds"):
        live_service(settings).add(
            {"platform": "youtube", "label": "Duplicate", "locator": CHANNEL}
        )


def test_v6_upgrade_and_read_only_view_preserve_existing_feed_history(settings):
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat()
    with sqlite3.connect(settings.db_path) as connection:
        migrate(connection, "explorer", MIGRATIONS[:6])
        connection.execute(
            "INSERT INTO feed_subscriptions(name,url,topic,enabled,next_refresh_at,"
            "last_result,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                "Mastodon",
                "https://social.example/profile.rss",
                "Posts",
                0,
                stamp,
                json.dumps({"imported": 2}),
                stamp,
                stamp,
            ),
        )
    before = settings.db_path.read_bytes()
    snapshot = Repository(settings.db_path, read_only=True)
    snapshot.initialize()
    old = snapshot.feeds.list_page()[0]
    assert old.platform == "rss" and not old.enabled
    assert settings.db_path.read_bytes() == before
    repo = Repository(settings.db_path)
    repo.initialize()
    upgraded = repo.feeds.list_page()[0]
    assert upgraded.id == old.id and not upgraded.enabled
    assert upgraded.last_result == {"imported": 2}
    assert upgraded.url == old.url and upgraded.locator == old.url
    assert upgraded.canonical_key == "rss:" + old.url


def test_collection_owned_records_can_be_analyzed_without_a_second_fetch(settings, monkeypatch):
    from evidence_explorer.collection import registry
    from evidence_explorer.collection.models import CollectedBatch
    from evidence_explorer.collection.runtime import CollectionRuntime
    from evidence_explorer.review.integrations.sources import SourceAcquisition, SourceItem

    service = live_service(settings)
    source_id = service.add(
        {"platform": "telegram", "label": "Source", "locator": "example", "enabled": False}
    )
    service.enqueue(source_id)
    item = SourceItem(
        platform="telegram",
        channel_id="example",
        native_id="10",
        channel_name="Source",
        text="The public bulletin confirms the bridge remains closed today.",
        source_url="https://t.me/example/10",
        acquired_at=datetime.now(UTC).isoformat(),
        provenance=SourceAcquisition(
            method="telegram_public",
            content_scope="post_text",
            retrieved_url="https://t.me/s/example",
        ),
    )
    runtime = CollectionRuntime(
        service.repository, collector=lambda source: CollectedBatch(items=(item,))
    )
    assert runtime.run_once()
    monkeypatch.setattr(
        registry, "collect", lambda source: pytest.fail("Saved record projection fetched a source.")
    )
    explorer = ExplorerService(settings)
    job_id = explorer.enqueue_collected_source_analysis(source_id, analyze=False)
    assert run_worker(settings, once=True) == 1
    job = explorer.repo.jobs.get_record(job_id)
    assert job.status == "succeeded", job.error
    assert job.result["imported"] == 1 and job.result["analysis_job_id"] is None
    assert "no new platform request" in job.result["warnings"][0]
    record = explorer.repo.articles.get_article(job.result["article_ids"][0])
    assert record.source_platform == "telegram" and record.source_native_id == "10"
    assert explorer.list_feeds() == []
