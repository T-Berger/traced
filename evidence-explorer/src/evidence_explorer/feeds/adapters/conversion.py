"""Lossless typed source metadata beside article text for the shared model pipeline."""

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from ...collection.models import CollectedBatch
from ...ingestion.models import CollectedArticle, EntryError, FeedCollection, SourceProvenance
from ...models import ArticleInput
from ...source_contracts import SourceItem
from .base import SourceAdapterError
from .contracts import NormalizedSource


def _article(item: SourceItem, source: NormalizedSource, name: str, topic: str) -> CollectedArticle:
    if item.platform != source.platform:
        raise ValueError("The returned item belongs to a different platform.")
    if not item.source_url:
        raise ValueError("The returned platform item has no source permalink.")
    acquisition = item.provenance
    retrieved_url = (
        acquisition.retrieved_url if acquisition and acquisition.retrieved_url else source.url
    )
    feed_url = acquisition.feed_url if acquisition and acquisition.feed_url else source.url
    origin = {
        "source_platform": item.platform,
        "source_channel_id": item.channel_id,
        "source_native_id": item.native_id,
        "source_author_id": item.author_id,
        "acquisition_method": acquisition.method if acquisition else "saved_import",
        "content_scope": acquisition.content_scope if acquisition else "post_text",
    }
    title = item.title or next(
        (line.strip() for line in item.text.splitlines() if line.strip()), ""
    )
    title = title[:300] or f"{name} · {item.native_id}"[:300]
    published = item.published_at or ""
    author = item.author_id[:300] if item.author_id else None
    article = ArticleInput(
        title=title,
        publisher=item.channel_name[:150],
        url=item.source_url,
        published_at=published,
        text=item.text,
        language="und",
        topic=topic,
        is_sample=False,
        requested_url=source.url,
        final_url=retrieved_url,
        feed_url=feed_url,
        retrieved_at=item.acquired_at,
        author=author,
        publication_source="platform" if published else "unknown",
        content_source="platform_post",
        source_metadata_json=json.dumps(
            item.model_dump(mode="json", exclude={"text"}),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ),
        **origin,
    )
    return CollectedArticle(
        article=article,
        provenance=SourceProvenance(
            requested_url=source.url,
            final_url=retrieved_url,
            feed_url=feed_url,
            retrieved_at=item.acquired_at,
            author=author,
            published_at=published or None,
            publication_source="platform" if published else "unknown",
            content_source="platform_post",
        ),
    )


def batch_to_feed(
    batch: CollectedBatch,
    source: NormalizedSource,
    *,
    name: str,
    topic: str,
    limit: int,
) -> FeedCollection:
    """Convert provider results without fetching post URLs or expanding linked media."""
    if not isinstance(batch, CollectedBatch):
        raise SourceAdapterError("The platform collector returned an invalid batch.")
    acquired = datetime.now(UTC).isoformat()
    result = FeedCollection(
        requested_url=source.url,
        final_url=source.url,
        retrieved_at=acquired,
        entries_seen=len(batch.items),
        warnings=list(batch.warnings),
        cursor=batch.cursor,
    )
    seen: set[tuple[str, str, str]] = set()
    timestamps = []
    for supplied in batch.items[:limit]:
        try:
            item = SourceItem.model_validate(supplied)
            identity = (item.platform, item.channel_id, item.native_id)
            if identity in seen:
                result.duplicates_skipped += 1
                continue
            converted = _article(item, source, name, topic)
            result.articles.append(converted)
            seen.add(identity)
            timestamps.append(item.acquired_at)
            if len(result.articles) == 1:
                result.final_url = converted.provenance.final_url
        except (ValueError, TypeError, ValidationError):
            result.errors.append(
                EntryError(
                    url=getattr(supplied, "source_url", None) or source.url,
                    code="invalid_platform_record",
                    message="A platform record could not be imported with valid source metadata.",
                )
            )
    if timestamps:
        result.retrieved_at = max(timestamps, key=datetime.fromisoformat)
    if len(batch.items) > limit:
        result.warnings.append("The source returned more records than this refresh's item limit.")
    return result


def source_item_from_article(record: BaseModel | dict[str, Any]) -> SourceItem | None:
    """Reconstruct the shared source contract; ordinary news articles return None.

    The saved article body always wins over metadata JSON. Its platform identity
    must still agree with the explicit, indexed provenance columns.
    """
    values = record.model_dump(mode="json") if isinstance(record, BaseModel) else record
    if not isinstance(values, dict):
        raise SourceAdapterError(
            "The saved platform article is invalid.", code="invalid_platform_record"
        )
    encoded = values.get("source_metadata_json")
    if encoded is None:
        return None
    try:
        metadata = json.loads(encoded)
        if not isinstance(metadata, dict):
            raise ValueError
        item = SourceItem.model_validate({**metadata, "text": values["text"]})
        for field, expected in (
            ("source_platform", item.platform),
            ("source_channel_id", item.channel_id),
            ("source_native_id", item.native_id),
            ("source_author_id", item.author_id),
        ):
            if field in values and values[field] != expected:
                raise ValueError
        if values.get("url") != item.source_url:
            raise ValueError
        return item
    except (KeyError, ValueError, TypeError, ValidationError):
        raise SourceAdapterError(
            "The saved platform metadata does not match its article.",
            code="invalid_platform_record",
        ) from None
