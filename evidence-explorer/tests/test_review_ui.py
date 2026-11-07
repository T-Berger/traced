"""UI boundaries preserve exact evidence and readable, local graph navigation."""

import json

from dash import html

from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.presentation import (
    evidence_graph,
    highlight_text,
    job_panel,
    review_history,
)
from evidence_explorer.review.selectors import initial_selection
from evidence_explorer.review.service import ReviewService
from evidence_explorer.review.storage import ReviewRepository
from evidence_explorer.review.web import create_app


def test_highlight_only_matches_verified_saved_offset():
    text = "A <script> tag is source text. Exact quotation."
    start = text.index("Exact")
    parts, verified = highlight_text(
        text,
        [
            {"selector": {"start": start, "end": len(text)}, "quote": "Exact quotation."},
            {"selector": {"start": 0, "end": 1}, "quote": "wrong"},
        ],
    )
    assert verified
    assert parts[0] == "A <script> tag is source text. "
    assert isinstance(parts[1], html.Mark)
    assert parts[1].children == "Exact quotation."
    wrong_parts, wrong_verified = highlight_text(
        text,
        [
            {"selector": {"start": 0, "end": 1}, "quote": "wrong"},
        ],
    )
    assert not wrong_verified
    assert wrong_parts == [text]


def test_focus_shows_direct_premises_and_keeps_exclusion_visible():
    bundle = load_demo_bundle().model_dump(mode="json")
    assert initial_selection(bundle) == ("harbor:c3-v1", "harbor:r01")
    elements = evidence_graph(bundle, "harbor:c3-v1", {"harbor:c3-v1"}, {"harbor:r01"})
    nodes = {item["data"]["ref_id"]: item for item in elements if "ref_id" in item["data"]}
    assert set(nodes) == {"harbor:c3-v1", "harbor:c2-v1", "harbor:r01", "harbor:r02"}
    assert "affected" in nodes["harbor:c3-v1"]["classes"]
    assert "excluded" in nodes["harbor:r01"]["classes"]
    assert all("position" in node for node in nodes.values())


def test_app_serves_layout_and_health_with_real_service(tmp_path):
    repository = ReviewRepository(tmp_path / "review.sqlite3")
    repository.initialize()
    service = ReviewService(repository)
    service.seed_demo()
    app = create_app(service)
    client = app.server.test_client()
    assert client.get("/health").json["status"] == "ok"
    layout = client.get("/_dash-layout")
    assert layout.status_code == 200
    assert "harbor:c3-v1" in layout.get_data(as_text=True)
    assert "rv-challenge-mode" in layout.get_data(as_text=True)
    assert client.get("/_dash-dependencies").status_code == 200


def _text(component):
    if isinstance(component, str):
        return component
    if isinstance(component, list):
        return " ".join(_text(child) for child in component)
    return _text(getattr(component, "children", ""))


def test_review_does_not_transfer_to_new_scenario_version():
    events = [
        {
            "event_type": "finding_reviewed",
            "payload": {
                "finding_id": "claim-1",
                "scenario_id": "scenario-1",
                "scenario_version": 1,
                "case_fingerprint": "case-v1",
                "disposition": "qualify",
                "reason": "Attribution remains uncertain.",
                "created_at": "2026-11-06T10:00:00Z",
            },
        }
    ]
    current = _text(review_history(events, "claim-1", "scenario-1", 1, "case-v1"))
    changed = _text(review_history(events, "claim-1", "scenario-1", 2, "case-v1"))
    assert "Current version" in current
    assert "No decision recorded for this scenario version." in changed
    assert "Previous scenario / version" in changed
    assert "Current version" not in changed
    new_case = _text(review_history(events, "claim-1", "scenario-1", 1, "case-v2"))
    assert "Current version" not in new_case
    events[0]["payload"].pop("case_fingerprint")
    legacy = _text(review_history(events, "claim-1", "scenario-1", 1, "case-v1"))
    assert "Current version" not in legacy


def test_search_scope_is_readable_and_audit_details_are_collapsed():
    parts = job_panel(
        {
            "status": "completed",
            "mode": "demo",
            "result": {
                "findings": [],
                "search_scope": {
                    "record_count": 1,
                    "total_record_count": 10,
                    "excluded_record_ids": ["record-2"],
                    "fingerprint": "long-internal-id",
                    "limitations": ["Only saved records were searched."],
                },
            },
        }
    )
    assert any(
        isinstance(part, html.P) and "Reviewed 1 of 10 saved records" in _text(part)
        for part in parts
    )
    details = next(part for part in parts if isinstance(part, html.Details))
    assert not getattr(details, "open", False)
    assert "long-internal-id" in _text(details)


