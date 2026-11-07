"""Purpose guidance reflects current saved assessments without creating workflow state."""

from __future__ import annotations

import json

import pytest
from dash import Dash, dcc, html
from dash.development.base_component import Component

from evidence_explorer.config import Settings
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.integrations import use_cases_ui
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService

PURPOSE = "cited_claim_investigation"


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


def process_status(node, step_id):
    for item in components(node):
        props = item.to_plotly_json()["props"]
        if props.get("data-process-step") == step_id:
            return props["data-process-status"]
    raise AssertionError(f"Missing process step {step_id}")


@pytest.fixture
def desk(tmp_path):
    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    service.seed_examples(include_swarm=False)
    app = Dash(__name__, suppress_callback_exceptions=True)
    app.layout = lambda: html.Div(
        [
            dcc.Store(id="rv-session"),
            dcc.Store(id="rv-draft-store"),
            dcc.Textarea(id="rv-review-reason"),
            use_cases_ui.use_cases_panel(),
        ]
    )
    use_cases_ui.register_use_case_callbacks(app, service)

    def callback(output):
        return app.callback_map[output]["callback"].__wrapped__

    return service, app, callback


def test_catalog_layout_and_selection_are_session_local(desk):
    service, app, callback = desk
    before = service.repository.list_events(load_demo_bundle().case.id)
    nodes = list(components(app.layout()))
    ids = [node.id for node in nodes if hasattr(node, "id")]
    assert len(ids) == len(set(ids))
    purpose_store = next(
        node for node in nodes if getattr(node, "id", None) == "trail-use-case-purpose"
    )
    selector = next(node for node in nodes if getattr(node, "id", None) == "trail-use-case-select")
    assert purpose_store.storage_type == "session"
    assert selector.persistence_type == "session"
    assert selector.value is None
    assert len(selector.options) == 5
    choose = callback("trail-use-case-purpose.data")
    assert choose(PURPOSE) == {"purpose_id": PURPOSE}
    assert choose("not-a-catalog-purpose") is None
    assert choose(None) is None
    assert service.repository.list_events(load_demo_bundle().case.id) == before


def test_no_case_has_no_invented_progress_or_active_alert():
    rendered = use_cases_ui.render_use_case_details(PURPOSE)
    content = text(rendered)
    assert "Choose a case to see its progress" in content
    assert "Selecting a purpose does not confirm the case scope" in content
    assert "20%" in content
    assert "not a platform rule" in content
    assert "No live alert is configured" in content
    assert not any(
        "data-process-status" in item.to_plotly_json()["props"] for item in components(rendered)
    )
    assert "Purpose unavailable" in text(use_cases_ui.render_use_case_details("unknown-purpose"))


def test_alert_example_uses_the_shared_evaluator_and_an_inclusive_illustrative_rule(monkeypatch):
    from evidence_explorer.use_cases import alerts

    rendered = use_cases_ui._alert_example()
    content = text(rendered)
    props = rendered.to_plotly_json()["props"]
    assert props["data-alert-origin"] == "synthetic"
    assert props["data-alert-status"] == "review_needed"
    assert "5 synthetic records" in content and "6 synthetic records" in content
    assert "At least 20% increase" in content
    assert "20.00%" in content
    assert "100% of the declared synthetic subset" in content
    assert "threshold is inclusive" in content
    assert "not the current case" in content
    rule, baseline, observation = alerts.illustrative_activity_example()
    monkeypatch.setattr(
        alerts,
        "illustrative_activity_example",
        lambda: (rule.model_copy(update={"threshold_percent": 30}), baseline, observation),
    )
    changed = use_cases_ui._alert_example()
    assert changed.to_plotly_json()["props"]["data-alert-status"] == "not_triggered"
    assert "At least 30% increase" in text(changed)
    assert "20.00%" in text(changed)


def test_current_context_uses_saved_reviews_without_marking_scope_complete(desk):
    service, _, callback = desk
    case_id = load_demo_bundle().case.id
    show = callback("trail-use-case-details.children")
    selection = {"purpose_id": PURPOSE}
    rendered = show(selection, {"case_id": case_id, "scenario_id": None})
    assert process_status(rendered, "scope") != "completed"
    assert process_status(rendered, "human_review") != "completed"
    assert "0 statements currently included" in text(rendered)

    baseline = service.baseline_scenario(case_id, "harbor:c1-v1")
    service.review(case_id, "harbor:c1-v1", baseline["id"], "use", "Checked the exact saved posts.")
    current = show(selection, {"case_id": case_id, "scenario_id": baseline["id"]})
    assert "1 statement currently included" in text(current)

    scenario = service.start_scenario(case_id, "harbor:c1-v1")
    changed = show(selection, {"case_id": case_id, "scenario_id": scenario["id"]})
    assert "0 statements currently included" in text(changed)
    assert process_status(changed, "human_review") != "completed"


