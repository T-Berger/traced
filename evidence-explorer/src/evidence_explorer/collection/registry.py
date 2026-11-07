"""Platform dispatch and locator validation, independent of storage and the interface."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from ..ingestion.url_policy import normalize_url
from .models import CollectedBatch, CollectionFailure, CollectionSource


def normalize_locator(platform: str, locator: str) -> str:
    value = locator.strip()
    parsed = urlsplit(value)
    if platform in {"news", "other"}:
        return normalize_url(value)
    if platform == "youtube":
        if parsed.scheme:
            if (
                parsed.scheme != "https"
                or parsed.username
                or parsed.port not in {None, 443}
                or parsed.hostname
                not in {
                    "youtube.com",
                    "www.youtube.com",
                }
            ):
                raise CollectionFailure("Use a YouTube channel ID or its HTTPS channel URL.")
            if parsed.path.startswith("/channel/"):
                value = parsed.path.split("/")[2]
            elif parsed.path in {"/feeds/videos.xml", "/xml/feeds/videos.xml"}:
                value = parse_qs(parsed.query).get("channel_id", [""])[0]
        if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", value):
            raise CollectionFailure(
                "Use the YouTube channel ID beginning with UC, or its channel URL."
            )
        return value
    hosts = {
        "telegram": {"t.me", "www.t.me", "telegram.me"},
        "x": {"x.com", "www.x.com", "twitter.com", "www.twitter.com"},
        "reddit": {"reddit.com", "www.reddit.com", "old.reddit.com"},
    }
    if platform not in hosts:
        raise CollectionFailure("Choose one of the supported source platforms.")
    if parsed.scheme:
        if parsed.scheme != "https" or parsed.hostname not in hosts[platform] or parsed.username:
            raise CollectionFailure("Use a public channel or profile URL on the selected platform.")
        path = parsed.path.strip("/").split("/")
        if platform == "telegram" and path[0] == "s":
            path = path[1:]
        if platform == "reddit" and path[0] == "r":
            path = path[1:]
        if len(path) != 1:
            raise CollectionFailure(
                "Enter the channel or community URL, rather than an individual post."
            )
        value = path[0]
    value = value.removeprefix("@").removeprefix("r/")
    pattern = {
        "telegram": r"[A-Za-z][A-Za-z0-9_]{3,31}",
        "x": r"[A-Za-z0-9_]{1,15}",
        "reddit": r"[A-Za-z0-9_]{2,21}",
    }[platform]
    if not re.fullmatch(pattern, value):
        raise CollectionFailure("Enter a valid public channel or account name for this platform.")
    return value.lower()


def collect(source: CollectionSource) -> CollectedBatch:
    if source.platform in {"x", "reddit"}:
        from .apis import collect_api

        return collect_api(source)
    from .public import collect_public

    return collect_public(source)
