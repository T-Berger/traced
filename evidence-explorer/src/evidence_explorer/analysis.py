"""Execute Explorer jobs for both standalone and integrated worker entry points.

Ingestion delegates to its existing processor. Analysis and search load one saved
article at a time, reuse cached chunks/embeddings, and optionally call Ollama.
This module persists derived article data; workers own job claiming, completion,
and failure status. It does not create shared-review decisions.
"""

from collections.abc import Callable, Iterator
from contextlib import nullcontext

from .chunking import chunk_text
from .claim_selection import CLAIMS_POLICY_VERSION
from .config import Settings
from .db import Repository
from .demo import DEMO_CLAIMS_KEY, DEMO_EMBEDDING_KEY, demo_claims, demo_embed
from .grounding import validate_claims as validate_claims
from .grounding import validate_extractive_claims
from .models_client import OllamaClient
from .records import (
    AnalysisRequest,
    AnalysisResult,
    ArticleRecord,
    ChunkRecord,
    JobRecord,
    SearchRequest,
    SearchResult,
)
from .retrieval import hybrid_search_records


def _chunks(repo: Repository, article: ArticleRecord) -> list[ChunkRecord]:
    chunks = repo.passages.list_chunks(article.id)
    return chunks or repo.passages.replace_chunks(article.id, chunk_text(article.text))


def _articles(repo: Repository, request: AnalysisRequest) -> tuple[Iterator[int], int]:
    if request.article_ids is None:
        scope = repo.articles.scope()
        return repo.articles.iter_ids(upper_id=scope.upper_id), scope.count
    ids = sorted(set(request.article_ids))
    unknown = [article_id for article_id in ids if repo.articles.get_article(article_id) is None]
    if unknown:
        raise ValueError(f"Articles do not exist: {unknown}")
    return iter(ids), len(ids)


def _embed_missing(
    repo: Repository,
    chunks: list[ChunkRecord],
    model_key: str,
    embed: Callable[[list[str]], list[list[float]]],
) -> int:
    missing = [
        chunk for chunk in chunks if repo.embeddings.get_embedding(chunk.id, model_key) is None
    ]
    for start in range(0, len(missing), 8):
        batch = missing[start : start + 8]
        vectors = embed([chunk.text for chunk in batch])
        for chunk, vector in zip(batch, vectors, strict=True):
            repo.embeddings.save_embedding(chunk.id, model_key, vector)
    return len(missing)


def process_job(
    repo: Repository,
    settings: Settings,
    job: JobRecord | dict,
    progress: Callable[[str, float], None],
) -> dict:
    """Validate a queued Explorer request and return its serialized typed result.

    Model and collection I/O run outside repository transactions. Passage,
    embedding, and claim writes use separate repository operations, so this is
    not an all-or-nothing transaction for the entire job. Persisted caches may
    remain after a later failure; the caller records that job's final status.
    """
    kind = job.kind if isinstance(job, JobRecord) else job["kind"]
    if kind == "ingest":
        from .ingestion_jobs import process_ingestion

        return process_ingestion(repo, settings, job, progress).model_dump(mode="json")
    payload = job.payload if isinstance(job, JobRecord) else job.get("payload") or {}
    if kind not in {"analyze", "search"}:
        raise ValueError(f"Unknown job kind: {kind}")
    request = (SearchRequest if kind == "search" else AnalysisRequest).model_validate(payload)
    article_ids, total = _articles(repo, request)
    context = OllamaClient(settings) if request.mode == "ollama" else nullcontext(None)
    with context as model:
        embedding_key = model.model_key("embedding") if model else DEMO_EMBEDDING_KEY
        embed = model.embed if model else demo_embed
        claims_key = (
            (model.model_key("claims") if model else DEMO_CLAIMS_KEY) if kind == "analyze" else ""
        )
        embeddings_created = claims_created = claims_rejected = cached = 0
        for index, article_id in enumerate(article_ids):
            article = repo.articles.get_article(article_id)
            if article is None:
                raise ValueError(
                    f"Article {article_id} was removed during processing. Retry the job."
                )
            progress(f"Article {index + 1}/{total}: embeddings", index / max(total, 1))
            chunks = _chunks(repo, article)
            created = _embed_missing(repo, chunks, embedding_key, embed)
            embeddings_created += created
            if isinstance(request, SearchRequest):
                continue
            cache_key = f"claims:{article.id}:{article.content_hash}:{claims_key}"
            if repo.get_meta(cache_key) == "done":
                repo.claims.activate_cached_result(
                    article.id,
                    claims_key,
                    expected_content_hash=article.content_hash,
                    cache_key=cache_key,
                )
                diagnostics = repo.get_meta(f"{cache_key}:quality", {})
                rejected = (
                    diagnostics.get("rejected_claims", 0) if isinstance(diagnostics, dict) else 0
                )
                if type(rejected) is int and rejected >= 0:
                    claims_rejected += rejected
                cached += int(created == 0)
                continue
            progress(f"Article {index + 1}/{total}: claims", (index + 0.5) / max(total, 1))
            extracted = (
                model.extract_claims(article.model_dump(), [chunk.model_dump() for chunk in chunks])
                if model
                else demo_claims(article, chunks)
            )
            article_rejected = 0
            if model:
                metadata = getattr(model, "last_claim_extraction_metadata", {})
                article_rejected = metadata.get("rejected_claims", 0)
                claims_rejected += article_rejected
            claims_created += repo.claims.replace_claims(
                article.id,
                (
                    validate_extractive_claims(extracted, chunks)
                    if claims_key.endswith(":" + CLAIMS_POLICY_VERSION)
                    else validate_claims(extracted, chunks)
                ),
                claims_key,
                expected_content_hash=article.content_hash,
                cache_key=cache_key,
                cache_diagnostics={"rejected_claims": article_rejected} if model else None,
            )
        if isinstance(request, SearchRequest):
            progress("Retrieving evidence", 0.95)
            return SearchResult(
                matches=hybrid_search_records(
                    repo, request.query, embed([request.query])[0], embedding_key, request.limit
                ),
                mode=request.mode,
                query=request.query,
                model_key=embedding_key,
            ).model_dump(mode="json")
        return AnalysisResult(
            articles=total,
            embeddings_created=embeddings_created,
            claims_created=claims_created,
            claims_rejected=claims_rejected,
            cached=cached,
            mode=request.mode,
            model_key=claims_key,
        ).model_dump(mode="json")
