"""Saved-platform form behavior and credential-free queue boundaries."""

import importlib
import json
from dataclasses import replace

import pytest


@pytest.fixture
def options():
    return [
        {
            "platform": platform,
            "label": label,
            "help": f"Public {label} scope.",
            "ready": platform not in {"reddit", "x"},
            "reason": f"{label} server configuration needed"
            if platform in {"reddit", "x"}
            else "Public source configuration is available.",
            "credential": "secret-must-never-reach-the-browser",
        }
        for platform, label in [
            ("rss", "RSS / Atom"),
            ("telegram", "Telegram (public channel)"),
            ("youtube", "YouTube (channel)"),
            ("reddit", "Reddit (community)"),
            ("x", "X (account)"),
        ]
    ]


@pytest.fixture
def dashboard(settings, repo, monkeypatch, options):
    monkeypatch.setenv("EVIDENCE_DATA_DIR", str(settings.data_dir))
    app = importlib.import_module("evidence_explorer.web").create_app(settings)
    monkeypatch.setattr(app.explorer_service, "feed_source_options", lambda: options, raising=False)

    def no_network():
        pytest.fail("Platform form callbacks must not probe models or make network requests")

    monkeypatch.setattr(app.explorer_service, "status", no_network)
    return app


def _serialize(value):
    return json.dumps(value, default=lambda component: component.to_plotly_json())


def _components(value):
    result = []

    def walk(item):
        if isinstance(item, dict):
            if "props" in item:
                result.append(item["props"])
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return result


def _post(app, trigger, *, input_values=None, state_values=None):
    input_values, state_values = input_values or {}, state_values or {}
    key, callback = next(
        (key, callback)
        for key, callback in app.callback_map.items()
        if any(item["id"] == trigger for item in callback["inputs"])
    )
    outputs = callback["output"]
    output_payload = [
        {"id": output.component_id, "property": output.component_property} for output in outputs
    ]
    property_name = next(item["property"] for item in callback["inputs"] if item["id"] == trigger)
    response = app.server.test_client().post(
        "/_dash-update-component",
        json={
            "output": key,
            "outputs": output_payload,
            "changedPropIds": [f"{trigger}.{property_name}"],
            "inputs": [
                {**item, "value": input_values.get(item["id"])} for item in callback["inputs"]
            ],
            "state": [
                {**item, "value": state_values.get(item["id"])} for item in callback["state"]
            ],
        },
    )
    assert response.status_code in {200, 204}, response.get_data(as_text=True)
    return response.get_json()["response"] if response.status_code == 200 else {}


def _choose(app, platform):
    return _post(app, "saved-feed-platform", input_values={"saved-feed-platform": platform})


def _save(app, platform="telegram", locator="  @public_channel  "):
    return _post(
        app,
        "saved-platform-save",
        input_values={"saved-platform-save": 1},
        state_values={
            "saved-feed-platform": platform,
            "saved-feed-locator": locator,
            "saved-feed-name": "  Public source  ",
            "saved-feed-topic": "",
            "saved-feed-interval": 60,
            "saved-feed-limit": 5,
            "saved-feed-analyze": ["analyze"],
        },
    )


def test_provider_options_default_to_rss_without_serializing_credentials(dashboard):
    response = dashboard.server.test_client().get("/_dash-layout")
    assert response.status_code == 200
    rendered = response.get_data(as_text=True)
    props = {
        item.get("id"): item
        for item in _components(response.get_json())
        if isinstance(item.get("id"), str)
    }
    selector = props["saved-feed-platform"]
    assert selector["value"] == "rss"
    assert {option["value"] for option in selector["options"]} == {
        "rss",
        "telegram",
        "youtube",
        "reddit",
        "x",
    }
    assert "secret-must-never-reach-the-browser" not in rendered
    assert not any(item.get("type") == "password" for item in props.values())


@pytest.mark.parametrize(
    ("platform", "label"),
    [
        ("telegram", "Public channel"),
        ("youtube", "Channel ID or channel URL"),
        ("reddit", "Community"),
        ("x", "Account"),
    ],
)
def test_platform_selection_uses_locator_and_disables_rss_controls(dashboard, platform, label):
    result = _choose(dashboard, platform)
    assert result["saved-feed-url-field"]["style"] == {"display": "none"}
    assert result["saved-feed-url"]["disabled"] is True
    assert result["saved-feed-locator-field"]["style"] == {}
    assert result["saved-feed-locator-label"]["children"] == label
    assert result["saved-feed-content-mode"]["disabled"] is True
    assert result["saved-feed-mode-field"]["style"] == {"display": "none"}
    assert result["saved-feed-save"]["style"] == {"display": "none"}
    assert result["saved-platform-save"]["style"] == {}
    assert "secret-must-never-reach-the-browser" not in _serialize(result)


def test_returning_to_rss_preserves_the_existing_rss_content_choice(dashboard):
    result = _choose(dashboard, "rss")
    assert result["saved-feed-url-field"]["style"] == {}
    assert result["saved-feed-url"]["disabled"] is False
    assert result["saved-feed-locator"]["disabled"] is True
    assert result["saved-feed-locator-field"]["style"] == {"display": "none"}
    assert result["saved-feed-content-mode"] == {"disabled": False}
    assert result["saved-platform-save"]["style"] == {"display": "none"}


