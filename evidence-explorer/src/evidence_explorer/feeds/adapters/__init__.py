"""Provider wrappers for the saved-feed scheduler and shared local-analysis pipeline."""

from .base import SourceAdapterError
from .contracts import AdapterReadiness, NormalizedSource, PlatformAdapter
from .conversion import batch_to_feed, source_item_from_article
from .dispatcher import collect_platform_feed
from .locators import normalize_source, source_readiness
from .reddit import RedditAdapter
from .telegram import TelegramAdapter
from .x import XAdapter
from .youtube import YouTubeAdapter

__all__ = [
    "AdapterReadiness",
    "NormalizedSource",
    "PlatformAdapter",
    "RedditAdapter",
    "SourceAdapterError",
    "TelegramAdapter",
    "XAdapter",
    "YouTubeAdapter",
    "batch_to_feed",
    "collect_platform_feed",
    "normalize_source",
    "source_item_from_article",
    "source_readiness",
]
