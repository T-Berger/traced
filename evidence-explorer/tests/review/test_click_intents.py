"""Dynamic remounts are rejected before they can schedule the session writer."""

import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from types import SimpleNamespace

import pytest
from dash import html, no_update
from dash.development.base_component import Component
from dash.exceptions import PreventUpdate

from evidence_explorer.review import web
from evidence_explorer.review.click_intents import (
    CLICK_PATTERNS,
    INTENT_SCRIPT,
    trigger_from_intent,
)
from evidence_explorer.review.navigation import NextAction, navigation_context
from evidence_explorer.review.selectors import evidence_ids
from evidence_explorer.review.service import ReviewService
from evidence_explorer.review.storage import ReviewRepository

BUTTONS = [
    {"type": "rv-finding", "fid": "finding"},
    {"type": "rv-record", "rid": "record", "location": "source-card"},
    {"type": "rv-open-case", "cid": "case"},
    {"type": "rv-citation", "fid": "finding", "rid": "record"},
    {"type": "rv-graph-premise", "fid": "finding", "location": "graph-premise"},
    {
        "type": "rv-next-action",
        "action": "source",
        "context": "nav-v1:fixture",
        "fid": "finding",
        "rid": "record",
        "location": "handoff-gap-0",
    },
]


def change(button, value, property_name="n_clicks"):
    return {
        "prop_id": json.dumps(button, sort_keys=True, separators=(",", ":")) + "." + property_name,
        "value": value,
    }


def ingress_events(events):
    """Execute the exact production synchronous function, without a browser."""
    node = shutil.which("node")
    assert node, "Node is required for the actual clientside-ingress regression"
    program = r"""
const fs = require('fs'), vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const ignored = Object.freeze({ignored: true});
const runtime = vm.createContext({
    dash_clientside: {no_update: ignored}, Date: {now: () => 42}
});
const ingress = vm.runInContext('(' + input.script + ')', runtime);
const results = input.events.map(event => {
    runtime.dash_clientside.callback_context = event;
    const before = JSON.stringify(event);
    const result = ingress();
    if (result && typeof result.then === 'function') throw Error('Ingress must be synchronous');
    if (JSON.stringify(event) !== before) throw Error('Ingress mutated the callback context');
    return result === ignored ? {updated: false} : {updated: true, data: result};
});
process.stdout.write(JSON.stringify(results));
"""
    result = subprocess.run(
        [node, "-e", program],
        input=json.dumps({"script": INTENT_SCRIPT, "events": events}),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("button", BUTTONS, ids=lambda button: button["type"])
def test_each_exact_click_gets_a_fresh_token_and_preserves_target(button):
    events = [{"triggered": [change(button, 1)]}] * 2
    first, second = ingress_events(events)
    assert first["updated"] and second["updated"]
    assert first["data"]["event_id"] != second["data"]["event_id"]
    for result in (first, second):
        envelope = result["data"]
        before = deepcopy(envelope)
        decoded = trigger_from_intent(envelope)
        assert decoded == button
        assert decoded is not envelope["trigger"]
        decoded["type"] = "changed by caller"
        assert envelope == before
        assert envelope["clicks"] == 1


@pytest.mark.parametrize("button", BUTTONS, ids=lambda button: button["type"])
def test_zero_remount_ignores_another_buttons_positive_count(button):
    event = {
        "triggered": [change(button, 0)],
        "inputs_list": [[{"id": BUTTONS[1], "property": "n_clicks", "value": 7}]],
    }
    assert ingress_events([event]) == [{"updated": False}]


@pytest.mark.parametrize("count", [None, False, True, 0, -1, 1.5, "1", [], 2**53])
def test_non_click_counts_never_emit_intent(count):
    assert ingress_events([{"triggered": [change(BUTTONS[1], count)]}]) == [{"updated": False}]


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"triggered": []},
        {"triggered": [change(BUTTONS[1], 1), change(BUTTONS[5], 1)]},
        {"triggered": [change(BUTTONS[1], 1, "children")]},
        {"triggered": [change({"type": "rv-record", "rid": ["ALL"], "location": ["ALL"]}, [0])]},
        {"triggered": [change({**BUTTONS[1], "unexpected": "value"}, 1)]},
        {"triggered": [change({"type": "rv-unrecognized", "rid": "record"}, 1)]},
        {"triggered": [{"prop_id": "{bad-json.n_clicks", "value": 1}]},
    ],
)
def test_ambiguous_or_malformed_ingress_is_ignored(event):
    assert ingress_events([event]) == [{"updated": False}]


