"""Storage invariants that protect provenance and resumable analysis."""

import pytest

from evidence_explorer.db import Repository
from evidence_explorer.models import ChunkInput, ClaimInput


def add_chunk(repo, article):
    article_id = repo.upsert_article(article)
    chunks = repo.replace_chunks(
        article_id,
        [ChunkInput(ordinal=0, text=article.text, start_char=0, end_char=len(article.text))],
    )
    return article_id, chunks[0]


def test_repeated_ingestion_retains_article_and_analysis(repo, article):
    article_id, chunk = add_chunk(repo, article)
    repo.save_embedding(chunk["id"], "test-model:v1", [1.0, 0.0, 0.0])
    repo.replace_claims(
        article_id,
        [
            ClaimInput(
                claim_text="The city announced a bridge inspection.",
                quote="The fictional city announced a bridge inspection on Monday.",
                chunk_id=chunk["id"],
            )
        ],
        "test-claims:v1",
    )

    assert repo.upsert_article(article) == article_id
    assert len(repo.list_articles()) == 1
    assert len(repo.list_chunks(article_id)) == 1
    assert len(repo.list_claims(article_id)) == 1
    assert repo.get_embedding(chunk["id"], "test-model:v1") == [1.0, 0.0, 0.0]


def test_content_change_invalidates_derived_analysis(repo, article):
    article_id, chunk = add_chunk(repo, article)
    repo.save_embedding(chunk["id"], "test-model:v1", [1.0, 0.0])
    repo.replace_claims(
        article_id,
        [
            ClaimInput(
                claim_text="The city announced a bridge inspection.",
                quote="The fictional city announced a bridge inspection on Monday.",
                chunk_id=chunk["id"],
            )
        ],
        "test-claims:v1",
    )

    updated = article.model_copy(update={"text": article.text + " A later update postponed it."})
    assert repo.upsert_article(updated) == article_id
    assert repo.list_chunks(article_id) == []
    assert repo.list_claims(article_id) == []
    assert repo.list_embeddings("test-model:v1") == []


def test_search_returns_evidence_and_treats_punctuation_as_data(repo, article):
    article_id, chunk = add_chunk(repo, article)
    matches = repo.keyword_search("bridge inspection")
    assert matches
    assert matches[0]["article_id"] == article_id
    assert matches[0]["id"] == chunk["id"]
    assert matches[0]["url"] == article.url
    for query in ['"', "*", "NEAR(", "bridge OR )", "'; DROP TABLE documents; --"]:
        assert isinstance(repo.keyword_search(query), list)
    assert len(repo.list_articles()) == 1


def test_jobs_persist_and_recover_after_interruption(repo, settings):
    job_id = repo.enqueue_job("analyze", {"mode": "demo"}, dedupe_key="demo-analysis")
    assert repo.enqueue_job("analyze", {"mode": "demo"}, dedupe_key="demo-analysis") == job_id
    job = repo.claim_job()
    assert job["id"] == job_id
    assert job["payload"] == {"mode": "demo"}
    assert repo.claim_job() is None

    reopened = Repository(settings.db_path)
    reopened.initialize()
    assert reopened.get_job(job_id)["status"] == "running"
    reopened.recover_interrupted_jobs()
    interrupted = reopened.get_job(job_id)
    assert interrupted["status"] == "failed"
    assert interrupted["error"]
    retry_id = reopened.enqueue_job("analyze", {"mode": "demo"}, dedupe_key="demo-analysis")
    assert retry_id != job_id
    recovered = reopened.claim_job()
    assert recovered["id"] == retry_id
    reopened.update_job(retry_id, status="succeeded", progress=1.0, result={"articles": 1})
    finished = reopened.get_job(retry_id)
    assert finished["result"] == {"articles": 1}
    assert finished["progress"] == 1.0


def test_backup_is_independent_and_readable(repo, settings, article, tmp_path):
    add_chunk(repo, article)
    destination = tmp_path / "snapshot.sqlite3"
    repo.backup(destination)
    snapshot = Repository(destination, read_only=True)
    snapshot.initialize()
    assert snapshot.list_articles()[0]["url"] == article.url
    assert snapshot.keyword_search("bridge")
    with pytest.raises(PermissionError):
        snapshot.upsert_article(article)
    with pytest.raises(PermissionError):
        snapshot.enqueue_job("analyze", {"mode": "demo"})


def test_claims_from_demo_and_real_models_coexist(repo, article):
    article_id, chunk = add_chunk(repo, article)
    claim = ClaimInput(
        claim_text="The city announced a bridge inspection.",
        quote="The fictional city announced a bridge inspection on Monday.",
        chunk_id=chunk["id"],
    )
    repo.replace_claims(article_id, [claim], "demo:fixture")
    repo.replace_claims(article_id, [claim], "ollama:qwen@revision")
    assert {row["model_key"] for row in repo.list_claims(article_id)} == {
        "demo:fixture",
        "ollama:qwen@revision",
    }
    repo.replace_claims(article_id, [], "demo:fixture")
    assert {row["model_key"] for row in repo.list_claims(article_id)} == {"ollama:qwen@revision"}


def test_changed_source_invalidates_claim_completion_markers(repo, article):
    article_id, _ = add_chunk(repo, article)
    old_hash = repo.get_article(article_id)["content_hash"]
    cache_key = f"claims:{article_id}:{old_hash}:demo:fixture"
    repo.set_meta(cache_key, "done")
    repo.upsert_article(article.model_copy(update={"text": article.text + " Updated."}))
    assert repo.get_meta(cache_key) is None


def test_stale_passages_cannot_be_written_after_the_source_changes(repo, article):
    article_id = repo.upsert_article(article)
    stale = ChunkInput(ordinal=0, text=article.text, start_char=0, end_char=len(article.text))
    updated = article.model_copy(update={"text": article.text.replace("bridge", "station")})
    repo.upsert_article(updated)
    with pytest.raises(ValueError):
        repo.replace_chunks(article_id, [stale])
    assert repo.list_chunks(article_id) == []


def test_claim_completion_is_atomic_and_rejects_stale_source_versions(repo, article):
    article_id, _ = add_chunk(repo, article)
    content_hash = repo.get_article(article_id)["content_hash"]
    model_key = "ollama:test@revision"
    cache_key = f"claims:{article_id}:{content_hash}:{model_key}"
    with pytest.raises(ValueError):
        repo.replace_claims(
            article_id, [], model_key, expected_content_hash="stale", cache_key=cache_key
        )
    assert repo.get_meta(cache_key) is None
    repo.replace_claims(
        article_id, [], model_key, expected_content_hash=content_hash, cache_key=cache_key
    )
    assert repo.get_meta(cache_key) == "done"
    repo.upsert_article(article.model_copy(update={"text": article.text + " New information."}))
    assert repo.get_meta(cache_key) is None


def test_failed_claim_replacement_preserves_existing_evidence(repo, article):
    article_id, chunk = add_chunk(repo, article)
    model_key = "test-model:v1"
    original = ClaimInput(
        claim_text="The city announced a bridge inspection.",
        quote="The fictional city announced a bridge inspection on Monday.",
        chunk_id=chunk["id"],
    )
    repo.replace_claims(article_id, [original], model_key)
    invalid = original.model_copy(update={"quote": "This quotation was never in the article."})
    with pytest.raises(ValueError):
        repo.replace_claims(article_id, [invalid], model_key)
    saved = repo.list_claims(article_id)
    assert len(saved) == 1
    assert saved[0]["quote"] == original.quote
