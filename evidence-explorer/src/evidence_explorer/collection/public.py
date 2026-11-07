"""Public Telegram previews, YouTube metadata, and news feeds behind one adapter.

Each fetch uses the existing public-address checks and response bounds. Parsers
save supplied text and provenance; links and video media are never fetched by a
post-text or feed-content collection. A recent window is refreshed so edits can
be detected by the repository's semantic deduplication.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from ..ingestion.collector import NewsCollector
from ..ingestion.feed_entries import collect_feed_entry, iter_feed_entries
from ..ingestion.fetching import FetchedResource, PublicFetcher
from ..ingestion.models import CollectedArticle, CollectionError
from ..ingestion.parsing import (
    HTML_TYPES,
    clean_metadata,
    parse_article,
    parse_feed,
    publication_date,
)
from ..ingestion.url_policy import Resolver
from ..source_contracts import (
    SourceAcquisition,
    SourceIdentity,
    SourceItem,
    safe_source_url,
)
from .models import CollectedBatch, CollectionFailure, CollectionSource

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "wbr"}
_IGNORED = {"script", "style", "noscript", "template", "iframe", "object", "svg", "nav"}
_BREAKS = {"br", "p", "div", "blockquote", "li"}
_TELEGRAM_HOSTS = {"t.me", "www.t.me", "telegram.me"}
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def normalize_public_locator(platform: str, locator: str) -> str:
    """Use the central registry's pure normalization at every public boundary."""
    from .registry import normalize_locator

    return normalize_locator(platform, locator)


def _published(value: object, parsed=None) -> str | None:
    value = publication_date(value, parsed)
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            return None
        return stamp.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError):
        return None


def _raw_date(value: object) -> str | None:
    return value if isinstance(value, str) and len(value) <= 100 else None


def _links(text: str, supplied=()) -> list[str]:
    result = set()
    for raw in [*supplied, *_URL.findall(text)]:
        if not isinstance(raw, str):
            continue
        try:
            result.add(safe_source_url(raw.rstrip(".,;!?")))
        except ValueError:
            continue
    return sorted(result)[:100]


def _failure(exc: CollectionError) -> CollectionFailure:
    status = re.search(r"Source returned HTTP (\d{3})", str(exc))
    if status:
        code = int(status[1])
        message = f"The public source returned HTTP {code}. Check its availability and retry."
        return CollectionFailure(message, retry_after_seconds=300 if code == 429 else None)
    messages = {
        "timeout": "The public source timed out. Retry later or choose another source.",
        "network_error": "The public source could not be reached. Check network access and retry.",
        "blocked_url": "The source URL is blocked. Use a public HTTP(S) source.",
        "response_too_large": "The source response exceeds the collection size limit.",
        "redirect_limit": "The source redirected too many times. Check its public URL.",
        "invalid_feed": "The source did not return a readable RSS or Atom feed.",
        "unsupported_content": "The source returned an unsupported content type.",
    }
    return CollectionFailure(messages.get(exc.code, "The public source could not be collected."))


def _public_feed(resource: FetchedResource):
    try:
        return parse_feed(resource)
    except CollectionError as exc:
        if exc.code != "invalid_feed" or resource.content_type != "text/xml":
            raise
        # PublicFetcher stores base MIME without charset. Feedparser interprets
        # legacy text/xml without charset as ASCII, flagging valid UTF-8 XML as
        # CharacterEncodingOverride (observed on BBC, DW and Guardian feeds).
        # The format-neutral XML MIME lets its XML declaration govern decoding;
        # the same strict feed parser still rejects malformed XML.
        return parse_feed(replace(resource, content_type="application/xml"))


@dataclass
class _TelegramPost:
    identity: str
    parts: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    published: str | None = None
    forward: str | None = None


@dataclass
class _Frame:
    tag: str
    text: bool = False
    ignored: bool = False
    forward: bool = False
    root: bool = False


