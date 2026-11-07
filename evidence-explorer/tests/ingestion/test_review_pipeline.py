"""Collection, actual local-model HTTP protocol, and portable review evidence."""

import json
from collections import Counter

import httpx
import pytest

from evidence_explorer import analysis, ingestion_jobs
from evidence_explorer.ingestion import NewsCollector
from evidence_explorer.models_client import OllamaClient
from evidence_explorer.review.integrations.explorer import bundle_from_repository
from evidence_explorer.review.integrations.packages import export_case_package, import_case_package
from evidence_explorer.service import ExplorerService
from evidence_explorer.worker import run_worker

from .fixtures import PUBLIC_IP, QUOTE, article_html

ARTICLE_URL = "https://news.example/articles/bridge"
FEED_URL = "https://news.example/feed.xml"
TRACKED_URL = ARTICLE_URL + "?utm_source=review-test#latest"


def wire_http_boundaries(monkeypatch, settings, *, published, invalid_quote=False):
    """Use the production collectors/client; replace only their HTTP transports."""
    requests = Counter()

    def news(request):
        requests[f"news:{request.url.path}"] += 1
        if request.url.path == "/feed.xml":
            return httpx.Response(
                200,
                headers={"content-type": "application/rss+xml"},
                text=(
                    '<rss version="2.0"><channel><title>Example Newsroom</title>'
                    "<link>https://news.example/</link><description>Public updates</description>"
                    f"<item><title>Bridge update</title><link>{TRACKED_URL}</link></item>"
                    "</channel></rss>"
                ),
            )
        assert str(request.url) == ARTICLE_URL
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=article_html(published=published),
        )

    def model(request):
        requests[f"ollama:{request.url.path}"] += 1
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": settings.embedding_model, "digest": "embedding-v1"},
                        {"name": settings.chat_model, "digest": "claims-v1"},
                    ]
                },
            )
        body = json.loads(request.content)
        if request.url.path == "/api/embed":
            assert body["model"] == settings.embedding_model
            return httpx.Response(200, json={"embeddings": [[1.0, 0.5] for _text in body["input"]]})
        assert request.url.path == "/api/chat"
        assert body["model"] == settings.chat_model
        assert body["stream"] is False
        assert "claims" in body["format"]["properties"]
        # A bounded correction preserves the original article input and appends
        # validation feedback, so the final message is no longer source JSON.
        supplied = json.loads(body["messages"][1]["content"])
        if len(body["messages"]) > 2:
            assert invalid_quote
            assert len(body["messages"]) == 4
            assert "Source selection validation failed" in body["messages"][-1]["content"]
            requests["corrections"] += 1
        passage = next(item for item in supplied["candidates"] if QUOTE in item["text"])
        claim = {
            "candidate_id": "fabricated-excerpt" if invalid_quote else passage["candidate_id"],
        }
        return httpx.Response(
            200,
            json={
                "model": settings.chat_model,
                "done": True,
                "done_reason": "stop",
                "message": {"content": json.dumps({"claims": [claim]})},
            },
        )

    monkeypatch.setattr(
        ingestion_jobs,
        "NewsCollector",
        lambda: NewsCollector(
            transport=httpx.MockTransport(news), resolver=lambda hostname: [PUBLIC_IP]
        ),
    )
    monkeypatch.setattr(
        analysis,
        "OllamaClient",
        lambda configured: OllamaClient(configured, transport=httpx.MockTransport(model)),
    )
    return requests


