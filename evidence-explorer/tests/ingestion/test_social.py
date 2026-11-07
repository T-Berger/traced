"""Explicit social feed text mode never fetches post pages or outbound links."""

from datetime import UTC, datetime
from html import escape

import httpx
import pytest

from evidence_explorer.ingestion import CollectionError, NewsCollector

from .fixtures import PUBLIC_IP

FEED_URL = "https://social.example/@updates.rss"
POST_URL = "https://social.example/@updates/123"
POST_TEXT = "Engineers confirm the river bridge remains closed until the safety review is complete."


def rss_post(*, body=None, summary=None, title=None, link=POST_URL, published=True):
    headline = f"<title>{escape(title)}</title>" if title is not None else ""
    full = f"<content:encoded><![CDATA[{body}]]></content:encoded>" if body is not None else ""
    teaser = f"<description><![CDATA[{summary}]]></description>" if summary is not None else ""
    date = "<pubDate>Sun, 01 Nov 2026 09:30:00 GMT</pubDate>" if published else ""
    return (
        f"<item>{headline}<link>{escape(link)}</link>"
        f"<dc:creator>@updates@social.example</dc:creator>{date}{teaser}{full}</item>"
    )


def rss_feed(*items):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<channel><title>Public updates</title><link>https://social.example/@updates</link>"
        "<description>Public posts</description><language>en</language>"
        + "".join(items)
        + "</channel></rss>"
    )


def collector_for(feed, requests, *, resolver=None):
    def respond(request):
        requests.append(str(request.url))
        assert request.url.path == "/@updates.rss", "Post and outbound links must not be fetched"
        return httpx.Response(200, text=feed, headers={"content-type": "application/rss+xml"})

    return NewsCollector(
        transport=httpx.MockTransport(respond), resolver=resolver or (lambda hostname: [PUBLIC_IP])
    )


def test_full_post_content_wins_and_records_actual_feed_provenance():
    requests = []
    html = (
        "<article><nav>Navigation must disappear</nav>"
        f"<p>{POST_TEXT}</p><script>script must disappear</script>"
        '<p>See <a href="http://127.0.0.1/private">the original notice</a> for context.</p>'
        "<style>style must disappear</style></article>"
    )
    feed = rss_feed(rss_post(body=html, summary="A short teaser that must not replace the post."))
    with collector_for(feed, requests) as collector:
        result = collector.collect_feed(
            FEED_URL + "?utm_source=reader", content_mode="feed_content", topic="River flooding"
        )
    assert result.errors == []
    article = result.articles[0]
    assert POST_TEXT in article.article.text
    assert "the original notice" in article.article.text
    assert "short teaser" not in article.article.text
    assert "must disappear" not in article.article.text
    assert "<" not in article.article.text
    assert article.article.url == POST_URL
    assert article.article.topic == "River flooding"
    assert article.article.language == "en"
    assert article.article.is_sample is False
    assert article.provenance.author == "@updates@social.example"
    assert article.provenance.requested_url == FEED_URL + "?utm_source=reader"
    assert article.provenance.final_url == FEED_URL
    assert article.provenance.feed_url == FEED_URL
    assert article.provenance.retrieved_at
    assert article.provenance.content_source == "feed_content"
    assert article.article.content_source == "feed_content"
    published = datetime.fromisoformat(article.article.published_at)
    assert published == datetime(2026, 11, 1, 9, 30, tzinfo=UTC)
    assert requests == [FEED_URL]


def test_mastodon_style_description_without_title_is_explicit_summary_content():
    requests = []
    feed = rss_feed(rss_post(summary=f"<p>{POST_TEXT}</p>"))
    with collector_for(feed, requests) as collector:
        result = collector.collect_feed(FEED_URL, content_mode="feed_content")
    assert result.errors == []
    article = result.articles[0]
    assert article.article.text == POST_TEXT
    assert article.article.title == POST_TEXT
    assert article.provenance.content_source == "feed_summary"
    assert article.article.content_source == "feed_summary"
    assert requests == [FEED_URL]


