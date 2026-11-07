"""Both feed consumers retain their distinct policies over shared traversal."""

from html import escape

import httpx
import pytest

from evidence_explorer.collection.models import CollectionFailure, CollectionSource
from evidence_explorer.collection.public import collect_public
from evidence_explorer.ingestion.collector import NewsCollector
from tests.ingestion.fixtures import PUBLIC_IP, article_html

FEED_URL = "https://news.example/feed.xml"
TEXT = "This is saved feed text with enough context to inspect the exact original source statement."


def feed(*links):
    entries = []
    for link in links:
        locator = f"<link>{escape(link)}</link>" if link is not None else ""
        entries.append(
            f"<item><title>Source</title>{locator}<description>{TEXT}</description></item>"
        )
    return (
        '<rss version="2.0"><channel><title>News</title>'
        "<link>https://news.example/</link><description>Updates</description>"
        + "".join(entries)
        + "</channel></rss>"
    )


def collect(adapter, body, requests, *, statuses=None, limit=5, content_mode="article_links"):
    statuses = statuses or {}

    def respond(request):
        requests.append(str(request.url))
        if request.url.path == "/feed.xml":
            return httpx.Response(200, text=body, headers={"content-type": "application/rss+xml"})
        return httpx.Response(
            statuses.get(request.url.path, 200),
            text=article_html(),
            headers={"content-type": "text/html"},
        )

    options = {"transport": httpx.MockTransport(respond), "resolver": lambda host: [PUBLIC_IP]}
    if adapter == "ingestion":
        with NewsCollector(**options) as collector:
            return collector.collect_feed(FEED_URL, limit=limit, content_mode=content_mode)
    return collect_public(
        CollectionSource(
            id="source",
            platform="news",
            label="News",
            locator=FEED_URL,
            item_limit=limit,
            content_mode=content_mode,
        ),
        **options,
    )


@pytest.mark.parametrize("adapter", ["ingestion", "collection"])
def test_failed_link_duplicate_does_not_spend_another_attempt(adapter):
    requests = []
    result = collect(
        adapter,
        feed("/bad", "/bad#again", "/good", "/unreached"),
        requests,
        statuses={"/bad": 404},
        limit=2,
    )
    assert requests == [FEED_URL, "https://news.example/bad", "https://news.example/good"]
    if adapter == "ingestion":
        assert len(result.articles) == len(result.errors) == result.duplicates_skipped == 1
    else:
        assert len(result.items) == 1
        assert "1 feed entries were unavailable, invalid, or unreadable." in result.warnings


@pytest.mark.parametrize("adapter", ["ingestion", "collection"])
def test_missing_and_invalid_links_exhaust_the_attempt_budget(adapter):
    requests = []
    body = feed(None, "javascript:invalid", "/unreached")
    if adapter == "ingestion":
        result = collect(adapter, body, requests, limit=2)
        assert result.articles == []
        assert len(result.errors) == 2
        assert result.errors[0].code == "missing_link"
    else:
        with pytest.raises(CollectionFailure, match="no collectable text"):
            collect(adapter, body, requests, limit=2)
    assert requests == [FEED_URL]


@pytest.mark.parametrize("adapter", ["ingestion", "collection"])
def test_feed_content_never_fetches_permalinks(adapter):
    requests = []
    result = collect(
        adapter, feed("/post", "/post#duplicate"), requests, content_mode="feed_content"
    )
    assert requests == [FEED_URL]
    if adapter == "ingestion":
        assert len(result.articles) == 1 and result.duplicates_skipped == 1
        assert result.articles[0].article.text == TEXT
    else:
        assert len(result.items) == 1 and result.items[0].text == TEXT
        assert any("full article pages were not fetched" in warning for warning in result.warnings)


def test_rate_limit_is_an_entry_error_for_ingestion_and_a_batch_failure_for_collection():
    body = feed("/good", "/limited", "/later")
    requests = []
    result = collect("ingestion", body, requests, statuses={"/limited": 429})
    assert len(result.articles) == 2 and len(result.errors) == 1
    assert requests[-1] == "https://news.example/later"
    requests = []
    with pytest.raises(CollectionFailure) as failure:
        collect("collection", body, requests, statuses={"/limited": 429})
    assert failure.value.retry_after_seconds == 300
    assert requests == [FEED_URL, "https://news.example/good", "https://news.example/limited"]
