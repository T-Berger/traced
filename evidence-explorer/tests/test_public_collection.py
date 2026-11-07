"""Live public adapters preserve evidence semantics without fetching linked media."""

from datetime import datetime
from html import escape

import httpx
import pytest

from evidence_explorer.collection.models import CollectionFailure, CollectionSource
from evidence_explorer.collection.public import collect_public, normalize_public_locator
from tests.ingestion.fixtures import article_html

PUBLIC_IP = "93.184.216.34"
CHANNEL = "UC1234567890123456789012"


def source(platform, locator, **options):
    return CollectionSource(
        id="source-test", platform=platform, locator=locator, label="Collected source", **options
    )


def collect(item, content, content_type, *, status=200):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if isinstance(content, Exception):
            raise content
        return httpx.Response(status, text=content, headers={"content-type": content_type})

    result = collect_public(
        item,
        transport=httpx.MockTransport(respond),
        resolver=lambda hostname: [PUBLIC_IP],
    )
    return result, requests


def telegram_post(number, text, *, forwarded="", timestamp="2026-11-06T10:30:00+01:00"):
    return f"""<div class="tgme_widget_message js-widget_message" data-post="example/{number}">
      <div class="tgme_widget_message_author"><a href="https://t.me/example">Example</a></div>
      {forwarded}
      <div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>
      <div class="tgme_widget_message_footer">
        <a class="tgme_widget_message_date" href="https://t.me/example/{number}">
          <time datetime="{timestamp}">11:30</time>
        </a>
      </div>
    </div>"""


def test_telegram_preserves_caption_boundaries_exact_dates_links_and_explicit_forward():
    forwarded = (
        '<div class="tgme_widget_message_forwarded_from">Forwarded from '
        '<a href="https://t.me/original/42">Original</a></div>'
    )
    page = (
        "<html><body><nav>Navigation text</nav>"
        + telegram_post(
            21,
            "Original &amp; quoted.<br>Second <b>line</b>. "
            '<a href="https://news.example/story">Source</a>'
            "<script>Never save active code</script><style>Hidden CSS</style>",
            forwarded=forwarded,
        )
        + "</body></html>"
    )
    batch, requests = collect(source("telegram", "https://t.me/Example"), page, "text/html")
    assert requests == ["https://t.me/s/example"]
    item = batch.items[0]
    assert item.native_id == "21"
    assert item.channel_id == "example"
    assert item.text == "Original & quoted.\nSecond line. Source"
    assert "Navigation" not in item.text and "active code" not in item.text
    assert item.published_at == "2026-11-06T09:30:00Z"
    assert item.provenance.published_at_raw == "2026-11-06T10:30:00+01:00"
    assert datetime.fromisoformat(item.acquired_at.replace("Z", "+00:00")).tzinfo
    assert item.shared_urls == ["https://news.example/story"]
    assert item.repost_of.platform == "telegram"
    assert item.repost_of.channel_id == "original" and item.repost_of.native_id == "42"
    assert item.author_id is None
    assert item.provenance.method == "telegram_public"
    assert item.provenance.content_scope == "post_text"
    assert item.provenance.retrieved_url == requests[0]
    assert batch.cursor == "21"


def test_telegram_latest_window_rechecks_edits_and_reports_media_only_posts():
    page = (
        telegram_post(9, "Older post")
        + telegram_post(10, "")
        + telegram_post(11, "An edited recent post")
        + telegram_post(12, "The newest post")
        + telegram_post(12, "Repeated card must not duplicate evidence")
    )
    batch, _ = collect(source("telegram", "example", cursor="12", item_limit=2), page, "text/html")
    assert [item.native_id for item in batch.items] == ["12", "11"]
    assert batch.items[1].text == "An edited recent post"
    assert any("media-only" in warning for warning in batch.warnings)


def test_telegram_media_placeholder_does_not_hide_a_later_readable_card():
    page = telegram_post(21, "") + telegram_post(21, "The actual supplied caption.")
    batch, _ = collect(source("telegram", "example"), page, "text/html")
    assert len(batch.items) == 1
    assert batch.items[0].text == "The actual supplied caption."