def test_default_article_mode_does_not_silently_import_feed_text():
    feed = rss_feed(rss_post(body=f"<p>{POST_TEXT}</p>", summary=POST_TEXT))
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if request.url.path == "/@updates.rss":
            return httpx.Response(200, text=feed, headers={"content-type": "application/rss+xml"})
        return httpx.Response(403, text="Post page unavailable")

    with NewsCollector(
        transport=httpx.MockTransport(respond), resolver=lambda hostname: [PUBLIC_IP]
    ) as collector:
        result = collector.collect_feed(FEED_URL)
    assert result.articles == []
    assert len(result.errors) == 1
    assert requests == [FEED_URL, POST_URL]


def test_atom_full_content_keeps_published_separate_from_updated():
    feed = f"""<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="de">
<title>Public posts</title><id>https://social.example/</id>
<updated>2026-11-02T09:00:00Z</updated>
<entry><id>{POST_URL}</id><link href="{POST_URL}"/>
<author><name>@updates@social.example</name></author>
<published>2026-11-01T10:30:00+01:00</published><updated>2026-11-02T09:00:00Z</updated>
<content type="html">&lt;p&gt;{POST_TEXT}&lt;/p&gt;</content></entry>
<entry><id>second</id><link href="{POST_URL}4"/>
<updated>2026-11-02T09:00:00Z</updated>
<summary type="html">&lt;p&gt;{POST_TEXT}&lt;/p&gt;</summary></entry></feed>"""
    requests = []
    with collector_for(feed, requests) as collector:
        result = collector.collect_feed(FEED_URL, content_mode="feed_content")
    assert result.errors == []
    first, second = result.articles
    assert first.provenance.content_source == "feed_content"
    assert first.article.published_at == "2026-11-01T10:30:00+01:00"
    assert first.article.language == "de"
    assert second.article.published_at == ""
    assert second.provenance.publication_source == "unknown"
    assert second.provenance.content_source == "feed_summary"
    assert requests == [FEED_URL]


def test_short_full_post_is_rejected_without_falling_back_to_a_long_teaser():
    feed = rss_feed(rss_post(body="<p>Too short</p>", summary=POST_TEXT))
    requests = []
    with collector_for(feed, requests) as collector:
        result = collector.collect_feed(FEED_URL, content_mode="feed_content")
    assert result.articles == []
    assert result.errors[0].code == "feed_content_too_short"
    assert "30" in result.errors[0].message
    assert requests == [FEED_URL]


def test_deduplication_and_failure_budget_apply_without_post_requests():
    feed = rss_feed(
        rss_post(summary=POST_TEXT),
        rss_post(summary=POST_TEXT, link=POST_URL + "?utm_source=duplicate#reply"),
        rss_post(summary="Short", link=POST_URL + "4"),
        rss_post(summary=POST_TEXT, link=POST_URL + "5"),
    )
    requests = []
    with collector_for(feed, requests) as collector:
        result = collector.collect_feed(FEED_URL, content_mode="feed_content", limit=2)
    assert len(result.articles) == 1
    assert len(result.errors) == 1
    assert result.duplicates_skipped == 1
    assert requests == [FEED_URL]


@pytest.mark.parametrize("link", ["http://127.0.0.1/private", "https://internal.example/post"])
def test_post_permalinks_must_be_public_even_though_they_are_not_fetched(link):
    feed = rss_feed(rss_post(summary=POST_TEXT, link=link))
    requests = []

    def resolver(hostname):
        return ["10.0.0.5"] if hostname == "internal.example" else [PUBLIC_IP]

    with collector_for(feed, requests, resolver=resolver) as collector:
        result = collector.collect_feed(FEED_URL, content_mode="feed_content")
    assert result.articles == []
    assert result.errors[0].code == "blocked_url"
    assert requests == [FEED_URL]


def test_invalid_content_mode_is_rejected_before_fetching():
    requests = []
    with collector_for(rss_feed(), requests) as collector:
        with pytest.raises(CollectionError):
            collector.collect_feed(FEED_URL, content_mode="private_api")
    assert requests == []