@pytest.mark.parametrize("platform", ["reddit", "x"])
def test_unconfigured_api_source_is_disabled_and_forged_save_cannot_reach_service(
    dashboard, monkeypatch, platform, options
):
    calls = []
    monkeypatch.setattr(
        dashboard.explorer_service, "save_feed", lambda **payload: calls.append(payload)
    )
    view = _choose(dashboard, platform)
    assert view["saved-platform-save"]["disabled"] is True
    reason = next(option["reason"] for option in options if option["platform"] == platform)
    rendered = _serialize(view["saved-feed-connection-status"])
    assert reason in rendered
    assert "Configuration does not confirm API access." in rendered
    assert "https://" in rendered

    saved = _save(dashboard, platform=platform, locator="public_account")

    assert calls == []
    assert "feeds-action-store" not in saved
    assert reason in _serialize(saved["saved-feeds-feedback"])
    assert dashboard.explorer_service.repo.stats()["jobs"] == 0


@pytest.mark.parametrize("platform", ["telegram", "youtube", "reddit", "x"])
def test_configured_source_save_uses_platform_locator_without_browser_credentials(
    dashboard, monkeypatch, options, platform
):
    for option in options:
        option["ready"] = True
    calls = []
    monkeypatch.setattr(
        dashboard.explorer_service,
        "save_feed",
        lambda **payload: calls.append(payload) or {"id": 7, "enabled": True},
    )

    saved = _save(dashboard, platform=platform, locator="  public-source  ")

    assert calls == [
        {
            "name": "Public source",
            "url": "",
            "platform": platform,
            "locator": "public-source",
            "topic": "General",
            "interval_minutes": 60,
            "item_limit": 5,
            "content_mode": "feed_content",
            "analyze": True,
        }
    ]
    assert saved["feeds-action-store"]["data"]["feed_id"] == 7
    assert set(saved) == {"feeds-action-store", "saved-feeds-feedback"}


def test_read_only_snapshot_disables_locator_and_blocks_forged_source_save(
    dashboard, monkeypatch, options
):
    app = importlib.import_module("evidence_explorer.web").create_app(
        replace(dashboard.explorer_service.settings, read_only=True)
    )
    monkeypatch.setattr(app.explorer_service, "feed_source_options", lambda: options)
    calls = []
    monkeypatch.setattr(app.explorer_service, "save_feed", lambda **payload: calls.append(payload))

    view = _choose(app, "telegram")
    saved = _save(app)

    assert view["saved-feed-locator"]["disabled"] is True
    assert view["saved-platform-save"]["disabled"] is True
    assert calls == []
    assert "feeds-action-store" not in saved
    assert app.explorer_service.repo.stats()["jobs"] == 0


@pytest.mark.parametrize("platform", ["rss", "unsupported"])
def test_platform_save_rejects_invalid_provider_without_using_rss_url(
    dashboard, monkeypatch, platform
):
    calls = []
    monkeypatch.setattr(
        dashboard.explorer_service, "save_feed", lambda **payload: calls.append(payload)
    )
    result = _save(dashboard, platform=platform)
    assert calls == []
    assert "feeds-action-store" not in result
    assert "saved-feeds-feedback" in result


def test_api_feed_card_shows_missing_configuration_and_disables_refresh():
    module = importlib.import_module("evidence_explorer.feeds_ui")
    row = {
        "id": 7,
        "name": "Public X source",
        "url": "https://x.com/public_account",
        "platform": "x",
        "locator": "public_account",
        "enabled": True,
        "ready": False,
        "readiness_reason": "X server credential is missing",
        "analyze": True,
    }

    rendered = json.loads(_serialize(module.feed_cards([row], False)))
    props = _components(rendered)
    refresh = next(
        item
        for item in props
        if isinstance(item.get("id"), dict) and item["id"].get("action") == "refresh"
    )
    pause = next(
        item
        for item in props
        if isinstance(item.get("id"), dict) and item["id"].get("action") == "pause"
    )
    assert refresh["disabled"] is True
    assert pause["disabled"] is False
    assert row["readiness_reason"] in json.dumps(rendered)


def test_public_telegram_source_saves_through_real_sqlite_without_collecting(dashboard):
    result = _save(dashboard)
    feed_id = result["feeds-action-store"]["data"]["feed_id"]
    row = next(row for row in dashboard.explorer_service.list_feeds() if row["id"] == feed_id)
    assert row["platform"] == "telegram"
    assert row["locator"] == "public_channel"
    assert row["url"].startswith("https://t.me/")
    assert row["enabled"] is True
    assert dashboard.explorer_service.repo.stats()["jobs"] == 0


def test_unknown_provider_stays_disabled_and_unsafe_source_urls_are_not_links(dashboard):
    view = _choose(dashboard, "unsupported")
    assert view["saved-platform-save"]["disabled"] is True
    assert view["saved-feed-locator"]["disabled"] is True
    assert view["saved-feed-save"]["style"] == {"display": "none"}
    module = importlib.import_module("evidence_explorer.feeds_ui")
    row = {
        "id": 7,
        "name": "Unknown source",
        "url": "javascript:alert(1)",
        "platform": "unsupported",
        "enabled": True,
        "ready": False,
    }

    props = _components(json.loads(_serialize(module.feed_cards([row], True))))

    assert not any(str(item.get("href", "")).startswith("javascript:") for item in props)
    buttons = [
        item
        for item in props
        if isinstance(item.get("id"), dict) and item["id"].get("type") == "saved-feed-action"
    ]
    assert buttons and all(item["disabled"] for item in buttons)