class _TelegramPreview(HTMLParser):
    """Read only post-text regions and explicit dates in the public preview HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.frames: list[_Frame] = []
        self.posts: list[_TelegramPost] = []
        self.current: _TelegramPost | None = None

    def _finish(self):
        if self.current is not None:
            self.posts.append(self.current)
            self.current = None
        if len(self.posts) > 1000:
            raise CollectionFailure("The public preview contains too many posts to collect safely.")

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        parent = self.frames[-1] if self.frames else _Frame("")
        root = bool("tgme_widget_message" in classes and attrs.get("data-post"))
        if root and not parent.ignored:
            self._finish()
            self.current = _TelegramPost(attrs["data-post"])
        frame = _Frame(
            tag,
            text=parent.text or "tgme_widget_message_text" in classes,
            ignored=parent.ignored or tag in _IGNORED,
            forward=parent.forward or "tgme_widget_message_forwarded_from" in classes,
            root=root,
        )
        if self.current is not None and not frame.ignored:
            if frame.text and tag in _BREAKS:
                self.current.parts.append("\n")
            if tag == "a" and attrs.get("href"):
                if frame.text:
                    self.current.links.append(attrs["href"])
                elif frame.forward:
                    self.current.forward = attrs["href"]
            if tag == "time" and attrs.get("datetime"):
                self.current.published = attrs["datetime"]
        if tag not in _VOID:
            self.frames.append(frame)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.frames) - 1, -1, -1):
            if self.frames[index].tag == tag:
                frames = self.frames[index:]
                if self.current is not None:
                    if any(frame.text and not frame.ignored for frame in frames) and tag in _BREAKS:
                        self.current.parts.append("\n")
                    if any(frame.root for frame in frames):
                        self._finish()
                del self.frames[index:]
                break

    def handle_data(self, data):
        if self.current is not None and self.frames:
            if self.frames[-1].text and not self.frames[-1].ignored:
                self.current.parts.append(data)

    def close(self):
        super().close()
        self._finish()


def _forward_identity(url: str | None) -> SourceIdentity | None:
    if not url:
        return None
    try:
        safe_source_url(url)
        parsed = urlsplit(url)
        parts = parsed.path.strip("/").split("/")
        if parts and parts[0] == "s":
            parts = parts[1:]
        if (
            parsed.hostname not in _TELEGRAM_HOSTS
            or len(parts) != 2
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", parts[0])
            or not parts[1].isdigit()
        ):
            return None
        return SourceIdentity(platform="telegram", channel_id=parts[0].lower(), native_id=parts[1])
    except (ValueError, ValidationError):
        return None


def _telegram(source: CollectionSource, fetcher: PublicFetcher) -> CollectedBatch:
    channel = normalize_public_locator("telegram", source.locator)
    resource = fetcher.fetch(f"https://t.me/s/{channel}")
    if urlsplit(resource.final_url).hostname not in _TELEGRAM_HOSTS:
        raise CollectionFailure("Telegram redirected away from its public channel preview.")
    if resource.content_type not in HTML_TYPES:
        raise CollectionFailure("Telegram did not return a public channel preview.")
    parser = _TelegramPreview()
    try:
        parser.feed(resource.body.decode("utf-8-sig"))
        parser.close()
    except (UnicodeError, RecursionError):
        raise CollectionFailure("Telegram returned an unreadable public preview.") from None
    items, skipped, seen = [], 0, set()
    for post in parser.posts:
        parts = post.identity.split("/")
        if len(parts) != 2 or parts[0].lower() != channel or not parts[1].isdigit():
            continue
        if parts[1] in seen:
            continue
        text = "".join(post.parts).strip()
        if not text or len(text) > 50_000:
            skipped += 1
            continue
        repost = _forward_identity(post.forward)
        if repost and repost.channel_id == channel and repost.native_id == parts[1]:
            repost = None
        try:
            items.append(
                SourceItem(
                    platform="telegram",
                    channel_id=channel,
                    channel_name=source.label,
                    native_id=parts[1],
                    text=text,
                    source_url=f"https://t.me/{channel}/{parts[1]}",
                    author_id=None,
                    published_at=_published(post.published),
                    acquired_at=resource.retrieved_at,
                    shared_urls=_links(text, post.links),
                    repost_of=repost,
                    provenance=SourceAcquisition(
                        method="telegram_public",
                        content_scope="post_text",
                        retrieved_url=resource.final_url,
                        published_at_raw=_raw_date(post.published),
                    ),
                )
            )
            seen.add(parts[1])
        except ValidationError:
            skipped += 1
    if not items:
        raise CollectionFailure(
            "Telegram exposes no readable public post text for this channel. "
            "Check its public preview; private or restricted posts require separate access."
        )
    items.sort(key=lambda item: int(item.native_id), reverse=True)
    warnings = [
        "Telegram collection covers the recent public text preview, not a complete archive."
    ]
    if skipped:
        warnings.append(f"{skipped} media-only, oversized, or invalid preview posts were skipped.")
    return CollectedBatch(
        items=tuple(items[: source.item_limit]),
        warnings=tuple(warnings),
        cursor=items[0].native_id,
    )


def _youtube(source: CollectionSource, fetcher: PublicFetcher) -> CollectedBatch:
    channel = normalize_public_locator("youtube", source.locator)
    # The conventional feed serves entries; /xml/feeds can return a discovery
    # document despite appearing in the API's push-notification guide.
    try:
        resource = fetcher.fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel}")
    except CollectionError as exc:
        if exc.code == "http_error" and str(exc).startswith("Source returned HTTP 404;"):
            raise CollectionFailure(
                "YouTube's public channel feed returned HTTP 404. Try refreshing it again later."
            ) from None
        raise
    if urlsplit(resource.final_url).hostname not in {"youtube.com", "www.youtube.com"}:
        raise CollectionFailure("YouTube redirected away from its public channel feed.")
    feed = _public_feed(resource)
    declared_feed_id = str(feed.feed.get("id") or "")
    # Public channel feeds use the channel's suffix in their feed-level ID,
    # while each video's yt:channelId contains the complete UC-prefixed ID.
    accepted_feed_ids = {f"yt:channel:{channel}", f"yt:channel:{channel[2:]}"}
    if declared_feed_id.startswith("yt:channel:") and declared_feed_id not in accepted_feed_ids:
        raise CollectionFailure("YouTube returned a feed for a different channel.")
    items, skipped, seen = [], 0, set()
    for entry in feed.entries:
        if len(items) >= source.item_limit:
            break
        ident = entry.get("yt_videoid") or str(entry.get("id") or "").removeprefix("yt:video:")
        declared_channel = entry.get("yt_channelid")
        if (
            not isinstance(ident, str)
            or not _VIDEO_ID.fullmatch(ident)
            or (declared_channel and declared_channel != channel)
        ):
            skipped += 1
            continue
        if ident in seen:
            continue
        seen.add(ident)
        title = clean_metadata(entry.get("title"), 1000)
        description = entry.get("media_description") or entry.get("summary") or ""
        description = _plain_text(description) if isinstance(description, str) else ""
        text = "\n\n".join(part for part in (title, description) if part).strip()
        if not text or len(text) > 50_000:
            skipped += 1
            continue
        try:
            items.append(
                SourceItem(
                    platform="youtube",
                    channel_id=channel,
                    channel_name=source.label,
                    native_id=ident,
                    title=title or None,
                    text=text,
                    source_url=f"https://www.youtube.com/watch?v={ident}",
                    author_id=channel,
                    published_at=_published(entry.get("published"), entry.get("published_parsed")),
                    acquired_at=resource.retrieved_at,
                    shared_urls=_links(description),
                    provenance=SourceAcquisition(
                        method="youtube_feed",
                        content_scope="title_description",
                        retrieved_url=resource.final_url,
                        feed_url=resource.final_url,
                        published_at_raw=_raw_date(entry.get("published")),
                        author_label=clean_metadata(feed.feed.get("title"), 300) or None,
                    ),
                )
            )
        except ValidationError:
            skipped += 1
    if not items:
        raise CollectionFailure("YouTube returned no readable recent video titles or descriptions.")
    warnings = [
        "YouTube collection saves published titles and descriptions, not video transcripts."
    ]
    if skipped:
        warnings.append(f"{skipped} invalid or oversized video entries were skipped.")
    return CollectedBatch(tuple(items), tuple(warnings), items[0].native_id)


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored: list[str] = []

    def handle_starttag(self, tag, attrs):
        if self.ignored or tag in _IGNORED:
            if tag not in _VOID:
                self.ignored.append(tag)
        elif tag in _BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.ignored:
            if tag in self.ignored:
                index = len(self.ignored) - 1 - self.ignored[::-1].index(tag)
                del self.ignored[index:]
        elif tag in _BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def _plain_text(value: str) -> str:
    parser = _VisibleText()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts).strip()


def _news_item(
    source: CollectionSource, collected: CollectedArticle, channel: str, feed_url: str | None
) -> SourceItem:
    article, provenance = collected.article, collected.provenance
    is_article = provenance.content_source == "article"
    return SourceItem(
        platform=source.platform,
        channel_id=channel,
        channel_name=source.label if feed_url else channel,
        native_id=hashlib.sha256(article.url.encode("utf-8")).hexdigest(),
        title=article.title,
        text=article.text,
        source_url=article.url,
        author_id=None,
        published_at=_published(article.published_at),
        acquired_at=provenance.retrieved_at,
        shared_urls=_links(article.text),
        provenance=SourceAcquisition(
            method="rss_article" if is_article else "rss_content",
            content_scope="article_text" if is_article else provenance.content_source,
            retrieved_url=provenance.final_url,
            feed_url=feed_url,
            published_at_raw=_raw_date(article.published_at),
            author_label=article.author,
        ),
    )


def _news(source: CollectionSource, collector: NewsCollector) -> CollectedBatch:
    locator = normalize_public_locator(source.platform, source.locator)
    resource = collector.fetcher.fetch(locator)
    if resource.content_type in HTML_TYPES:
        article = parse_article(resource)
        channel = urlsplit(article.article.url).hostname or source.id
        return CollectedBatch((_news_item(source, article, channel, None),))
    feed = _public_feed(resource)
    channel = (
        locator if len(locator) <= 512 else "feed:" + hashlib.sha256(locator.encode()).hexdigest()
    )
    items, saved_ids, failed, unknown_dates = [], set(), 0, 0
    for entry in iter_feed_entries(
        feed.entries, final_url=resource.final_url, limit=source.item_limit
    ):
        if entry.error is not None:
            failed += 1
            continue
        if entry.duplicate:
            continue
        try:
            collected = collect_feed_entry(
                collector,
                entry,
                resource,
                feed.feed,
                topic="Live sources",
                content_mode=source.content_mode,
                empty_title="",
            )
            item = _news_item(source, collected, channel, resource.final_url)
            if item.native_id in saved_ids:
                continue
            saved_ids.add(item.native_id)
            unknown_dates += int(item.published_at is None)
            items.append(item)
        except CollectionError as exc:
            if re.search(r"Source returned HTTP 429\b", str(exc)):
                raise _failure(exc) from None
            failed += 1
        except ValidationError:
            failed += 1
    if not items:
        raise CollectionFailure(
            "The feed contains no collectable text within this check's item limit. "
            "Check the feed URL and whether it supplies full text or only summaries."
        )
    warnings = []
    if failed:
        warnings.append(f"{failed} feed entries were unavailable, invalid, or unreadable.")
    if unknown_dates:
        warnings.append(f"{unknown_dates} entries lack a timezone-aware publication timestamp.")
    if source.content_mode == "feed_content":
        warnings.append("Saved text comes from the feed; full article pages were not fetched.")
    return CollectedBatch(tuple(items), tuple(warnings))


def collect_public(
    source: CollectionSource,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
) -> CollectedBatch:
    """Collect a bounded public window; platform API credentials stay elsewhere."""
    source = CollectionSource.model_validate(source)
    try:
        if source.platform in {"news", "other"}:
            with NewsCollector(transport=transport, resolver=resolver) as collector:
                return _news(source, collector)
        fetcher = PublicFetcher(transport=transport, resolver=resolver)
        try:
            if source.platform == "telegram":
                return _telegram(source, fetcher)
            if source.platform == "youtube":
                return _youtube(source, fetcher)
            raise CollectionFailure("This platform requires its dedicated API collector.")
        finally:
            fetcher.close()
    except CollectionError as exc:
        raise _failure(exc) from None
    except ValidationError:
        raise CollectionFailure("The source returned invalid collection metadata.") from None
