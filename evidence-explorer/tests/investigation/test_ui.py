"""Reasoning reports cannot outlive their evidence binding or invent exact quotes."""

from copy import deepcopy

import pytest
from dash import Dash, dcc, html
from dash.development.base_component import Component
from dash.exceptions import PreventUpdate

from evidence_explorer.investigation import ui, workspace

from . import test_workspace as fixtures

desk = fixtures.desk
draft = fixtures.draft
state = fixtures.state


def walk(node):
    if isinstance(node, Component):
        yield node
        yield from walk(getattr(node, "children", None))
    elif isinstance(node, list | tuple):
        for child in node:
            yield from walk(child)


def text(node):
    if isinstance(node, Component):
        return text(getattr(node, "children", None))
    if isinstance(node, list | tuple):
        return " ".join(text(child) for child in node)
    return str(node or "")


def callbacks(service):
    app = Dash(__name__, suppress_callback_exceptions=True)
    app.layout = html.Div([dcc.Store(id="rv-session"), ui.check_panel()])
    ui.register_check_callbacks(app, service)
    return {
        entry["callback"].__wrapped__.__name__: entry["callback"].__wrapped__
        for entry in app.callback_map.values()
    }


def test_long_verified_passage_retains_full_text_and_bound_source_button():
    quote = "A full immutable passage with important qualifications. " * 10
    record = {"id": "r", "text": "prefix\n" + quote + "\nsuffix"}
    span = {"record_id": "r", "start": 7, "end": 7 + len(quote), "quote": quote[:297] + "…"}
    shown = ui._passages([span], "nav-v1:fixture", {"r": record}, "test")
    assert quote in text(shown)
    button = next(node for node in walk(shown) if isinstance(node, html.Button))
    assert button.id["rid"] == "r"
    assert button.id["context"] == "nav-v1:fixture"
    assert button.id["action"] == "source"


@pytest.mark.parametrize("changes", [{"quote": "invented"}, {"start": -1}, {"end": 1000}])
def test_invalid_offsets_or_quote_never_become_checked_evidence(changes):
    span = {"record_id": "r", "start": 0, "end": 5, "quote": "saved", **changes}
    assert ui._passages([span], "context", {"r": {"text": "saved text"}}, "test") == []


def test_report_cache_rejects_client_report_payload_and_bounds_retention():
    cache = ui.ReportCache(limit=1)
    first = cache.put({"report": "first"})
    assert cache.get({**first, "report": "forged"}) is None
    assert cache.get({"id": ["bad"]}) is None
    second = cache.put({"report": "second"})
    assert cache.get(first) is None
    assert cache.get(second) == {"report": "second"}


def test_native_unavailable_and_model_not_run_stay_visible(desk):
    selected = draft(desk)["session"]
    report = workspace.check_case(desk, selected)["report"]
    report["records"][0]["frozen"] = "unavailable"
    rendered = ui.render_report(
        report, selected, desk.view(selected["case_id"], selected["scenario_id"])
    )
    content = text(rendered)
    assert "original-record verification remains unavailable" in content
    assert "Model check not run" in content
    assert "Human decision still required" in content
    assert "not factual truth" in content
    assert "Conclusions · concerns first" in content


def test_cached_check_survives_same_run_selection_but_rejects_other_assessment(desk):
    selected = draft(desk)["session"]
    handlers = callbacks(desk)
    token, message = handlers["run_check"](1, selected)
    assert message == ""
    step = next(
        finding
        for finding in desk.get_case(selected["case_id"])["findings"]
        if finding["run_id"]
        == desk.view(selected["case_id"], selected["scenario_id"])["scenario"]["base_run_id"]
        and finding["details"].get("role") == "step"
    )
    stepped = {**selected, "finding_id": step["snapshot_id"]}
    rendered, disabled = handlers["show_check"](token, stepped)
    assert not disabled
    assert "This step" in text(rendered)
    original = state(desk)
    rendered, disabled = handlers["show_check"](token, original)
    assert disabled
    assert "Earlier reports" in text(rendered)
    with pytest.raises(PreventUpdate):
        handlers["download_check"](1, token, original)


def test_selected_claim_shows_dependency_result_even_when_direct_text_is_supported(desk):
    selected = draft(desk)["session"]
    report = workspace.check_case(desk, selected)["report"]
    claim = next(item for item in report["findings"] if item["id"] == selected["finding_id"])
    conclusion = next(item for item in report["conclusions"] if item["id"] == claim["id"])
    claim["verdict"] = "supported"
    conclusion["status"] = "undermined"
    rendered = ui.render_report(
        report, selected, desk.view(selected["case_id"], selected["scenario_id"])
    )
    selected_card = next(
        node
        for node in walk(rendered)
        if getattr(node, "className", None) == "investigation-selected"
    )
    assert "Unsupported or conflicting dependency" in text(selected_card)
    assert "Supported by saved text" not in text(selected_card)


def test_changed_case_fingerprint_rejects_cached_report_and_download(desk):
    selected = draft(desk)["session"]
    handlers = callbacks(desk)
    token, _ = handlers["run_check"](1, selected)
    draft(desk, selected, claim="A second draft about the same source.")
    rendered, disabled = handlers["show_check"](token, selected)
    assert disabled
    assert "changed" in text(rendered).lower()
    with pytest.raises(PreventUpdate):
        handlers["download_check"](1, token, selected)


def test_client_cannot_supply_a_conclusion_instead_of_server_report(desk):
    selected = draft(desk)["session"]
    handlers = callbacks(desk)
    token, _ = handlers["run_check"](1, selected)
    forged = deepcopy(token)
    forged["report"] = {"conclusions": [{"status": "holds"}]}
    _, disabled = handlers["show_check"](forged, selected)
    assert disabled
