"""Explicit, read-only import of one pinned SQLite collector case.

This is a source adapter, not a classifier, account-resolution method or Swarm
detector. It preserves supplied fields and does not assert sample completeness.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..review.comparison_ui import safe_source_url
from ..review.contracts import EvidenceBundle
from ..review.integrations.packages import MAX_FILE_BYTES

if TYPE_CHECKING:
    from ..review.integrations.workspace import WorkspaceService

MAX_SNAPSHOT_BYTES = 100_000_000
MAX_RECORDS = 10_000
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,199}\Z")


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _rows(connection: sqlite3.Connection, query: str, parameters=()) -> list[dict]:
    return [dict(row) for row in connection.execute(query, parameters)]


def build_collector_snapshot_bundle(
    source: str | Path,
    *,
    expected_sha256: str,
    source_case_slug: str,
    new_case_id: str,
    provenance: dict | None = None,
) -> EvidenceBundle:
    """Build a new source case from all rows of one pinned collector snapshot.

    The caller must explicitly supply a verified source hash and case slug. The
    input SQLite file is opened immutable and read-only. Source row, author and
    collection run fields remain attributed collector data, including native IDs,
    timestamps, labels and counters. Raw JSON stays in the separately retained
    source snapshot; derived hashes and row locators identify it. These fields are
    not new findings or independently verified identities. No coverage or human
    decision is created.
    """
    if not isinstance(expected_sha256, str) or not _HASH.fullmatch(expected_sha256):
        raise ValueError("A lowercase SHA-256 source hash is required.")
    if not isinstance(new_case_id, str) or not _CASE_ID.fullmatch(new_case_id):
        raise ValueError(
            "Provide a new case ID using letters, digits, colon, dot, dash or underscore."
        )
    if not isinstance(source_case_slug, str) or not source_case_slug.strip():
        raise ValueError("Choose an explicit collector case slug.")
    if provenance is not None and not isinstance(provenance, dict):
        raise ValueError("Snapshot provenance must be an object.")
    path = Path(source).resolve(strict=True)
    if not path.is_file() or path.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise ValueError("The selected SQLite snapshot is unavailable or too large.")
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("The source has an uncheckpointed WAL; use a consistent saved snapshot.")
    if _file_hash(path) != expected_sha256:
        raise ValueError("The selected snapshot does not match the expected SHA-256.")

    uri = path.as_uri() + "?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            cases = _rows(connection, "SELECT * FROM cases WHERE slug=?", (source_case_slug,))
            if len(cases) != 1:
                raise ValueError("The source case slug is missing or ambiguous.")
            source_case = cases[0]
            rows = _rows(
                connection,
                "SELECT * FROM content_items WHERE case_id=? ORDER BY id LIMIT ?",
                (source_case["id"], MAX_RECORDS + 1),
            )
            if not rows or len(rows) > MAX_RECORDS:
                raise ValueError("The source case is empty or exceeds the 10,000-record bound.")
            authors = {row["id"]: row for row in _rows(connection, "SELECT * FROM authors")}
            runs = _rows(
                connection,
                "SELECT * FROM collection_runs WHERE case_id=? ORDER BY id",
                (source_case["id"],),
            )
            media = _rows(
                connection,
                "SELECT m.* FROM media_assets m JOIN content_items i ON i.id=m.content_item_id "
                "WHERE i.case_id=? ORDER BY m.id LIMIT ?",
                (source_case["id"], MAX_RECORDS * 10 + 1),
            )
            if len(media) > MAX_RECORDS * 10:
                raise ValueError("The source exceeds the bounded media metadata limit.")
            export_info = _rows(connection, "SELECT key,value FROM export_info ORDER BY key")
    except sqlite3.Error as exc:
        raise ValueError("The selected source is not a supported collector snapshot.") from exc
    if _file_hash(path) != expected_sha256:
        raise ValueError("The snapshot changed during reading; no case was imported.")

    media_by_item: dict[str, list[dict]] = {}
    for item in media:
        fields = {
            k: v
            for k, v in item.items()
            if k not in {"raw", "content_item_id"}
            and (v is not None or k in {"sha256", "local_path"})
        }
        media_by_item.setdefault(item["content_item_id"], []).append(fields)
    native_ids = Counter((item.get("platform"), item.get("platform_item_id")) for item in rows)
    normalized_platform_sources: dict[str, set[str]] = {}
    for item in rows:
        supplied = item.get("platform")
        if isinstance(supplied, str) and supplied:
            normalized = "x" if supplied == "twitter" else supplied
            normalized_platform_sources.setdefault(normalized, set()).add(supplied)
    author_ids = Counter(
        (author.get("platform"), author.get("platform_user_id"))
        for author in authors.values()
        if author.get("platform") and author.get("platform_user_id")
    )
    uniform_empty_labels = all(item.get("labels") == "{}" for item in rows)
    no_source_text_hashes = all(item.get("content_sha256") is None for item in rows)
    records = []
    for source_row in rows:
        native_id = source_row.get("id")
        text = source_row.get("text")
        if not isinstance(native_id, str) or not native_id or not isinstance(text, str):
            raise ValueError("Every source row requires its own ID and saved text.")
        suffix = hashlib.sha256(native_id.encode("utf-8")).hexdigest()[:24]
        record_id = f"collector:{expected_sha256[:16]}:item:{suffix}"
        supplied_url = source_row.get("url")
        original_raw = source_row.get("raw")
        source_author = authors.get(source_row.get("author_id"))
        source_fields = {
            k: v
            for k, v in source_row.items()
            if k
            not in {
                "id",
                "case_id",
                "platform",
                "platform_item_id",
                "url",
                "posted_at",
                "collected_at",
                "raw",
                "text",
            }
            and (v is not None or k == "content_sha256")
            and not (k == "labels" and uniform_empty_labels)
            and not (k == "content_sha256" and no_source_text_hashes)
        }
        author_fields = (
            {k: source_author.get(k) for k in ("id", "platform", "platform_user_id", "handle")}
            if source_author
            else None
        )
        supplied_platform = source_row.get("platform")
        platform = "x" if supplied_platform == "twitter" else supplied_platform
        native_author = source_author.get("platform_user_id") if source_author else None
        if (
            not isinstance(native_author, str)
            or not native_author
            or not source_author
            or source_author.get("platform") != supplied_platform
            or author_ids[(supplied_platform, native_author)] != 1
        ):
            native_author = None
        native_item = source_row.get("platform_item_id")
        parent = source_row.get("parent_item_id")
        parent_count = native_ids.get((source_row.get("platform"), parent), 0) if parent else 0
        parent_resolution = (
            "not_supplied"
            if not parent
            else "one_saved_native_match"
            if parent_count == 1
            else "ambiguous"
            if parent_count > 1
            else "unresolved"
        )
        records.append(
            {
                "id": record_id,
                "kind": "saved_source_item",
                "source_label": f"Collector item {native_id[:8]}",
                "text": text,
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "source_url": safe_source_url(supplied_url)
                if isinstance(supplied_url, str)
                else None,
                "published_at": source_row.get("posted_at"),
                "acquired_at": source_row.get("collected_at"),
                "metadata": {
                    "source_item_id": native_id,
                    "platform": platform,
                    **(
                        {"source_platform": supplied_platform}
                        if len(normalized_platform_sources.get(platform, set())) > 1
                        else {}
                    ),
                    **({"native_id": native_item} if native_item else {}),
                    **(
                        {"channel_id": native_author, "author_id": native_author}
                        if platform == "x" and native_author
                        else {}
                    ),
                    **(
                        {"provenance": {"author_label": source_author["handle"]}}
                        if native_author and source_author and source_author.get("handle")
                        else {}
                    ),
                    **({"parent_resolution": parent_resolution} if parent else {}),
                    "source_row": source_fields,
                    "source_raw_sha256_derived": hashlib.sha256(
                        original_raw.encode("utf-8")
                    ).hexdigest()
                    if isinstance(original_raw, str)
                    else None,
                    **({"source_author": author_fields} if not native_author else {}),
                    **(
                        {"source_media": media_by_item[native_id]}
                        if native_id in media_by_item
                        else {}
                    ),
                },
            }
        )
    if len({record["id"] for record in records}) != len(records):
        raise ValueError("The source contains duplicate record identities.")

    first = next((record for record in records if record["text"]), records[0])
    run_id = f"{new_case_id}:collector-run:{expected_sha256[:16]}"
    finding_id = f"{run_id}:observation"
    reference = {"type": "record", "id": first["id"]}
    fragment_ids = []
    fragments = []
    if first["text"]:
        quote = first["text"][:1000]
        fragment_id = f"{first['id']}:opening-fragment"
        fragments.append(
            {
                "id": fragment_id,
                "record_id": first["id"],
                "selector": {"kind": "text", "start": 0, "end": len(quote)},
                "quote": quote,
            }
        )
        reference = {"type": "fragment", "id": fragment_id}
        fragment_ids = [fragment_id]
    bundle = EvidenceBundle.model_validate(
        {
            "case": {
                "id": new_case_id,
                "title": (
                    f"Collector snapshot: {source_case.get('subject_name') or source_case_slug}"
                ),
                "question": "What can the saved source records support after human review?",
                "synthetic": False,
                "coverage": {
                    "status": "unknown",
                    "snapshot_sha256": expected_sha256,
                    "snapshot_bytes": path.stat().st_size,
                    "snapshot_file": path.name,
                    "source_schema": "collector-sqlite-content-items/v1",
                    "normalized_platform_sources": {
                        key: sorted(values) for key, values in normalized_platform_sources.items()
                    },
                    "source_row_locator": (
                        "content_items.id is metadata.source_item_id in the pinned snapshot"
                    ),
                    "source_labels": "All source rows contain the exact empty JSON object '{}'."
                    if uniform_empty_labels
                    else "Preserved separately in each source_row.",
                    "source_text_hashes": "All collector-supplied content_sha256 values were NULL."
                    if no_source_text_hashes
                    else "Preserved separately in each source_row.",
                    "source_case": source_case,
                    "saved_source_record_count": len(records),
                    "source_media_metadata_count": len(media),
                    "import_record_limit": MAX_RECORDS,
                    "collection_run_counts": dict(Counter(str(run.get("status")) for run in runs)),
                    "collection_runs": runs,
                    "export_info": export_info,
                    "provenance": provenance or {},
                },
                "limitations": [
                    "The snapshot is a collected sample, not declared complete historical or "
                    "platform coverage.",
                    "Collector-supplied account IDs, timestamps, URLs, metrics and labels require "
                    "independent review.",
                    "Raw JSON remains in the separately retained SQLite snapshot; hashes of its "
                    "fields were calculated during import.",
                    "Media metadata and URLs do not establish that media bytes were captured "
                    "or independently verified.",
                    "The source snapshot is not a Swarm analysis, model classification or "
                    "human decision.",
                    "Original posts, authenticity, omissions, account identity and platform "
                    "actions are not independently verified by import.",
                ],
            },
            "records": records,
            "fragments": fragments,
            "runs": [
                {
                    "id": run_id,
                    "input_fingerprint": expected_sha256,
                    "input_manifest": [record["id"] for record in records],
                    "method": "compliance_review.collector_snapshot.import",
                    "method_version": "1",
                    "created_at": datetime.now(UTC).isoformat(),
                    "full_inputs_available": False,
                    "recomputation_capability": "dependency_review_only",
                    "configuration": {"source_case_slug": source_case_slug},
                    "coverage": {"status": "unknown", "saved_records": len(records)},
                }
            ],
            "findings": [
                {
                    "snapshot_id": finding_id,
                    "lineage_id": finding_id,
                    "version": 1,
                    "run_id": run_id,
                    "kind": "observation",
                    "statement": (
                        "One saved collector record is available to inspect; its source and "
                        "context need review."
                    ),
                    "details": {"domain": "collector_import", "review_needed": True},
                }
            ],
            "links": [
                {
                    "id": f"{finding_id}:link",
                    "source": reference,
                    "target": {"type": "finding", "id": finding_id},
                    "relation": "supports",
                    "fragment_ids": fragment_ids,
                    "basis": (
                        "The collector supplied this saved record in the pinned SQLite snapshot."
                    ),
                    "run_id": run_id,
                }
            ],
        }
    )
    size = len(
        json.dumps(
            bundle.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8")
    )
    if size > MAX_FILE_BYTES:
        raise ValueError(
            f"The source bundle is {size} bytes and exceeds the existing 20 MiB case package limit."
        )
    return bundle


def import_new_collector_case(service: WorkspaceService, bundle: EvidenceBundle) -> str:
    """Atomically import a newly named case; never merge into an existing case."""
    if service.settings.read_only:
        raise PermissionError("A read-only workspace cannot import a collector snapshot.")
    bundle = EvidenceBundle.model_validate(bundle.model_dump(mode="json"))
    from ..review.integrations.worker import _TransactionRepository

    with service.repository._connection(write=True) as connection:
        repository = _TransactionRepository(service.repository, connection)
        try:
            repository.get_bundle(bundle.case.id)
        except KeyError:
            pass
        else:
            raise ValueError("A case with this ID already exists; choose a new case ID.")
        return repository.import_bundle(bundle)


def main() -> None:
    """Import a pinned source into a new, explicitly named isolated workspace."""
    import argparse

    from ..config import Settings
    from ..review.integrations.workspace import WorkspaceRepository, WorkspaceService

    parser = argparse.ArgumentParser(
        description=(
            "Read a pinned SQLite collector snapshot and create a NEW local workspace. "
            "The source is copied, not moved; no analysis or review runs."
        )
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--source-case", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--source-url", default=None)
    parser.add_argument("--source-revision", default=None)
    args = parser.parse_args()
    destination = args.data_dir.expanduser().resolve()
    if destination.exists():
        parser.error("The selected data directory already exists; choose a new directory.")
    provenance = {
        k: v for k, v in {"url": args.source_url, "revision": args.source_revision}.items() if v
    }
    try:
        bundle = build_collector_snapshot_bundle(
            args.source,
            expected_sha256=args.sha256,
            source_case_slug=args.source_case,
            new_case_id=args.case_id,
            provenance=provenance,
        )
        destination.mkdir(parents=True, mode=0o700)
        source_dir = destination / "source"
        source_dir.mkdir(mode=0o700)
        retained_source = source_dir / args.source.name
        with args.source.open("rb") as source_stream, retained_source.open("xb") as target_stream:
            shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
        if _file_hash(retained_source) != args.sha256:
            raise ValueError("The retained snapshot does not match the expected source hash.")
        settings = Settings(data_dir=destination)
        repository = WorkspaceRepository(settings.db_path)
        repository.initialize()
        service = WorkspaceService(repository, settings, destination / "artifacts")
        case_id = import_new_collector_case(service, bundle)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Import did not complete: {exc}\n")
    print(
        json.dumps(
            {
                "case_id": case_id,
                "data_dir": str(destination),
                "retained_snapshot": str(retained_source),
                "source_sha256": args.sha256,
                "saved_records": len(bundle.records),
                "coverage": "unknown",
                "classification_or_human_review_created": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