@pytest.mark.parametrize(
    "forward",
    [
        "https://t.me/original",
        "https://t.me/+private-invite",
        "https://evil.example/original/42",
        "javascript:alert(1)",
        "https://t.me/example/21",
    ],
)
def test_telegram_does_not_invent_original_posts_from_forwarded_channel_names(forward):
    page = telegram_post(
        21,
        "A readable post.",
        forwarded='<div class="tgme_widget_message_forwarded_from">'
        f'<a href="{escape(forward)}">Forwarded label</a></div>',
    )
    batch, _ = collect(source("telegram", "example"), page, "text/html")
    assert batch.items[0].repost_of is None


def test_telegram_missing_or_unzoned_dates_stay_unknown_and_other_channel_cards_are_ignored():
    page = telegram_post(1, "A source post.", timestamp="2026-11-06T11:00:00")
    page += telegram_post(2, "Misattributed post").replace(
        'data-post="example/', 'data-post="other/'
    )
    batch, _ = collect(source("telegram", "example"), page, "text/html")
    assert len(batch.items) == 1
    assert batch.items[0].published_at is None
    assert batch.items[0].provenance.published_at_raw == "2026-11-06T11:00:00"


@pytest.mark.parametrize("page", ["<html>Please open Telegram</html>", telegram_post(1, "")])
def test_telegram_restricted_or_media_only_preview_fails_visibly(page):
    with pytest.raises(CollectionFailure, match="no readable public post text"):
        collect(source("telegram", "example"), page, "text/html")


def youtube_feed(*entries):
    return (
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
        'xmlns:media="http://search.yahoo.com/mrss/">'
        f"<id>yt:channel:{CHANNEL}</id><title>Example video channel</title>"
        + "".join(entries)
        + "</feed>"
    )


def video(ident, *, title="A short title", description="", published=None, channel=CHANNEL):
    date = f"<published>{published}</published>" if published is not None else ""
    return (
        f"<entry><id>yt:video:{ident}</id><yt:videoId>{ident}</yt:videoId>"
        f"<yt:channelId>{channel}</yt:channelId><title>{escape(title)}</title>"
        f'<link rel="alternate" href="https://www.youtube.com/watch?v={ident}"/>'
        f"<author><name>Example video channel</name></author>{date}"
        "<updated>2026-11-06T12:00:00Z</updated>"
        f"<media:group><media:description>{escape(description)}</media:description></media:group>"
        "</entry>"
    )


def test_youtube_saves_title_description_as_metadata_without_fetching_watch_pages():
    xml = youtube_feed(
        video(
            "abcdefghijk",
            title="Research & evidence",
            description="A saved description. Read https://news.example/story for context.",
            published="2026-11-06T11:00:00+01:00",
        )
    )
    batch, requests = collect(source("youtube", CHANNEL), xml, "application/atom+xml")
    assert requests == [f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}"]
    item = batch.items[0]
    assert item.text == (
        "Research & evidence\n\nA saved description. Read https://news.example/story for context."
    )
    assert item.native_id == "abcdefghijk" and item.author_id == CHANNEL
    assert item.channel_name == "Collected source"
    assert item.source_url == "https://www.youtube.com/watch?v=abcdefghijk"
    assert item.published_at == "2026-11-06T10:00:00Z"
    assert item.provenance.content_scope == "title_description"
    assert item.provenance.feed_url == requests[0]
    assert item.shared_urls == ["https://news.example/story"]
    assert any("not video transcripts" in warning for warning in batch.warnings)


def test_youtube_short_titles_missing_dates_duplicate_and_mismatched_ids():
    xml = youtube_feed(
        video("abcdefghijk", title="Hi"),
        video("abcdefghijk", title="Duplicate"),
        video("bad!", title="Invalid ID"),
        video("lmnopqrstuv", channel="UCotherchannel000000000000"),
        video("12345678901", title="Also short"),
    )
    batch, _ = collect(source("youtube", CHANNEL), xml, "application/atom+xml")
    assert [item.native_id for item in batch.items] == ["abcdefghijk", "12345678901"]
    assert all(item.published_at is None for item in batch.items)
    assert batch.items[0].text == "Hi"
    assert any("2 invalid" in warning for warning in batch.warnings)


