"""Pure extraction of bounded downloaded bytes; these functions never fetch URLs."""

import calendar
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlsplit

import feedparser
import trafilatura
from pydantic import ValidationError

from ..models import ArticleInput
from .fetching import FetchedResource
from .metadata import declared_metadata
from .models import CollectedArticle, CollectionError, SourceProvenance
from .url_policy import normalize_url

HTML_TYPES = {"text/html", "application/xhtml+xml"}
FEED_TYPES = {
    "application/rss+xml",
    "application/atom+xml",
    "application/xml",
    "text/xml",
    "application/rdf+xml",
}


def clean_metadata(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", value))).strip()[:limit]


def publication_date(value: Any, parsed: Any = None) -> str:
    """Normalize known dates without substituting the collection timestamp."""
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            try:
                return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
            except ValueError:
                return ""
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                stamp = parsedate_to_datetime(value)
            except (ValueError, TypeError, OverflowError):
                stamp = None
        if stamp is not None:
            # An absent timezone stays absent; it is not silently asserted to be UTC.
            return stamp.isoformat()
    if parsed:
        try:
            return datetime.fromtimestamp(calendar.timegm(parsed), UTC).isoformat()
        except (ValueError, TypeError, OverflowError):
            pass
    return ""


def parse_article(
    resource: FetchedResource,
    *,
    topic: str = "General",
    fallback: dict | None = None,
) -> CollectedArticle:
    fallback = fallback or {}
    if resource.content_type not in HTML_TYPES:
        raise CollectionError(
            "Source is not an HTML article.", code="unsupported_content", url=resource.final_url
        )
    try:
        declared_fields = declared_metadata(resource.body)
        document = trafilatura.bare_extraction(
            resource.body,
            url=resource.final_url,
            with_metadata=True,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
            date_extraction_params={"original_date": True, "extensive_search": False},
        )
        data = (
            document.as_dict()
            if document is not None and hasattr(document, "as_dict")
            else document
        )
    except Exception as exc:
        raise CollectionError(
            "Article HTML could not be extracted.", code="extraction_error", url=resource.final_url
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        raise CollectionError(
            "No readable article body was found; feed teasers were not imported.",
            code="empty_extraction",
            url=resource.final_url,
        )
    text = data["text"].strip()
    if len(text) < 30:
        raise CollectionError(
            "The extracted article body is too short.",
            code="empty_extraction",
            url=resource.final_url,
        )
    if len(text) > 50_000:
        raise CollectionError(
            "Extracted article exceeds the 50,000-character limit.",
            code="article_too_large",
            url=resource.final_url,
        )
    title = clean_metadata(data.get("title"), 300) or clean_metadata(fallback.get("title"), 300)
    if not title:
        raise CollectionError(
            "Article has no title and no feed title was available.",
            code="invalid_metadata",
            url=resource.final_url,
        )
    publisher = (
        clean_metadata(data.get("sitename"), 150)
        or clean_metadata(fallback.get("publisher"), 150)
        or (urlsplit(resource.final_url).hostname or "Unknown publisher")
    )
    extracted_date = next(
        (
            parsed
            for date in declared_fields["publication_dates"]
            if (parsed := publication_date(date))
        ),
        "",
    )
    # Trafilatura can return a modification date even with original_date=True.
    # A modified-only declaration cannot establish when an article was published.
    if not extracted_date and not declared_fields["has_modified_date"]:
        extracted_date = publication_date(data.get("date"))
    published_at = extracted_date or publication_date(fallback.get("published_at"))
    publication_source = "article" if extracted_date else "feed" if published_at else "unknown"
    final_url = resource.final_url
    # A same-host canonical URL can merge alternate article links. Cross-site
    # canonical metadata cannot silently change source identity.
    declared = data.get("url")
    if isinstance(declared, str):
        try:
            candidate = normalize_url(urljoin(final_url, declared))
        except CollectionError:
            candidate = None
        if candidate and urlsplit(candidate).hostname == urlsplit(final_url).hostname:
            final_url = candidate
    language = (
        declared_fields["language"]
        or clean_metadata(data.get("language"), 30)
        or clean_metadata(fallback.get("language"), 30)
        or "und"
    )
    author = (
        clean_metadata(data.get("author"), 300)
        or clean_metadata(fallback.get("author"), 300)
        or None
    )
    try:
        article = ArticleInput(
            title=title,
            publisher=publisher,
            url=final_url,
            published_at=published_at,
            text=text,
            language=language,
            topic=topic,
            is_sample=False,
        )
    except ValidationError as exc:
        raise CollectionError(
            "Extracted source metadata failed article validation.",
            code="invalid_metadata",
            url=final_url,
        ) from exc
    return CollectedArticle(
        article=article,
        provenance=SourceProvenance(
            requested_url=resource.requested_url,
            final_url=resource.final_url,
            feed_url=fallback.get("feed_url"),
            retrieved_at=resource.retrieved_at,
            author=author,
            published_at=published_at or None,
            publication_source=publication_source,
        ),
    )


def parse_feed(resource: FetchedResource):
    if resource.content_type not in FEED_TYPES and resource.content_type not in {"text/plain", ""}:
        raise CollectionError(
            "Source is not an RSS or Atom feed.", code="invalid_feed", url=resource.final_url
        )
    parsed = feedparser.parse(
        resource.body,
        response_headers={
            "content-location": resource.final_url,
            "content-type": resource.content_type,
        },
    )
    if parsed.get("bozo") or not parsed.get("version"):
        raise CollectionError(
            "Feed is malformed or is not supported RSS/Atom XML.",
            code="invalid_feed",
            url=resource.final_url,
        )
    return parsed
