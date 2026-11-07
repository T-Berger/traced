"""Offline imports through the durable worker, real parser, and SQLite repositories."""

from dataclasses import replace

import httpx
import pytest

from evidence_explorer import analysis, ingestion_jobs
from evidence_explorer.demo import seed_demo
from evidence_explorer.ingestion import NewsCollector
from evidence_explorer.ingestion_jobs import process_ingestion
from evidence_explorer.models import ClaimInput
from evidence_explorer.models_client import ModelError
from evidence_explorer.service import ExplorerService
from evidence_explorer.worker import run_worker

from .fixtures import PUBLIC_IP, QUOTE, RSS, article_html

ARTICLE_URL = "https://news.example/articles/bridge"
FEED_URL = "https://news.example/feed.xml"
EMPTY_FEED = (
    '<rss version="2.0"><channel><title>Newsroom</title><link>https://news.example/</link>'
    "<description>No updates</description></channel></rss>"
)


@pytest.fixture
def explorer(settings):
    return ExplorerService(settings)


def use_collector(monkeypatch, handler):
    def factory():
        return NewsCollector(
            transport=httpx.MockTransport(handler), resolver=lambda hostname: [PUBLIC_IP]
        )

    monkeypatch.setattr(ingestion_jobs, "NewsCollector", factory)
    return factory


def html_response(request):
    return httpx.Response(200, text=article_html(), headers={"content-type": "text/html"})


def import_article(explorer, settings, *, analyze=False):
    job_id = explorer.enqueue_ingestion(ARTICLE_URL, topic="River flooding", analyze=analyze)
    run_worker(settings, once=True)
    job = explorer.repo.get_job(job_id)
    assert job["status"] == "succeeded", job["error"]
    return job["result"]


def test_source_only_worker_import_is_searchable_and_never_loads_ollama(
    explorer, settings, monkeypatch
):
    def unexpected_model(*args, **kwargs):
        raise AssertionError("A source-only import must not construct an Ollama client.")

    monkeypatch.setattr(analysis, "OllamaClient", unexpected_model)
    use_collector(monkeypatch, html_response)
    assert explorer.repo.has_samples() is False
    requested = ARTICLE_URL + "?utm_source=newsletter#latest"
    job_id = explorer.enqueue_ingestion(requested, topic="River flooding", analyze=False)
    run_worker(settings, once=True)
    job = explorer.repo.get_job(job_id)
    assert job["status"] == "succeeded", job["error"]
    result = job["result"]
    assert result["imported"] == 1
    assert result["analysis_job_id"] is None
    saved = explorer.repo.get_article(result["article_ids"][0])
    assert saved["requested_url"] == requested
    assert saved["final_url"] == ARTICLE_URL
    assert saved["url"] == ARTICLE_URL
    assert saved["author"] == "Alex Example"
    assert saved["retrieved_at"]
    assert saved["published_at"] == "2026-11-01T09:30:00+01:00"
    assert saved["publication_source"] == "article"
    assert saved["topic"] == "River flooding"
    assert saved["is_sample"] is False
    assert explorer.repo.has_samples() is False
    chunks = explorer.repo.list_chunks(saved["id"])
    assert chunks
    assert all(saved["text"][row["start_char"] : row["end_char"]] == row["text"] for row in chunks)
    assert QUOTE in saved["text"]
    assert explorer.search_keyword("bridge")[0]["article_id"] == saved["id"]
    assert explorer.repo.list_claims(saved["id"]) == []
    assert all(job["kind"] != "analyze" for job in explorer.repo.list_jobs())
    seed_demo(explorer.repo)
    assert explorer.repo.has_samples() is True


def test_repeat_import_keeps_cached_evidence_and_changed_text_invalidates_it(
    explorer, settings, monkeypatch
):
    body = {"html": article_html()}
    use_collector(
        monkeypatch,
        lambda request: httpx.Response(
            200, text=body["html"], headers={"content-type": "text/html"}
        ),
    )
    first = import_article(explorer, settings)
    article_id = first["article_ids"][0]
    original = explorer.repo.get_article(article_id)
    chunk = next(row for row in explorer.repo.list_chunks(article_id) if QUOTE in row["text"])
    model_key = "ollama:test@revision"
    cache_key = f"claims:{article_id}:{original['content_hash']}:{model_key}"
    explorer.repo.save_embedding(chunk["id"], model_key, [1.0, 0.0])
    explorer.repo.replace_claims(
        article_id,
        [
            ClaimInput(
                claim_text="The bridge remains closed to vehicles.",
                quote=QUOTE,
                chunk_id=chunk["id"],
            )
        ],
        model_key,
        expected_content_hash=original["content_hash"],
        cache_key=cache_key,
    )
    original_passage_ids = [row["id"] for row in explorer.repo.list_chunks(article_id)]

    repeated = import_article(explorer, settings)
    assert repeated["unchanged"] == 1
    assert repeated["imported"] == repeated["updated"] == 0
    assert repeated["article_ids"] == [article_id]
    assert [row["id"] for row in explorer.repo.list_chunks(article_id)] == original_passage_ids
    assert explorer.repo.get_embedding(chunk["id"], model_key) == [1.0, 0.0]
    assert explorer.repo.list_claims(article_id)
    assert explorer.repo.get_meta(cache_key) == "done"

    body["html"] = body["html"].replace(
        QUOTE, "The bridge reopened to vehicles after the inspection."
    )
    changed = import_article(explorer, settings)
    assert changed["updated"] == 1
    assert changed["imported"] == changed["unchanged"] == 0
    assert changed["article_ids"] == [article_id]
    assert explorer.repo.list_claims(article_id) == []
    assert explorer.repo.list_embeddings(model_key) == []
    assert explorer.repo.get_meta(cache_key) is None
    assert explorer.search_keyword("reopened")[0]["article_id"] == article_id