def _invoke_action(
    app,
    state,
    trigger,
    *,
    review_reason="Test review decision",
    exclude_reason="Test source decision",
    assessment=None,
):
    key = next(key for key in app.callback_map if "rv-action-message" in key)
    definition = app.callback_map[key]
    inputs = []
    for item in definition["inputs"]:
        ident = item["id"]
        if ident == "rv-case":
            value = state["case_id"]
        elif ident == "rv-scenario":
            value = assessment or state.get("scenario_id") or "baseline"
        elif ident.startswith("{"):
            matches = isinstance(trigger, dict) and json.loads(ident)["type"] == trigger["type"]
            value = [1] if matches else [0]
        elif ident == "rv-graph":
            value = None
        else:
            value = 1 if ident == trigger else 0
        inputs.append({**item, "value": value})
    values = {
        "rv-session": state,
        "rv-exclude-reason": exclude_reason,
        "rv-review-reason": review_reason,
        "rv-challenge-mode": "demo",
    }
    trigger_id = (
        json.dumps(trigger, sort_keys=True, separators=(",", ":"))
        if isinstance(trigger, dict)
        else trigger
    )
    trigger_property = (
        "value"
        if isinstance(trigger, str) and trigger in {"rv-case", "rv-scenario"}
        else "n_clicks"
    )
    response = app.server.test_client().post(
        "/_dash-update-component",
        json={
            "output": key,
            "outputs": [
                {"id": output.component_id, "property": output.component_property}
                for output in definition["output"]
            ],
            "inputs": inputs,
            "state": [{**item, "value": values.get(item["id"])} for item in definition["state"]],
            "changedPropIds": [trigger_id + "." + trigger_property],
        },
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.json["response"]


def test_actions_pin_scenario_to_selected_run_and_reset_on_run_switch(tmp_path):
    repository = ReviewRepository(tmp_path / "review.sqlite3")
    repository.initialize()
    service = ReviewService(repository)
    bundle = load_demo_bundle().model_dump(mode="json")
    original_run = bundle["runs"][0]["id"]
    bundle["runs"].append({**bundle["runs"][0], "id": "another-run"})
    bundle["findings"].append(
        {
            **bundle["findings"][0],
            "snapshot_id": "another-finding",
            "lineage_id": "another-lineage",
            "run_id": "another-run",
        }
    )
    case_id = service.import_bundle(bundle)
    app = create_app(service)
    state = {
        "case_id": case_id,
        "finding_id": "harbor:c3-v1",
        "record_id": "harbor:r01",
        "case_fingerprint": service.case_fingerprint(case_id),
        "scenario_id": None,
        "scenario_version": None,
        "job_id": None,
        "revision": 0,
    }
    challenged = _invoke_action(app, state, "rv-challenge")["rv-session"]["data"]
    scenario = service.view(case_id, challenged["scenario_id"])["scenario"]
    assert scenario["base_run_id"] == original_run
    switched = _invoke_action(app, challenged, {"type": "rv-finding", "fid": "another-finding"})
    switched_state = switched["rv-session"]["data"]
    assert switched_state["scenario_id"] is None
    assert switched_state["scenario_version"] is None
    assert switched_state["job_id"] is None
    saved = _invoke_action(app, switched_state, "rv-qualify")["rv-session"]["data"]
    assert service.view(case_id, saved["scenario_id"])["scenario"]["base_run_id"] == "another-run"


def test_review_conflict_refreshes_case_identity_without_saving_decision(tmp_path):
    repository = ReviewRepository(tmp_path / "review.sqlite3")
    repository.initialize()
    service = ReviewService(repository)
    case_id = service.seed_demo()
    scenario = service.start_scenario(case_id, "harbor:c3-v1")
    old_fingerprint = service.case_fingerprint(case_id)
    state = {
        "case_id": case_id,
        "finding_id": "harbor:c3-v1",
        "record_id": "harbor:r01",
        "case_fingerprint": old_fingerprint,
        "scenario_id": scenario["id"],
        "scenario_version": scenario["version"],
        "job_id": None,
        "revision": 0,
    }
    changed = service.get_case(case_id)
    changed["records"].append({**changed["records"][0], "id": "newly-imported-record"})
    service.import_bundle(changed)
    response = _invoke_action(create_app(service), state, "rv-use")
    assert response["rv-session"]["data"]["case_fingerprint"] == service.case_fingerprint(case_id)
    assert response["rv-session"]["data"]["case_fingerprint"] != old_fingerprint
    assert not any(
        event["event_type"] == "finding_reviewed" for event in service.view(case_id)["events"]
    )
