"""Fetch discovery leads into content-addressed, self-contained source records."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from itertools import islice

from ..ingestion.collector import NewsCollector
from ..ingestion.models import CollectionError
from ..ingestion.url_policy import normalize_url
from .contracts import EvidenceCollection, Issue, SavedEvidence, SearchHit


def collect_evidence(
    hits: Iterable[SearchHit],
    *,
    collector: NewsCollector | None = None,
    limit: int = 6,
) -> EvidenceCollection:
    """Collect at most ``limit`` URLs; failed attempts consume the same budget.

    Only fetched article text becomes evidence. Snippets and discovered titles
    remain labelled discovery metadata and never fill missing article content.
    Persistence belongs to the caller, which freezes this result in its job.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8:
        raise ValueError("Evidence collection limit must be between 1 and 8.")
    own_collector = collector is None
    collector = collector or NewsCollector(timeout=15.0, max_bytes=3_000_000)
    records: list[SavedEvidence] = []
    issues: list[Issue] = []
    attempted_urls: list[str] = []
    seen: set[str] = set()
    final_urls: set[str] = set()
    try:
        # The workflow supplies <=30 leads. Bound arbitrary/injected iterators too.
        for hit in islice(hits, 64):
            if len(attempted_urls) >= limit:
                break
            try:
                url = normalize_url(hit.url)
            except CollectionError as exc:
                attempted_urls.append(hit.url)
                issues.append(Issue(code=exc.code, message=str(exc), url=hit.url))
                continue
            if url in seen:
                continue
            seen.add(url)
            attempted_urls.append(url)
            try:
                collected = collector.collect_article(url, topic="External claim verification")
                article, provenance = collected.article, collected.provenance
                if provenance.final_url in final_urls:
                    continue
                final_urls.add(provenance.final_url)
                content_hash = hashlib.sha256(article.text.encode("utf-8")).hexdigest()
                identity = hashlib.sha256(
                    f"{provenance.final_url}\n{content_hash}".encode()
                ).hexdigest()
                records.append(
                    SavedEvidence(
                        id=f"external-{identity[:24]}",
                        url=provenance.final_url,
                        requested_url=provenance.requested_url,
                        title=article.title,
                        publisher=article.publisher,
                        text=article.text,
                        published_at=provenance.published_at,
                        retrieved_at=provenance.retrieved_at,
                        content_hash=content_hash,
                        metadata={
                            "provenance": provenance.model_dump(mode="json"),
                            "language": article.language,
                            "discovery_only": hit.model_dump(mode="json"),
                            "content_source": "fetched_article",
                            "independence": "unreviewed",
                        },
                    )
                )
            except CollectionError as exc:
                issues.append(Issue(code=exc.code, message=str(exc), url=exc.url or url))
    finally:
        if own_collector:
            collector.close()
    return EvidenceCollection(records=records, issues=issues, attempted_urls=attempted_urls)
