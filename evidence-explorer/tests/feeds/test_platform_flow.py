"""Platform HTTP adapters share saved-source scheduling and cached local analysis."""

import json
from dataclasses import replace
from datetime import datetime, timedelta

import httpx
import pytest

from evidence_explorer import analysis
from evidence_explorer.collection.apis import collect_api
from evidence_explorer.collection.public import collect_public
from evidence_explorer.feeds.runtime import FeedScheduler
from evidence_explorer.models_client import OllamaClient
from evidence_explorer.service import ExplorerService
from evidence_explorer.worker import run_worker

QUOTE = "The transport authority confirmed that the bridge remains closed today."
CHANNEL = "UC1234567890123456789012"
VIDEO = "abcdefghijk"
PUBLIC_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def platform_credentials(monkeypatch):
    for name in (
        "X_BEARER_TOKEN",
        "REDDIT_ACCESS_TOKEN",
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_USER_AGENT",
    ):
        monkeypatch.delenv(name, raising=False)


def provider_transport(platform, calls, *, failure=None, text=QUOTE):
    def respond(request):
        calls.append(request)
        if failure is not None:
            return httpx.Response(
                failure,
                text="do-not-display provider body or fixture-private-token",
                headers={"retry-after": "3600"},
            )
        if platform == "telegram":
            assert str(request.url) == "https://t.me/s/example"
            page = (
                '<div class="tgme_widget_message" data-post="example/10">'
                f'<div class="tgme_widget_message_text">{text}</div>'
                '<time datetime="2026-11-06T10:30:00+00:00"></time></div>'
            )
            return httpx.Response(200, text=page, headers={"content-type": "text/html"})
        if platform == "youtube":
            assert request.url.host == "www.youtube.com"
            assert request.url.path in {"/xml/feeds/videos.xml", "/feeds/videos.xml"}
            assert request.url.params["channel_id"] == CHANNEL
            feed = (
                '<feed xmlns="http://www.w3.org/2005/Atom" '
                'xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
                'xmlns:media="http://search.yahoo.com/mrss/">'
                f"<id>yt:channel:{CHANNEL}</id><title>Bridge videos</title>"
                f"<entry><id>yt:video:{VIDEO}</id><yt:videoId>{VIDEO}</yt:videoId>"
                f"<yt:channelId>{CHANNEL}</yt:channelId><title>Bridge update</title>"
                f'<link rel="alternate" href="https://www.youtube.com/watch?v={VIDEO}"/>'
                "<published>2026-11-06T10:30:00Z</published><media:group>"
                f"<media:description>{text}</media:description></media:group>"
                "</entry></feed>"
            )
            return httpx.Response(200, text=feed, headers={"content-type": "application/atom+xml"})
        if platform == "reddit":
            assert request.url.host == "oauth.reddit.com"
            assert request.url.path == "/r/science/new"
            assert request.headers["Authorization"] == "Bearer fixture-private-token"
            return httpx.Response(
                200,
                json={
                    "kind": "Listing",
                    "data": {
                        "after": None,
                        "children": [
                            {
                                "kind": "t3",
                                "data": {
                                    "id": "abc",
                                    "subreddit": "science",
                                    "title": "Bridge inspection",
                                    "selftext": text,
                                    "permalink": "/r/science/comments/abc/bridge_inspection/",
                                    "author": "public_author",
                                    "created_utc": 1793966400,
                                },
                            }
                        ],
                    },
                },
            )
        assert platform == "x"
        assert request.url.host == "api.x.com"
        assert request.headers["Authorization"] == "Bearer fixture-private-token"
        if request.url.path == "/2/users/by/username/example":
            return httpx.Response(200, json={"data": {"id": "123", "username": "example"}})
        assert request.url.path == "/2/users/123/tweets"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "100",
                        "author_id": "123",
                        "text": text,
                        "created_at": "2026-11-06T10:30:00Z",
                    }
                ]
            },
        )

    return httpx.MockTransport(respond)