def test_youtube_real_feed_level_channel_id_can_omit_uc_prefix():
    xml = youtube_feed(video("abcdefghijk")).replace(
        f"<id>yt:channel:{CHANNEL}</id>", f"<id>yt:channel:{CHANNEL[2:]}</id>"
    )
    batch, _ = collect(source("youtube", CHANNEL), xml, "application/atom+xml")
    assert batch.items[0].channel_id == CHANNEL


def test_youtube_different_feed_level_channel_identity_is_rejected():
    xml = youtube_feed(video("abcdefghijk")).replace(
        f"<id>yt:channel:{CHANNEL}</id>", "<id>yt:channel:another-channel</id>"
    )
    with pytest.raises(CollectionFailure, match="different channel"):
        collect(source("youtube", CHANNEL), xml, "application/atom+xml")


def rss(*entries):
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Research news</title>'
        "<link>https://news.example/</link><description>Updates</description>"
        + "".join(entries)
        + "</channel></rss>"
    )


def feed_entry(link, *, description=None, published=True):
    text = description or "The bridge remains closed while engineers inspect the supports."
    date = "<pubDate>Fri, 06 Nov 2026 10:00:00 GMT</pubDate>" if published else ""
    return (
        f"<item><title>Engineering update</title><link>{escape(link)}</link>{date}"
        f"<description><![CDATA[<p>{text}</p>]]></description></item>"
    )


def test_feed_summary_mode_preserves_actual_retrieval_scope_and_does_not_follow_links():
    xml = rss(feed_entry("https://news.example/story"))
    batch, requests = collect(
        source("news", "https://news.example/rss"), xml, "application/rss+xml"
    )
    assert requests == ["https://news.example/rss"]
    item = batch.items[0]
    assert item.provenance.method == "rss_content"
    assert item.provenance.content_scope == "feed_summary"
    assert item.provenance.retrieved_url == "https://news.example/rss"
    assert item.provenance.feed_url == "https://news.example/rss"
    assert item.source_url == "https://news.example/story"
    assert item.published_at == "2026-11-06T10:00:00Z"
    assert item.author_id is None
    assert len(item.native_id) == 64
    assert any("full article pages were not fetched" in warning for warning in batch.warnings)


def test_feed_partial_failures_deduplicate_links_and_keep_unknown_dates_unknown():
    xml = rss(
        feed_entry("https://news.example/story", published=False),
        feed_entry("https://news.example/story?utm_source=duplicate"),
        feed_entry("http://127.0.0.1/private"),
        feed_entry("https://news.example/short", description="Short"),
        feed_entry("https://news.example/out-of-budget"),
    )
    batch, requests = collect(
        source("news", "https://news.example/rss", item_limit=3),
        xml,
        "application/rss+xml",
    )
    assert len(batch.items) == 1
    assert batch.items[0].published_at is None
    assert requests == ["https://news.example/rss"]
    assert any("2 feed entries" in warning for warning in batch.warnings)
    assert any("timezone-aware" in warning for warning in batch.warnings)


def test_feed_article_mode_fetches_pages_and_saves_article_provenance():
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if request.url.path == "/rss":
            return httpx.Response(
                200,
                text=rss(feed_entry("https://news.example/story")),
                headers={"content-type": "application/rss+xml"},
            )
        return httpx.Response(200, text=article_html(), headers={"content-type": "text/html"})

    batch = collect_public(
        source("news", "https://news.example/rss", content_mode="article_links"),
        transport=httpx.MockTransport(respond),
        resolver=lambda hostname: [PUBLIC_IP],
    )
    assert requests == ["https://news.example/rss", "https://news.example/story"]
    item = batch.items[0]
    assert "bridge remains closed" in item.text
    assert item.provenance.method == "rss_article"
    assert item.provenance.content_scope == "article_text"
    assert item.provenance.retrieved_url == "https://news.example/story"
    assert item.provenance.feed_url == "https://news.example/rss"
    assert item.published_at == "2026-11-01T08:30:00Z"
    assert item.author_id is None


