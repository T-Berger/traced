"""Adapt saved Evidence Explorer results to the shared review contract.

This reads the existing repository; it never reruns inference, rewrites sources,
or treats extracted statements as verified facts. The adapter's run describes a
stored-result snapshot. Original model execution times are not available in the
starter's claim table and remain explicitly unknown.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from ...analysis import validate_claims
from ...models import ClaimInput
from ..contracts import EvidenceBundle

if TYPE_CHECKING:
    from ...db import Repository


def _digest(value: Any) -> str:
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def bundle_from_repository(
    repo: Repository, *, case_id: str = "explorer:case", model_key: str | None = None
) -> EvidenceBundle:
    """Return a versioned, source-linked bundle from saved articles and claims.

    ``model_key`` explicitly selects one retained historical extractor. Without
    it, only each article's active successful result is exported (legacy imports
    without an active pointer retain their previous compatibility behavior).
    No claims or analysis results are created as a side effect. Empty repositories
    produce an empty case.

    IDs depend on retained content and metadata, rather than mutable SQL claim
    row IDs. Repeated exports of an unchanged repository are byte-equivalent.
    Source edits or changed extraction results produce new immutable snapshots.
    """
    if not case_id.strip():
        raise ValueError("Case ID must be a nonempty namespace.")
    articles = sorted(repo.list_articles(), key=lambda row: (row["url"], row["id"]))
    chunks = repo.list_chunks()
    if model_key is not None:
        claims = [claim for claim in repo.list_claims() if claim["model_key"] == model_key]
    else:
        claims = repo.list_active_claims()

    article_by_id = {article["id"]: article for article in articles}
    chunks_by_id = {chunk["id"]: chunk for chunk in chunks}
    chunks_by_article: dict[int, list[dict]] = defaultdict(list)
    for chunk in chunks:
        article = article_by_id.get(chunk["article_id"])
        if article is None:
            raise ValueError(f"Saved chunk {chunk['id']} has no article.")
        start, end = chunk["start_char"], chunk["end_char"]
        if not isinstance(start, int) or not isinstance(end, int):
            raise ValueError(f"Saved chunk {chunk['id']} has invalid text offsets.")
        if not (0 <= start < end <= len(article["text"])):
            raise ValueError(f"Saved chunk {chunk['id']} has invalid text offsets.")
        if article["text"][start:end] != chunk["text"]:
            raise ValueError(f"Saved chunk {chunk['id']} does not match its original text.")
        chunks_by_article[chunk["article_id"]].append(chunk)

    records, fragments, links = [], [], []
    record_ids: dict[int, str] = {}
    fragment_ids: dict[int, str] = {}
    records_by_hash: dict[str, list[str]] = defaultdict(list)
    snapshot_times: list[str] = []
    for article in articles:
        digest = hashlib.sha256(article["text"].encode("utf-8")).hexdigest()
        if article.get("content_hash") not in (None, digest):
            raise ValueError(f"Saved article {article['id']} has a content hash mismatch.")
        saved_at = _optional_text(article.get("updated_at")) or _optional_text(
            article.get("created_at")
        )
        if saved_at:
            snapshot_times.append(saved_at)
        retrieved_at = _optional_text(article.get("retrieved_at"))
        record = {
            "kind": "article",
            "source_label": f"{article['publisher']} · {article['title']}",
            "text": article["text"],
            "content_hash": digest,
            "source_url": _optional_text(article.get("url")),
            "published_at": _optional_text(article.get("published_at")),
            "acquired_at": retrieved_at,
            "metadata": {
                "synthetic": bool(article.get("is_sample")),
                "title": article["title"],
                "publisher": article["publisher"],
                "language": article.get("language"),
                "topic": article.get("topic"),
                "author": _optional_text(article.get("author")),
                "requested_url": _optional_text(article.get("requested_url")),
                "final_url": _optional_text(article.get("final_url")),
                "feed_url": _optional_text(article.get("feed_url")),
                "retrieved_at": retrieved_at,
                "publication_source": _optional_text(article.get("publication_source")),
                "source_first_saved_at": article.get("created_at"),
                "source_last_saved_at": article.get("updated_at"),
                "acquisition_time_basis": (
                    "Saved source retrieval timestamp supplied by the article collector."
                    if retrieved_at
                    else "Original retrieval time is unavailable for this saved record."
                ),
            },
        }
        record_id = f"{case_id}:article:{_digest(record)[:24]}"
        record["id"] = record_id
        record_ids[article["id"]] = record_id
        records.append(record)
        records_by_hash[digest].append(record_id)

    for chunk in chunks:
        record_id = record_ids[chunk["article_id"]]
        selector = {"kind": "text", "start": chunk["start_char"], "end": chunk["end_char"]}
        fid = f"{case_id}:chunk:{_digest([record_id, selector, chunk['text']])[:24]}"
        fragment_ids[chunk["id"]] = fid
        fragments.append(
            {"id": fid, "record_id": record_id, "selector": selector, "quote": chunk["text"]}
        )

    normalized_claims = []
    seen_claims = set()
    for claim in claims:
        article = article_by_id.get(claim["article_id"])
        chunk = chunks_by_id.get(claim["chunk_id"])
        if article is None or chunk is None or chunk["article_id"] != claim["article_id"]:
            raise ValueError("A saved claim does not refer to a chunk in its own article.")
        validated = ClaimInput.model_validate(claim)
        validate_claims([validated], chunks_by_article[claim["article_id"]])
        # Choose the first exact occurrence within the already cited chunk. The
        # quote remains checkable even when the source repeats that same text.
        offset = chunk["text"].index(validated.quote)
        start = chunk["start_char"] + offset
        payload = {
            "record_id": record_ids[claim["article_id"]],
            "chunk_fragment_id": fragment_ids[claim["chunk_id"]],
            "statement": validated.claim_text,
            "quote": validated.quote,
            "start": start,
            "end": start + len(validated.quote),
            "subject": validated.subject,
            "attribution": validated.attribution,
            "event_time": validated.event_time,
            "location": validated.location,
            "model_key": claim["model_key"],
            "synthetic": bool(article.get("is_sample")),
            "url": article["url"],
        }
        identity = _digest(payload)
        if identity not in seen_claims:
            normalized_claims.append(payload)
            seen_claims.add(identity)
    normalized_claims.sort(key=_digest)

    if articles and not snapshot_times:
        raise ValueError("Saved articles have no retained timestamp for this repository snapshot.")
    latest_snapshot_time = max(snapshot_times) if snapshot_times else None
    keys = sorted(
        {claim["model_key"] for claim in normalized_claims}
        | (repo.claims.active_model_keys() if model_key is None else set())
    )
    if articles and not keys:
        keys = [model_key or "saved_articles_without_claims"]
    input_fingerprint = _digest(records)
    runs, findings = [], []
    for key in keys:
        saved_claims = [claim for claim in normalized_claims if claim["model_key"] == key]
        run_id = f"{case_id}:run:{_digest([input_fingerprint, key, saved_claims])[:24]}"
        runs.append(
            {
                "id": run_id,
                "input_fingerprint": input_fingerprint,
                "input_manifest": [record["id"] for record in records],
                "method": "evidence_explorer_repository_snapshot",
                "method_version": "1",
                "created_at": latest_snapshot_time,
                "time_basis": (
                    "Latest stored article update; original model execution time unavailable."
                ),
                "original_model_created_at": None,
                "full_inputs_available": True,
                "recomputation_capability": "dependency_review_only",
                "configuration": {"model_key": key, "inference_performed_by_adapter": False},
                "coverage": {
                    "articles": len(records),
                    "saved_chunks": len(chunks),
                    "saved_claims": len(saved_claims),
                },
            }
        )
        for claim in saved_claims:
            quote_id = f"{case_id}:quote:{_digest(claim)[:24]}"
            fragments.append(
                {
                    "id": quote_id,
                    "record_id": claim["record_id"],
                    "selector": {"kind": "text", "start": claim["start"], "end": claim["end"]},
                    "quote": claim["quote"],
                }
            )
            snapshot_id = f"{case_id}:finding:{_digest([run_id, claim])[:24]}"
            lineage = _digest([claim["url"], key, claim["statement"], claim["attribution"]])[:24]
            findings.append(
                {
                    "snapshot_id": snapshot_id,
                    "lineage_id": f"{case_id}:claim:{lineage}",
                    "version": 1,
                    "run_id": run_id,
                    "kind": "inference",
                    "statement": claim["statement"],
                    "details": {
                        "producer": "evidence_explorer",
                        "synthetic": claim["synthetic"],
                        "model_key": key,
                        "extraction_origin": (
                            "curated_demo" if key.startswith("demo:") else "saved_model"
                        ),
                        "subject": claim["subject"],
                        "attribution": claim["attribution"] or None,
                        "claimed_event_time": claim["event_time"],
                        "claimed_location": claim["location"],
                        "review_state": "unreviewed",
                        "version_basis": (
                            "First imported snapshot; the repository retains no extraction "
                            "version sequence."
                        ),
                        "interpretation": (
                            "Extracted source statement; quotation presence does not verify truth."
                        ),
                        "source_chunk_fragment_id": claim["chunk_fragment_id"],
                    },
                }
            )
            links.append(
                {
                    "id": f"{case_id}:citation:{_digest([quote_id, snapshot_id])[:24]}",
                    "source": {"type": "fragment", "id": quote_id},
                    "target": {"type": "finding", "id": snapshot_id},
                    "relation": "supports",
                    "fragment_ids": [quote_id],
                    "basis": (
                        "Saved extractor cites this exact passage. This records citation "
                        "provenance; "
                        "whether the passage supports the wording remains for review."
                    ),
                    "run_id": run_id,
                }
            )

    for group in records_by_hash.values():
        first, *others = sorted(group)
        for other in others:
            links.append(
                {
                    "id": f"{case_id}:duplicate:{_digest([first, other])[:24]}",
                    "source": {"type": "record", "id": first},
                    "target": {"type": "record", "id": other},
                    "relation": "same_content",
                    "basis": (
                        "Identical SHA-256 of saved original text. Original publication origin "
                        "and source independence are not established by identical wording."
                    ),
                }
            )

    sample_count = sum(bool(article.get("is_sample")) for article in articles)
    mixed = 0 < sample_count < len(articles)
    provenance = (
        "mixed" if mixed else "synthetic" if sample_count else "real" if articles else "empty"
    )
    limitations = [
        "Saved extraction results are review suggestions, not verified facts.",
        "Original model execution timestamps are unavailable.",
        "The adapter does not rerun models or establish independent corroboration.",
    ]
    missing_retrieval_count = sum(record["acquired_at"] is None for record in records)
    if missing_retrieval_count:
        limitations.append(
            f"Original retrieval timestamps are unavailable for {missing_retrieval_count} "
            "saved record(s); repository save times are not acquisition times."
        )
    if mixed:
        limitations.append(
            "This case mixes synthetic demonstration articles with non-sample records."
        )
    return EvidenceBundle.model_validate(
        {
            "case": {
                "id": case_id,
                "title": "Evidence Explorer saved investigation",
                "question": (
                    "Which saved article findings are supported well enough for a briefing?"
                ),
                "synthetic": bool(articles) and sample_count == len(articles),
                "coverage": {
                    "articles": len(articles),
                    "chunks": len(chunks),
                    "findings": len(findings),
                    "sample_articles": sample_count,
                    "content_provenance": provenance,
                    "model_filter": model_key,
                },
                "limitations": limitations,
            },
            "records": records,
            "fragments": fragments,
            "runs": runs,
            "findings": findings,
            "links": links,
        }
    )
