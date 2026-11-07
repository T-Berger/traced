"""Persist collected news, then queue local analysis without coupling HTTP to models."""

import hashlib
import json
from collections.abc import Callable

from .chunking import chunk_text
from .config import Settings
from .db import Repository
from .ingestion import CollectedArticle, CollectionError, NewsCollector
from .ingestion_contracts import ImportFailure, IngestionRequest, IngestionResult
from .models import ArticleInput
from .records import AnalysisRequest, JobRecord

Progress = Callable[[str, float], None]


def _save_article(repo: Repository, collected: CollectedArticle) -> tuple[int, str]:
    provenance = collected.provenance.model_dump(
        include={
            "requested_url",
            "final_url",
            "feed_url",
            "retrieved_at",
            "author",
            "publication_source",
            "content_source",
        }
    )
    article = ArticleInput.model_validate(
        {**collected.article.model_dump(), **provenance, "is_sample": False}
    )
    previous = repo.articles.get_by_url(article.url)
    digest = hashlib.sha256(article.text.encode()).hexdigest()
    state = (
        "new" if previous is None else "unchanged" if previous.content_hash == digest else "updated"
    )
    article_id = repo.articles.upsert_article(article)
    # Populate FTS immediately even for source-only imports without Ollama.
    current = repo.articles.get_article(article_id)
    if current is None:
        raise ValueError("The article was removed during ingestion. Retry the import.")
    if not repo.passages.list_chunks(article_id):
        repo.passages.replace_chunks(article_id, chunk_text(current.text))
    return article_id, state


def process_ingestion(
    repo: Repository,
    settings: Settings,
    job: JobRecord | dict,
    progress: Progress,
    *,
    collector_factory: Callable[[], NewsCollector] | None = None,
) -> IngestionResult:
    """Collect outside transactions; each successful article survives later failures."""
    if repo.read_only:
        raise PermissionError("A read-only snapshot cannot collect news.")
    payload = job.payload if isinstance(job, JobRecord) else job.get("payload", {})
    request = IngestionRequest.model_validate(payload)
    if request.feed_id is not None:
        saved_feed = repo.feeds.get(request.feed_id)
        if saved_feed is None or (request.scheduled and not saved_feed.enabled):
            reason = "Saved feed is unavailable." if saved_feed is None else "Saved feed is paused."
            progress(reason, 1.0)
            return IngestionResult(
                source_type=request.source_type, url=request.url, skipped=True, skip_reason=reason
            )
    factory = collector_factory or NewsCollector
    progress("Fetching news sources", 0.05)
    warnings, next_cursor = [], None
    if request.collection_source_id is not None:
        from .feeds.adapters.stored import stored_platform_feed

        feed = stored_platform_feed(
            repo.db_path, request.collection_source_id, topic=request.topic, limit=request.limit
        )
    elif request.platform != "rss":
        from .feeds.adapters import collect_platform_feed

        try:
            feed = collect_platform_feed(
                request.platform,
                request.locator or request.url,
                name=saved_feed.name if request.feed_id else request.topic,
                topic=request.topic,
                limit=request.limit,
                source_id=request.feed_id,
                interval_minutes=saved_feed.interval_minutes if request.feed_id else 60,
                cursor=request.cursor,
                progress=progress,
            )
        except CollectionError as exc:
            job_id = job.id if isinstance(job, JobRecord) else job.get("id")
            if job_id is not None:
                retry = getattr(exc, "retry_after_seconds", None)
                result = IngestionResult(
                    source_type="feed",
                    url=request.url,
                    errors=[ImportFailure(url=request.url, error=str(exc), code=exc.code)],
                    retry_after_seconds=min(retry, 86400)
                    if type(retry) is int and retry >= 0
                    else None,
                )
                repo.jobs.update_job(job_id, result=result.model_dump(mode="json"))
            raise
    elif request.source_type == "feed":
        with factory() as collector:
            feed = collector.collect_feed(
                request.url,
                limit=request.limit,
                topic=request.topic,
                content_mode=request.content_mode,
                progress=lambda stage, value: progress(stage, min(0.7, max(0.05, value * 0.7))),
            )
    else:
        with factory() as collector:
            collected = [collector.collect_article(request.url, topic=request.topic)]
            failures, entries_seen, duplicates = [], 1, 0
    if request.source_type == "feed":
        collected = feed.articles
        failures = [
            ImportFailure(url=error.url, error=error.message, code=error.code)
            for error in feed.errors
        ]
        entries_seen, duplicates = feed.entries_seen, feed.duplicates_skipped
        warnings, next_cursor = feed.warnings, feed.cursor

    groups: dict[str, list[int]] = {"new": [], "updated": [], "unchanged": []}
    article_ids, seen_urls = [], set()
    for index, article in enumerate(collected):
        progress("Saving collected articles", 0.75 + 0.2 * index / max(len(collected), 1))
        if article.article.url in seen_urls:
            duplicates += 1
            continue
        seen_urls.add(article.article.url)
        article_id, state = _save_article(repo, article)
        groups[state].append(article_id)
        article_ids.append(article_id)

    if request.feed_id and next_cursor is not None and not failures:
        repo.feeds.update_cursor(request.feed_id, next_cursor)

    analysis_job_id = None
    if request.analyze and article_ids:
        # Include unchanged sources too: they may have been collected without analysis.
        # Model revision/content caches decide whether any inference is actually needed.
        analysis = AnalysisRequest(mode="ollama", article_ids=article_ids)
        analysis_payload = analysis.model_dump(mode="json")
        identity = {
            "payload": analysis_payload,
            "embedding": settings.embedding_model,
            "claims": settings.chat_model,
        }
        key = (
            "analyze:ingestion:"
            + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        )
        analysis_job_id = repo.jobs.enqueue_job("analyze", analysis_payload, dedupe_key=key)

    result = IngestionResult(
        source_type=request.source_type,
        url=request.url,
        imported=len(groups["new"]),
        updated=len(groups["updated"]),
        unchanged=len(groups["unchanged"]),
        article_ids=article_ids,
        new_ids=groups["new"],
        updated_ids=groups["updated"],
        unchanged_ids=groups["unchanged"],
        errors=failures,
        analysis_job_id=analysis_job_id,
        duplicates_skipped=duplicates,
        entries_seen=entries_seen,
        warnings=warnings,
    )
    if failures and not article_ids:
        job_id = job.id if isinstance(job, JobRecord) else job.get("id")
        if job_id is not None:
            repo.jobs.update_job(job_id, result=result.model_dump(mode="json"))
        raise CollectionError(
            f"No articles were imported. {len(failures)} sources failed: {failures[0].error}",
            code="all_sources_failed",
            url=request.url,
        )
    progress(
        "Import complete; local analysis queued" if analysis_job_id else "Import complete", 1.0
    )
    return result
