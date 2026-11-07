"""Pure bounded source-browser projections over supplied saved evidence.

Records, comparisons and filter options are derived from the supplied bundle.
No repository, collection provider or worker is accessed while rendering them.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from ..source_contracts import (
    PLATFORM_LABELS,
    _channel_key,
    _text_hash,
    _timestamp,
    safe_source_url,
)
from .contracts import EvidenceBundle

MAX_BROWSER_ITEMS = 200


def _browser_record(record: dict) -> dict:
    metadata = record.get("metadata", {})
    platform = metadata.get("platform")
    if not isinstance(platform, str) or platform not in PLATFORM_LABELS:
        kind = record.get("kind", "")
        platform = (
            "telegram" if kind.startswith("telegram_") else "news" if kind == "article" else "other"
        )
    raw_swarm = metadata.get("swarm", {})
    if not isinstance(raw_swarm, dict):
        raw_swarm = {}
    channel_id = str(
        metadata.get("channel_id") or raw_swarm.get("channel_key") or record["source_label"]
    )
    channel_name = str(
        metadata.get("channel_name") or raw_swarm.get("name") or record["source_label"]
    )
    if platform == "news":
        channel_id = str(metadata.get("channel_id") or metadata.get("publisher") or channel_id)
        channel_name = str(
            metadata.get("channel_name") or metadata.get("publisher") or channel_name
        )
    published = record.get("published_at")
    acquired = record.get("acquired_at")
    return {
        **record,
        "platform": platform,
        "platform_label": PLATFORM_LABELS[platform],
        "channel_id": channel_id,
        "channel_name": channel_name,
        "channel_key": _channel_key(platform, channel_id),
        "native_id": metadata.get("native_id"),
        "author_key": metadata.get("author_key"),
        "published_label": published or "Unknown publication time",
        "acquired_label": acquired or "Unknown acquisition time",
    }


def _sort_publication(record: dict) -> tuple[bool, datetime, str]:
    try:
        timestamp = (
            datetime.fromisoformat(_timestamp(record["published_at"]))
            if record.get("published_at")
            else None
        )
    except (ValueError, TypeError, AttributeError):
        timestamp = None
    return timestamp is None, timestamp or datetime.max.replace(tzinfo=UTC), record["id"]


def source_browser_data(
    bundle: dict | EvidenceBundle,
    platforms: list[str] | None = None,
    channels: list[str] | None = None,
    limit: int = 100,
) -> dict:
    """Return bounded saved records and distinct, descriptive relationship groups.

    Filter values are platform keys and namespaced channel keys. Group membership
    covers filtered records, with group lists and member IDs capped by ``limit``.
    Empty filter lists mean all available sources. No locator is fetched.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_BROWSER_ITEMS:
        raise ValueError(f"Source browser limit must be between 1 and {MAX_BROWSER_ITEMS}.")
    payload = bundle.model_dump(mode="json") if isinstance(bundle, EvidenceBundle) else bundle
    records = [_browser_record(record) for record in payload.get("records", [])]
    channel_names = {
        record["channel_key"]: record["channel_name"]
        for record in records
        if record["kind"] == "telegram_channel"
    }
    for record in records:
        if record["channel_key"] in channel_names:
            record["channel_name"] = channel_names[record["channel_key"]]
    platform_options = [
        {"label": PLATFORM_LABELS[key], "value": key}
        for key in PLATFORM_LABELS
        if any(record["platform"] == key for record in records)
    ]
    eligible_channels = {
        record["channel_key"]: {
            "label": f"{record['platform_label']} · {record['channel_name']}",
            "value": record["channel_key"],
        }
        for record in records
        if not platforms or record["platform"] in platforms
    }
    filtered = sorted(
        (
            record
            for record in records
            if (not platforms or record["platform"] in platforms)
            and (not channels or record["channel_key"] in channels)
        ),
        key=_sort_publication,
    )
    by_hash: dict[str, list[str]] = defaultdict(list)
    by_url: dict[str, list[str]] = defaultdict(list)
    for record in filtered:
        if record.get("text") is not None:
            by_hash[_text_hash(record["text"])].append(record["id"])
        shared_urls = record.get("metadata", {}).get("shared_urls", [])
        urls = (
            {url for url in shared_urls if isinstance(url, str)}
            if isinstance(shared_urls, list)
            else set()
        )
        if record.get("source_url"):
            urls.add(record["source_url"])
        for url in sorted(urls):
            try:
                safe_source_url(url)
            except (ValueError, TypeError):
                continue
            by_url[url].append(record["id"])

    def groups(collection: dict[str, list[str]], key_name: str, basis: str) -> list[dict]:
        return [
            {
                key_name: key,
                "record_ids": ids[:limit],
                "count": len(ids),
                "truncated": len(ids) > limit,
                "basis": basis,
            }
            for key, ids in sorted(collection.items())
            if len(ids) > 1
        ][:limit]

    filtered_ids = {record["id"] for record in filtered}
    reposts = [
        {
            "source_id": link["source"]["id"],
            "target_id": link["target"]["id"],
            "basis": link["basis"],
        }
        for link in payload.get("links", [])
        if link.get("relation") == "documented_repost"
        and link.get("source", {}).get("type") == "record"
        and link.get("target", {}).get("type") == "record"
        and link["source"]["id"] in filtered_ids
        and link["target"]["id"] in filtered_ids
    ]
    return {
        "platforms": platform_options,
        "channels": sorted(
            eligible_channels.values(), key=lambda option: (option["label"], option["value"])
        ),
        "records": filtered[:limit],
        "total": len(records),
        "filtered_count": len(filtered),
        "truncated": len(filtered) > limit,
        "exact_groups": groups(
            by_hash, "content_hash", "Byte-identical saved UTF-8 text; origin is unknown."
        ),
        "url_groups": groups(
            by_url,
            "url",
            "Same supplied URL; matching content and actor identity are not established.",
        ),
        "reposts": reposts[:limit],
    }