@pytest.mark.parametrize("remount_count", [0, None])
def test_one_genuine_click_survives_batched_remount_entries(remount_count):
    event = {"triggered": [change(BUTTONS[5], remount_count), change(BUTTONS[1], 1)]}
    result = ingress_events([event])[0]
    assert result["updated"]
    assert trigger_from_intent(result["data"]) == BUTTONS[1]


@pytest.mark.parametrize(
    "intent",
    [
        None,
        {},
        {"event_id": "", "trigger": BUTTONS[1], "clicks": 1},
        {"event_id": 1, "trigger": BUTTONS[1], "clicks": 1},
        {"event_id": "event", "trigger": BUTTONS[1], "clicks": 0},
        {"event_id": "event", "trigger": BUTTONS[1], "clicks": True},
        {"event_id": "event", "trigger": BUTTONS[1], "clicks": 2**53},
        {"event_id": "event", "trigger": {**BUTTONS[1], "rid": None}, "clicks": 1},
        {"event_id": "event", "trigger": {**BUTTONS[1], "extra": "value"}, "clicks": 1},
    ],
)
def test_server_rejects_malformed_intent(intent):
    with pytest.raises(PreventUpdate):
        trigger_from_intent(intent)


def components(node):
    if isinstance(node, Component):
        yield node
        yield from components(getattr(node, "children", None))
    elif isinstance(node, (list, tuple)):
        for child in node:
            yield from components(child)


@pytest.fixture
def desk(tmp_path):
    repository = ReviewRepository(tmp_path / "review.sqlite3")
    repository.initialize()
    service = ReviewService(repository)
    service.seed_demo()
    app = web.create_app(service, prefix="/review/")
    state = next(
        node.data for node in components(app.layout()) if getattr(node, "id", None) == "rv-session"
    )
    return service, app, state


def writer(app):
    return next(
        value for key, value in app.callback_map.items() if "rv-action-message.children" in key
    )


def invoke_writer(app, monkeypatch, state, intent, note=""):
    monkeypatch.setattr(
        web,
        "ctx",
        SimpleNamespace(
            triggered_id="rv-click-intent",
            triggered=[{"prop_id": "rv-click-intent.data"}],
        ),
    )
    values = {
        "rv-click-intent": intent,
        "rv-session": state,
        "rv-case": state["case_id"],
        "rv-scenario": state.get("scenario_id") or "baseline",
        "rv-review-reason": note,
        "rv-exclude-reason": "",
        "rv-challenge-mode": "demo",
    }
    callback = writer(app)
    arguments = [values.get(item["id"], 0) for item in [*callback["inputs"], *callback["state"]]]
    return callback["callback"].__wrapped__(*arguments)


