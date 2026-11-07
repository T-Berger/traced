"""News import UI regressions: queue boundaries, partial failures, and provenance."""

import importlib
import json
from dataclasses import replace

import pytest


@pytest.fixture
def dashboard(settings, repo, monkeypatch):
    monkeypatch.setenv("EVIDENCE_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("OLLAMA_BASE_URL", settings.ollama_url)
    app = importlib.import_module("evidence_explorer.web").create_app(settings)

    def no_network_status():
        pytest.fail("The import callback must not probe models or perform network requests")

    monkeypatch.setattr(app.explorer_service, "status", no_network_status)
    return app


def _post_import(app, *, trigger="ingestion-button.n_clicks", active=None, **overrides):
    values = {
        "ingestion-url": "https://news.example.org/report",
        "ingestion-source-type": "article",
        "ingestion-limit": 5,
        "ingestion-topic": "",
        "ingestion-analyze": ["analyze"],
    }
    values.update(overrides)
    callback = next(key for key in app.callback_map if key.startswith("..ingestion-store.data"))
    response = app.server.test_client().post(
        "/_dash-update-component",
        json={
            "output": callback,
            "outputs": [
                {"id": "ingestion-store", "property": "data"},
                {"id": "ingestion-feedback", "property": "children"},
            ],
            "changedPropIds": [trigger],
            "inputs": [
                {"id": "ingestion-button", "property": "n_clicks", "value": 1},
                {"id": "poll", "property": "n_intervals", "value": 1},
            ],
            "state": [
                *[
                    {"id": key, "property": "value", "value": value}
                    for key, value in values.items()
                ],
                {"id": "ingestion-store", "property": "data", "value": active},
            ],
        },
    )
    assert response.status_code in {200, 204}, response.get_data(as_text=True)
    return response.get_json()["response"] if response.status_code == 200 else {}


def _serialize(component):
    return json.dumps(component, default=lambda value: value.to_plotly_json())


def _components(layout):
    result = {}

    def collect(value):
        if isinstance(value, dict):
            if "props" in value and isinstance(value["props"].get("id"), str):
                result[value["props"]["id"]] = value["props"]
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(layout)
    return result


def _queue_stub(app, monkeypatch):
    calls = []

    def enqueue(url, **options):
        calls.append({"url": url, **options})
        return app.explorer_service.repo.enqueue_job("ingest", {"url": url, **options})

    monkeypatch.setattr(app.explorer_service, "enqueue_ingestion", enqueue, raising=False)
    return calls


def test_article_import_only_queues_with_local_analysis_enabled_by_default(dashboard, monkeypatch):
    calls = _queue_stub(dashboard, monkeypatch)
    before_articles = dashboard.explorer_service.repo.stats()["articles"]

    result = _post_import(dashboard)

    assert calls == [
        {
            "url": "https://news.example.org/report",
            "source_type": "article",
            "limit": 5,
            "topic": "General",
            "analyze": True,
        }
    ]
    job_id = result["ingestion-store"]["data"]["id"]
    assert dashboard.explorer_service.repo.get_job(job_id)["status"] == "queued"
    assert set(result) == {"ingestion-store", "ingestion-feedback"}
    assert dashboard.explorer_service.repo.stats()["articles"] == before_articles
    form = _components(dashboard.server.test_client().get("/_dash-layout").get_json())
    assert form["ingestion-source-type"]["value"] == "article"
    assert form["ingestion-limit"]["value"] == 5
    assert form["ingestion-limit"]["min"] == 1
    assert form["ingestion-limit"]["max"] == 20
    assert form["ingestion-analyze"]["value"] == ["analyze"]


def test_feed_import_passes_limit_topic_and_opt_out_without_touching_analysis_watch(
    dashboard, monkeypatch
):
    calls = _queue_stub(dashboard, monkeypatch)

    result = _post_import(
        dashboard,
        **{
            "ingestion-url": "https://news.example.org/rss.xml",
            "ingestion-source-type": "feed",
            "ingestion-limit": 20,
            "ingestion-topic": "  Flood response  ",
            "ingestion-analyze": [],
        },
    )

    assert calls[0] == {
        "url": "https://news.example.org/rss.xml",
        "source_type": "feed",
        "limit": 20,
        "topic": "Flood response",
        "analyze": False,
    }
    assert "job-store" not in result
    assert "search-store" not in result
    assert result["ingestion-store"]["data"]["id"]


def test_read_only_import_is_disabled_and_forged_callback_cannot_enqueue(dashboard, monkeypatch):
    web = importlib.import_module("evidence_explorer.web")
    app = web.create_app(replace(dashboard.explorer_service.settings, read_only=True))
    calls = _queue_stub(app, monkeypatch)
    before = dashboard.explorer_service.repo.stats()["jobs"]

    result = _post_import(app)

    assert calls == []
    assert dashboard.explorer_service.repo.stats()["jobs"] == before
    assert "ingestion-store" not in result
    assert "ingestion-feedback" in result
    layout = app.server.test_client().get("/_dash-layout").get_json()
    components = _components(layout)
    assert components["ingestion-button"]["disabled"] is True
    assert components["ingestion-url"]["disabled"] is True
    assert components["ingestion-source-type"]["disabled"] is True
    assert components["ingestion-analyze"]["options"][0]["disabled"] is True


@pytest.mark.parametrize(
    ("source_type", "read_only", "disabled"),
    [("article", False, True), ("feed", False, False), ("feed", True, True)],
)
def test_feed_limit_is_editable_only_for_writable_feed_imports(
    dashboard, source_type, read_only, disabled
):
    app = dashboard
    if read_only:
        web = importlib.import_module("evidence_explorer.web")
        app = web.create_app(replace(dashboard.explorer_service.settings, read_only=True))
    response = app.server.test_client().post(
        "/_dash-update-component",
        json={
            "output": "ingestion-limit.disabled",
            "outputs": {"id": "ingestion-limit", "property": "disabled"},
            "changedPropIds": ["ingestion-source-type.value"],
            "inputs": [{"id": "ingestion-source-type", "property": "value", "value": source_type}],
            "state": [],
        },
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["response"]["ingestion-limit"]["disabled"] is disabled


@pytest.mark.parametrize("has_samples", [True, False])
def test_sample_banner_reflects_exact_sample_presence_without_loading_source_bodies(
    dashboard, monkeypatch, has_samples
):
    repo = dashboard.explorer_service.repo
    monkeypatch.setattr(repo, "has_samples", lambda: has_samples)

    def no_full_corpus():
        pytest.fail("Banner refresh must not materialize the full source corpus")

    monkeypatch.setattr(repo, "list_articles", no_full_corpus)
    response = dashboard.server.test_client().post(
        "/_dash-update-component",
        json={
            "output": "sample-banner.style",
            "outputs": {"id": "sample-banner", "property": "style"},
            "changedPropIds": ["collection-signature.data"],
            "inputs": [{"id": "collection-signature", "property": "data", "value": "changed"}],
            "state": [],
        },
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    expected = {} if has_samples else {"display": "none"}
    assert response.get_json()["response"]["sample-banner"]["style"] == expected


def test_validation_failure_is_visible_without_creating_a_job(dashboard, monkeypatch):
    def reject(_url, **_options):
        raise ValueError("Use a public HTTP or HTTPS URL.")

    monkeypatch.setattr(dashboard.explorer_service, "enqueue_ingestion", reject, raising=False)
    before = dashboard.explorer_service.repo.stats()["jobs"]

    result = _post_import(dashboard, **{"ingestion-url": "http://localhost/private"})

    assert dashboard.explorer_service.repo.stats()["jobs"] == before
    assert "ingestion-store" not in result
    assert "Use a public HTTP or HTTPS URL." in _serialize(result["ingestion-feedback"])


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_finished_import_preserves_per_source_errors_and_successful_article_links(
    dashboard, status
):
    repo = dashboard.explorer_service.repo
    article = repo.list_articles()[0]
    job_id = repo.enqueue_job(
        "ingest", {"url": "https://news.example.org/rss", "source_type": "feed"}
    )
    analysis_job_id = repo.enqueue_job("analyze", {"mode": "ollama"})
    result = {
        "imported": 1 if status == "succeeded" else 0,
        "updated": 0,
        "unchanged": 0,
        "article_ids": [article["id"]] if status == "succeeded" else [],
        "errors": [
            {"url": "https://news.example.org/blocked", "error": "Source returned HTTP 403"}
        ],
    }
    if status == "succeeded":
        result["analysis_job_id"] = analysis_job_id
    repo.update_job(
        job_id,
        status=status,
        result=result,
        error="No articles could be imported" if status == "failed" else None,
    )

    response = _post_import(dashboard, trigger="poll.n_intervals", active={"id": job_id})

    assert response["ingestion-store"]["data"]["finished"] is True
    rendered = _serialize(response["ingestion-feedback"])
    assert result["errors"][0]["url"] in rendered
    assert result["errors"][0]["error"] in rendered
    assert "job-store" not in response
    if status == "succeeded":
        assert f"#job-{analysis_job_id}" in rendered
        assert f"{article['id']}-import" in rendered
    else:
        assert "#job-" not in rendered


def test_publication_timeline_omits_unknown_or_invalid_dates_without_using_retrieval_time():
    ui = importlib.import_module("evidence_explorer.ui")
    articles = [
        {
            "id": 1,
            "publisher": "Known source",
            "title": "Dated report",
            "published_at": "2026-11-01T09:00:00+00:00",
        },
        {
            "id": 2,
            "publisher": "Unknown date",
            "title": "Undated report",
            "published_at": "",
            "retrieved_at": "2026-11-06T15:00:00+00:00",
        },
        {"id": 3, "publisher": "Invalid date", "title": "Invalid metadata", "published_at": "junk"},
    ]

    figure = ui.timeline_figure(articles)

    plotted_ids = [point[0] for trace in figure.data for point in trace.customdata]
    assert plotted_ids == [1]
    assert all(value for trace in figure.data for value in trace.x)


def test_article_panel_shows_author_and_collection_provenance_with_unknown_publication(article):
    ui = importlib.import_module("evidence_explorer.ui")
    imported = {
        **article.model_dump(),
        "id": 1,
        "is_sample": False,
        "published_at": "",
        "author": "Alex Reporter",
        "retrieved_at": "2026-11-06T15:00:00+00:00",
        "requested_url": "https://news.example.org/original",
        "final_url": "https://news.example.org/report",
        "feed_url": "https://news.example.org/rss",
        "publication_source": "unknown",
    }

    rendered = _serialize(ui.article_panel(imported, []))

    assert "Alex Reporter" in rendered
    assert imported["requested_url"] in rendered
    assert imported["final_url"] in rendered
    assert imported["feed_url"] in rendered
    assert "Publication date unavailable" in rendered


def test_import_results_are_also_visible_in_processing_activity():
    ui = importlib.import_module("evidence_explorer.ui")
    result = {
        "imported": 2,
        "updated": 1,
        "unchanged": 0,
        "analysis_job_id": 8,
        "errors": [{"url": "https://news.example.org/failed", "error": "HTTP 403"}],
    }

    rendered = _serialize(
        ui.job_cards(
            [
                {
                    "id": 7,
                    "kind": "ingest",
                    "status": "succeeded",
                    "stage": "complete",
                    "progress": 1,
                    "result": result,
                }
            ]
        )
    )

    assert "2 new" in rendered
    assert "1 updated" in rendered
    assert result["errors"][0]["url"] in rendered
    assert result["errors"][0]["error"] in rendered
    assert "#job-8" in rendered
    assert "job-7" in rendered


def test_keyword_results_refresh_after_new_chunks_without_replacing_watched_analysis(
    dashboard, article, monkeypatch
):
    from evidence_explorer.chunking import chunk_text

    service = dashboard.explorer_service
    imported = article.model_copy(
        update={
            "url": "https://news.example.org/harbor",
            "text": "The harbor authority announced a new harbor cleanup on Thursday. "
            "Officials published a public timetable for the harbor work.",
            "is_sample": False,
        }
    )
    article_id = service.repo.upsert_article(imported)
    service.repo.replace_chunks(article_id, chunk_text(imported.text))
    watched_id = service.repo.enqueue_job("analyze", {"mode": "ollama"})
    watched = {"id": watched_id, "kind": "analysis", "mode": "ollama"}
    monkeypatch.setattr(service, "status", lambda: {"worker": {"online": False}})
    output = next(key for key in dashboard.callback_map if key.startswith("..search-store.data"))

    response = dashboard.server.test_client().post(
        "/_dash-update-component",
        json={
            "output": output,
            "outputs": [
                {"id": "search-store", "property": "data"},
                {"id": "job-store", "property": "data"},
                {"id": "action-message", "property": "children"},
            ],
            "changedPropIds": ["poll.n_intervals"],
            "inputs": [
                {"id": "search-button", "property": "n_clicks", "value": 0},
                {"id": "search-input", "property": "n_submit", "value": 0},
                {"id": "analyze-button", "property": "n_clicks", "value": 0},
                {"id": "poll", "property": "n_intervals", "value": 1},
            ],
            "state": [
                {"id": "search-input", "property": "value", "value": "harbor"},
                {"id": "search-mode", "property": "value", "value": "keyword"},
                {"id": "analysis-mode", "property": "value", "value": "ollama"},
                {
                    "id": "search-store",
                    "property": "data",
                    "value": {"query": "harbor", "mode": "keyword", "matches": []},
                },
                {"id": "job-store", "property": "data", "value": watched},
            ],
        },
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    result = response.get_json()["response"]
    assert result["search-store"]["data"]["matches"][0]["article_id"] == article_id
    assert "job-store" not in result
    assert service.repo.get_job(watched_id)["status"] == "queued"


@pytest.mark.parametrize("new_button_clicks", [0, 1])
def test_new_imported_article_button_does_not_inherit_a_previous_buttons_click(
    dashboard, new_button_clicks
):
    articles = dashboard.explorer_service.repo.list_articles()
    selected_id, target_id = articles[0]["id"], articles[1]["id"]
    old_button = {"type": "open-article", "index": f"{selected_id}-0"}
    new_button = {"type": "open-article", "index": f"{target_id}-import"}
    trigger = json.dumps(new_button, sort_keys=True, separators=(",", ":"))
    response = dashboard.server.test_client().post(
        "/_dash-update-component",
        json={
            "output": "selection-store.data",
            "outputs": {"id": "selection-store", "property": "data"},
            "changedPropIds": [f"{trigger}.n_clicks"],
            "inputs": [
                [
                    {"id": old_button, "property": "n_clicks", "value": 1},
                    {"id": new_button, "property": "n_clicks", "value": new_button_clicks},
                ],
                {"id": "source-graph", "property": "tapNodeData", "value": None},
                {"id": "timeline", "property": "clickData", "value": None},
                {"id": "search-store", "property": "data", "value": {}},
            ],
            "state": [{"id": "selection-store", "property": "data", "value": selected_id}],
        },
    )
    assert response.status_code in {200, 204}, response.get_data(as_text=True)
    result = response.get_json()["response"] if response.status_code == 200 else {}
    if new_button_clicks:
        assert result["selection-store"]["data"] == target_id
    else:
        assert "selection-store" not in result
