"""Project already collected platform records without making another source request."""

import sqlite3
from pathlib import Path

from ...collection.models import CollectedBatch, CollectionFailure
from ...collection.persistence import CollectionRepository
from ...source_contracts import SourceItem
from .conversion import batch_to_feed
from .locators import normalize_source


def stored_platform_feed(db_path: Path, source_id: str, *, topic: str, limit: int):
    repository = CollectionRepository(db_path)
    try:
        source = repository.source(source_id)
        if source.platform not in {"telegram", "youtube", "reddit", "x"}:
            raise ValueError("Choose a saved Telegram, YouTube, Reddit, or X source.")
        items = repository.items(limit=limit, source_id=source_id)
    except sqlite3.Error:
        raise CollectionFailure("Saved platform source records are unavailable.") from None
    batch = CollectedBatch(
        items=tuple(SourceItem.model_validate(item) for item in items),
        warnings=("Used previously saved source records; no new platform request was made.",),
    )
    return batch_to_feed(
        batch,
        normalize_source(source.platform, source.locator),
        name=source.label,
        topic=topic,
        limit=limit,
    )