def test_dynamic_inputs_only_schedule_synchronous_ingress(desk):
    _, app, _ = desk
    inputs = writer(app)["inputs"]
    assert {"id": "rv-click-intent", "property": "data"} in inputs
    assert not any(item["id"].startswith("{") for item in inputs)
    ingress = app.callback_map["rv-click-intent.data"]
    assert len(ingress["inputs"]) == len(CLICK_PATTERNS) == 6
    assert {json.loads(item["id"])["type"] for item in ingress["inputs"]} == {
        button["type"] for button in BUTTONS
    }
    registration = next(
        item for item in app._callback_list if item["output"] == "rv-click-intent.data"
    )
    assert registration["clientside_function"]
    assert sum("rv-session.data" in key and "@" not in key for key in app.callback_map) == 1
    assert sum(key == "rv-page.value" for key in app.callback_map) == 1
    stores = [
        node for node in components(app.layout()) if getattr(node, "id", None) == "rv-click-intent"
    ]
    assert len(stores) == 1 and stores[0].storage_type == "memory"


def test_pending_record_selection_survives_forced_zero_remounts(desk, monkeypatch):
    service, app, state = desk
    before_state = deepcopy(state)
    before_view = service.view(state["case_id"])
    bundle = before_view["bundle"]
    records, fragment_ids = evidence_ids(bundle, state["finding_id"])
    target = next(record for record in sorted(records) if record != state["record_id"])
    button = {"type": "rv-record", "rid": target, "location": "source-card"}
    selected = ingress_events([{"triggered": [change(button, 1)]}])[0]
    ready, release = Event(), Event()
    original_view = service.view

    def blocked_view(*args, **kwargs):
        ready.set()
        assert release.wait(5), "Test must release the pending selection"
        return original_view(*args, **kwargs)

    monkeypatch.setattr(service, "view", blocked_view)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(invoke_writer, app, monkeypatch, state, selected["data"])
        try:
            assert ready.wait(5), "Genuine selection must be in flight before remount"
            remounts = [
                {"triggered": [change({**BUTTONS[5], "location": f"gap-{i}"}, 0)]} for i in range(4)
            ]
            assert ingress_events(remounts) == [{"updated": False}] * 4
            assert not pending.done()
        finally:
            release.set()
        result = pending.result(timeout=5)
    selected_state = result[0]
    assert selected_state["record_id"] == target
    assert state == before_state
    assert original_view(state["case_id"]) == before_view
    renderer = next(
        value for key, value in app.callback_map.items() if "rv-findings.children" in key
    )
    displayed = renderer["callback"].__wrapped__(selected_state)
    assert target in repr(displayed[4])
    assert any(
        isinstance(getattr(node, "id", None), dict)
        and node.id.get("rid") == target
        and getattr(node, "aria-pressed", None) == "true"
        for node in components(displayed[5])
    )
    expected_quotes = {
        item["quote"]
        for item in bundle["fragments"]
        if item["id"] in fragment_ids and item["record_id"] == target
    }
    shown_quotes = {
        node.children for node in components(displayed[4]) if isinstance(node, html.Mark)
    }
    assert expected_quotes and expected_quotes <= shown_quotes


@pytest.mark.parametrize("mode", ["fresh", "stale", "dirty"])
def test_actual_handoff_intent_retains_current_context_and_draft_guards(desk, monkeypatch, mode):
    service, app, state = desk
    before_state = deepcopy(state)
    view = service.view(state["case_id"])
    trigger = NextAction(
        "brief",
        "Brief",
        "Review the handoff",
        navigation_context(view, state),
        location="handoff-race-check",
    ).button_id()
    if mode == "stale":
        trigger["context"] = "nav-v1:earlier-assessment"
    intent = ingress_events([{"triggered": [change(trigger, 1)]}])[0]["data"]
    result = invoke_writer(
        app, monkeypatch, state, intent, "Unsaved note" if mode == "dirty" else ""
    )
    if mode == "stale":
        assert result[0] is no_update
        assert "earlier case or assessment" in repr(result[1])
    else:
        assert result[0]["navigate_to"] == ("review" if mode == "dirty" else "brief")
        assert result[0]["navigate_target"] == (
            "rv-review-reason" if mode == "dirty" else "rv-brief-page"
        )
        assert result[0]["finding_id"] == state["finding_id"]
    assert state == before_state
    assert service.view(state["case_id"]) == view
