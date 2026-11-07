"""Canonical configured-source identity shared by collection and saved feeds."""

from urllib.parse import parse_qs, urlsplit


def canonical_source_key(platform: str, locator: str) -> str:
    from .collection.registry import normalize_locator
    from .ingestion.url_policy import normalize_url

    if platform in {"rss", "news", "other"}:
        url = normalize_url(locator)
        parsed = urlsplit(url)
        if parsed.hostname in {"youtube.com", "www.youtube.com"} and parsed.path in {
            "/feeds/videos.xml",
            "/xml/feeds/videos.xml",
        }:
            channel = parse_qs(parsed.query).get("channel_id", [""])[0]
            try:
                return "youtube:" + normalize_locator("youtube", channel)
            except ValueError:
                pass
        return "rss:" + url
    return platform + ":" + normalize_locator(platform, locator)
