"""Extract text explicitly supplied by a public RSS/Atom item, without fetching links."""

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from ..models import ArticleInput
from .fetching import FetchedResource
from .models import CollectedArticle, CollectionError, SourceProvenance
from .parsing import clean_metadata, publication_date

_TEXT_TYPES = {"text/plain", "text/html", "application/xhtml+xml"}
_IGNORED = {"script", "style", "nav", "noscript", "template", "iframe", "object", "svg", "form"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "wbr"}
_BREAKS = {"p", "div", "br", "hr", "li", "blockquote", "section", "article", "h1", "h2", "h3"}
_LANGUAGE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")


def _normalize_text(value: str) -> str:
    return "\n".join(line for raw in value.splitlines() if (line := " ".join(raw.split())))


class _PostText(HTMLParser):
    """Keep visible text boundaries and discard active/navigation subtrees."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored: list[str] = []

    def handle_starttag(self, tag, attrs):
        if self.ignored or tag in _IGNORED:
            if tag not in _VOID:
                self.ignored.append(tag)
            return
        if tag in _BREAKS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if not self.ignored and tag in _BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.ignored:
            if tag in self.ignored:
                position = len(self.ignored) - 1 - self.ignored[::-1].index(tag)
                del self.ignored[position:]
            return
        if tag in _BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def _text(value: str, media_type: str) -> str:
    if media_type == "text/plain":
        return _normalize_text(value)
    parser = _PostText()
    parser.feed(value)
    parser.close()
    return _normalize_text("".join(parser.parts))


def _body(entry: dict[str, Any]) -> tuple[str, str, str]:
    """Prefer one full text representation; summary is an explicit-mode fallback."""
    contents = entry.get("content") or []
    if isinstance(contents, list):
        for content in contents:
            if not isinstance(content, dict):
                continue
            value = content.get("value")
            media_type = str(content.get("type") or "text/html").split(";", 1)[0].lower()
            if isinstance(value, str) and value.strip() and media_type in _TEXT_TYPES:
                return _text(value, media_type), "feed_content", str(content.get("language") or "")
    # feedparser normalizes RSS description and Atom summary to these fields.
    value = entry.get("summary") or entry.get("description")
    detail = entry.get("summary_detail") or {}
    media_type = str(detail.get("type") or "text/html").split(";", 1)[0].lower()
    if isinstance(value, str) and value.strip() and media_type in _TEXT_TYPES:
        return _text(value, media_type), "feed_summary", str(detail.get("language") or "")
    raise CollectionError("Feed item contains no readable post text.", code="empty_feed_content")


def _language(*values: Any) -> str:
    for value in values:
        if isinstance(value, str):
            candidate = value.strip().replace("_", "-")
            if _LANGUAGE.fullmatch(candidate):
                return candidate
    return "und"


def parse_feed_content(
    entry: dict[str, Any],
    *,
    permalink: str,
    resource: FetchedResource,
    topic: str,
    publisher: str,
    language: str,
    feed_author: str = "",
) -> CollectedArticle:
    """The permalink identifies the post; provenance records the actual feed fetch."""
    try:
        text, content_source, body_language = _body(entry)
    except CollectionError as exc:
        raise CollectionError(str(exc), code=exc.code, url=permalink) from exc
    if len(text) < 30:
        raise CollectionError(
            "Feed post text must contain at least 30 characters; no article link was fetched.",
            code="feed_content_too_short",
            url=permalink,
        )
    if len(text) > 50_000:
        raise CollectionError(
            "Feed post text exceeds the 50,000-character limit.",
            code="feed_content_too_large",
            url=permalink,
        )
    title = clean_metadata(entry.get("title"), 300) or text.splitlines()[0][:140]
    author = clean_metadata(entry.get("author"), 300) or clean_metadata(feed_author, 300) or None
    published_at = publication_date(entry.get("published"), entry.get("published_parsed"))
    try:
        article = ArticleInput(
            title=title,
            publisher=publisher or urlsplit(resource.final_url).hostname or "Unknown publisher",
            url=permalink,
            published_at=published_at,
            text=text,
            language=_language(entry.get("language"), body_language, language),
            topic=topic,
            is_sample=False,
            content_source=content_source,
        )
    except ValidationError as exc:
        raise CollectionError(
            "Feed post metadata failed article validation.", code="invalid_metadata", url=permalink
        ) from exc
    return CollectedArticle(
        article=article,
        provenance=SourceProvenance(
            requested_url=resource.requested_url,
            final_url=resource.final_url,
            feed_url=resource.final_url,
            retrieved_at=resource.retrieved_at,
            author=author,
            published_at=published_at or None,
            publication_source="feed" if published_at else "unknown",
            content_source=content_source,
        ),
    )
