"""Ordinary startup/import never substitutes fictional sources for an empty store."""

import json
import sys

import pytest
from dash import Dash

from evidence_explorer.review.integrations import __main__ as entrypoint
from evidence_explorer.review.integrations import web
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService


@pytest.fixture
def workspace(settings, repo):
    reviews = WorkspaceRepository(settings.db_path)
    reviews.initialize()
    return WorkspaceService(reviews, settings, settings.data_dir / "trail-artifacts")


@pytest.fixture
def run_cli(monkeypatch, settings):
    monkeypatch.setattr(entrypoint.Settings, "from_env", lambda: settings)

    def run(*arguments):
        monkeypatch.setattr(sys, "argv", ["evidence-trail", *arguments])
        entrypoint.main()

    return run


def test_empty_explorer_import_is_actionable_and_leaves_evidence_empty(workspace, repo):
    with pytest.raises(ValueError, match="No saved Explorer articles") as error:
        workspace.import_explorer()

    assert "Import or collect articles in Explorer" in str(error.value)
    assert "explicit seed command" in str(error.value)
    assert repo.articles.count() == 0
    assert repo.get_meta("demo_seed_version") is None
    assert workspace.list_cases() == []
    assert repo.jobs.list_jobs() == []


def test_empty_cli_import_reports_the_next_action_without_a_traceback(
    run_cli, workspace, repo, capsys
):
    with pytest.raises(SystemExit) as error:
        run_cli("explorer")

    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "No saved Explorer articles" in captured.err
    assert "Traceback" not in captured.err
    assert not captured.out
    assert repo.articles.count() == 0
    assert workspace.list_cases() == []


@pytest.mark.parametrize("has_saved_article", [False, True], ids=["empty", "saved-articles"])
def test_web_startup_preserves_source_store_and_opens_without_creating_cases(
    run_cli, workspace, repo, article, monkeypatch, has_saved_article
):
    if has_saved_article:
        repo.upsert_article(article)
    before = repo.articles.list_articles()
    opened = []

    def open_locally(app, **_kwargs):
        client = app.server.test_client()
        assert client.get("/").status_code == 200
        layout = client.get("/_dash-layout")
        assert layout.status_code == 200
        assert "trail-upload" in layout.get_data(as_text=True)
        opened.append(app)

    monkeypatch.setattr(Dash, "run", open_locally)
    run_cli("web")
    run_cli("web")  # Restarting is equally non-mutating.

    assert len(opened) == 2
    assert workspace.list_cases() == []
    assert repo.articles.list_articles() == before
    assert repo.get_meta("demo_seed_version") is None
    assert repo.jobs.list_jobs() == []


def test_empty_import_button_surfaces_the_error_without_creating_a_case(workspace, repo):
    app = web.create_app(workspace)
    output = next(key for key in app.callback_map if "trail-import-message.children" in key)
    response = app.server.test_client().post(
        "/_dash-update-component",
        json={
            "output": output,
            "outputs": [
                {"id": "rv-case", "property": "options"},
                {"id": "rv-case", "property": "value"},
                {"id": "trail-import-message", "property": "children"},
            ],
            "inputs": [
                {"id": "trail-upload", "property": "contents", "value": None},
                {"id": "trail-explorer", "property": "n_clicks", "value": 1},
            ],
            "state": [],
            "changedPropIds": ["trail-explorer.n_clicks"],
        },
    )

    assert response.status_code == 200
    assert "No saved Explorer articles" in response.get_data(as_text=True)
    assert "Import or collect articles in Explorer" in response.get_data(as_text=True)
    assert repo.articles.count() == 0
    assert workspace.list_cases() == []


@pytest.mark.parametrize("has_saved_article", [False, True], ids=["empty", "saved-articles"])
def test_explicit_seed_still_works_and_preserves_existing_articles(
    run_cli, workspace, repo, article, capsys, has_saved_article
):
    if has_saved_article:
        repo.upsert_article(article)
    original_articles = repo.articles.list_articles()

    run_cli("seed", "--without-swarm")
    assert len(json.loads(capsys.readouterr().out)) == 3
    saved_articles = repo.articles.list_articles()
    fingerprints = {
        case["id"]: workspace.repository.get_bundle(case["id"]).fingerprint()
        for case in workspace.list_cases()
    }
    assert saved_articles
    if has_saved_article:
        assert saved_articles == original_articles
        assert repo.get_meta("demo_seed_version") is None
    else:
        assert all(article.is_sample for article in saved_articles)
        assert repo.get_meta("demo_seed_version") == "1"

    run_cli("seed", "--without-swarm")
    assert len(json.loads(capsys.readouterr().out)) == 3
    assert repo.articles.list_articles() == saved_articles
    assert {
        case["id"]: workspace.repository.get_bundle(case["id"]).fingerprint()
        for case in workspace.list_cases()
    } == fingerprints
    assert repo.jobs.list_jobs() == []