def test_feed_article_redirect_aliases_deduplicate_the_final_source_identity():
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if request.url.path == "/rss":
            return httpx.Response(
                200,
                text=rss(
                    feed_entry("https://news.example/alias-one"),
                    feed_entry("https://news.example/alias-two"),
                ),
                headers={"content-type": "application/rss+xml"},
            )
        if request.url.path.startswith("/alias-"):
            return httpx.Response(302, headers={"location": "/story"})
        return httpx.Response(200, text=article_html(), headers={"content-type": "text/html"})

    batch = collect_public(
        source("news", "https://news.example/rss", content_mode="article_links"),
        transport=httpx.MockTransport(respond),
        resolver=lambda hostname: [PUBLIC_IP],
    )
    assert len(batch.items) == 1
    assert batch.items[0].source_url == "https://news.example/story"


def test_article_rate_limit_stops_the_feed_before_further_article_requests():
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if request.url.path == "/rss":
            return httpx.Response(
                200,
                text=rss(
                    feed_entry("https://news.example/limited"),
                    feed_entry("https://news.example/not-requested"),
                ),
                headers={"content-type": "application/rss+xml"},
            )
        return httpx.Response(429)

    with pytest.raises(CollectionFailure, match="429") as caught:
        collect_public(
            source("news", "https://news.example/rss", content_mode="article_links"),
            transport=httpx.MockTransport(respond),
            resolver=lambda hostname: [PUBLIC_IP],
        )
    assert caught.value.retry_after_seconds == 300
    assert requests == ["https://news.example/rss", "https://news.example/limited"]


def test_valid_utf8_text_xml_feed_uses_its_xml_encoding_declaration():
    xml = rss(
        feed_entry(
            "https://news.example/story",
            description="Überprüfung der Brücke: Engineers continue the safety inspection.",
        )
    ).replace('<?xml version="1.0"?>', '<?xml version="1.0" encoding="UTF-8"?>')
    batch, _ = collect(source("news", "https://news.example/rss"), xml, "text/xml")
    assert "Überprüfung der Brücke" in batch.items[0].text


def test_text_xml_compatibility_does_not_accept_malformed_feed_documents():
    xml = '<?xml version="1.0" encoding="UTF-8"?><rss><channel>broken & unclosed'
    with pytest.raises(CollectionFailure, match="RSS or Atom"):
        collect(source("news", "https://news.example/rss"), xml, "text/xml")


def test_single_public_article_is_downloaded_once():
    batch, requests = collect(
        source("news", "https://news.example/story"), article_html(), "text/html"
    )
    assert requests == ["https://news.example/story"]
    assert len(batch.items) == 1
    assert batch.items[0].provenance.feed_url is None
    assert batch.items[0].provenance.content_scope == "article_text"
    assert batch.items[0].channel_name == "news.example"


@pytest.mark.parametrize("status", [403, 404, 429, 500])
def test_http_failures_are_safe_actionable_and_rate_limits_include_backoff(status):
    with pytest.raises(CollectionFailure) as caught:
        collect(
            source("telegram", "example"),
            "Untrusted private server response",
            "text/html",
            status=status,
        )
    assert str(status) in str(caught.value)
    assert "Untrusted" not in str(caught.value)
    assert caught.value.retry_after_seconds == (300 if status == 429 else None)


def test_network_errors_do_not_echo_remote_text_or_urls_with_tokens():
    with pytest.raises(CollectionFailure, match="could not be reached") as caught:
        collect(
            source("telegram", "example"),
            httpx.ConnectError("SECRET https://source.example/?token=private"),
            "text/html",
        )
    assert "SECRET" not in str(caught.value) and "private" not in str(caught.value)


def test_public_destination_checks_reject_private_dns_before_a_request():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, text="must not be reached")

    with pytest.raises(CollectionFailure, match="blocked"):
        collect_public(
            source("news", "https://private.example/rss"),
            transport=httpx.MockTransport(respond),
            resolver=lambda hostname: ["10.0.0.5"],
        )
    assert requests == []


@pytest.mark.parametrize(
    ("platform", "locator", "expected"),
    [
        ("telegram", "@Example", "example"),
        ("telegram", "https://t.me/s/Example", "example"),
        ("youtube", f"https://www.youtube.com/channel/{CHANNEL}", CHANNEL),
        ("news", "https://news.example/rss?utm_source=tracker", "https://news.example/rss"),
    ],
)
def test_public_normalization_reuses_platform_registry(platform, locator, expected):
    assert normalize_public_locator(platform, locator) == expected
