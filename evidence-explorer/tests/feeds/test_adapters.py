"""Offline bridge checks: existing providers, stable provenance, and safe failures."""

import json
import socket

import pytest

from evidence_explorer.collection import registry
from evidence_explorer.collection.models import CollectedBatch, CollectionFailure
from evidence_explorer.feeds.adapters import (
    SourceAdapterError,
    collect_platform_feed,
    normalize_source,
    source_item_from_article,
    source_readiness,
)
from evidence_explorer.ingestion import FeedCollection
from evidence_explorer.review.integrations.sources import SourceItem

CHANNEL = "UCLA_DiR1FfKNvjuUpBHmylQ"
TEXT = "The source supplied this exact factual statement.\nA second line."


def supplied_item(platform, *, text=TEXT, published="2026-11-07T09:30:00Z"):
    values = {
        "telegram": (
            "publicnews",
            "123",
            "telegram_public",
            "post_text",
            "https://t.me/publicnews/123",
            "https://t.me/s/publicnews",
        ),
        "youtube": (
            CHANNEL,
            "dQw4w9WgXcQ",
            "youtube_feed",
            "title_description",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}",
        ),
        "reddit": (
            "science",
            "abc123",
            "reddit_api",
            "post_text",
            "https://www.reddit.com/r/science/comments/abc123/report/",
            "https://oauth.reddit.com/r/science/new",
        ),
        "x": (
            "98765",
            "12345",
            "x_api",
            "post_text",
            "https://x.com/example/status/12345",
            "https://api.x.com/2/users/98765/tweets",
        ),
    }
    channel, native, method, scope, url, acquired_url = values[platform]
    return SourceItem(
        platform=platform,
        channel_id=channel,
        native_id=native,
        channel_name="Example source",
        text=text,
        title="Source update",
        source_url=url,
        author_id="scoped-author-123",
        published_at=published,
        acquired_at="2026-11-07T10:00:00Z",
        shared_urls=["https://example.com/report?sig=A%2Fb%20C"],
        provenance={
            "method": method,
            "content_scope": scope,
            "retrieved_url": acquired_url,
            "published_at_raw": published,
        },
    )


def locator(platform):
    return {"telegram": "PublicNews", "youtube": CHANNEL, "reddit": "r/science", "x": "@example"}[
        platform
    ]


@pytest.mark.parametrize(
    "platform,raw,expected,url",
    [
        ("telegram", "https://t.me/s/PublicNews", "publicnews", "https://t.me/s/publicnews"),
        (
            "reddit",
            "https://www.reddit.com/r/Science/",
            "science",
            "https://www.reddit.com/r/science/",
        ),
        ("x", "https://twitter.com/EXAMPLE", "example", "https://x.com/example"),
        (
            "youtube",
            f"https://www.youtube.com/channel/{CHANNEL}",
            CHANNEL,
            f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}",
        ),
        (
            "rss",
            "https://news.example/rss?item=2&utm_source=feed",
            "https://news.example/rss?item=2",
            "https://news.example/rss?item=2",
        ),
    ],
)
def test_locator_normalization_reuses_existing_provider_contract_without_dns(
    monkeypatch,
    platform,
    raw,
    expected,
    url,
):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: pytest.fail("No DNS in normalization")
    )
    normalized = normalize_source(platform, raw)
    assert normalized.platform == platform
    assert normalized.locator == expected
    assert normalized.url == url


def test_youtube_handles_require_an_explicit_channel_id():
    with pytest.raises(ValueError, match="channel ID"):
        normalize_source("youtube", "https://www.youtube.com/@NASA")


@pytest.mark.parametrize("platform", ["telegram", "youtube", "reddit", "x"])
def test_platform_items_keep_native_identity_text_and_reconstruct_shared_contract(platform):
    item = supplied_item(platform)
    calls = []

    def collect(source):
        calls.append(source)
        return CollectedBatch(
            (item,), ("Only the supplied recent text window is available.",), "cursor-1"
        )

    result = collect_platform_feed(
        platform,
        locator(platform),
        name="Example source",
        topic="Transport",
        collector=collect,
    )
    assert len(calls) == 1
    assert (
        calls[0].platform == platform
        and calls[0].locator == normalize_source(platform, locator(platform)).locator
    )
    assert not result.errors
    article = result.articles[0].article
    assert article.text == item.text
    assert article.url == item.source_url
    assert article.published_at == item.published_at
    assert article.source_platform == platform
    assert article.source_channel_id == item.channel_id
    assert article.source_native_id == item.native_id
    assert article.source_author_id == item.author_id
    assert article.acquisition_method == item.provenance.method
    assert article.content_scope == item.provenance.content_scope
    assert article.content_source == "platform_post" and not article.is_sample
    assert article.publication_source == "platform"
    assert result.articles[0].provenance.final_url == item.provenance.retrieved_url
    assert result.cursor == "cursor-1" and result.warnings
    metadata = json.loads(article.source_metadata_json)
    assert "text" not in metadata
    assert metadata["shared_urls"] == item.shared_urls
    assert source_item_from_article(article) == item


