"""Live subscriptions preserve bounded evidence, review snapshots, and retry state."""

from datetime import UTC, datetime, timedelta

import pytest

from evidence_explorer.collection.models import CollectedBatch, CollectionFailure, CollectionSource
from evidence_explorer.collection.persistence import CollectionRepository
from evidence_explorer.collection.runtime import CollectionRuntime
from evidence_explorer.collection.service import CollectionService
from evidence_explorer.config import Settings
from evidence_explorer.review.integrations.sources import SourceItem
from evidence_explorer.review.integrations.worker import WorkspaceWorker
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService

CLOCK = datetime(2035, 1, 2, 10, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    for name in (
        "X_BEARER_TOKEN",
        "REDDIT_ACCESS_TOKEN",
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_USER_AGENT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def store(tmp_path):
    repository = CollectionRepository(tmp_path / "collection.sqlite3")
    service = CollectionService(repository)
    return repository, service


def add_source(repository, *, platform="news", key="source", enabled=True, limit=5):
    source = CollectionSource(
        id=key,
        platform=platform,
        label="Public research source",
        locator=f"https://publisher.example/{key}/feed.xml" if platform == "news" else "sample",
        enabled=enabled,
        item_limit=limit,
    )
    assert repository.add(source, now=CLOCK) == source.id
    return source


def item(*, platform="news", native="post-1", channel="channel-1", text=None, acquired=CLOCK):
    return SourceItem(
        platform=platform,
        channel_id=channel,
        channel_name="Public research source",
        native_id=native,
        text=text or "A public inspection announcement was published for the bridge.",
        source_url=f"https://publisher.example/{channel}/{native}",
        author_id="public-author",
        published_at="2035-01-02T09:00:00Z",
        acquired_at=acquired.isoformat(),
        provenance={
            "method": "x_api" if platform == "x" else "rss_content",
            "content_scope": "post_text" if platform == "x" else "feed_content",
            "retrieved_url": "https://publisher.example/feed.xml",
        },
    )


def claim(repository, source_id, *, now=CLOCK):
    queued = repository.enqueue(source_id, now=now)
    job = repository.claim(now=now)
    assert job is not None and job["id"] == queued
    return job


def workspace(path):
    settings = Settings(data_dir=path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, path / "artifacts")
    return settings, repository, service


def test_source_configuration_is_canonical_idempotent_and_never_stores_credentials(
    store, monkeypatch
):
    repository, service = store
    monkeypatch.setenv("X_BEARER_TOKEN", "server-secret-token")
    first = service.add({"platform": "x", "locator": "https://x.com/Sample", "label": "Sample"})
    assert service.add({"platform": "x", "locator": "@sample", "label": "Sample"}) == first
    assert len(repository.sources()) == 1
    assert repository.source(first).locator == "sample"
    assert service.sources()[0]["ready"] is True
    assert "server-secret-token" not in str(service.status())
    with pytest.raises(CollectionFailure):
        service.add(
            {
                "platform": "reddit",
                "locator": "worldnews",
                "label": "World news",
                "access_token": "submitted-secret",
            }
        )
    assert len(repository.sources()) == 1
    assert "submitted-secret" not in str(service.status())


def test_runtime_schedules_public_sources_without_scheduling_unconnected_apis(store):
    repository, service = store
    public = add_source(repository)
    add_source(repository, platform="x", key="x-source")
    add_source(repository, platform="reddit", key="reddit-source")
    seen = []

    def collect(source):
        seen.append(source.id)
        return CollectedBatch(items=(item(),))

    runtime = CollectionRuntime(repository, collector=collect)
    assert runtime.run_once(now=CLOCK) is True
    assert runtime.run_once(now=CLOCK) is False
    assert seen == [public.id]
    assert len(repository.jobs()) == 1
    assert repository.jobs()[0]["status"] == "succeeded"
    unavailable = [row for row in service.sources() if row["platform"] in {"x", "reddit"}]
    assert all(row["ready"] is False and row["last_status"] == "idle" for row in unavailable)


def test_configured_api_is_scheduled_through_the_injected_collector(store, monkeypatch):
    repository, _ = store
    monkeypatch.setenv("X_BEARER_TOKEN", "fixture-token")
    source = add_source(repository, platform="x")
    seen = []

    def collect(config):
        seen.append(config)
        return CollectedBatch(items=(item(platform="x"),), cursor="100")

    assert CollectionRuntime(repository, collector=collect).run_once(now=CLOCK)
    assert [config.id for config in seen] == [source.id]
    assert repository.source(source.id).cursor == "100"
    assert repository.items()[0]["platform"] == "x"
    assert "fixture-token" not in str(repository.jobs()) + str(repository.items())


def test_scheduling_and_manual_collection_share_one_active_job(store):
    repository, _ = store
    source = add_source(repository)
    assert repository.schedule(now=CLOCK) == 1
    scheduled = repository.jobs()[0]["id"]
    assert repository.schedule(now=CLOCK) == 0
    assert repository.enqueue(source.id, now=CLOCK) == scheduled
    job = repository.claim(now=CLOCK)
    assert job["id"] == scheduled
    assert job["scheduled"] == 0
    assert repository.enqueue(source.id, now=CLOCK) == scheduled
    assert repository.claim(now=CLOCK) is None
    assert len(repository.jobs()) == 1


def test_pausing_cancels_scheduled_work_but_allows_an_explicit_collection(store):
    repository, _ = store
    source = add_source(repository)
    assert repository.schedule(now=CLOCK) == 1
    repository.set_enabled(source.id, False, now=CLOCK)
    assert repository.claim(now=CLOCK) is None
    assert repository.jobs()[0]["status"] == "cancelled"
    queued = repository.enqueue(source.id, now=CLOCK)
    runtime = CollectionRuntime(
        repository, collector=lambda config: CollectedBatch(items=(item(),))
    )
    assert runtime.run_once(now=CLOCK)
    job = next(job for job in repository.jobs() if job["id"] == queued)
    assert job["status"] == "succeeded"
    assert repository.source(source.id).enabled is False
    assert len(repository.items()) == 1
    assert runtime.run_once(now=CLOCK + timedelta(days=1)) is False


def test_refetch_is_idempotent_preserves_first_acquisition_and_saves_cursor(store):
    repository, _ = store
    source = add_source(repository)
    first = repository.complete(
        claim(repository, source.id), CollectedBatch(items=(item(),), cursor="first"), now=CLOCK
    )
    saved = repository.items()
    assert first == {"new": 1, "updated": 0, "unchanged": 0, "warnings": []}
    later = CLOCK + timedelta(minutes=1)
    second = repository.complete(
        claim(repository, source.id, now=later),
        CollectedBatch(items=(item(acquired=later),), cursor="second"),
        now=later,
    )
    assert second == {"new": 0, "updated": 0, "unchanged": 1, "warnings": []}
    assert repository.items() == saved
    reopened = CollectionRepository(repository.db_path)
    reopened.initialize()
    assert reopened.source(source.id).cursor == "second"
    assert reopened.items()[0]["acquired_at"] == "2035-01-02T10:00:00Z"


@pytest.mark.parametrize("bad_batch", ["platform", "duplicate", "invalid", "over_limit"])
def test_invalid_batches_never_partially_replace_saved_evidence(store, bad_batch):
    repository, _ = store
    source = add_source(repository, limit=2)
    repository.complete(claim(repository, source.id), CollectedBatch(items=(item(),)), now=CLOCK)
    original = repository.items()
    changed = item(text="This changed source text must roll back with the invalid batch.")
    batches = {
        "platform": (changed, item(platform="x", native="other")),
        "duplicate": (changed, changed),
        "invalid": (changed, SourceItem.model_construct(platform="news")),
        "over_limit": (changed, item(native="two"), item(native="three")),
    }
    job = claim(repository, source.id, now=CLOCK + timedelta(minutes=1))
    with pytest.raises(ValueError):
        repository.complete(job, CollectedBatch(items=batches[bad_batch]), now=CLOCK)
    assert repository.items() == original
    assert next(row for row in repository.jobs() if row["id"] == job["id"])["status"] == "running"


def test_unexpected_failure_retains_evidence_and_sanitizes_exception_text(store):
    repository, _ = store
    source = add_source(repository, enabled=False)
    repository.complete(claim(repository, source.id), CollectedBatch(items=(item(),)), now=CLOCK)
    original = repository.items()
    queued = repository.enqueue(source.id, now=CLOCK + timedelta(minutes=1))

    def fail(config):
        raise RuntimeError("private bearer-token-value from an HTTP exception")

    assert CollectionRuntime(repository, collector=fail).run_once(now=CLOCK + timedelta(minutes=1))
    job = next(row for row in repository.jobs() if row["id"] == queued)
    assert job["status"] == "failed"
    assert "private bearer-token" not in str(job) + str(repository.sources())
    assert repository.items() == original


def test_retry_after_blocks_manual_and_scheduled_requests_even_after_pause_resume(store):
    repository, _ = store
    source = add_source(repository)
    repository.fail(
        claim(repository, source.id),
        "Platform rate limit reached.",
        retry_after_seconds=600,
        now=CLOCK,
    )
    paused_at = CLOCK + timedelta(seconds=30)
    repository.set_enabled(source.id, False, now=paused_at)
    with pytest.raises(CollectionFailure, match="rate limited"):
        repository.enqueue(source.id, now=paused_at)
    repository.set_enabled(source.id, True, now=paused_at)
    other = add_source(repository, key="other-source")
    # A rate-limited source must not roll back scheduling for unrelated sources.
    assert repository.schedule(now=paused_at) == 1
    available = repository.claim(now=paused_at)
    assert available["source_id"] == other.id
    repository.complete(available, CollectedBatch(items=()), now=paused_at)
    assert repository.schedule(now=CLOCK + timedelta(seconds=599)) == 0
    assert repository.enqueue(source.id, now=CLOCK + timedelta(seconds=600))


def test_failed_collection_backs_off_and_success_restores_regular_interval(store):
    repository, _ = store
    source = add_source(repository)
    repository.fail(claim(repository, source.id), "Temporary network failure.", now=CLOCK)
    assert repository.schedule(now=CLOCK + timedelta(minutes=14)) == 0
    assert repository.schedule(now=CLOCK + timedelta(minutes=15)) == 1
    second = repository.claim(now=CLOCK + timedelta(minutes=15))
    repository.fail(second, "Temporary network failure.", now=CLOCK + timedelta(minutes=15))
    assert repository.schedule(now=CLOCK + timedelta(minutes=44)) == 0
    assert repository.schedule(now=CLOCK + timedelta(minutes=45)) == 1
    third = repository.claim(now=CLOCK + timedelta(minutes=45))
    repository.complete(third, CollectedBatch(items=(item(),)), now=CLOCK + timedelta(minutes=45))
    assert repository.schedule(now=CLOCK + timedelta(minutes=59)) == 0
    assert repository.schedule(now=CLOCK + timedelta(minutes=60)) == 1


def test_recovered_job_rejects_stale_success_and_failure_attempts(store):
    repository, _ = store
    source = add_source(repository)
    first = claim(repository, source.id)
    repository.recover()
    resumed = repository.claim(now=CLOCK + timedelta(minutes=1))
    assert resumed["id"] == first["id"]
    assert resumed["attempt"] > first["attempt"]
    with pytest.raises(ValueError, match="superseded"):
        repository.complete(first, CollectedBatch(items=(item(text="Stale source contents."),)))
    assert repository.items() == []
    repository.complete(resumed, CollectedBatch(items=(item(),)), now=CLOCK + timedelta(minutes=1))
    saved = repository.items()
    with pytest.raises(ValueError, match="superseded"):
        repository.fail(first, "A late failure must not replace the completed result.")
    assert repository.items() == saved
    assert repository.sources()[0]["last_status"] == "succeeded"


def test_same_native_ids_remain_separate_across_platforms_and_channels(store):
    repository, _ = store
    news = add_source(repository)
    x_source = add_source(repository, platform="x", key="x-source")
    repository.complete(
        claim(repository, news.id),
        CollectedBatch(items=(item(), item(channel="channel-2"))),
        now=CLOCK,
    )
    repository.complete(
        claim(repository, x_source.id),
        CollectedBatch(items=(item(platform="x"),)),
        now=CLOCK,
    )
    identities = {
        (record["platform"], record["channel_id"], record["native_id"])
        for record in repository.items()
    }
    assert identities == {
        ("news", "channel-1", "post-1"),
        ("news", "channel-2", "post-1"),
        ("x", "channel-1", "post-1"),
    }
    assert len(repository.items(source_id=news.id)) == 2
    assert [row["platform"] for row in repository.items(source_id=x_source.id)] == ["x"]
    assert repository.items(source_id="missing-source") == []


def test_workspace_worker_opens_updated_collection_without_changing_saved_review(tmp_path):
    settings, review_repository, service = workspace(tmp_path / "original")
    source_id = service.add_collection_source(
        {
            "platform": "news",
            "locator": "https://publisher.example/feed.xml",
            "label": "Public research source",
            "enabled": False,
        }
    )
    worker = WorkspaceWorker(review_repository, settings)
    worker.collection_runtime.collector = lambda source: CollectedBatch(items=(item(),))
    service.collect_source(source_id)
    assert worker.drain() == 1
    first_case = service.open_collection_case()
    assert first_case is not None
    first = service.load_case(first_case)
    selected = first.findings[0].snapshot_id
    assessment = service.baseline_scenario(first_case, selected)
    decision = service.review(
        first_case,
        selected,
        assessment["id"],
        "qualify",
        "This assessment describes the source snapshot collected at this time.",
    )
    original = service.export_bundle(first_case)
    worker.collection_runtime.collector = lambda source: CollectedBatch(
        items=(
            item(
                text="The publisher has revised the saved announcement.",
                acquired=CLOCK + timedelta(minutes=1),
            ),
        )
    )
    service.collect_source(source_id)
    assert worker.drain() == 1
    second_case = service.open_collection_case()
    assert second_case != first_case
    assert service.export_bundle(first_case).model_dump() == original.model_dump()
    assert (
        service.load_case(second_case).records[0].text
        == "The publisher has revised the saved announcement."
    )
    assert service.collection_status()["jobs"][0]["result"]["updated"] == 1
    _, _, restored = workspace(tmp_path / "restored")
    assert restored.import_package(service.export_package(first_case)) == first_case
    reopened = restored.load_case(first_case)
    assert any(review.id == decision["id"] for review in reopened.reviews)
    assert reopened.model_dump() == original.model_dump()
