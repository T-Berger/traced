"""Saved-feed form and callback regressions without source/model network access."""

import importlib
import json
from dataclasses import replace

import pytest


@pytest.fixture
def feed():
    return {
        "id": 7,
        "name": "Public updates",
        "url": "https://news.example.org/rss",
        "topic": "General",
        "interval_minutes": 60,
        "item_limit": 5,
        "content_mode": "article_links",
        "analyze": True,
        "enabled": True,
        "next_refresh_at": "2026-11-06T22:00:00+00:00",
        "last_refreshed_at": None,
        "last_result": None,
        "last_error": None,
        "last_analysis_job_id": None,
    }


@pytest.fixture
def dashboard(settings, repo, monkeypatch):
    monkeypatch.setenv("EVIDENCE_DATA_DIR", str(settings.data_dir))
    app = importlib.import_module("evidence_explorer.web").create_app(settings)
    rows = []
    monkeypatch.setattr(
        app.explorer_service,
        "list_feeds",
        lambda limit=30, offset=0: rows[offset : offset + limit],
        raising=False,
    )

    def no_network():
        pytest.fail("Saved-feed callbacks must not probe models or make network requests")

    monkeypatch.setattr(app.explorer_service, "status", no_network)
    return app


def _serialize(value):
    return json.dumps(value, default=lambda component: component.to_plotly_json())


def _components(value):
    result = []

    def visit(child):
        if isinstance(child, dict):
            if "props" in child:
                result.append(child["props"])
            for nested in child.values():
                visit(nested)
        elif isinstance(child, list):
            for nested in child:
                visit(nested)

    visit(value)
    return result


def _post_action(app, *, action=None, action_counts=None, **overrides):
    values = {
        "saved-feed-name": "  Public updates  ",
        "saved-feed-url": "https://news.example.org/rss",
        "saved-feed-topic": "",
        "saved-feed-interval": 60,
        "saved-feed-limit": 5,
        "saved-feed-content-mode": "article_links",
        "saved-feed-analyze": ["analyze"],
    }
    values.update(overrides)
    actions = action_counts if action_counts is not None else [(action, 1)] if action else []
    inputs = [
        {"id": identifier, "property": "n_clicks", "value": count} for identifier, count in actions
    ]
    trigger = (
        json.dumps(action, sort_keys=True, separators=(",", ":")) + ".n_clicks"
        if action
        else "saved-feed-save.n_clicks"
    )
    pattern = json.dumps(
        {"type": "saved-feed-action", "feed_id": ["ALL"], "action": ["ALL"]},
        sort_keys=True,
        separators=(",", ":"),
    )
    output = next(key for key in app.callback_map if key.startswith("..feeds-action-store.data"))
    response = app.server.test_client().post(
        "/_dash-update-component",
        json={
            "output": output,
            "outputs": [
                {"id": "feeds-action-store", "property": "data"},
                {"id": "saved-feeds-feedback", "property": "children"},
            ],
            "changedPropIds": [trigger],
            "inputs": [
                {"id": "saved-feed-save", "property": "n_clicks", "value": 0 if action else 1},
                inputs,
            ],
            "state": [
                *[
                    {"id": identifier, "property": "value", "value": value}
                    for identifier, value in values.items()
                ],
                {"id": pattern, "property": "id", "value": [item for item, _ in actions]},
            ],
        },
    )
    assert response.status_code in {200, 204}, response.get_data(as_text=True)
    return response.get_json()["response"] if response.status_code == 200 else {}


