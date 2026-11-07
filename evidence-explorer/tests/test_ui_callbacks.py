"""Regression checks for queued searches, article selection, and presentation mode.

These exercise Dash's HTTP callback protocol so pattern IDs and read-only guards
are checked at the same boundary used by the browser, without a model server.
"""

import importlib
import json
from dataclasses import replace

import pytest


@pytest.fixture
def dashboard(settings, repo, monkeypatch):
    monkeypatch.setenv("EVIDENCE_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("OLLAMA_BASE_URL", settings.ollama_url)
    web = importlib.import_module("evidence_explorer.web")
    app = web.create_app(settings)
    monkeypatch.setattr(
        app.explorer_service,
        "status",
        lambda: {"worker": {"online": False}, "ollama": {"available": False}},
    )
    return app


@pytest.fixture
def completed_search(dashboard):
    repo = dashboard.explorer_service.repo
    matches = repo.keyword_search("bridge")
    assert matches
    job_id = repo.enqueue_job("search", {"query": "bridge", "mode": "demo"})
    repo.update_job(job_id, status="succeeded", progress=1, result={"matches": matches})
    return {"id": job_id, "kind": "search", "query": "bridge", "mode": "demo"}


def _post_action(dashboard, trigger, *, query="bridge", mode="demo", active_job=None):
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
            "changedPropIds": [trigger],
            "inputs": [
                {"id": "search-button", "property": "n_clicks", "value": 1},
                {"id": "search-input", "property": "n_submit", "value": 0},
                {"id": "analyze-button", "property": "n_clicks", "value": 1},
                {"id": "poll", "property": "n_intervals", "value": 1},
            ],
            "state": [
                {"id": "search-input", "property": "value", "value": query},
                {"id": "search-mode", "property": "value", "value": mode},
                {"id": "analysis-mode", "property": "value", "value": "demo"},
                {"id": "search-store", "property": "data", "value": {}},
                {"id": "job-store", "property": "data", "value": active_job},
            ],
        },
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["response"]


@pytest.mark.parametrize(
    ("trigger", "mode"),
    [
        ("search-button.n_clicks", "demo"),
        ("search-button.n_clicks", "ollama"),
        ("analyze-button.n_clicks", "demo"),
    ],
)
def test_forged_read_only_actions_never_create_jobs(dashboard, trigger, mode):
    web = importlib.import_module("evidence_explorer.web")
    settings = replace(dashboard.explorer_service.settings, read_only=True)
    read_only_app = web.create_app(settings)
    repo = dashboard.explorer_service.repo
    before = repo.stats()["jobs"]

    result = _post_action(read_only_app, trigger, mode=mode)

    assert repo.stats()["jobs"] == before
    assert "job-store" not in result
    assert "search-store" not in result
    assert "action-message" in result


def test_read_only_presentation_can_still_search_keywords(dashboard):
    web = importlib.import_module("evidence_explorer.web")
    read_only_app = web.create_app(replace(dashboard.explorer_service.settings, read_only=True))

    result = _post_action(read_only_app, "search-button.n_clicks", query="airport", mode="keyword")

    search = result["search-store"]["data"]
    assert search["query"] == "airport"
    assert search["mode"] == "keyword"
    assert search["matches"]
    assert read_only_app.explorer_service.repo.stats()["jobs"] == 0


@pytest.mark.parametrize(
    ("query", "mode"),
    [("airport", "demo"), ("bridge", "keyword"), ("bridge", "ollama"), ("airport", "keyword")],
)
def test_old_completed_search_cannot_replace_changed_query_or_mode(
    dashboard, completed_search, query, mode
):
    result = _post_action(
        dashboard, "poll.n_intervals", query=query, mode=mode, active_job=completed_search
    )

    assert "search-store" not in result
    assert result["job-store"]["data"]["finished"] is True


def test_successful_same_query_poll_displays_the_completed_matches(dashboard, completed_search):
    result = _post_action(dashboard, "poll.n_intervals", active_job=completed_search)

    expected = dashboard.explorer_service.repo.get_job(completed_search["id"])["result"]["matches"]
    search = result["search-store"]["data"]
    assert search == {"query": "bridge", "mode": "demo", "matches": expected}
    assert result["job-store"]["data"]["finished"] is True


def test_new_keyword_search_stops_watching_previous_search(dashboard, completed_search):
    result = _post_action(
        dashboard,
        "search-button.n_clicks",
        query="airport",
        mode="keyword",
        active_job=completed_search,
    )

    assert result["search-store"]["data"]["query"] == "airport"
    assert result["search-store"]["data"]["matches"]
    assert result["job-store"]["data"]["finished"] is True


@pytest.mark.parametrize("clicks", [0, 1])
def test_pattern_article_button_selects_only_after_a_click(dashboard, clicks):
    articles = dashboard.explorer_service.repo.list_articles()
    selected_id, target_id = articles[0]["id"], articles[1]["id"]
    button_id = json.dumps(
        {"type": "open-article", "index": f"{target_id}-0"}, sort_keys=True, separators=(",", ":")
    )
    pattern = json.dumps(
        {"type": "open-article", "index": ["ALL"]}, sort_keys=True, separators=(",", ":")
    )
    response = dashboard.server.test_client().post(
        "/_dash-update-component",
        json={
            "output": "selection-store.data",
            "outputs": {"id": "selection-store", "property": "data"},
            "changedPropIds": [f"{button_id}.n_clicks"],
            "inputs": [
                {"id": pattern, "property": "n_clicks", "value": [clicks]},
                {"id": "source-graph", "property": "tapNodeData", "value": None},
                {"id": "timeline", "property": "clickData", "value": None},
                {"id": "search-store", "property": "data", "value": {}},
            ],
            "state": [{"id": "selection-store", "property": "data", "value": selected_id}],
        },
    )

    if clicks:
        assert response.status_code == 200, response.get_data(as_text=True)
        assert response.get_json()["response"]["selection-store"]["data"] == target_id
    else:
        assert response.status_code in {200, 204}
        if response.status_code == 200:
            assert "selection-store" not in response.get_json()["response"]
