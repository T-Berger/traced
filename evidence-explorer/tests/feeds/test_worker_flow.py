"""A saved source travels through scheduling, collection, and cached local analysis."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from typer.testing import CliRunner

from evidence_explorer import analysis, ingestion_jobs
from evidence_explorer.cli import app
from evidence_explorer.feeds.runtime import FeedScheduler
from evidence_explorer.ingestion import NewsCollector
from evidence_explorer.models import ClaimInput
from evidence_explorer.service import ExplorerService
from evidence_explorer.worker import run_worker

FEED_URL = "https://news.example/feed.xml"
QUOTE = "The transport authority confirmed that the bridge remains closed today."
RSS = (
    '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
    "<channel><title>Public updates</title><link>https://news.example/</link>"
    "<description>Public reports</description><item><title>Bridge status</title>"
    "<link>https://news.example/posts/bridge</link><author>Public newsroom</author>"
    "<pubDate>Fri, 06 Nov 2026 10:00:00 GMT</pubDate>"
    f"<content:encoded><![CDATA[<p>{QUOTE}</p>]]></content:encoded>"
    "</item></channel></rss>"
)
HTML = (
    '<html lang="en"><head><title>Bridge status</title>'
    '<meta property="article:published_time" content="2026-11-06T10:00:00Z">'
    "</head><body><article><h1>Bridge status</h1>"
    f"<p>{QUOTE}</p><p>Engineers will publish another update after the inspection."
    " Residents should use the other crossing until then.</p></article></body></html>"
)


def install_sources(monkeypatch):
    requested = []

    def respond(request):
        requested.append(str(request.url))
        if request.url.path == "/feed.xml":
            return httpx.Response(200, text=RSS, headers={"content-type": "application/rss+xml"})
        return httpx.Response(200, text=HTML, headers={"content-type": "text/html"})

    monkeypatch.setattr(
        ingestion_jobs,
        "NewsCollector",
        lambda: NewsCollector(
            transport=httpx.MockTransport(respond), resolver=lambda host: ["93.184.216.34"]
        ),
    )
    return requested


class LocalModel:
    extraction_calls = 0
    embedding_calls = 0

    def __init__(self, settings):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def model_key(self, kind):
        return f"ollama:local-{kind}@test-revision"

    def embed(self, texts):
        type(self).embedding_calls += 1
        return [[1.0, 0.5] for text in texts]

    def extract_claims(self, article, chunks):
        type(self).extraction_calls += 1
        chunk = next(chunk for chunk in chunks if QUOTE in chunk["text"])
        return [
            ClaimInput(claim_text="The bridge is closed today.", quote=QUOTE, chunk_id=chunk["id"])
        ]


@pytest.mark.parametrize("content_mode", ["article_links", "feed_content"])
def test_worker_schedules_imports_analyzes_and_reuses_cache(settings, monkeypatch, content_mode):
    service = ExplorerService(settings)
    requested = install_sources(monkeypatch)
    LocalModel.extraction_calls = LocalModel.embedding_calls = 0
    monkeypatch.setattr(analysis, "OllamaClient", LocalModel)
    feed = service.save_feed("Bridge updates", FEED_URL, content_mode=content_mode, item_limit=1)

    assert run_worker(settings, once=True) == 2

    stored = service.repo.feeds.get(feed["id"])
    assert stored.last_result["imported"] == 1
    assert stored.last_analysis_job_id
    assert stored.last_refreshed_at
    model_job = service.repo.jobs.get_record(stored.last_analysis_job_id)
    assert model_job.status == "succeeded"
    assert model_job.result["mode"] == "ollama"
    article_id = stored.last_result["article_ids"][0]
    article = service.repo.articles.get_article(article_id)
    assert not article.is_sample
    assert article.content_source == (
        "article" if content_mode == "article_links" else "feed_content"
    )
    assert article.feed_url == FEED_URL
    assert service.repo.claims.list_claims(article_id)[0].quote == QUOTE
    assert len(requested) == (2 if content_mode == "article_links" else 1)

    service.enqueue_feed_refresh(feed["id"])
    assert run_worker(settings, once=True) == 2
    stored = service.repo.feeds.get(feed["id"])
    assert stored.last_result["unchanged"] == 1
    cached = service.repo.jobs.get_record(stored.last_analysis_job_id)
    assert cached.result["cached"] == 1
    assert cached.result["claims_created"] == cached.result["embeddings_created"] == 0
    assert LocalModel.extraction_calls == LocalModel.embedding_calls == 1
    assert service.repo.articles.count() == 1


def test_pausing_a_queued_schedule_prevents_network_but_manual_refresh_still_works(
    settings, monkeypatch
):
    service = ExplorerService(settings)
    feed = service.save_feed("Bridge updates", FEED_URL, analyze=False)
    queued = service.repo.feeds.queue_due()
    assert len(queued) == 1
    service.set_feed_enabled(feed["id"], False)

    def forbidden_source():
        raise AssertionError("A paused scheduled import must not make a network request.")

    monkeypatch.setattr(ingestion_jobs, "NewsCollector", forbidden_source)
    assert run_worker(settings, once=True) == 1
    job = service.repo.jobs.get_record(queued[0])
    assert job.status == "succeeded" and job.result["skipped"]
    assert service.repo.articles.count() == 0
    assert service.repo.feeds.get(feed["id"]).last_refreshed_at is None

    requested = install_sources(monkeypatch)
    manual = service.enqueue_feed_refresh(feed["id"])
    assert run_worker(settings, once=True) == 1
    assert service.repo.jobs.get_record(manual).status == "succeeded"
    assert service.repo.articles.count() == 1
    assert requested
    assert not service.repo.feeds.get(feed["id"]).enabled


def test_scheduler_error_is_visible_and_does_not_block_other_jobs(settings, article, monkeypatch):
    from evidence_explorer.feeds import scheduler

    service = ExplorerService(settings)
    article_id = service.repo.articles.upsert_article(article)
    job_id = service.enqueue_analysis("demo", [article_id])

    def broken_tick(*args, **kwargs):
        raise RuntimeError("Schedule database temporarily unavailable")

    monkeypatch.setattr(scheduler, "tick_feeds", broken_tick)
    assert run_worker(settings, once=True) == 1
    assert service.repo.jobs.get_record(job_id).status == "succeeded"
    assert "temporarily unavailable" in service.feed_scheduler_status()["error"]


def test_read_only_scheduler_is_inert(settings, monkeypatch, tmp_path):
    service = ExplorerService(settings)
    destination = tmp_path / "snapshot" / "evidence.sqlite3"
    service.repo.backup(destination)
    snapshot = ExplorerService(replace(settings, data_dir=destination.parent, read_only=True))
    before = destination.read_bytes()
    assert FeedScheduler(snapshot.repo).tick(force=True) is None
    with pytest.raises(PermissionError):
        snapshot.save_feed("Public news", FEED_URL)
    assert snapshot.list_feeds() == []
    assert destination.read_bytes() == before


def test_cli_saves_lists_and_pauses_schedules_without_collecting(settings, monkeypatch):
    monkeypatch.setenv("EVIDENCE_DATA_DIR", str(settings.data_dir))
    runner = CliRunner()
    added = runner.invoke(
        app, ["feeds", "add", "Public posts", FEED_URL, "--social", "--no-analyze"]
    )
    assert added.exit_code == 0, added.output
    service = ExplorerService(settings)
    feed = service.list_feeds()[0]
    assert feed["content_mode"] == "feed_content" and not feed["analyze"]
    assert service.repo.articles.count() == 0
    assert runner.invoke(app, ["feeds", "list"]).exit_code == 0
    assert runner.invoke(app, ["feeds", "pause", str(feed["id"])]).exit_code == 0
    assert not service.list_feeds()[0]["enabled"]
    assert runner.invoke(app, ["feeds", "refresh", str(feed["id"])]).exit_code == 0
    assert service.repo.jobs.list_records()[0].kind == "ingest"


def test_one_shot_worker_does_not_start_another_period_after_slow_analysis(settings, monkeypatch):
    from evidence_explorer.feeds import persistence
    from evidence_explorer.persistence import jobs

    clock = {"now": datetime.now(UTC)}
    real_instant = persistence._instant
    monkeypatch.setattr(
        persistence, "_instant", lambda value=None: real_instant(value or clock["now"])
    )
    monkeypatch.setattr(jobs, "_now", lambda: clock["now"].isoformat())

    class SlowLocalModel(LocalModel):
        def extract_claims(self, article, chunks):
            result = super().extract_claims(article, chunks)
            clock["now"] += timedelta(minutes=10)
            return result

    service = ExplorerService(settings)
    install_sources(monkeypatch)
    monkeypatch.setattr(analysis, "OllamaClient", SlowLocalModel)
    service.save_feed("Five-minute feed", FEED_URL, interval_minutes=5, item_limit=1)

    assert run_worker(settings, once=True) == 2
    assert len(service.repo.jobs.list_records()) == 2
    assert service.repo.stats()["jobs_queued"] == 0


def test_workspace_drain_processes_all_due_feeds_beyond_one_scheduler_batch(settings, monkeypatch):
    from evidence_explorer.review.integrations import worker as workspace_worker
    from evidence_explorer.review.storage import ReviewRepository

    review = ReviewRepository(settings.db_path)
    review.initialize()
    worker = workspace_worker.WorkspaceWorker(review, settings)
    service = ExplorerService(settings)
    for index in range(25):
        service.save_feed(f"Feed {index}", f"https://news.example/feed/{index}", analyze=False)
    monkeypatch.setattr(workspace_worker, "process_job", lambda *args: {"imported": 0})

    assert worker.drain() == 25
    assert service.repo.stats()["jobs_queued"] == 0
    assert all(feed.last_refreshed_at for feed in service.repo.feeds.list_page())


def test_analysis_reports_excluded_proposals_without_storing_them(settings, monkeypatch):
    from evidence_explorer.ui import job_cards

    class ReportingLocalModel(LocalModel):
        def extract_claims(self, article, chunks):
            self.last_claim_extraction_metadata = {"attempts": 2, "rejected_claims": 2}
            return super().extract_claims(article, chunks)

    service = ExplorerService(settings)
    install_sources(monkeypatch)
    monkeypatch.setattr(analysis, "OllamaClient", ReportingLocalModel)
    service.save_feed("Bridge updates", FEED_URL, item_limit=1)
    assert run_worker(settings, once=True) == 2
    job = service.repo.jobs.get_record(service.list_feeds()[0]["last_analysis_job_id"])
    assert job.result["claims_created"] == 1
    assert job.result["claims_rejected"] == 2
    assert len(service.repo.claims.list_claims()) == 1
    assert "2 proposed claim(s) excluded" in str(job_cards([job.model_dump(mode="json")]))
    service.enqueue_feed_refresh(service.list_feeds()[0]["id"])
    assert run_worker(settings, once=True) == 2
    cached = service.repo.jobs.get_record(service.list_feeds()[0]["last_analysis_job_id"])
    assert cached.result["cached"] == 1
    assert cached.result["claims_rejected"] == 2