def test_analysis_followup_includes_unchanged_sources_that_have_not_been_analyzed(
    explorer, settings, monkeypatch
):
    def respond(request):
        if request.url.path == "/feed.xml":
            return httpx.Response(200, text=RSS, headers={"content-type": "application/rss+xml"})
        return html_response(request)

    factory = use_collector(monkeypatch, respond)
    previous = import_article(explorer, settings)
    existing_id = previous["article_ids"][0]
    job_id = explorer.enqueue_ingestion(FEED_URL, source_type="feed", analyze=True)
    job = explorer.repo.jobs.claim_record()
    assert job.id == job_id
    result = process_ingestion(
        explorer.repo, settings, job, lambda stage, value: None, collector_factory=factory
    )
    assert result.imported == 1
    assert result.unchanged == 1
    assert existing_id in result.unchanged_ids
    followup = explorer.repo.jobs.get_record(result.analysis_job_id)
    assert followup.kind == "analyze"
    assert followup.status == "queued"
    assert followup.payload["mode"] == "ollama"
    assert set(followup.payload["article_ids"]) == set(result.article_ids)
    assert set(result.article_ids) == set(result.new_ids + result.unchanged_ids)


@pytest.mark.parametrize("all_failed", [False, True])
def test_feed_partial_success_or_all_failed_result_is_preserved(
    explorer, settings, monkeypatch, all_failed
):
    def respond(request):
        if request.url.path == "/feed.xml":
            return httpx.Response(200, text=RSS, headers={"content-type": "application/rss+xml"})
        if all_failed or request.url.path == "/articles/tide":
            return httpx.Response(503, text="temporarily unavailable")
        return html_response(request)

    use_collector(monkeypatch, respond)
    job_id = explorer.enqueue_ingestion(FEED_URL, source_type="feed", analyze=False)
    run_worker(settings, once=True)
    job = explorer.repo.get_job(job_id)
    result = job["result"]
    assert job["status"] == ("failed" if all_failed else "succeeded")
    assert result["imported"] == (0 if all_failed else 1)
    assert len(result["errors"]) == (2 if all_failed else 1)
    assert all(error["error"] for error in result["errors"])
    assert result["analysis_job_id"] is None
    assert len(explorer.repo.list_articles()) == (0 if all_failed else 1)
    if not all_failed:
        article = explorer.repo.get_article(result["article_ids"][0])
        assert article["feed_url"] == FEED_URL
        assert explorer.search_keyword("bridge")


def test_empty_valid_feed_succeeds_without_creating_analysis(explorer, settings, monkeypatch):
    use_collector(
        monkeypatch,
        lambda request: httpx.Response(
            200, text=EMPTY_FEED, headers={"content-type": "application/rss+xml"}
        ),
    )
    job_id = explorer.enqueue_ingestion(FEED_URL, source_type="feed", analyze=True)
    run_worker(settings, once=True)
    job = explorer.repo.get_job(job_id)
    assert job["status"] == "succeeded", job["error"]
    assert job["result"]["article_ids"] == []
    assert job["result"]["analysis_job_id"] is None
    assert len(explorer.repo.list_jobs()) == 1


def test_missing_local_model_fails_analysis_without_undoing_the_import(
    explorer, settings, monkeypatch
):
    class MissingModel:
        def __init__(self, settings):
            raise ModelError("Model is missing. Run: ollama pull bge-m3")

    use_collector(monkeypatch, html_response)
    monkeypatch.setattr(analysis, "OllamaClient", MissingModel)
    result = import_article(explorer, settings, analyze=True)
    followup = explorer.repo.get_job(result["analysis_job_id"])
    assert followup["status"] == "failed"
    assert "ollama pull bge-m3" in followup["error"]
    article_id = result["article_ids"][0]
    assert explorer.repo.get_article(article_id)
    assert explorer.search_keyword("bridge")
    assert explorer.repo.list_claims(article_id) == []


def test_enqueue_is_network_free_and_deduplicates_active_normalized_urls(explorer, monkeypatch):
    def unexpected_collector():
        raise AssertionError("Collection must run in the worker, never while queueing.")

    monkeypatch.setattr(ingestion_jobs, "NewsCollector", unexpected_collector)
    first = explorer.enqueue_ingestion(ARTICLE_URL + "?utm_source=one", analyze=False)
    second = explorer.enqueue_ingestion(ARTICLE_URL + "?utm_source=two#details", analyze=False)
    assert second == first
    assert explorer.repo.get_job(first)["kind"] == "ingest"
    assert explorer.repo.list_articles() == []


def test_read_only_snapshot_cannot_queue_or_collect_sources(
    explorer, settings, monkeypatch, tmp_path
):
    target = tmp_path / "snapshot" / "evidence.sqlite3"
    explorer.repo.backup(target)
    snapshot = ExplorerService(replace(settings, data_dir=target.parent, read_only=True))
    with pytest.raises(PermissionError):
        snapshot.enqueue_ingestion(ARTICLE_URL, analyze=False)
    with pytest.raises(PermissionError):
        process_ingestion(
            snapshot.repo,
            snapshot.settings,
            {"kind": "ingest", "payload": {"url": ARTICLE_URL, "analyze": False}},
            lambda stage, value: None,
        )
    assert snapshot.repo.list_jobs() == []


@pytest.mark.parametrize("limit", [0, 21, True])
def test_invalid_feed_limits_do_not_create_jobs(explorer, limit):
    with pytest.raises(ValueError):
        explorer.enqueue_ingestion(FEED_URL, source_type="feed", limit=limit)
    assert explorer.repo.list_jobs() == []
