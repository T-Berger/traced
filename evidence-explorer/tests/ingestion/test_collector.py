"""Collection preserves evidence metadata and validates every network hop."""

import gzip
from datetime import UTC, datetime

import httpx
import pytest

from evidence_explorer.ingestion import CollectionError, NewsCollector, normalize_url

from .fixtures import ATOM, PUBLIC_IP, QUOTE, RSS, article_html


def public_resolver(hostname):
    return [PUBLIC_IP]


def make_collector(handler, **kwargs):
    return NewsCollector(transport=httpx.MockTransport(handler), resolver=public_resolver, **kwargs)


def test_html_article_retains_metadata_and_verbatim_evidence():
    requested = "https://news.example/articles/bridge?utm_source=feed&id=42#details"
    before = datetime.now(UTC)
    with make_collector(
        lambda request: httpx.Response(
            200, text=article_html(), headers={"content-type": "text/html; charset=utf-8"}
        )
    ) as collector:
        collected = collector.collect_article(requested, topic="River flooding")
    assert collected.article.title == "Rivergate bridge inspection update"
    assert collected.article.publisher == "Example Newsroom"
    assert collected.article.topic == "River flooding"
    assert collected.article.is_sample is False
    assert collected.article.language == "en"
    assert QUOTE in collected.article.text
    assert "Subscribe to our newsletter" not in collected.article.text
    assert collected.article.url == "https://news.example/articles/bridge?id=42"
    assert collected.provenance.requested_url == requested
    assert collected.provenance.final_url == collected.article.url
    assert collected.provenance.author == "Alex Example"
    assert collected.provenance.publication_source
    published = datetime.fromisoformat(collected.article.published_at.replace("Z", "+00:00"))
    assert published == datetime(2026, 11, 1, 8, 30, tzinfo=UTC)
    retrieved = datetime.fromisoformat(collected.provenance.retrieved_at.replace("Z", "+00:00"))
    assert before <= retrieved <= datetime.now(UTC)


def test_missing_publication_time_is_not_replaced_by_retrieval_time():
    with make_collector(
        lambda request: httpx.Response(
            200, text=article_html(published=False), headers={"content-type": "text/html"}
        )
    ) as collector:
        collected = collector.collect_article("https://news.example/articles/undated")
    assert collected.article.published_at == ""
    assert collected.provenance.published_at is None
    assert collected.provenance.retrieved_at


def test_modified_time_is_not_mislabeled_as_publication_time():
    body = article_html(published=False).replace(
        "</head>", '<meta property="article:modified_time" content="2026-11-02T10:00:00Z"></head>'
    )
    with make_collector(
        lambda request: httpx.Response(200, text=body, headers={"content-type": "text/html"})
    ) as collector:
        collected = collector.collect_article("https://news.example/articles/undated")
    assert collected.article.published_at == ""
    assert collected.provenance.publication_source == "unknown"


def test_feed_publication_fallback_does_not_use_atom_updated_time():
    def respond(request):
        if request.url.path == "/feed.xml":
            return httpx.Response(200, text=ATOM, headers={"content-type": "application/atom+xml"})
        return httpx.Response(
            200, text=article_html(published=False), headers={"content-type": "text/html"}
        )

    with make_collector(respond) as collector:
        result = collector.collect_feed("https://news.example/feed.xml")
    by_url = {item.article.url: item for item in result.articles}
    published = by_url["https://news.example/articles/bridge"]
    assert published.article.published_at == "2026-11-01T08:30:00+00:00"
    assert published.provenance.publication_source == "feed"
    updated_only = by_url["https://news.example/articles/tide"]
    assert updated_only.article.published_at == ""
    assert updated_only.provenance.publication_source == "unknown"


