"""Explicit normalized JSONL import; raw Telegram schema adaptation follows inspection."""

from __future__ import annotations

import json
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from .config import positive_number
from .storage import fingerprint


def normalized_timestamp(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Timestamps must be timezone-aware ISO 8601 strings.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Timestamp has no timezone: {value}")
    return parsed.astimezone(UTC).isoformat()


_KEY_FIELDS = {"channel": "channel_key", "post": "post_key", "message": "message_key"}
_REQUIRED_FIELDS = {
    "channel": ("channel_key",),
    "post": ("post_key", "channel_key", "timestamp_utc"),
    "message": ("message_key", "post_key", "channel_key", "timestamp_utc"),
}
_TEXT_FIELDS = {
    "metadata": ("dataset_id", "label"),
    "channel": ("name", "category", "rationale"),
    "post": ("post_family_key", "title", "text_original", "source_ref"),
    "message": ("text_original", "source_ref", "sender_type"),
}


def _validate_record(row: dict, record_type: str) -> None:
    """Validate field shapes at the file boundary, before normalizing references."""
    for field in _REQUIRED_FIELDS.get(record_type, ()):
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a nonempty string.")
    for field in _TEXT_FIELDS[record_type]:
        if field in row and not isinstance(row[field], str):
            raise ValueError(f"{field} must be text.")
    if record_type != "metadata":
        return
    manifest = row.get("manifest", {})
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object.")
    if "identity_stable_across_channels" in manifest and not isinstance(
        manifest["identity_stable_across_channels"], bool
    ):
        raise ValueError("identity_stable_across_channels must be a JSON boolean.")
    limitations = manifest.get("limitations", [])
    if not isinstance(limitations, list) or not all(isinstance(note, str) for note in limitations):
        raise ValueError("limitations must be a list of strings.")


def _read_records(path: str | Path) -> tuple[dict, dict, Counter]:
    """Read and deduplicate records, preserving line numbers in input errors."""
    metadata = {}
    metadata_seen = False
    collections = {record_type: {} for record_type in _KEY_FIELDS}
    duplicates = Counter()
    with Path(path).open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("Each record must be an object.")
                record_type = row.pop("record_type")
                if not isinstance(record_type, str) or record_type not in _TEXT_FIELDS:
                    raise ValueError(f"Unknown record_type: {record_type}")
                _validate_record(row, record_type)
                if record_type == "metadata":
                    if metadata_seen:
                        raise ValueError("Only one metadata record is allowed.")
                    metadata, metadata_seen = row, True
                    continue
                key = row[_KEY_FIELDS[record_type]]
                if key in collections[record_type]:
                    if collections[record_type][key] != row:
                        raise ValueError(f"Conflicting duplicate {record_type} key: {key}")
                    duplicates[record_type] += 1
                    continue
                collections[record_type][key] = row
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"Invalid normalized record at line {line_number}: {exc}") from exc
    return metadata, collections, duplicates


