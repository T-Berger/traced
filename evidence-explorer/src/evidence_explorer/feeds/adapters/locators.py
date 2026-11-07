"""Reuse provider normalization and expose credential presence without secrets."""

from ...collection.models import CollectionFailure
from ...collection.registry import normalize_locator
from ...ingestion.models import CollectionError
from ...ingestion.url_policy import normalize_url
from .contracts import AdapterReadiness, NormalizedSource


def normalize_source(platform: str, locator: str) -> NormalizedSource:
    if not isinstance(platform, str) or not isinstance(locator, str):
        raise CollectionError(
            "Choose a source platform and enter its public locator.", code="invalid_source"
        )
    platform = platform.strip().lower()
    if platform in {"rss", "news", "other"}:
        url = normalize_url(locator)
        return NormalizedSource(platform="rss", locator=url, url=url)
    if platform not in {"telegram", "youtube", "reddit", "x"}:
        raise CollectionError("Choose a supported saved-source platform.", code="invalid_source")
    try:
        normalized = normalize_locator(platform, locator)
    except (CollectionFailure, ValueError):
        messages = {
            "telegram": "Enter a public Telegram channel name or its channel URL.",
            "youtube": "Enter a YouTube channel ID beginning with UC, or its channel URL.",
            "reddit": "Enter a subreddit name or its public community URL.",
            "x": "Enter an X account name or its public profile URL.",
        }
        raise CollectionError(messages[platform], code="invalid_source") from None
    urls = {
        "telegram": f"https://t.me/s/{normalized}",
        "youtube": f"https://www.youtube.com/feeds/videos.xml?channel_id={normalized}",
        "reddit": f"https://www.reddit.com/r/{normalized}/",
        "x": f"https://x.com/{normalized}",
    }
    return NormalizedSource(platform=platform, locator=normalized, url=urls[platform])


def source_readiness(platform: str) -> AdapterReadiness:
    if platform in {"rss", "news", "other", "telegram", "youtube"}:
        return AdapterReadiness(
            ready=True, message="No credentials required. Availability is checked on refresh."
        )
    if platform in {"reddit", "x"}:
        from ...collection.apis import configuration_status

        status = configuration_status(platform)
        return AdapterReadiness(ready=bool(status["ready"]), message=status["message"])
    return AdapterReadiness(ready=False, message="Choose a supported saved-source platform.")
