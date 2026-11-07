"""Live controls queue work, show truthful status, and open saved evidence directly."""

from types import SimpleNamespace

import pytest
from dash import Dash, dcc, html, no_update
from dash.development.base_component import Component
from dash.exceptions import PreventUpdate

from evidence_explorer.collection.models import CollectedBatch
from evidence_explorer.config import Settings
from evidence_explorer.review.integrations import collection_ui
from evidence_explorer.review.integrations.sources import SourceItem
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService


def components(node):
    if isinstance(node, Component):
        yield node
        yield from components(getattr(node, "children", None))
    elif isinstance(node, (list, tuple)):
        for child in node:
            yield from components(child)


def text(node):
    if isinstance(node, str):
        return node
    if isinstance(node, (list, tuple)):
        return " ".join(text(child) for child in node)
    return text(getattr(node, "children", ""))


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("REDDIT_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    app = Dash(__name__, suppress_callback_exceptions=True)
    app.layout = collection_ui.collection_tools
    collection_ui.register_collection_callbacks(app, service)

    def callback(output):
        return next(
            item["callback"].__wrapped__ for key, item in app.callback_map.items() if output in key
        )

    return service, app, callback


def add(callback, *, platform="news", locator="https://publisher.example/rss", scheduled=None):
    return callback("live-action.children")(
        1,
        0,
        [],
        [],
        platform,
        "A saved public source",
        locator,
        15,
        5,
        "feed_content",
        scheduled or [],
        0,
    )


def item():
    return SourceItem(
        platform="news",
        channel_id="publisher.example",
        channel_name="A saved public source",
        native_id="post-1",
        text="A supplied article reports that the bridge inspection has started.",
        source_url="https://publisher.example/post-1",
        published_at=None,
        acquired_at="2026-11-06T12:00:00Z",
    )


def test_layout_has_modular_controls_and_does_not_enable_scheduling_by_default(desk):
    _, app, callback = desk
    nodes = list(components(app.layout()))
    ids = [node.id for node in nodes if hasattr(node, "id")]
    assert len(ids) == len(set(ids))
    assert "Live sources" in text(app.layout())
    assert {node.id for node in nodes if isinstance(node, dcc.Store)} == {
        "live-revision",
        "live-case-token",
    }
    scheduled = next(node for node in nodes if getattr(node, "id", None) == "live-scheduled")
    assert scheduled.value == []
    polling = next(node for node in nodes if getattr(node, "id", None) == "live-poll")
    assert polling.disabled is True
    summary, _, disabled, _, collect_disabled, open_disabled = callback("live-summary.children")(
        0, 0
    )
    assert "0 saved sources" in text(summary)
    assert disabled and collect_disabled and open_disabled


def test_catalog_prefills_source_form_without_creating_a_subscription(desk):
    service, _, callback = desk
    options = callback("live-preset.options")(0)
    assert len(options) >= 15
    assert any("setup needed" in option["label"] for option in options)
    platform, label, locator, mode, status = callback("live-preset-status.children")(
        "news-bbc-world"
    )
    assert platform == "news" and label == "BBC World"
    assert locator.startswith("https://")
    assert mode == "feed_content"
    assert "not checked" in text(status).lower()
    assert service.list_collection_sources() == []


def test_preset_selection_restores_its_content_mode_instead_of_the_previous_mode(desk, monkeypatch):
    service, _, callback = desk
    monkeypatch.setattr(
        service,
        "collection_catalog",
        lambda: [
            {
                "id": "other-hn",
                "platform": "other",
                "label": "Hacker News",
                "locator": "https://news.ycombinator.com/rss",
                "ready": True,
                "status": "Public source · not checked yet",
                "content_mode": "article_links",
            },
            {
                "id": "feed-default",
                "platform": "news",
                "label": "A news feed",
                "locator": "https://publisher.example/rss",
                "ready": True,
                "status": "Public source · not checked yet",
            },
        ],
    )
    choose = callback("live-preset-status.children")
    assert choose("other-hn")[3] == "article_links"
    assert choose("feed-default")[3] == "feed_content"


def test_saved_feed_ownership_disables_duplicate_controls_and_rejects_resume(desk, monkeypatch):
    service, _, callback = desk
    source = {
        "id": "managed-source",
        "platform": "news",
        "label": "A source managed elsewhere",
        "locator": "https://publisher.example/rss",
        "enabled": False,
        "ready": False,
        "managed_by": "saved_feed",
        "connection_status": "This source is already managed in Saved feeds.",
    }
    card = collection_ui.source_card(source)
    controls = [
        node
        for node in components(card)
        if isinstance(getattr(node, "id", None), dict)
        and node.id["type"] in {"live-collect", "live-toggle"}
    ]
    assert len(controls) == 2 and all(control.disabled for control in controls)
    assert "already managed in Saved feeds" in text(card)
    assert "Collect now remains available" not in text(card)
    monkeypatch.setattr(service, "list_collection_sources", lambda: [source])
    monkeypatch.setattr(
        service,
        "set_collection_enabled",
        lambda *_: pytest.fail("The duplicate feed schedule must not be changed"),
    )
    monkeypatch.setattr(
        collection_ui,
        "ctx",
        SimpleNamespace(triggered_id={"type": "live-toggle", "sid": source["id"]}),
    )
    revision, message = callback("live-action.children")(
        0, 0, [], [1], "news", "", "", 15, 5, "feed_content", [], 0
    )
    assert revision is no_update and "managed in Saved feeds" in text(message)


def test_add_queues_background_collection_and_scheduling_requires_explicit_selection(
    desk, monkeypatch
):
    service, _, callback = desk
    monkeypatch.setattr(collection_ui, "ctx", SimpleNamespace(triggered_id="live-add"))
    revision, message = add(callback)
    assert revision == 1 and "queued" in text(message)
    saved = service.list_collection_sources()
    assert len(saved) == 1 and saved[0]["enabled"] is False
    assert saved[0]["last_status"] == "queued"
    assert service.collection_status()["jobs"][0]["status"] == "queued"
    summary, cards, disabled, interval, collect_disabled, open_disabled = callback(
        "live-summary.children"
    )(1, 0)
    assert "in progress" in text(summary)
    assert disabled is False and interval == 2500 and collect_disabled and not open_disabled
    assert "Waiting for collection" in text(cards)
    assert not service.list_cases()

    add(callback, locator="https://other-publisher.example/rss", scheduled=["scheduled"])
    assert service.list_collection_sources()[1]["enabled"] is True


def test_missing_credentials_save_setup_source_without_queueing_or_leaking_credentials(
    desk, monkeypatch
):
    service, _, callback = desk
    monkeypatch.setattr(collection_ui, "ctx", SimpleNamespace(triggered_id="live-add"))
    _, message = add(callback, platform="x", locator="NASA")
    assert "Complete connection setup" in text(message)
    assert service.collection_status()["jobs"] == []
    source = service.list_collection_sources()[0]
    assert source["ready"] is False
    card = collection_ui.source_card(source)
    collect = next(
        node
        for node in components(card)
        if isinstance(getattr(node, "id", None), dict) and node.id["type"] == "live-collect"
    )
    assert collect.disabled is True
    links = [node for node in components(card) if isinstance(node, html.A)]
    assert any(link.href == "https://console.x.com" for link in links)
    assert not any(isinstance(node, dcc.Input) for node in components(card))
    assert "X_BEARER_TOKEN" not in text(card)


def test_saved_collection_opens_source_chooser_without_creating_a_case(desk, monkeypatch):
    service, _, callback = desk
    monkeypatch.setattr(collection_ui, "ctx", SimpleNamespace(triggered_id="live-add"))
    add(callback)
    repository = service.collection.repository
    claimed = repository.claim()
    repository.complete(claimed, CollectedBatch(items=(item(),)))
    _, cards, disabled, _, collect_disabled, open_disabled = callback("live-summary.children")(1, 1)
    assert disabled is True and collect_disabled is False and open_disabled is False
    assert "Collected" in text(cards) and "1 saved records" in text(cards)
    opened, message = callback("live-open-message.children")(1)
    assert opened is True and "preview the sources" in text(message)
    assert not service.list_cases()
    assert repository.items()[0]["text"] == item().text


def test_pause_resume_uses_saved_state_and_does_not_launch_a_network_request(desk, monkeypatch):
    service, _, callback = desk
    monkeypatch.setattr(collection_ui, "ctx", SimpleNamespace(triggered_id="live-add"))
    add(callback)
    source_id = service.list_collection_sources()[0]["id"]
    monkeypatch.setattr(
        collection_ui,
        "ctx",
        SimpleNamespace(triggered_id={"type": "live-toggle", "sid": source_id}),
    )
    act = callback("live-action.children")
    result = act(0, 0, [], [1], "news", "", "", 15, 5, "feed_content", [], 1)
    assert result[0] == 2 and "enabled" in text(result[1])
    assert service.list_collection_sources()[0]["enabled"] is True
    result = act(0, 0, [], [2], "news", "", "", 15, 5, "feed_content", [], 2)
    assert "paused" in text(result[1])
    assert service.list_collection_sources()[0]["enabled"] is False
    assert len(service.collection_status()["jobs"]) == 1


def test_status_failure_uses_safe_message_and_empty_chooser_does_not_create_case(desk, monkeypatch):
    service, _, callback = desk
    result = callback("live-open-message.children")(1)
    assert result[0] is True and "Choose and preview" in text(result[1])
    assert not service.list_cases()

    def unavailable():
        raise RuntimeError("Authorization: Bearer should-never-reach-the-browser")

    monkeypatch.setattr(service, "collection_status", unavailable)
    summary, _, disabled, _, collect_disabled, open_disabled = callback("live-summary.children")(
        0, 0
    )
    assert "unavailable" in text(summary)
    assert "Bearer" not in text(summary)
    assert disabled and collect_disabled and open_disabled
    monkeypatch.setattr(service, "add_collection_source", lambda _: unavailable())
    monkeypatch.setattr(collection_ui, "ctx", SimpleNamespace(triggered_id="live-add"))
    revision, message = add(callback)
    assert revision is no_update
    assert "could not be completed" in text(message) and "Bearer" not in text(message)


def test_dynamic_control_insertion_does_not_trigger_collection(desk, monkeypatch):
    service, _, callback = desk
    monkeypatch.setattr(
        collection_ui,
        "ctx",
        SimpleNamespace(triggered_id={"type": "live-collect", "sid": "absent"}),
    )
    with pytest.raises(PreventUpdate):
        callback("live-action.children")(
            0, 0, [0], [], "news", "", "", 15, 5, "feed_content", [], 0
        )
    assert service.collection_status()["jobs"] == []
