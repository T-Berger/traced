"""Look up one existing Wayback capture without fetching or saving the page."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from ..ingestion.fetching import PublicFetcher
from ..ingestion.models import CollectionError
from ..ingestion.url_policy import Resolver, normalize_url, public_addresses
from .models import CollectionFailure

_AVAILABILITY_URL = "https://archive.org/wayback/available"
_CAPTURE_PATH = re.compile(r"/web/([0-9]{14})/(https?://.+)")
_INVALID_CAPTURE = "Wayback returned an invalid or mismatched capture."


@dataclass(frozen=True)
class WaybackLookup:
    original_url: str
    archive_url: str | None
    captured_at: str | None
    checked_at: str

    @property
    def found(self) -> bool:
        return self.archive_url is not None


def _public_page_url(url: str) -> str:
    if not isinstance(url, str) or "?" in url or "#" in url:
        raise CollectionFailure("Use a public page URL without a query string or fragment.")
    try:
        normalized = normalize_url(url)
    except CollectionError as exc:
        raise CollectionFailure(str(exc)) from None
    if urlsplit(normalized).port is not None:
        raise CollectionFailure("Use a public page URL on the standard HTTP or HTTPS port.")
    return normalized


def _validated_capture(snapshot: dict, original_url: str) -> tuple[str, str]:
    raw_url = snapshot.get("url")
    if not isinstance(raw_url, str):
        raise CollectionFailure(_INVALID_CAPTURE)
    try:
        archive = urlsplit(_public_page_url(raw_url))
        capture = _CAPTURE_PATH.fullmatch(archive.path)
        if archive.hostname != "web.archive.org" or not capture:
            raise ValueError
        timestamp, captured_url = capture.groups()
        if snapshot.get("timestamp") != timestamp or str(snapshot.get("status")) != "200":
            raise ValueError
        captured = urlsplit(_public_page_url(captured_url))
        original = urlsplit(original_url)
        if (captured.netloc, captured.path) != (original.netloc, original.path):
            raise ValueError
        captured_at = datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        raise CollectionFailure(_INVALID_CAPTURE) from None
    archive_url = urlunsplit(("https", archive.netloc, archive.path, "", ""))
    return archive_url, captured_at.isoformat()


def lookup_wayback(
    url: str,
    capture_date: date | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
) -> WaybackLookup:
    """Return the closest accessible capture, or the latest when no date is given."""
    original_url = _public_page_url(url)
    params = {"url": original_url}
    if capture_date is not None:
        if not isinstance(capture_date, date) or isinstance(capture_date, datetime):
            raise CollectionFailure("Enter a capture date in YYYY-MM-DD format.")
        params["timestamp"] = capture_date.isoformat().replace("-", "")
    request_url = f"{_AVAILABILITY_URL}?{urlencode(params)}"
    if len(request_url) > 2000:
        raise CollectionFailure("Use a shorter public page URL for Wayback.")

    fetcher = PublicFetcher(
        transport=transport, resolver=resolver, timeout=12, max_bytes=65_536, max_redirects=0
    )
    try:
        public_addresses(original_url, fetcher.resolver)
        resource = fetcher.fetch(request_url)
    except CollectionError as exc:
        status = re.match(r"Source returned HTTP ([0-9]{3});", str(exc))
        if status:
            raise CollectionFailure(f"Wayback returned HTTP {status[1]}.") from None
        if exc.code == "blocked_url":
            raise CollectionFailure("Use a page URL with a public address.") from None
        if exc.code == "timeout":
            raise CollectionFailure("The Wayback lookup timed out.") from None
        raise CollectionFailure("The Wayback lookup could not be completed.") from None
    finally:
        fetcher.close()

    if resource.content_type != "application/json":
        raise CollectionFailure("Wayback returned an unreadable availability response.")
    try:
        payload = json.loads(resource.body)
    except (ValueError, RecursionError):
        raise CollectionFailure("Wayback returned an unreadable availability response.") from None
    snapshots = payload.get("archived_snapshots") if isinstance(payload, dict) else None
    if not isinstance(snapshots, dict):
        raise CollectionFailure("Wayback returned an unreadable availability response.")
    closest = snapshots.get("closest")
    if not snapshots or (isinstance(closest, dict) and closest.get("available") is False):
        return WaybackLookup(original_url, None, None, resource.retrieved_at)
    if not isinstance(closest, dict) or closest.get("available") is not True:
        raise CollectionFailure(_INVALID_CAPTURE)
    archive_url, captured_at = _validated_capture(closest, original_url)
    return WaybackLookup(original_url, archive_url, captured_at, resource.retrieved_at)