@pytest.mark.parametrize(
    "feed, mime", [(RSS, "application/rss+xml"), (ATOM, "application/atom+xml")]
)
def test_rss_and_atom_resolve_article_urls_and_keep_feed_provenance(feed, mime):
    fetched = []

    def respond(request):
        fetched.append(str(request.url))
        if request.url.path == "/feeds/latest.xml":
            return httpx.Response(200, text=feed, headers={"content-type": mime})
        return httpx.Response(200, text=article_html(), headers={"content-type": "text/html"})

    feed_url = "https://news.example/feeds/latest.xml"
    with make_collector(respond) as collector:
        result = collector.collect_feed(feed_url, limit=5, topic="News investigation")
    assert len(result.articles) == 2
    assert not result.errors
    assert {item.article.url for item in result.articles} == {
        "https://news.example/articles/bridge",
        "https://news.example/articles/tide",
    }
    assert all(item.provenance.feed_url == feed_url for item in result.articles)
    assert all(item.article.topic == "News investigation" for item in result.articles)
    assert sum("/articles/bridge" in url for url in fetched) == 1
    if feed == RSS:
        assert result.duplicates_skipped == 1


def test_feed_entry_failure_does_not_discard_successful_articles():
    def respond(request):
        if request.url.path == "/feed.xml":
            return httpx.Response(200, text=RSS, headers={"content-type": "application/rss+xml"})
        if request.url.path == "/articles/tide":
            return httpx.Response(503, text="temporarily unavailable")
        return httpx.Response(200, text=article_html(), headers={"content-type": "text/html"})

    with make_collector(respond) as collector:
        result = collector.collect_feed("https://news.example/feed.xml")
    assert len(result.articles) == 1
    assert len(result.errors) == 1
    assert result.errors[0].url == "https://news.example/articles/tide"
    assert result.errors[0].code
    assert result.errors[0].message


def test_redirect_retains_original_and_final_urls():
    def respond(request):
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "/articles/bridge"})
        return httpx.Response(200, text=article_html(), headers={"content-type": "text/html"})

    with make_collector(respond) as collector:
        result = collector.collect_article("https://news.example/old")
    assert result.provenance.requested_url == "https://news.example/old"
    assert result.provenance.final_url == "https://news.example/articles/bridge"
    assert result.article.url == result.provenance.final_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434/api/tags",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/private",
        "http://127.1/private",
        "http://0177.0.0.1/private",
        "http://0x7f000001/private",
        "http://2130706433/private",
        "http://printer.home.arpa/private",
        "file:///etc/passwd",
        "https://user:password@news.example/article",
    ],
)
def test_private_or_unsupported_urls_are_blocked_before_http(url):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, text=article_html())

    with make_collector(respond) as collector:
        with pytest.raises(CollectionError):
            collector.collect_article(url)
    assert not requests


def test_private_dns_and_redirect_targets_are_checked():
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    with make_collector(respond) as collector:
        with pytest.raises(CollectionError):
            collector.collect_article("https://news.example/redirect")
    assert requests == ["https://news.example/redirect"]
    with NewsCollector(
        transport=httpx.MockTransport(respond), resolver=lambda hostname: ["10.0.0.5"]
    ) as collector:
        with pytest.raises(CollectionError):
            collector.collect_article("https://internal.example/article")
    assert len(requests) == 1


def test_oversized_response_is_a_collection_error():
    with make_collector(
        lambda request: httpx.Response(
            200, text=article_html(), headers={"content-type": "text/html"}
        ),
        max_bytes=100,
    ) as collector:
        with pytest.raises(CollectionError) as error:
            collector.collect_article("https://news.example/articles/bridge")
    assert error.value.code


def test_timeout_is_reported_as_a_collection_error():
    def timeout(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    with make_collector(timeout) as collector:
        with pytest.raises(CollectionError) as error:
            collector.collect_article("https://news.example/articles/bridge")
    assert error.value.code


def test_redirect_loop_is_bounded():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "/redirect"})

    with make_collector(respond, max_redirects=2) as collector:
        with pytest.raises(CollectionError):
            collector.collect_article("https://news.example/redirect")
    assert len(requests) <= 3


