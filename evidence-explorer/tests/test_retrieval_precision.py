"""Cosine retrieval must depend on vector direction, not finite magnitude."""

import json
import math
import sys

import pytest

from evidence_explorer import retrieval
from evidence_explorer.models import ChunkInput
from evidence_explorer.retrieval import hybrid_search_records

MODEL_KEY = "precision-fixture"


def _index_vectors(repo, article, vectors):
    article_ids = []
    for index, vector in enumerate(vectors):
        article_id = repo.upsert_article(
            article.model_copy(update={"url": f"https://example.invalid/precision/{index}"})
        )
        chunk = repo.passages.replace_chunks(
            article_id,
            [ChunkInput(ordinal=0, text=article.text, start_char=0, end_char=len(article.text))],
        )[0]
        repo.embeddings.save_embedding(chunk.id, MODEL_KEY, vector)
        article_ids.append(article_id)
    return article_ids


@pytest.mark.parametrize(
    ("stored_scale", "query_scale"),
    [
        (1e20, 1.0),
        (1e-30, 1.0),
        (1e40, 1.0),
        (1e20, 1e20),
        (1e-300, 1e300),
        (sys.float_info.max, sys.float_info.max),
        (5e-324, 5e-324),
        (sys.float_info.max, -1e-300),
        (1e-300, -sys.float_info.max),
    ],
)
def test_finite_scales_preserve_positive_negative_orthogonal_and_zero_directions(
    repo, article, stored_scale, query_scale
):
    article_ids = _index_vectors(
        repo,
        article,
        [[stored_scale, 0.0], [-stored_scale, 0.0], [0.0, stored_scale], [0.0, 0.0]],
    )

    matches = hybrid_search_records(repo, "unmatchedneedle", [query_scale, 0.0], MODEL_KEY)

    assert [match.article_id for match in matches] == [article_ids[query_scale < 0]]
    assert matches[0].semantic_score == pytest.approx(1.0)
    assert 0.0 < matches[0].semantic_score <= 1.0
    json.dumps([match.model_dump(mode="json") for match in matches], allow_nan=False)


def test_multicomponent_norm_and_dot_product_remain_finite(repo, article):
    article_ids = _index_vectors(
        repo,
        article,
        [[sys.float_info.max, sys.float_info.max], [1e-300, 1e-300], [1.0, 0.0]],
    )

    matches = hybrid_search_records(
        repo, "unmatchedneedle", [sys.float_info.max, sys.float_info.max], MODEL_KEY
    )

    assert [match.article_id for match in matches] == article_ids
    assert [match.semantic_score for match in matches] == pytest.approx(
        [1.0, 1.0, 1 / math.sqrt(2)]
    )
    assert all(
        math.isfinite(match.semantic_score) and match.semantic_score <= 1 for match in matches
    )


def test_normal_scale_ordering_and_ties_cross_batch_and_candidate_boundaries(
    repo, article, monkeypatch
):
    article_ids = _index_vectors(
        repo, article, [[0.5, 1.0], [1.0, 1.0], [1.0, 0.0], [2.0, 0.0], [3.0, 0.0]]
    )
    monkeypatch.setattr(retrieval, "EMBEDDING_BATCH_SIZE", 2)
    monkeypatch.setattr(retrieval, "CANDIDATE_LIMIT", 3)

    matches = hybrid_search_records(repo, "unmatchedneedle", [1.0, 0.0], MODEL_KEY)

    assert [match.article_id for match in matches] == article_ids[2:]
    assert [match.semantic_score for match in matches] == [1.0, 1.0, 1.0]
    monkeypatch.setattr(retrieval, "CANDIDATE_LIMIT", 4)
    matches = hybrid_search_records(repo, "unmatchedneedle", [1.0, 0.0], MODEL_KEY)
    assert [match.article_id for match in matches] == article_ids[2:] + [article_ids[1]]


def test_zero_query_keeps_keyword_results_without_semantic_scores(repo, article):
    article_ids = _index_vectors(repo, article, [[1e300, 0.0], [0.0, 0.0]])

    assert hybrid_search_records(repo, "unmatchedneedle", [0.0, 0.0], MODEL_KEY) == []
    matches = hybrid_search_records(repo, "fictional", [0.0, 0.0], MODEL_KEY)

    assert {match.article_id for match in matches} == set(article_ids)
    assert all(
        match.semantic_score is None and match.keyword_score is not None for match in matches
    )


@pytest.mark.parametrize(
    "query_vector",
    [[], [[1.0, 0.0]], [float("nan"), 0.0], [float("inf"), 0.0], [-float("inf"), 0.0]],
)
def test_malformed_queries_fail_explicitly(repo, query_vector):
    with pytest.raises(ValueError, match="finite, nonempty vector"):
        hybrid_search_records(repo, "unmatchedneedle", query_vector, MODEL_KEY)


@pytest.mark.parametrize("query_vector", [[1.0, 0.0, 0.0], [0.0, 0.0, 0.0]])
def test_dimension_mismatch_still_fails_for_nonzero_and_zero_queries(repo, article, query_vector):
    _index_vectors(repo, article, [[1.0, 0.0]])
    with pytest.raises(ValueError, match="dimensions changed"):
        hybrid_search_records(repo, "unmatchedneedle", query_vector, MODEL_KEY)
