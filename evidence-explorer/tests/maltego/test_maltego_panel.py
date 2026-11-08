"""The Live sources panel shows Maltego sign-in state and connects only on request."""

from types import SimpleNamespace

import pytest
from dash import Dash, html
from dash.development.base_component import Component

from evidence_explorer.collection.maltego.settings import MaltegoSettingsError
from evidence_explorer.collection.maltego.transport import ServerIdentity
from evidence_explorer.collection.models import CollectionFailure
from evidence_explorer.review.integrations import external_sources_ui as ui


def descendants(node):
    if isinstance(node, Component):
        yield node
        yield from descendants(getattr(node, "children", None))
    elif isinstance(node, list | tuple):
        for child in node:
            yield from descendants(child)


@pytest.fixture
def render(monkeypatch, settings):
    monkeypatch.setattr(ui, "maltego_settings", lambda: settings)
    app = Dash(__name__)
    app.layout = ui.external_source_tools()
    ui.register_external_source_callbacks(app)
    return next(
        entry["callback"].__wrapped__
        for key, entry in app.callback_map.items()
        if "live-maltego-result.children" in key
    )


def fixture_source(calls, outcome):
    class Source:
        def __init__(self, settings):
            calls.append(settings)

        def check(self):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    return Source


def test_the_panel_offers_commands_without_linking_to_the_endpoint():
    panel = ui.external_source_tools()
    codes = [node.children for node in descendants(panel) if isinstance(node, html.Code)]
    assert "./traced maltego login" in codes
    assert any(code.startswith("./traced maltego case") for code in codes)
    assert not any("datamcp" in str(getattr(node, "href", "")) for node in descendants(panel))


def test_signed_out_panel_explains_sign_in_and_never_connects(render, monkeypatch, settings):
    calls = []
    monkeypatch.setattr(ui, "MaltegoDataSource", fixture_source(calls, AssertionError()))
    for clicks in (0, 1):
        setup, result = render(clicks)
        assert result == ""
    assert calls == []
    assert settings.server_url in [
        node.children for node in descendants(setup) if isinstance(node, html.Span)
    ]
    assert "./traced maltego login" in setup[-1].children
    assert not any(isinstance(node, html.A) for node in descendants(setup))


def test_signed_in_panel_connects_only_when_asked(render, monkeypatch, signed_in):
    calls = []
    check = SimpleNamespace(
        server=ServerIdentity("2025-06-18", "Fixture Data MCP", "9.9", None), tools=(1, 2, 3)
    )
    monkeypatch.setattr(ui, "MaltegoDataSource", fixture_source(calls, check))
    _setup, result = render(0)
    assert result == "" and calls == []
    _setup, result = render(1)
    assert result.children.startswith("Connected to Fixture Data MCP 9.9 · 3 tools available.")
    assert len(calls) == 1


def test_connection_failures_keep_safe_copy_and_hide_unexpected_details(
    render, monkeypatch, signed_in
):
    failure = CollectionFailure("The Data MCP denied access (HTTP 403).")
    monkeypatch.setattr(ui, "MaltegoDataSource", fixture_source([], failure))
    assert render(1)[1].children == "The Data MCP denied access (HTTP 403)."
    unexpected = RuntimeError("private provider details")
    monkeypatch.setattr(ui, "MaltegoDataSource", fixture_source([], unexpected))
    assert "private" not in render(1)[1].children


def test_invalid_settings_are_reported_as_setup_problems(render, monkeypatch):
    def broken():
        raise MaltegoSettingsError("Set TRACED_MALTEGO_MCP_URL to the HTTPS Data MCP endpoint.")

    monkeypatch.setattr(ui, "maltego_settings", broken)
    setup, result = render(1)
    assert "TRACED_MALTEGO_MCP_URL" in setup.children and result == ""