def install_local_models(settings, monkeypatch, *, selected_excerpts=None):
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": settings.embedding_model, "digest": "embedding-fixture"},
                        {"name": settings.chat_model, "digest": "qwen-fixture"},
                    ]
                },
            )
        body = json.loads(request.content)
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[1.0, 0.5] for text in body["input"]]})
        assert request.url.path == "/api/chat"
        candidates = json.loads(body["messages"][1]["content"])["candidates"]
        candidate = next(item for item in candidates if QUOTE in item["text"])
        if selected_excerpts is not None:
            selected_excerpts.append(candidate)
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps({"claims": [{"candidate_id": candidate["candidate_id"]}]})
                }
            },
        )

    monkeypatch.setattr(
        analysis,
        "OllamaClient",
        lambda runtime_settings: OllamaClient(
            runtime_settings, transport=httpx.MockTransport(respond)
        ),
    )
    return calls


def collect_mocked(source, transport):
    collector = collect_api if source.platform in {"x", "reddit"} else collect_public
    return collector(source, transport=transport, resolver=lambda host: [PUBLIC_IP])


def install_platform(monkeypatch, platform, *, failure=None, text=QUOTE):
    from evidence_explorer.collection import registry

    calls = []
    if platform == "x":
        monkeypatch.setenv("X_BEARER_TOKEN", "fixture-private-token")
    elif platform == "reddit":
        monkeypatch.setenv("REDDIT_ACCESS_TOKEN", "fixture-private-token")
        monkeypatch.setenv("REDDIT_USER_AGENT", "python:local.fixture:v1 (by /u/test_user)")
    transport = provider_transport(platform, calls, failure=failure, text=text)
    monkeypatch.setattr(registry, "collect", lambda source: collect_mocked(source, transport))
    return calls


def save_source(service, platform, *, analyze=True):
    locator = CHANNEL if platform == "youtube" else "science" if platform == "reddit" else "example"
    return service.save_feed(
        f"Test {platform} updates",
        platform=platform,
        locator=locator,
        topic="Bridge inspection",
        item_limit=1,
        interval_minutes=5,
        analyze=analyze,
    )


@pytest.mark.parametrize("platform", ["telegram", "youtube", "reddit", "x"])
def test_platform_schedule_collects_persists_and_runs_cached_local_model_pipeline(
    settings, monkeypatch, platform
):
    provider_calls = install_platform(monkeypatch, platform)
    selected_excerpts = []
    model_calls = install_local_models(settings, monkeypatch, selected_excerpts=selected_excerpts)
    service = ExplorerService(settings)
    saved = save_source(service, platform)
    assert provider_calls == model_calls == []
    tick = FeedScheduler(service.repo).tick(force=True)
    assert len(tick.queued_job_ids) == 1
    assert provider_calls == []  # The scheduler only queues; the worker collects.

    assert run_worker(settings, once=True) == 2
    stored = service.repo.feeds.get(saved["id"])
    assert stored.platform == platform
    assert stored.last_result["imported"] == 1
    assert stored.last_error is None
    article_id = stored.last_result["article_ids"][0]
    article = service.repo.articles.get_article(article_id)
    assert not article.is_sample
    assert article.content_source == "platform_post"
    assert article.source_platform == platform
    assert (
        article.source_channel_id
        == {
            "telegram": "example",
            "youtube": CHANNEL,
            "reddit": "science",
            "x": "123",
        }[platform]
    )
    assert (
        article.source_author_id
        == {
            "telegram": None,
            "youtube": CHANNEL,
            "reddit": "public_author",
            "x": "123",
        }[platform]
    )
    assert (
        article.source_native_id
        == {"telegram": "10", "youtube": VIDEO, "reddit": "abc", "x": "100"}[platform]
    )
    assert (
        article.acquisition_method
        == {
            "telegram": "telegram_public",
            "youtube": "youtube_feed",
            "reddit": "reddit_api",
            "x": "x_api",
        }[platform]
    )
    assert article.content_scope == ("title_description" if platform == "youtube" else "post_text")
    expected_text = {
        "telegram": QUOTE,
        "youtube": f"Bridge update\n\n{QUOTE}",
        "reddit": f"Bridge inspection\n\n{QUOTE}",
        "x": QUOTE,
    }[platform]
    assert article.text == expected_text
    assert service.search_keyword("bridge")
    chunks = service.repo.passages.list_chunks(article_id)
    claims = service.repo.claims.list_claims(article_id)
    assert len(claims) == len(selected_excerpts) == 1
    selected = selected_excerpts[0]
    claim = claims[0]
    assert claim.quote == claim.claim_text == selected["text"] == expected_text
    assert claim.chunk_id == selected["chunk_id"]
    assert article.text[selected["start_char"] : selected["end_char"]] == claim.quote
    supporting_chunk = next(chunk for chunk in chunks if chunk.id == claim.chunk_id)
    assert claim.quote in supporting_chunk.text
    model_job = service.repo.jobs.get_record(stored.last_analysis_job_id)
    assert model_job.status == "succeeded"
    assert model_job.result["mode"] == "ollama"
    metadata = article.source_metadata_json
    metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
    assert metadata["platform"] == platform
    assert "text" not in metadata
    assert "fixture-private-token" not in article.model_dump_json()
    assert "fixture-private-token" not in json.dumps(service.list_feeds())
    assert model_calls.count("/api/chat") == model_calls.count("/api/embed") == 1

    # Reopen the database, then refresh the same native item through the same path.
    service = ExplorerService(settings)
    service.enqueue_feed_refresh(saved["id"])
    assert run_worker(settings, once=True) == 2
    refreshed = service.repo.feeds.get(saved["id"])
    assert refreshed.last_result["unchanged"] == 1
    assert service.repo.articles.count() == 1
    cached = service.repo.jobs.get_record(refreshed.last_analysis_job_id)
    assert cached.result["cached"] == 1
    assert cached.result["claims_created"] == cached.result["embeddings_created"] == 0
    assert model_calls.count("/api/chat") == model_calls.count("/api/embed") == 1
    assert service.repo.claims.list_claims(article_id)[0].quote == expected_text
    assert len(selected_excerpts) == 1
    assert all("publisher.example" != request.url.host for request in provider_calls)


