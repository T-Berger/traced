"""Hybrid retrieval with bounded vector batches and selected-passage hydration."""

import heapq

import numpy as np

from .db import Repository
from .records import SearchMatch

CANDIDATE_LIMIT = 50
EMBEDDING_BATCH_SIZE = 256


def _unit_vectors(vectors: np.ndarray) -> np.ndarray:
    """Normalize finite rows without squaring their original magnitudes."""
    scales = np.max(np.abs(vectors), axis=-1, keepdims=True)
    scaled = np.divide(vectors, scales, out=np.zeros_like(vectors), where=scales > 0)
    norms = np.linalg.norm(scaled, axis=-1, keepdims=True)
    return np.divide(scaled, norms, out=np.zeros_like(scaled), where=norms > 0)


def _semantic_candidates(
    repo: Repository, query_vector: list[float], model_key: str
) -> list[tuple[int, float]]:
    vector = np.asarray(query_vector, dtype=np.float64)
    if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all():
        raise ValueError("A query embedding must be a finite, nonempty vector.")
    vector = _unit_vectors(vector)
    candidates: list[tuple[float, int]] = []
    for batch in repo.embeddings.iter_batches(model_key, batch_size=EMBEDDING_BATCH_SIZE):
        try:
            matrix = np.asarray([embedding.vector for embedding in batch], dtype=np.float64)
        except ValueError as exc:
            raise ValueError(
                "Embedding dimensions changed. Reanalyze using the current model."
            ) from exc
        if matrix.ndim != 2 or matrix.shape[1] != vector.size:
            raise ValueError("Embedding dimensions changed. Reanalyze using the current model.")
        # Max-absolute scaling preserves direction for tiny and huge finite values.
        # Clamp only rounding overshoot at the mathematical cosine bounds.
        similarities = np.clip(_unit_vectors(matrix) @ vector, -1.0, 1.0)
        for embedding, similarity in zip(batch, similarities, strict=True):
            if similarity <= 0:
                continue
            # Equal scores retain earlier chunk IDs, matching the stable dense ranking.
            candidate = (float(similarity), -embedding.chunk_id)
            if len(candidates) < CANDIDATE_LIMIT:
                heapq.heappush(candidates, candidate)
            elif candidate > candidates[0]:
                heapq.heapreplace(candidates, candidate)
    return [(-chunk_id, score) for score, chunk_id in sorted(candidates, reverse=True)]


def hybrid_search_records(
    repo: Repository, query: str, query_vector: list[float], model_key: str, limit: int = 10
) -> list[SearchMatch]:
    if limit <= 0:
        return []
    keywords = repo.passages.keyword_search(query, limit=CANDIDATE_LIMIT)
    semantic = _semantic_candidates(repo, query_vector, model_key)
    scores: dict[int, float] = {}
    semantic_scores = dict(semantic)
    keyword_scores = {row.id: row.score for row in keywords}
    for ranking in ([row.id for row in keywords], [chunk_id for chunk_id, _ in semantic]):
        for rank, chunk_id in enumerate(ranking, 1):
            scores[chunk_id] = scores.get(chunk_id, 0) + 1 / (60 + rank)

    # At most 100 passages are loaded; the corpus's text is never retained in search.
    chunks = repo.passages.by_ids(set(scores))
    matches: list[SearchMatch] = []
    seen_articles: set[int] = set()
    for chunk_id in sorted(scores, key=lambda key: (-scores[key], key)):
        chunk = chunks.get(chunk_id)
        if chunk is None or chunk.article_id in seen_articles:
            continue
        seen_articles.add(chunk.article_id)
        matches.append(
            SearchMatch(
                **chunk.model_dump(),
                score=scores[chunk_id],
                semantic_score=semantic_scores.get(chunk_id),
                keyword_score=keyword_scores.get(chunk_id),
                model_key=model_key,
            )
        )
        if len(matches) >= limit:
            break
    return matches


def hybrid_search(
    repo: Repository, query: str, query_vector: list[float], model_key: str, limit: int = 10
) -> list[dict]:
    """JSON adapter retained for existing command-line and worker callers."""
    return [
        match.model_dump(mode="json")
        for match in hybrid_search_records(repo, query, query_vector, model_key, limit)
    ]
