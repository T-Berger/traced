"""A complete offline flow through the durable queue and analysis pipeline."""

import pytest

from evidence_explorer.demo import sample_articles, seed_demo
from evidence_explorer.models import ArticleInput
from evidence_explorer.worker import WorkerAlreadyRunning, run_worker, worker_lock


def test_sample_seed_is_idempotent_and_identifies_fictional_sources(repo):
    inserted = seed_demo(repo)
    assert inserted > 0
    articles = repo.list_articles()
    assert len(articles) == inserted
    assert all(article["is_sample"] for article in articles)
    assert all(".example" in article["url"] for article in articles)
    assert repo.list_claims()
    assert seed_demo(repo) == 0
    assert len(repo.list_articles()) == inserted


def test_worker_analyzes_searches_and_reuses_cached_work(repo, settings):
    fixture = sample_articles()[0]
    article = ArticleInput(
        **{key: value for key, value in fixture.items() if key != "sample_claims"},
        is_sample=True,
    )
    article_id = repo.upsert_article(article)
    first = repo.enqueue_job("analyze", {"mode": "demo", "article_ids": [article_id]})
    run_worker(settings, once=True)
    completed = repo.get_job(first)
    assert completed["status"] == "succeeded", completed["error"]
    assert completed["result"]["embeddings_created"] > 0
    claims = repo.list_claims(article_id)
    assert claims
    assert all(claim["quote"] in article.text for claim in claims)

    second = repo.enqueue_job("analyze", {"mode": "demo", "article_ids": [article_id]})
    search = repo.enqueue_job("search", {"mode": "demo", "query": "bridge inspection", "limit": 5})
    run_worker(settings, once=True)
    cached = repo.get_job(second)
    assert cached["status"] == "succeeded", cached["error"]
    assert cached["result"]["embeddings_created"] == 0
    assert cached["result"]["claims_created"] == 0
    found = repo.get_job(search)
    assert found["status"] == "succeeded", found["error"]
    assert found["result"]["matches"]
    assert found["result"]["matches"][0]["article_id"] == article_id


def test_failed_job_keeps_error_and_worker_can_process_next_job(repo, settings, article):
    article_id = repo.upsert_article(article)
    bad_job = repo.enqueue_job("analyze", {"mode": "invalid"})
    good_job = repo.enqueue_job("analyze", {"mode": "demo", "article_ids": [article_id]})
    run_worker(settings, once=True)
    failed = repo.get_job(bad_job)
    assert failed["status"] == "failed"
    assert failed["error"]
    assert repo.get_job(good_job)["status"] == "succeeded"


def test_only_one_worker_can_own_a_data_directory(tmp_path):
    lock_path = tmp_path / "worker.lock"
    with worker_lock(lock_path):
        with pytest.raises(WorkerAlreadyRunning):
            with worker_lock(lock_path):
                pass
    with worker_lock(lock_path):
        assert lock_path.exists()
