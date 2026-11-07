"""The external lookup actions require an explicit click and leave local cases alone."""

from datetime import date
from types import SimpleNamespace

import pytest
from dash import Dash, html
from dash.development.base_component import Component

from evidence_explorer.collection.aleph import AlephConfiguration, AlephConnectionResult
from evidence_explorer.collection.models import CollectionFailure
from evidence_explorer.collection.wayback import WaybackLookup
from evidence_explorer.review.integrations import external_sources_ui as ui

PAGE = "https://example.org/public-article"
ARCHIVE = f"https://web.archive.org/web/20240102030405/{PAGE}"
CAPTURED = "2024-01-02T03:04:05+00:00"
CHECKED = "2026-11-07T14:00:00+00:00"
ALEPH = "https://aleph.occrp.org"


def descendants(node):
    if isinstance(node, Component):
        yield node
        yield from descendants(getattr(node, "children", None))
    elif isinstance(node, (list, tuple)):
        for child in node:
            yield from descendants(child)


@pytest.fixture
def callback():
    app = Dash(__name__)
    app.layout = ui.external_source_tools()
    ui.register_external_source_callbacks(app)

    def find(output):
        return next(
            entry["callback"].__wrapped__
            for key, entry in app.callback_map.items()
            if output in key
        )

    return find


def test_wayback_lookup_uses_requested_date_and_keeps_capture_and_check_times_distinct(
    callback, monkeypatch
):
    calls = []

    def lookup(url, requested_date):
        calls.append((url, requested_date))
        return WaybackLookup(PAGE, ARCHIVE, CAPTURED, CHECKED)

    monkeypatch.setattr(ui, "lookup_wayback", lookup)
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id="live-wayback-lookup"))
    render = callback("live-wayback-result.children")
    result = render(1, f" {PAGE} ", "2024-01-01")
    nodes = list(descendants(result))
    assert calls == [(PAGE, date(2024, 1, 1))]
    assert [node.dateTime for node in nodes if isinstance(node, html.Time)] == [CAPTURED, CHECKED]
    link = next(node for node in nodes if isinstance(node, html.A))
    assert (link.href, link.target, link.rel) == (ARCHIVE, "_blank", "noopener noreferrer")

    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id="live-wayback-date"))
    assert render(1, PAGE, "2024-01-02") == ""
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id="live-wayback-url"))
    assert render(1, "https://example.org/another-page", "2024-01-01") == ""
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["20240101", "2024-W01-1", "2024-02-30", "01/01/2024"])
def test_invalid_wayback_date_cannot_make_a_request(callback, monkeypatch, value):
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id="live-wayback-lookup"))
    monkeypatch.setattr(ui, "lookup_wayback", lambda *_: pytest.fail("Unexpected archive request"))
    result = callback("live-wayback-result.children")(1, PAGE, value)
    assert "YYYY-MM-DD" in result.children


def test_wayback_no_capture_keeps_check_time_and_has_no_archive_link(callback, monkeypatch):
    def no_capture(url, requested_date):
        assert url == PAGE and requested_date is None
        return WaybackLookup(PAGE, None, None, CHECKED)

    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id="live-wayback-lookup"))
    monkeypatch.setattr(ui, "lookup_wayback", no_capture)
    result = callback("live-wayback-result.children")(1, PAGE, "")
    nodes = list(descendants(result))
    assert result.children[0].children == "No archived capture was found."
    assert [node.dateTime for node in nodes if isinstance(node, html.Time)] == [CHECKED]
    assert not any(isinstance(node, html.A) for node in nodes)


def test_wayback_safe_failures_and_unexpected_errors_have_separate_copy(callback, monkeypatch):
    monkeypatch.setattr(ui, "ctx", SimpleNamespace(triggered_id="live-wayback-lookup"))

    def fail(*_):
        raise CollectionFailure("Wayback returned HTTP 429.")

    monkeypatch.setattr(ui, "lookup_wayback", fail)
    render = callback("live-wayback-result.children")
    assert render(1, PAGE, "").children == "Wayback returned HTTP 429."

    def unexpected(*_):
        raise RuntimeError("private provider details")

    monkeypatch.setattr(ui, "lookup_wayback", unexpected)
    assert "private" not in render(2, PAGE, "").children


def test_aleph_configuration_without_key_is_visible_and_skips_the_access_request(
    callback, monkeypatch
):
    config = AlephConfiguration(ALEPH, False, "Set a server-side Aleph API key to connect.")
    monkeypatch.setattr(ui, "aleph_configuration", lambda: config)
    monkeypatch.setattr(
        ui, "check_aleph_connection", lambda: pytest.fail("Unexpected Aleph request")
    )
    render = callback("live-aleph-result.children")
    for clicks in (0, 1):
        setup, result = render(clicks)
        assert ALEPH in [
            node.children for node in descendants(setup) if isinstance(node, html.Span)
        ]
        assert setup[-1].children == config.message
        assert result == ""


def test_aleph_checks_only_on_click_and_reports_provider_access_separately(callback, monkeypatch):
    calls = []

    def check():
        calls.append(True)
        if len(calls) == 1:
            return AlephConnectionResult(ALEPH, "connected", "Collections listing verified.", 200)
        return AlephConnectionResult(ALEPH, "setup_required", "Aleph rejected the API key.", 401)

    monkeypatch.setattr(
        ui, "aleph_configuration", lambda: AlephConfiguration(ALEPH, True, "Ready to check.")
    )
    monkeypatch.setattr(ui, "check_aleph_connection", check)
    render = callback("live-aleph-result.children")
    assert render(0)[1] == "" and not calls
    _, success = render(1)
    assert len(calls) == 1 and success.children == "Collections listing verified."
    assert "rv-notice--error" not in success.className
    _, rejected = render(2)
    assert rejected.children == "Aleph rejected the API key."
    assert "rv-notice--error" in rejected.className
