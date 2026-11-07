"""Real-news collection boundary: validated HTTP(S), RSS/Atom, and article text."""

from .collector import NewsCollector
from .models import CollectedArticle, CollectionError, EntryError, FeedCollection, SourceProvenance
from .url_policy import normalize_url

__all__ = [
    "CollectedArticle",
    "CollectionError",
    "EntryError",
    "FeedCollection",
    "NewsCollector",
    "SourceProvenance",
    "normalize_url",
]
