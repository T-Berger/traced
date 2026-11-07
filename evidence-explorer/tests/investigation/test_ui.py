"""Reasoning reports cannot outlive their evidence binding or invent exact quotes."""

from copy import deepcopy

import pytest
from dash import Dash, dcc, html
from dash._callback_context import context_value
from dash._utils import AttributeDict
from dash.development.base_component import Component
from dash.exceptions import PreventUpdate

from evidence_explorer.investigation import ui, workspace

from . import test_workspace as fixtures

desk = fixtures.desk
draft = fixtures.draft
state = fixtures.state
model_requests = fixtures.model_requests


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


def run_check(handlers, selected, *, openrouter=False):
    trigger = "investigation-check-openrouter" if openrouter else "investigation-check-run"
    token = context_value.set(AttributeDict(triggered_inputs=[{"prop_id": trigger + ".n_clicks"}]))
    try:
        return handlers["run_check"](0 if openrouter else 1, 1 if openrouter else 0, selected)
    finally:
        context_value.reset(token)


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
    token, message = run_check(handlers, selected)
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
    token, _ = run_check(handlers, selected)
    draft(desk, selected, claim="A second draft about the same source.")
    rendered, disabled = handlers["show_check"](token, selected)
    assert disabled
    assert "changed" in text(rendered).lower()
    with pytest.raises(PreventUpdate):
        handlers["download_check"](1, token, selected)


def test_client_cannot_supply_a_conclusion_instead_of_server_report(desk):
    selected = draft(desk)["session"]
    handlers = callbacks(desk)
    token, _ = run_check(handlers, selected)
    forged = deepcopy(token)
    forged["report"] = {"conclusions": [{"status": "holds"}]}
    _, disabled = handlers["show_check"](forged, selected)
    assert disabled


def test_openrouter_action_discloses_scope_and_configuration_without_a_provider_call(
    desk, model_requests
):
    requests = model_requests()
    selected = draft(desk)["session"]
    panel = ui.check_panel()
    assert "Check with OpenRouter" in text(panel)
    assert "selected analysis run" in text(panel)
    assert "nearby related text from the complete saved case" in text(panel)
    assert "Up to 4 requests" in text(panel)
    app = Dash(__name__, suppress_callback_exceptions=True)
    app.layout = panel
    ui.register_check_callbacks(app, desk)
    for definition in app.callback_map.values():
        outputs = definition["output"]
        assert all(
            output.component_id != "rv-page"
            for output in (outputs if isinstance(outputs, list) else [outputs])
        )
    handlers = callbacks(desk)
    status, disabled = handlers["model_configuration"](selected)
    assert not disabled
    assert "OpenRouter configured" in text(status)
    assert fixtures.TEST_MODEL in text(status)
    assert fixtures.TEST_API_KEY not in text(status)
    assert "availability is checked when you run" in text(status)
    assert requests == []
    run_check(handlers, selected)
    assert requests == []


def test_openrouter_missing_configuration_shows_recovery_and_keeps_offline_available(
    desk, model_requests, monkeypatch
):
    requests = model_requests()
    monkeypatch.delenv("OPENROUTER_API_KEY")
    selected = draft(desk)["session"]
    handlers = callbacks(desk)
    status, disabled = handlers["model_configuration"](selected)
    assert disabled
    assert "OpenRouter not configured" in text(status)
    assert "server" in text(status)
    token, message = run_check(handlers, selected, openrouter=True)
    assert token is None
    assert "Configure" in text(message)
    offline, message = run_check(handlers, selected)
    assert offline is not None
    assert requests == []


def test_explicit_openrouter_check_shows_model_provenance_and_bound_download(desk, model_requests):
    requests = model_requests()
    selected = draft(desk, claim="The saved evidence establishes an organisation.")["session"]
    handlers = callbacks(desk)
    token, message = run_check(handlers, selected, openrouter=True)
    assert message == ""
    assert requests
    rendered, disabled = handlers["show_check"](token, selected)
    assert not disabled
    assert "Model check completed" in text(rendered)
    assert fixtures.TEST_MODEL in text(rendered)
    assert fixtures.ACTUAL_MODEL in text(rendered)
    assert "proposals for review" in text(rendered)
    download = handlers["download_check"](1, token, selected)
    assert fixtures.TEST_API_KEY not in download["content"]
    assert fixtures.ACTUAL_MODEL in download["content"]
    draft(desk, selected, claim="Changed evidence context")
    assert handlers["show_check"](token, selected)[1]
    with pytest.raises(PreventUpdate):
        handlers["download_check"](1, token, selected)


@pytest.mark.parametrize(
    ("status", "label"),
    [
        ("partial", "Model check incomplete"),
        ("failed", "Model check failed"),
        ("not_run", "Model check not run"),
    ],
)
def test_model_incomplete_and_failed_states_and_finding_notes_are_visible(desk, status, label):
    selected = draft(desk)["session"]
    report = workspace.check_case(desk, selected)["report"]
    report["summary"]["model_checks"] = {
        "status": status,
        "provider": "openrouter",
        "model": "test/model",
        "calls": 4,
        "cached": 0,
        "errors": 1,
        "skipped": 2,
        "discarded_objections": 1,
        "reason": "The model check was incomplete; inspect finding notes.",
    }
    claim = next(item for item in report["findings"] if item["id"] == selected["finding_id"])
    claim["model_notes"] = ["discarded model objection: quotation is not verbatim"]
    rendered = ui.render_report(
        report, selected, desk.view(selected["case_id"], selected["scenario_id"])
    )
    content = text(rendered)
    assert label in content
    assert "Model check completed" not in content
    assert "Requests: 4" in content
    assert "Errors: 1" in content
    assert "Skipped checks: 2" in content
    assert "Discarded objections: 1" in content
    assert "Model notes for this finding" in content
    assert "quotation is not verbatim" in content
    assert "Human decision still required" in content


def test_model_based_links_are_proposals_not_supported_saved_text(desk):
    selected = draft(desk)["session"]
    report = workspace.check_case(desk, selected)["report"]
    for finding in report["findings"]:
        finding["basis"] = "model"
    for link in report["links"]:
        link.update(label="supported", basis="model")
    rendered = ui.render_report(
        report, selected, desk.view(selected["case_id"], selected["scenario_id"])
    )
    assert "Model raised no objection" in text(rendered)
    assert "Supported by saved text" not in text(rendered)
    assert "Model-proposed concern" in text(ui._label("unsupported", "model"))
    assert "Model-proposed conflict" in text(ui._label("contradicted", "model"))