def _post_list(app, previous=""):
    output = next(key for key in app.callback_map if key.startswith("..feeds-list.children"))
    response = app.server.test_client().post(
        "/_dash-update-component",
        json={
            "output": output,
            "outputs": [
                {"id": "feeds-list", "property": "children"},
                {"id": "feeds-list-signature", "property": "data"},
                {"id": "saved-feed-scheduler-alert", "property": "children"},
            ],
            "changedPropIds": ["poll.n_intervals"],
            "inputs": [
                {"id": "poll", "property": "n_intervals", "value": 1},
                {"id": "feeds-action-store", "property": "data", "value": None},
            ],
            "state": [{"id": "feeds-list-signature", "property": "data", "value": previous}],
        },
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["response"]


def test_form_defaults_and_bounds(dashboard):
    response = dashboard.server.test_client().get("/_dash-layout")
    assert response.status_code == 200
    props = {
        item.get("id"): item
        for item in _components(response.get_json())
        if isinstance(item.get("id"), str)
    }
    assert props["saved-feed-interval"]["value"] == 60
    assert props["saved-feed-interval"]["min"] == 5
    assert props["saved-feed-interval"]["max"] == 10080
    assert props["saved-feed-limit"]["value"] == 5
    assert props["saved-feed-limit"]["min"] == 1
    assert props["saved-feed-limit"]["max"] == 20
    assert props["saved-feed-content-mode"]["value"] == "article_links"
    assert {item["value"] for item in props["saved-feed-content-mode"]["options"]} == {
        "article_links",
        "feed_content",
    }
    assert props["saved-feed-analyze"]["value"] == ["analyze"]


def test_save_uses_service_facade_without_touching_other_job_watchers(dashboard, monkeypatch, feed):
    calls = []

    def save(**options):
        calls.append(options)
        return feed

    monkeypatch.setattr(dashboard.explorer_service, "save_feed", save, raising=False)

    result = _post_action(dashboard)

    assert calls == [
        {
            "name": "Public updates",
            "url": feed["url"],
            "topic": "General",
            "interval_minutes": 60,
            "item_limit": 5,
            "content_mode": "article_links",
            "analyze": True,
        }
    ]
    assert result["feeds-action-store"]["data"]["feed_id"] == 7
    assert set(result) == {"feeds-action-store", "saved-feeds-feedback"}
    assert dashboard.explorer_service.repo.stats()["jobs"] == 0


def test_social_rss_mode_and_analysis_opt_out_are_preserved(dashboard, monkeypatch, feed):
    calls = []
    monkeypatch.setattr(
        dashboard.explorer_service,
        "save_feed",
        lambda **options: calls.append(options) or feed,
        raising=False,
    )

    _post_action(
        dashboard,
        **{
            "saved-feed-content-mode": "feed_content",
            "saved-feed-limit": 20,
            "saved-feed-interval": 5,
            "saved-feed-topic": "  Public posts  ",
            "saved-feed-analyze": [],
        },
    )

    assert calls[0]["content_mode"] == "feed_content"
    assert calls[0]["analyze"] is False
    assert calls[0]["topic"] == "Public posts"
    assert calls[0]["item_limit"] == 20
    assert calls[0]["interval_minutes"] == 5


@pytest.mark.parametrize("action", ["refresh", "pause", "resume"])
def test_feed_actions_call_the_correct_facade(dashboard, monkeypatch, feed, action):
    calls = []
    monkeypatch.setattr(
        dashboard.explorer_service,
        "enqueue_feed_refresh",
        lambda feed_id: calls.append(("refresh", feed_id)) or 42,
        raising=False,
    )
    monkeypatch.setattr(
        dashboard.explorer_service,
        "set_feed_enabled",
        lambda feed_id, enabled: calls.append(("enabled", feed_id, enabled)) or feed,
        raising=False,
    )

    result = _post_action(
        dashboard, action={"type": "saved-feed-action", "feed_id": 7, "action": action}
    )

    expected = [("refresh", 7)] if action == "refresh" else [("enabled", 7, action == "resume")]
    assert calls == expected
    assert result["feeds-action-store"]["data"]["feed_id"] == 7
    assert "job-store" not in result
    if action == "refresh":
        assert "#job-42" in _serialize(result["saved-feeds-feedback"])


@pytest.mark.parametrize("action", [None, "refresh", "pause", "resume"])
def test_read_only_form_and_forged_actions_cannot_write(dashboard, monkeypatch, action):
    app = importlib.import_module("evidence_explorer.web").create_app(
        replace(dashboard.explorer_service.settings, read_only=True)
    )
    service = app.explorer_service
    calls = []
    monkeypatch.setattr(service, "list_feeds", lambda **_: [], raising=False)
    monkeypatch.setattr(service, "save_feed", lambda **_: calls.append("save"), raising=False)
    monkeypatch.setattr(
        service, "enqueue_feed_refresh", lambda *_: calls.append("refresh"), raising=False
    )
    monkeypatch.setattr(
        service, "set_feed_enabled", lambda *_: calls.append("enabled"), raising=False
    )

    result = _post_action(
        app,
        action={"type": "saved-feed-action", "feed_id": 7, "action": action} if action else None,
    )

    assert calls == []
    assert service.repo.stats()["jobs"] == 0
    assert "feeds-action-store" not in result
    assert "saved-feeds-feedback" in result
    props = {
        item.get("id"): item
        for item in _components(app.server.test_client().get("/_dash-layout").get_json())
        if isinstance(item.get("id"), str)
    }
    assert props["saved-feed-save"]["disabled"] is True
    assert props["saved-feed-name"]["disabled"] is True
    assert props["saved-feed-url"]["disabled"] is True
    assert props["saved-feed-analyze"]["options"][0]["disabled"] is True


def test_new_zero_click_feed_buttons_do_not_repeat_an_old_action(dashboard, monkeypatch):
    calls = []
    monkeypatch.setattr(
        dashboard.explorer_service,
        "set_feed_enabled",
        lambda *args: calls.append(args),
        raising=False,
    )
    previous = {"type": "saved-feed-action", "feed_id": 7, "action": "refresh"}
    new = {"type": "saved-feed-action", "feed_id": 7, "action": "pause"}

    result = _post_action(dashboard, action=new, action_counts=[(previous, 1), (new, 0)])

    assert calls == []
    assert result == {}


def test_service_validation_errors_are_visible_without_changing_feed_state(dashboard, monkeypatch):
    def reject(**_):
        raise ValueError("Refresh interval must be at least 5 minutes.")

    monkeypatch.setattr(dashboard.explorer_service, "save_feed", reject, raising=False)
    result = _post_action(dashboard, **{"saved-feed-interval": 1})
    assert "feeds-action-store" not in result
    assert "Refresh interval must be at least 5 minutes." in _serialize(
        result["saved-feeds-feedback"]
    )


def test_bounded_feed_cards_show_schedule_errors_results_and_analysis_link(feed):
    module = importlib.import_module("evidence_explorer.feeds_ui")
    row = {
        **feed,
        "last_refreshed_at": "2026-11-06T21:00:00Z",
        "last_analysis_job_id": 42,
        "last_error": "One source returned HTTP 403",
        "last_result": {
            "imported": 2,
            "updated": 1,
            "unchanged": 0,
            "errors": [{"url": "https://news.example.org/blocked", "error": "HTTP 403"}],
        },
    }
    rows = [{**row, "id": index, "name": f"Feed {index}"} for index in range(31)]

    rendered = _serialize(module.feed_cards(rows, False))

    assert "Feed 29" in rendered
    assert "Feed 30" not in rendered
    assert "2 new" in rendered and "1 updated" in rendered
    assert row["last_error"] in rendered
    assert row["last_result"]["errors"][0]["url"] in rendered
    assert "#job-42" in rendered
    assert "UTC" in rendered


def test_read_only_feed_rows_disable_mutation_buttons(feed):
    module = importlib.import_module("evidence_explorer.feeds_ui")
    props = _components(json.loads(_serialize(module.feed_cards([feed], True))))
    buttons = [
        item
        for item in props
        if isinstance(item.get("id"), dict) and item["id"].get("type") == "saved-feed-action"
    ]
    assert len(buttons) == 2
    assert all(item["disabled"] for item in buttons)


def test_scheduler_errors_are_visible_without_redrawing_an_unchanged_feed_list(
    dashboard, monkeypatch
):
    state = {"last_tick": None, "error": None}
    monkeypatch.setattr(dashboard.explorer_service, "feed_scheduler_status", lambda: state)
    first = _post_list(dashboard)
    assert first["saved-feed-scheduler-alert"]["children"] == ""
    signature = first["feeds-list-signature"]["data"]

    state["error"] = "Database write unavailable during scheduler tick"
    second = _post_list(dashboard, previous=signature)

    assert "feeds-list" not in second
    assert state["error"] in _serialize(second["saved-feed-scheduler-alert"])


def test_latest_local_analysis_failure_is_visible_on_the_feed_card(feed):
    module = importlib.import_module("evidence_explorer.feeds_ui")
    row = {
        **feed,
        "last_analysis_job_id": 42,
        "last_analysis_status": "failed",
        "last_analysis_error": "Requested local chat model is not installed",
    }

    rendered = _serialize(module.feed_cards([row], False))

    assert "Failed" in rendered
    assert row["last_analysis_error"] in rendered
    assert "#job-42" in rendered


def test_skipped_scheduled_refresh_shows_its_reason_instead_of_zero_import_counts(feed):
    module = importlib.import_module("evidence_explorer.feeds_ui")
    row = {
        **feed,
        "enabled": False,
        "last_result": {"skipped": True, "skip_reason": "Saved feed is paused."},
    }

    rendered = _serialize(module.feed_cards([row], False))

    assert "Saved feed is paused." in rendered
    assert "Last import:" not in rendered
    assert "0 new" not in rendered


def test_real_sqlite_callbacks_save_list_pause_and_manually_refresh_without_network(
    dashboard, monkeypatch
):
    service = dashboard.explorer_service
    monkeypatch.setattr(service, "list_feeds", type(service).list_feeds.__get__(service))

    saved = _post_action(dashboard, **{"saved-feed-name": "SQLite RSS integration"})
    feed_id = saved["feeds-action-store"]["data"]["feed_id"]
    rows = service.list_feeds()
    row = next(row for row in rows if row["id"] == feed_id)
    assert row["enabled"] is True
    assert row["next_refresh_at"]
    assert service.repo.stats()["jobs"] == 0
    listing = _post_list(dashboard)
    assert row["name"] in _serialize(listing["feeds-list"])

    _post_action(
        dashboard, action={"type": "saved-feed-action", "feed_id": feed_id, "action": "pause"}
    )
    assert service.list_feeds()[0]["enabled"] is False
    refreshed = _post_action(
        dashboard, action={"type": "saved-feed-action", "feed_id": feed_id, "action": "refresh"}
    )

    jobs = service.repo.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["kind"] == "ingest"
    assert jobs[0]["status"] == "queued"
    assert jobs[0]["payload"]["feed_id"] == feed_id
    assert jobs[0]["payload"]["scheduled"] is False
    assert "job-store" not in refreshed
    assert "ingestion-store" not in refreshed