def test_unknown_publication_stays_unknown_and_short_post_is_not_padded():
    item = supplied_item("telegram", text="OK", published=None)
    result = collect_platform_feed(
        "telegram",
        "publicnews",
        name="Example source",
        collector=lambda source: CollectedBatch((item,)),
    )
    assert not result.errors
    article = result.articles[0].article
    assert article.text == "OK"
    assert article.published_at == ""
    assert article.publication_source == "unknown"
    assert result.articles[0].provenance.published_at is None
    assert article.retrieved_at == item.acquired_at


def test_documented_repost_metadata_is_preserved_without_inventing_origin():
    payload = supplied_item("telegram").model_dump()
    payload["repost_of"] = {
        "platform": "telegram",
        "channel_id": "anotherchannel",
        "native_id": "88",
    }
    item = SourceItem.model_validate(payload)
    result = collect_platform_feed(
        "telegram",
        "publicnews",
        name="Example source",
        collector=lambda source: CollectedBatch((item,)),
    )
    rebuilt = source_item_from_article(result.articles[0].article)
    assert rebuilt.repost_of == item.repost_of


def test_duplicate_native_identity_is_not_imported_twice():
    item = supplied_item("x")
    result = collect_platform_feed(
        "x",
        "example",
        name="Example source",
        collector=lambda source: CollectedBatch((item, item)),
    )
    assert len(result.articles) == 1
    assert result.entries_seen == 2 and result.duplicates_skipped == 1


def test_provider_registry_is_resolved_at_call_time(monkeypatch):
    item = supplied_item("telegram")
    called = []
    monkeypatch.setattr(
        registry, "collect", lambda source: called.append(source) or CollectedBatch((item,))
    )
    result = collect_platform_feed("telegram", "publicnews", name="Example source")
    assert called and len(result.articles) == 1


def test_readiness_exposes_presence_only_and_never_credentials(monkeypatch):
    for name in (
        "X_BEARER_TOKEN",
        "REDDIT_ACCESS_TOKEN",
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    assert not source_readiness("x").ready
    assert not source_readiness("reddit").ready
    monkeypatch.setenv("X_BEARER_TOKEN", "SENSITIVE_BEARER_VALUE")
    monkeypatch.setenv("REDDIT_ACCESS_TOKEN", "SENSITIVE_REDDIT_VALUE")
    states = [source_readiness(platform).model_dump() for platform in ("x", "reddit")]
    assert all(state["ready"] for state in states)
    assert "SENSITIVE" not in json.dumps(states)


def test_known_rate_limit_is_safe_and_retains_retry_delay():
    def limited(source):
        raise CollectionFailure("X rate limit reached. Retry later.", retry_after_seconds=3600)

    with pytest.raises(SourceAdapterError) as caught:
        collect_platform_feed("x", "example", name="Example source", collector=limited)
    assert caught.value.retry_after_seconds == 3600
    assert "rate limit" in str(caught.value)


def test_unexpected_errors_do_not_expose_auth_headers():
    def broken(source):
        raise RuntimeError("Authorization: Bearer SENSITIVE_VALUE")

    with pytest.raises(SourceAdapterError) as caught:
        collect_platform_feed("x", "example", name="Example source", collector=broken)
    assert "SENSITIVE" not in str(caught.value)


def test_reconstruction_rejects_tampered_identity_and_uses_saved_body():
    item = supplied_item("reddit")
    article = (
        collect_platform_feed(
            "reddit",
            "science",
            name="Example source",
            collector=lambda source: CollectedBatch((item,)),
        )
        .articles[0]
        .article
    )
    record = article.model_dump()
    metadata = json.loads(record["source_metadata_json"])
    metadata["text"] = "Metadata must never replace the stored article body."
    record["source_metadata_json"] = json.dumps(metadata)
    assert source_item_from_article(record).text == item.text
    record["source_native_id"] = "different-record"
    with pytest.raises(SourceAdapterError):
        source_item_from_article(record)
    assert source_item_from_article({"text": TEXT, "url": "https://news.example/a"}) is None


def test_rss_dispatch_still_uses_existing_news_collector():
    called = []

    class News:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def collect_feed(self, url, **options):
            called.append((url, options))
            return FeedCollection(
                requested_url=url, final_url=url, retrieved_at="2026-11-07T10:00:00Z"
            )

    result = collect_platform_feed(
        "rss",
        "https://news.example/feed",
        name="Public news",
        content_mode="feed_content",
        news_collector_factory=News,
    )
    assert called[0][0] == "https://news.example/feed"
    assert called[0][1]["content_mode"] == "feed_content"
    assert result.articles == []