def test_case_change_or_missing_context_clears_the_previous_projection(desk):
    service, _, callback = desk
    show = callback("trail-use-case-details.children")
    selection = {"purpose_id": PURPOSE}
    first_id = load_demo_bundle().case.id
    second_id = next(case["id"] for case in service.list_cases() if case["id"] != first_id)
    first_title = service.get_case(first_id)["case"]["title"]
    second_title = service.get_case(second_id)["case"]["title"]
    first = show(selection, {"case_id": first_id})
    second = show(selection, {"case_id": second_id})
    assert first_title in text(first)
    assert second_title in text(second)
    assert first_title not in text(second)
    missing = show(selection, {"case_id": "missing-case"})
    assert "Current case unavailable" in text(missing)
    assert first_title not in text(missing) and second_title not in text(missing)
    assert "Choose a case to see its progress" in text(show(selection, {}))


def test_unsaved_review_text_is_not_presented_as_export_ready(desk):
    service, _, callback = desk
    case_id = load_demo_bundle().case.id
    baseline = service.baseline_scenario(case_id, "harbor:c1-v1")
    service.review(case_id, "harbor:c1-v1", baseline["id"], "use", "Saved review reason.")
    rendered = callback("trail-use-case-details.children")(
        {"purpose_id": PURPOSE},
        {
            "case_id": case_id,
            "finding_id": "harbor:c1-v1",
            "case_fingerprint": service.case_fingerprint(case_id),
            "scenario_id": baseline["id"],
            "scenario_version": baseline["version"],
        },
        None,
        "An unsaved changed assessment reason.",
    )
    assert "Unsaved review changes" in text(rendered)
    assert "Save review changes first" in text(rendered)
    assert "An unsaved changed assessment reason." not in text(rendered)


def test_render_callbacks_read_only_and_never_start_work(desk, monkeypatch):
    service, _, callback = desk
    case_id = load_demo_bundle().case.id
    before = service.repository.list_events(case_id)
    fingerprint = service.case_fingerprint(case_id)

    def forbidden(*args, **kwargs):
        raise AssertionError("Guidance must not mutate or enqueue work")

    for name in (
        "start_scenario",
        "collect_source",
        "add_collection_source",
        "challenge",
        "recompute",
        "record_decision",
        "import_bundle",
        "import_explorer",
    ):
        monkeypatch.setattr(service, name, forbidden)
    for _ in range(2):
        rendered = callback("trail-use-case-details.children")(
            {"purpose_id": PURPOSE}, {"case_id": case_id, "scenario_id": None}
        )
        assert process_status(rendered, "human_review") != "completed"
    assert service.repository.list_events(case_id) == before
    assert service.case_fingerprint(case_id) == fingerprint


def test_dash_endpoint_wires_selection_and_existing_case_session(desk):
    service, app, _ = desk
    client = app.server.test_client()
    assert client.get("/_dash-layout").status_code == 200
    dependencies = client.get("/_dash-dependencies").get_json()
    output = next(row for row in dependencies if row["output"] == "trail-use-case-details.children")
    assert {item["id"] for item in output["inputs"]} == {
        "trail-use-case-purpose",
        "rv-session",
        "rv-draft-store",
        "rv-review-reason",
        "trail-planning-refresh",
    }
    selection = client.post(
        "/_dash-update-component",
        json={
            "output": "trail-use-case-purpose.data",
            "outputs": {"id": "trail-use-case-purpose", "property": "data"},
            "inputs": [{"id": "trail-use-case-select", "property": "value", "value": PURPOSE}],
            "state": [],
            "changedPropIds": ["trail-use-case-select.value"],
        },
    )
    assert selection.status_code == 200
    stored = selection.get_json()["response"]["trail-use-case-purpose"]["data"]
    case_id = load_demo_bundle().case.id
    response = client.post(
        "/_dash-update-component",
        json={
            "output": "trail-use-case-details.children",
            "outputs": {"id": "trail-use-case-details", "property": "children"},
            "inputs": [
                {"id": "trail-use-case-purpose", "property": "data", "value": stored},
                {"id": "rv-session", "property": "data", "value": {"case_id": case_id}},
                {"id": "rv-draft-store", "property": "data", "value": None},
                {"id": "rv-review-reason", "property": "value", "value": None},
                {"id": "trail-planning-refresh", "property": "data", "value": None},
            ],
            "state": [],
            "changedPropIds": ["rv-session.data"],
        },
    )
    assert response.status_code == 200
    wire = json.dumps(response.get_json(), ensure_ascii=False)
    assert service.get_case(case_id)["case"]["title"] in wire
    assert "data-process-status" in wire
    assert service.repository.list_events(case_id) == []
