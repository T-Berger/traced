"""Project saved source contracts into the common immutable evidence format.

Parsing validates bounded uploads; projection preserves existing hashes, exact
text and explicit relationships. Neither path fetches sources, persists a case,
or records a human decision. The integration namespace re-exports this API for
existing callers.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from .review.contracts import EvidenceBundle
from .review.importing import ImportValidationError, decode_content
from .source_contracts import (
    PLATFORM_LABELS,
    SourceCase,
    SourceImport,
    SourceItem,
    _channel_key,
    _digest,
    _identity,
    _saved_item,
    _text_hash,
)

_LIMITATIONS = [
    "This collection contains supplied saved records; source completeness is unknown.",
    "Unknown publication times remain unknown; acquisition time is not publication time.",
    "Byte-identical saved text does not establish an independent source or an original author.",
    "Shared URLs establish a repeated supplied link, not matching content or a common actor.",
    "Repost relationships reflect explicit supplied references, not independently verified origin.",
    "Author identifiers remain scoped to their platform and channel; identities are not merged.",
]


def _validation_message(exc: ValidationError) -> str:
    details = []
    fields = (
        set(SourceImport.model_fields) | set(SourceCase.model_fields) | set(SourceItem.model_fields)
    )
    for issue in exc.errors(include_input=False, include_context=False, include_url=False)[:4]:
        parts = [str(part) for part in issue["loc"]]
        path = ".".join(parts)
        label = path[:120] if all(part in fields or part.isdigit() for part in parts) else "source"
        detail = (
            "required field is missing"
            if issue["type"] == "missing"
            else "field type, value or source constraints are invalid"
        )
        details.append(f"{label}: {detail}")
    return "Invalid source collection. " + "; ".join(details) + "."


def _validate_payload(payload: dict | SourceImport) -> SourceImport:
    try:
        return SourceImport.model_validate(payload)
    except ValidationError as exc:
        raise ImportValidationError(_validation_message(exc)) from None
    except (RecursionError, OverflowError, UnicodeError):
        raise ImportValidationError(
            "Source collection contains invalid or deeply nested text."
        ) from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ImportValidationError("Source JSON contains duplicate object keys.")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ImportValidationError("Source JSON must use finite numbers.")


def parse_source_import(content: str | bytes, filename: str | None = None) -> EvidenceBundle:
    """Parse a UTF-8 JSON envelope or base64 upload, capped at the 5 MiB boundary."""
    raw = decode_content(content, filename)
    try:
        payload = json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except UnicodeDecodeError:
        raise ImportValidationError("Source collection must contain valid UTF-8 text.") from None
    except json.JSONDecodeError as exc:
        raise ImportValidationError(
            f"Invalid source JSON at line {exc.lineno}, column {exc.colno}."
        ) from None
    except ImportValidationError:
        raise
    except ValueError:
        raise ImportValidationError("Source JSON contains an unsupported numeric value.") from None
    except (RecursionError, OverflowError):
        raise ImportValidationError("Source JSON is nested too deeply.") from None
    if not isinstance(payload, dict):
        raise ImportValidationError("A source collection must be one JSON object.")
    return build_source_bundle(payload)


def build_source_bundle(payload: dict | SourceImport) -> EvidenceBundle:
    """Build an immutable source snapshot and descriptive evidence relationships.

    Input ordering does not change identity. Edits to supplied content or metadata
    produce a new case and analysis snapshot. Unresolved explicit repost targets
    remain metadata; they never become invented records or resolved repost links.
    """
    supplied = _validate_payload(payload)
    items = sorted(supplied.items, key=_identity)
    identities = [_identity(item) for item in items]
    if len(identities) != len(set(identities)):
        raise ImportValidationError("Source identities must be unique within a collection.")
    channel_names: dict[tuple[str, str], str] = {}
    for item in items:
        key = (item.platform, item.channel_id)
        if key in channel_names and channel_names[key] != item.channel_name:
            raise ImportValidationError(
                "Each source channel must have one consistent display name."
            )
        channel_names[key] = item.channel_name
        if item.repost_of and _identity(item.repost_of) == _identity(item):
            raise ImportValidationError("A source record cannot reference itself as a repost.")
    snapshot = {
        "source_schema": supplied.source_schema,
        "case": supplied.case.model_dump(mode="json"),
        "items": [_saved_item(item) for item in items],
    }
    try:
        case_id = "sources:case:" + _digest(snapshot)
    except UnicodeError:
        raise ImportValidationError("Source collection must contain valid UTF-8 text.") from None
    records, fragments = [], []
    identity_records = {}
    for item in items:
        identity = _identity(item)
        saved = _saved_item(item)
        record_id = "sources:record:" + _digest([identity, saved])
        identity_records[identity] = record_id
        author_key = (
            "source:author:" + _digest([item.platform, item.channel_id, item.author_id])
            if item.author_id is not None
            else None
        )
        records.append(
            {
                "id": record_id,
                "kind": "saved_source_item",
                "source_label": f"{PLATFORM_LABELS[item.platform]} · {item.channel_name}",
                "text": item.text,
                "content_hash": _text_hash(item.text),
                "source_url": item.source_url,
                "published_at": item.published_at,
                "acquired_at": item.acquired_at,
                "metadata": {
                    "domain": "sources",
                    "source_schema": supplied.source_schema,
                    "platform": item.platform,
                    "channel_id": item.channel_id,
                    "channel_name": item.channel_name,
                    "channel_key": _channel_key(item.platform, item.channel_id),
                    "native_id": item.native_id,
                    "author_id": item.author_id,
                    "author_key": author_key,
                    "title": item.title,
                    "shared_urls": item.shared_urls,
                    "repost_of": saved["repost_of"],
                    "synthetic": supplied.case.synthetic,
                    **({"provenance": saved["provenance"]} if item.provenance else {}),
                },
            }
        )
        fragments.append(
            {
                "id": record_id + ":text",
                "record_id": record_id,
                "selector": {"kind": "text", "start": 0, "end": len(item.text)},
                "quote": item.text,
            }
        )
    fingerprint = _digest(records)
    run_id = "sources:run:" + _digest([case_id, fingerprint, "1"])
    findings, links = [], []

    def link(source: str, target: str, relation: str, basis: str, finding=False) -> None:
        links.append(
            {
                "id": "sources:link:" + _digest([run_id, source, target, relation]),
                "source": {"type": "record", "id": source},
                "target": {"type": "finding" if finding else "record", "id": target},
                "relation": relation,
                "basis": basis,
                "run_id": run_id,
                "fragment_ids": [source + ":text"] if finding else [],
            }
        )

    coverage = {
        "records": len(records),
        "channels": len(channel_names),
        "platforms": len({item.platform for item in items}),
        "platform_counts": dict(sorted(Counter(item.platform for item in items).items())),
        "unknown_publication_times": sum(item.published_at is None for item in items),
        "full_source_coverage": False,
    }
    observation_id = "sources:finding:" + _digest([run_id, "collection"])
    findings.append(
        {
            "snapshot_id": observation_id,
            "lineage_id": "sources:collection:" + _digest(supplied.case.model_dump(mode="json")),
            "version": 1,
            "run_id": run_id,
            "kind": "observation",
            "statement": (
                f"The supplied collection contains {len(records)} saved records from "
                f"{coverage['channels']} channels across {coverage['platforms']} platforms."
            ),
            "details": {"domain": "sources", **coverage},
        }
    )
    for record in records:
        link(
            record["id"],
            observation_id,
            "supports",
            "Counted saved record in this collection.",
            True,
        )
    by_hash: dict[str, list[str]] = defaultdict(list)
    for record in records:
        by_hash[record["content_hash"]].append(record["id"])
    for content_hash, record_ids in sorted(by_hash.items()):
        if len(record_ids) < 2:
            continue
        finding_id = "sources:finding:" + _digest([run_id, "exact_text", content_hash])
        findings.append(
            {
                "snapshot_id": finding_id,
                "lineage_id": "sources:exact_text:" + content_hash,
                "version": 1,
                "run_id": run_id,
                "kind": "observation",
                "statement": (
                    f"{len(record_ids)} supplied saved records contain byte-identical text."
                ),
                "details": {
                    "domain": "sources",
                    "content_hash": content_hash,
                    "record_ids": record_ids,
                    "origin_established": False,
                },
            }
        )
        for record_id in record_ids:
            link(
                record_id, finding_id, "supports", "Exact SHA-256 match of saved UTF-8 text.", True
            )
        # A spanning star retains every member without creating quadratic links.
        for record_id in record_ids[1:]:
            link(
                record_ids[0],
                record_id,
                "same_content",
                "Byte-identical saved UTF-8 text; origin and author identity are unknown.",
            )
    unresolved_reposts = 0
    for item in items:
        if item.repost_of is None:
            continue
        target = identity_records.get(_identity(item.repost_of))
        if target is None:
            unresolved_reposts += 1
            continue
        link(
            target,
            identity_records[_identity(item)],
            "documented_repost",
            "Explicit supplied repost reference resolves to this saved source identity; "
            "origin is not independently verified.",
        )
    coverage["unresolved_repost_references"] = unresolved_reposts
    limitations = list(_LIMITATIONS)
    if unresolved_reposts:
        limitations.append(
            f"{unresolved_reposts} supplied repost references point outside the saved collection."
        )
    return EvidenceBundle.model_validate(
        {
            "source_import": {"source_schema": supplied.source_schema},
            "case": {
                "id": case_id,
                **supplied.case.model_dump(mode="json"),
                "coverage": coverage,
                "limitations": limitations,
            },
            "records": records,
            "fragments": fragments,
            "runs": [
                {
                    "id": run_id,
                    "input_fingerprint": fingerprint,
                    "input_manifest": [record["id"] for record in records],
                    "method": "saved_source_collection",
                    "method_version": "1",
                    "created_at": max(
                        (item.acquired_at for item in items), key=datetime.fromisoformat
                    ),
                    "full_inputs_available": True,
                    "recomputation_capability": "dependency_review_only",
                    "configuration": {"network_access": False, "identity_merging": False},
                    "coverage": coverage,
                }
            ],
            "findings": findings,
            "links": links,
        }
    )