def test_empty_valid_feed_is_a_successful_no_op():
    empty = (
        '<rss version="2.0"><channel><title>Empty newsroom</title>'
        "<link>https://news.example/</link><description>No updates</description>"
        "</channel></rss>"
    )
    with make_collector(
        lambda request: httpx.Response(
            200, text=empty, headers={"content-type": "application/rss+xml"}
        )
    ) as collector:
        result = collector.collect_feed("https://news.example/feed.xml")
    assert result.articles == []
    assert result.errors == []
    assert result.entries_seen == 0


def test_malformed_xml_is_not_treated_as_an_empty_feed():
    with make_collector(
        lambda request: httpx.Response(
            200, text="<rss><channel><item>", headers={"content-type": "application/rss+xml"}
        )
    ) as collector:
        with pytest.raises(CollectionError):
            collector.collect_feed("https://news.example/feed.xml")


class BodyBlocks(httpx.SyncByteStream):
    def __init__(self, blocks):
        self.blocks = blocks

    def __iter__(self):
        yield from self.blocks


@pytest.mark.parametrize("compressed", [False, True])
def test_streamed_responses_enforce_the_expanded_body_limit(compressed):
    body = b"article text " * 1000
    if compressed:
        headers = {"content-type": "text/html", "content-encoding": "gzip"}
        blocks = [gzip.compress(body)]
    else:
        headers = {"content-type": "text/html"}
        blocks = [body[:100], body[100:]]
    with make_collector(
        lambda request: httpx.Response(200, stream=BodyBlocks(blocks), headers=headers),
        max_bytes=512,
    ) as collector:
        with pytest.raises(CollectionError) as error:
            collector.collect_article("https://news.example/article")
    assert error.value.code == "response_too_large"


def test_mixed_public_and_private_dns_answers_are_blocked():
    requests = []
    transport = httpx.MockTransport(lambda request: requests.append(request))
    with NewsCollector(
        transport=transport, resolver=lambda hostname: [PUBLIC_IP, "192.168.1.5"]
    ) as collector:
        with pytest.raises(CollectionError):
            collector.collect_article("https://news.example/article")
    assert requests == []


def test_production_fetch_pins_the_public_ip_and_keeps_the_logical_tls_host():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, text=article_html(), headers={"content-type": "text/html"})

    # Replace only the socket boundary; keep the production address-pinning path.
    with NewsCollector(resolver=public_resolver) as collector:
        collector.fetcher.client.close()
        collector.fetcher.client = httpx.Client(transport=httpx.MockTransport(respond))
        article = collector.collect_article("https://news.example/articles/bridge")
    assert requests[0].url.host == PUBLIC_IP
    assert requests[0].headers["host"] == "news.example"
    assert requests[0].extensions["sni_hostname"] == "news.example"
    assert article.provenance.final_url == "https://news.example/articles/bridge"


@pytest.mark.parametrize("suffix", [b"unexpected trailing data", gzip.compress(b"second member")])
def test_compressed_responses_cannot_hide_trailing_members(suffix):
    body = gzip.compress(article_html().encode()) + suffix
    with make_collector(
        lambda request: httpx.Response(
            200,
            stream=BodyBlocks([body]),
            headers={"content-type": "text/html", "content-encoding": "gzip"},
        )
    ) as collector:
        with pytest.raises(CollectionError):
            collector.collect_article("https://news.example/article")


def test_failed_feed_entries_still_count_toward_the_attempt_limit():
    feed = RSS.replace("/articles/bridge?utm_source=rss", "http://127.0.0.1/private")
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if request.url.path == "/feed.xml":
            return httpx.Response(200, text=feed, headers={"content-type": "application/rss+xml"})
        return httpx.Response(200, text=article_html(), headers={"content-type": "text/html"})

    with make_collector(respond) as collector:
        result = collector.collect_feed("https://news.example/feed.xml", limit=1)
    assert result.articles == []
    assert len(result.errors) == 1
    assert requests == ["https://news.example/feed.xml"]


def test_tracking_cleanup_preserves_functional_query_encoding_and_order():
    original = "https://news.example/a?sig=A%2Fb%20C&tag=x&tag=y&utm_source=rss"
    assert normalize_url(original) == "https://news.example/a?sig=A%2Fb%20C&tag=x&tag=y"