def test_youtube_404_preserves_last_successful_refresh_and_analysis(settings, monkeypatch):
    install_platform(monkeypatch, "youtube")
    model_calls = install_local_models(settings, monkeypatch)
    service = ExplorerService(settings)
    feed_id = save_source(service, "youtube")["id"]
    assert run_worker(settings, once=True) == 2
    previous = service.repo.feeds.get(feed_id)
    assert previous.last_refreshed_at and previous.cursor == VIDEO
    analysis_id = previous.last_analysis_job_id
    assert service.repo.jobs.get_record(analysis_id).status == "succeeded"
    article_id = previous.last_result["article_ids"][0]
    article = service.repo.articles.get_article(article_id)
    claims = service.repo.claims.list_claims(article_id)
    assert claims
    calls_before_failure = model_calls.copy()

    failed_requests = install_platform(monkeypatch, "youtube", failure=404)
    job_id = service.enqueue_feed_refresh(feed_id)
    assert run_worker(settings, once=True) == 1
    job = service.repo.jobs.get_record(job_id)
    failed = service.repo.feeds.get(feed_id)
    assert job.status == "failed"
    assert "YouTube" in job.error and "HTTP 404" in job.error
    assert failed.last_error == job.error
    assert "do-not-display" not in job.model_dump_json()
    assert "fixture-private-token" not in job.model_dump_json()
    assert failed.last_refreshed_at == previous.last_refreshed_at
    assert failed.cursor == previous.cursor
    assert failed.last_analysis_job_id == analysis_id
    assert failed.consecutive_failures == 1
    assert datetime.fromisoformat(failed.next_refresh_at) >= datetime.fromisoformat(
        failed.last_finished_at
    ) + timedelta(minutes=previous.interval_minutes)
    assert service.repo.articles.get_article(article_id) == article
    assert service.repo.claims.list_claims(article_id) == claims
    assert [job.id for job in service.repo.jobs.list_records() if job.kind == "analyze"] == [
        analysis_id
    ]
    assert model_calls == calls_before_failure
    assert len(failed_requests) == 1

    install_platform(monkeypatch, "youtube")
    service.enqueue_feed_refresh(feed_id)
    assert run_worker(settings, once=True) == 2
    recovered = service.repo.feeds.get(feed_id)
    assert recovered.last_error is None and recovered.consecutive_failures == 0
    assert recovered.last_result["unchanged"] == 1
    assert recovered.last_result["article_ids"] == [article_id]
    cached = service.repo.jobs.get_record(recovered.last_analysis_job_id)
    assert cached.result["cached"] == 1
    assert model_calls.count("/api/chat") == model_calls.count("/api/embed") == 1


