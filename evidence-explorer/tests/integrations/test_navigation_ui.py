"""Navigation uses the mounted shared workflow and never records progress."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from dash.development.base_component import Component

from evidence_explorer.config import Settings
from evidence_explorer.review import web
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.integrations.navigation_ui import render_next_action
from evidence_explorer.review.integrations.web import create_app
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.use_cases import project_case_progress

PURPOSE = "cited_claim_investigation"


def components(node):
    if isinstance(node, Component):
        yield node
        yield from components(getattr(node, "children", None))
    elif isinstance(node, (list, tuple)):
        for child in node:
            yield from components(child)


def action_button(node):
    return next(
        item
        for item in components(node)
        if isinstance(getattr(item, "id", None), dict) and item.id.get("type") == "rv-next-action"
    )


@pytest.fixture
def desk(tmp_path):
    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    service.import_bundle(load_demo_bundle().model_dump(mode="json"))
    return service, create_app(service)


def test_global_guidance_mount_and_single_page_writer(desk):
    service, app = desk
    before = service.repository.list_events(load_demo_bundle().case.id)
    nodes = list(components(app.layout()))
    slot = next(node for node in nodes if getattr(node, "id", None) == "rv-next-action-slot")
    assert any(getattr(node, "id", None) == "trail-next-action" for node in components(slot))
    assert sum(key == "rv-page.value" for key in app.callback_map) == 1
    assert "trail-next-action.children" in app.callback_map
    assert service.repository.list_events(load_demo_bundle().case.id) == before


def test_available_action_does_not_confirm_scope_or_create_reviews(desk):
    service, _ = desk
    state = session_for_case(service, load_demo_bundle().case.id)
    before = service.view(state["case_id"])
    rendered = render_next_action(service, state, "cases", {"purpose_id": PURPOSE})
    button = action_button(rendered)
    assert button.id["action"] == "purpose"
    progress = project_case_progress(PURPOSE, service.view(state["case_id"]))
    assert next(step for step in progress.steps if step.id == "scope").status != "completed"
    assert service.view(state["case_id"]) == before


def test_missing_purpose_offers_guidance_and_stale_context_clears_button(desk):
    service, _ = desk
    assert action_button(render_next_action(service, {}, "cases", None)).id["action"] == "purpose"
    state = session_for_case(service, load_demo_bundle().case.id)
    state["case_fingerprint"] = "outdated"
    stale = render_next_action(service, state, "review", {"purpose_id": PURPOSE})
    assert not any(isinstance(getattr(node, "id", None), dict) for node in components(stale))


@pytest.mark.parametrize(
    ("has_case", "purpose_id", "page", "target"),
    [
        (True, PURPOSE, "cases", "trail-planning-question"),
        (False, None, "cases", "trail-planning-question"),
        (False, PURPOSE, "cases", "trail-planning-question"),
    ],
)
def test_actual_shared_action_callback_navigates_without_writes(
    desk, monkeypatch, has_case, purpose_id, page, target
):
    service, app = desk
    case_id = load_demo_bundle().case.id
    state = session_for_case(service, case_id if has_case else None)
    rendered = render_next_action(service, state, "cases", {"purpose_id": purpose_id})
    trigger = action_button(rendered).id
    definition = next(
        value for key, value in app.callback_map.items() if "rv-action-message.children" in key
    )
    monkeypatch.setattr(
        web,
        "ctx",
        SimpleNamespace(
            triggered_id="rv-click-intent", triggered=[{"prop_id": "rv-click-intent.data"}]
        ),
    )
    values = {
        "rv-click-intent": {"event_id": "test-navigation", "trigger": trigger, "clicks": 1},
        "rv-session": state,
        "rv-case": state["case_id"],
        "rv-scenario": "baseline",
        "rv-review-reason": "",
        "rv-exclude-reason": "",
        "rv-challenge-mode": "demo",
    }
    arguments = []
    for field in [*definition["inputs"], *definition["state"]]:
        identifier = field["id"]
        arguments.append(values.get(identifier, 0))
    before = service.view(case_id)
    result = definition["callback"].__wrapped__(*arguments)
    assert result[0]["navigate_to"] == page
    assert result[0]["navigate_target"] == target
    assert result[0]["finding_id"] == state["finding_id"]
    assert service.view(case_id) == before