def load_jsonl(path: str | Path) -> dict:
    """Load the normalized contract without inferring raw Telegram schemas."""
    metadata, collections, duplicates = _read_records(path)
    channels, posts, messages = (
        list(collections[k].values()) for k in ("channel", "post", "message")
    )
    if not channels or not posts or not messages:
        raise ValueError("Import requires channel, post, and message records.")
    channel_ids = {c["channel_key"] for c in channels}
    for channel in channels:
        channel.setdefault("name", channel["channel_key"])
        channel.setdefault("category", "unknown")
        channel.setdefault("rationale", "Unreviewed channel classification.")
    post_index = {p["post_key"]: p for p in posts}
    for post in posts:
        if post["channel_key"] not in channel_ids:
            raise ValueError(f"Unknown channel in post {post['post_key']}")
        post["timestamp_utc"] = normalized_timestamp(post["timestamp_utc"])
        post.setdefault("post_family_key", post["post_key"])
        post.setdefault("title", post["post_key"])
        post.setdefault("text_original", "")
        post.setdefault("source_ref", f"jsonl:{Path(path).name}#post={post['post_key']}")
    manifest = dict(metadata.get("manifest", {}))
    cross_channel = manifest.get("identity_stable_across_channels") is True
    unknown_accounts = 0
    for message in messages:
        post = post_index.get(message["post_key"])
        if not post or message["channel_key"] != post["channel_key"]:
            raise ValueError(f"Unresolved or inconsistent parent for {message['message_key']}")
        message["timestamp_utc"] = normalized_timestamp(message["timestamp_utc"])
        if message["timestamp_utc"] < post["timestamp_utc"]:
            raise ValueError(f"Comment predates its parent post: {message['message_key']}")
        message.setdefault("sender_type", "user")
        account = message.get("account_key")
        if account is None or account == "":
            unknown_accounts += 1
            message["account_key"] = None
        elif not isinstance(account, str):
            raise ValueError("account_key must be a stable string or null.")
        elif not cross_channel:
            message["account_key"] = json.dumps(
                [message["channel_key"], account], separators=(",", ":")
            )
        message.setdefault("text_original", "")
        message.setdefault("urls", [])
        if (
            not isinstance(message["text_original"], str)
            or not isinstance(message["urls"], list)
            or not all(isinstance(u, str) for u in message["urls"])
        ):
            raise ValueError("text_original must be text and urls must be a list of strings.")
        message.setdefault(
            "source_ref", f"jsonl:{Path(path).name}#message={message['message_key']}"
        )
    precision = positive_number(
        manifest.get("timestamp_precision_seconds", 60), "timestamp_precision_seconds"
    )
    if "synthetic" in metadata and not isinstance(metadata["synthetic"], bool):
        raise ValueError("synthetic must be a JSON boolean.")
    manifest.update(
        {
            "identity_stable_across_channels": cross_channel,
            "timestamp_precision_seconds": precision,
            "timestamp_precision_declared": "timestamp_precision_seconds"
            in metadata.get("manifest", {}),
            "import_format": "normalized-jsonl-v1",
            "duplicate_records": dict(duplicates),
            "unknown_account_comments": unknown_accounts,
            "record_counts": {
                "channels": len(channels),
                "posts": len(posts),
                "messages": len(messages),
            },
            "limitations": [
                *manifest.get("limitations", []),
                *(
                    []
                    if cross_channel
                    else [
                        "Account IDs are scoped per channel; cross-channel identity is unverified."
                    ]
                ),
            ],
        }
    )
    dataset = {
        "dataset_id": metadata.get(
            "dataset_id",
            "import-"
            + fingerprint({"channels": channels, "posts": posts, "messages": messages})[:12],
        ),
        "label": metadata.get("label", Path(path).stem),
        "synthetic": metadata.get("synthetic", False),
        "channels": channels,
        "posts": posts,
        "messages": messages,
        "comparisons": [],
        "manifest": manifest,
    }
    if not isinstance(dataset["dataset_id"], str) or not dataset["dataset_id"].strip():
        raise ValueError("dataset_id must be a nonempty string.")
    return dataset


def dataset_readiness(dataset: dict, run: dict | None) -> dict:
    """Compatibility report for callers already holding complete wire records."""
    from .readiness import facts_from_records, readiness_from_facts

    return readiness_from_facts(dataset, run, facts_from_records(dataset, run))


def inspect_archive(path: str | Path, limit: int = 40) -> dict:
    path = Path(path)
    if path.suffix.lower() != ".zip" or path.name.endswith(".crdownload"):
        raise ValueError(
            "Inspect only a completed .zip archive; partial downloads remain untouched."
        )
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        candidates = [
            entry
            for entry in entries
            if Path(entry.filename).suffix.lower()
            in {".json", ".jsonl", ".csv", ".tsv", ".txt", ".md"}
        ]
        return {
            "path": str(path.resolve()),
            "bytes": path.stat().st_size,
            "member_count": len(entries),
            "uncompressed_bytes": sum(e.file_size for e in entries),
            "structured_or_text_members": len(candidates),
            "sample": [
                {"name": e.filename, "bytes": e.file_size}
                for e in candidates[: max(0, min(limit, 200))]
            ],
            "note": "Inventory only. No files were extracted and no imported content was executed.",
        }