@pytest.mark.parametrize("platform", ["telegram", "youtube", "reddit", "x"])
def test_short_platform_posts_are_stored_without_padding_or_ollama(settings, monkeypatch, platform):
    install_platform(monkeypatch, platform, text="A")
    model_calls = install_local_models(settings, monkeypatch)
    service = ExplorerService(settings)
    saved = save_source(service, platform, analyze=False)
    assert run_worker(settings, once=True) == 1
    stored = service.repo.feeds.get(saved["id"])
    assert stored.last_result["imported"] == 1
    article = service.repo.articles.get_article(stored.last_result["article_ids"][0])
    assert article.content_source == "platform_post"
    assert 0 < len(article.text) < 30
    if platform in {"telegram", "x"}:
        assert article.text == "A"
    assert stored.last_analysis_job_id is None
    assert model_calls == []


@pytest.mark.parametrize("platform", ["reddit", "x"])
def test_missing_api_credentials_block_saving_before_network(settings, monkeypatch, platform):
    calls = []
    from evidence_explorer.collection import registry

    monkeypatch.setattr(registry, "collect", lambda source: calls.append(source))
    service = ExplorerService(settings)
    with pytest.raises(ValueError, match="[Cc]onnect|credential|token|configured"):
        save_source(service, platform)
    assert service.list_feeds() == []
    assert service.repo.jobs.list_records() == []
    assert calls == []


@pytest.mark.parametrize("platform", ["reddit", "x"])
def test_credentials_removed_after_saving_fail_clearly_before_network(
    settings, monkeypatch, platform
):
    calls = install_platform(monkeypatch, platform)
    model_calls = install_local_models(settings, monkeypatch)
    service = ExplorerService(settings)
    saved = save_source(service, platform)
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("REDDIT_ACCESS_TOKEN", raising=False)
    assert run_worker(settings, once=True) == 1
    stored = service.repo.feeds.get(saved["id"])
    assert stored.last_error and "connect" in stored.last_error.lower()
    assert service.repo.jobs.get_record(stored.last_job_id).status == "failed"
    assert calls == model_calls == []
    assert service.repo.articles.count() == 0


@pytest.mark.parametrize("platform", ["reddit", "x"])
@pytest.mark.parametrize("status", [401, 403, 429])
def test_api_failure_is_visible_redacted_and_does_not_start_local_models(
    settings, monkeypatch, platform, status
):
    calls = install_platform(monkeypatch, platform, failure=status)
    model_calls = install_local_models(settings, monkeypatch)
    service = ExplorerService(settings)
    saved = save_source(service, platform)
    assert run_worker(settings, once=True) == 1
    stored = service.repo.feeds.get(saved["id"])
    job = service.repo.jobs.get_record(stored.last_job_id)
    assert job.status == "failed"
    assert stored.last_error
    assert "fixture-private-token" not in stored.model_dump_json()
    assert "do-not-display" not in stored.last_error
    assert service.repo.articles.count() == 0
    assert model_calls == []
    assert len(calls) == 1
    if status == 429:
        last_finished = datetime.fromisoformat(stored.last_finished_at)
        next_refresh = datetime.fromisoformat(stored.next_refresh_at)
        assert next_refresh >= last_finished + timedelta(minutes=59)


def test_read_only_platform_snapshot_cannot_save_or_refresh(settings, tmp_path, monkeypatch):
    calls = install_platform(monkeypatch, "telegram")
    service = ExplorerService(settings)
    saved = save_source(service, "telegram", analyze=False)
    path = tmp_path / "snapshot" / "evidence.sqlite3"
    service.repo.backup(path)
    snapshot = ExplorerService(replace(settings, data_dir=path.parent, read_only=True))
    before = path.read_bytes()
    assert FeedScheduler(snapshot.repo).tick(force=True) is None
    with pytest.raises(PermissionError):
        save_source(snapshot, "telegram", analyze=False)
    with pytest.raises(PermissionError):
        snapshot.enqueue_feed_refresh(saved["id"])
    assert calls == []
    assert path.read_bytes() == before