@pytest.mark.parametrize("source_type,published", [("article", False), ("feed", True)])
def test_collected_news_local_analysis_and_review_package_preserve_provenance(
    settings, monkeypatch, tmp_path, source_type, published
):
    requests = wire_http_boundaries(monkeypatch, settings, published=published)
    explorer = ExplorerService(settings)
    url = FEED_URL if source_type == "feed" else TRACKED_URL
    import_id = explorer.enqueue_ingestion(url, source_type=source_type, analyze=True)

    assert run_worker(settings, once=True) == 2
    imported = explorer.repo.get_job(import_id)
    assert imported["status"] == "succeeded", imported["error"]
    analysis_job = explorer.repo.get_job(imported["result"]["analysis_job_id"])
    assert analysis_job["status"] == "succeeded", analysis_job["error"]
    assert analysis_job["result"]["mode"] == "ollama"
    assert analysis_job["result"]["claims_created"] == 1
    assert "claims-v1" in analysis_job["result"]["model_key"]
    article_id = imported["result"]["article_ids"][0]
    saved = explorer.repo.get_article(article_id)
    assert saved["is_sample"] is False
    assert saved["published_at"] == ("2026-11-01T09:30:00+01:00" if published else "")
    assert saved["retrieved_at"]

    # A repeat import still checks the source, but does not repeat inference for
    # unchanged text/model revisions or replace the existing passage identities.
    chunk_ids = [chunk["id"] for chunk in explorer.repo.list_chunks(article_id)]
    inference_before = requests["ollama:/api/chat"], requests["ollama:/api/embed"]
    assert inference_before[0] == 1
    assert inference_before[1] >= 1
    repeated_id = explorer.enqueue_ingestion(url, source_type=source_type, analyze=True)
    assert run_worker(settings, once=True) == 2
    repeated = explorer.repo.get_job(repeated_id)
    assert repeated["status"] == "succeeded", repeated["error"]
    assert repeated["result"]["unchanged"] == 1
    repeated_analysis = explorer.repo.get_job(repeated["result"]["analysis_job_id"])
    assert repeated_analysis["status"] == "succeeded", repeated_analysis["error"]
    assert repeated_analysis["result"]["cached"] == 1
    assert requests["news:/articles/bridge"] == 2
    assert (requests["ollama:/api/chat"], requests["ollama:/api/embed"]) == inference_before
    assert [chunk["id"] for chunk in explorer.repo.list_chunks(article_id)] == chunk_ids

    bundle = bundle_from_repository(explorer.repo, case_id="test:collected-news")
    assert bundle.case.synthetic is False
    assert len(bundle.records) == len(bundle.findings) == 1
    record = bundle.records[0]
    latest_saved = explorer.repo.get_article(article_id)
    assert record.source_url == ARTICLE_URL
    assert record.published_at == (latest_saved["published_at"] or None)
    assert record.acquired_at == latest_saved["retrieved_at"]
    assert record.metadata["author"] == "Alex Example"
    for name in ("requested_url", "final_url", "feed_url", "retrieved_at", "publication_source"):
        assert record.metadata[name] == latest_saved[name]
    assert record.metadata["feed_url"] == (FEED_URL if source_type == "feed" else None)
    finding = bundle.findings[0]
    assert finding.details["extraction_origin"] == "saved_model"
    assert finding.details["claimed_event_time"] is None
    support = next(link for link in bundle.links if link.target.id == finding.snapshot_id)
    fragment = next(item for item in bundle.fragments if item.id == support.fragment_ids[0])
    assert fragment.record_id == record.id
    assert QUOTE in fragment.quote
    assert fragment.quote == finding.statement
    assert record.text[fragment.selector.start : fragment.selector.end] == fragment.quote
    assert finding.details["attribution"] is None
    assert finding.details["subject"] == ""

    reopened = import_case_package(export_case_package(bundle), tmp_path / "receiving-reviewer")
    assert reopened.fingerprint() == bundle.fingerprint()
    assert reopened.records[0].acquired_at == record.acquired_at
    assert reopened.findings[0].details["review_state"] == "unreviewed"


def test_bad_model_citation_fails_analysis_without_losing_collected_article(settings, monkeypatch):
    requests = wire_http_boundaries(monkeypatch, settings, published=False, invalid_quote=True)
    explorer = ExplorerService(settings)
    import_id = explorer.enqueue_ingestion(TRACKED_URL, analyze=True)

    assert run_worker(settings, once=True) == 2
    imported = explorer.repo.get_job(import_id)
    assert imported["status"] == "succeeded", imported["error"]
    failed = explorer.repo.get_job(imported["result"]["analysis_job_id"])
    assert failed["status"] == "failed"
    assert "source-selection checks after one correction; no claims saved" in failed["error"]
    assert requests["ollama:/api/chat"] == 2
    assert requests["corrections"] == 1
    article_id = imported["result"]["article_ids"][0]
    saved = explorer.repo.get_article(article_id)
    assert saved["published_at"] == ""
    assert explorer.repo.list_claims(article_id) == []
    with analysis.OllamaClient(settings) as model:
        model_key = model.model_key("claims")
    assert (
        explorer.repo.get_meta(f"claims:{article_id}:{saved['content_hash']}:{model_key}") is None
    )
    assert explorer.search_keyword("bridge")
    bundle = bundle_from_repository(explorer.repo, case_id="test:failed-extraction")
    assert len(bundle.records) == 1
    assert bundle.findings == []
    assert QUOTE in bundle.records[0].text
