"""Case labels never relabel separately collected or mixed-origin source records."""

from types import SimpleNamespace

import pytest
from dash import html
from dash.development.base_component import Component

from evidence_explorer.collection.models import CollectedBatch
from evidence_explorer.config import Settings
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.integrations import collection_scope_ui
from evidence_explorer.review.integrations.sources import SourceItem
from evidence_explorer.review.integrations.web import create_app
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService


def workspace(path):
    settings = Settings(data_dir=path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    return WorkspaceService(repository, settings, path / "artifacts")


def text(node):
    if isinstance(node, str):
        return node
    if isinstance(node, (list, tuple)):
        return " ".join(text(child) for child in node)
    return text(getattr(node, "children", ""))


def components(node):
    if isinstance(node, Component):
        yield node
        yield from components(getattr(node, "children", None))
    elif isinstance(node, (list, tuple)):
        for child in node:
            yield from components(child)


def callback(app, output):
    return next(
        item["callback"].__wrapped__ for key, item in app.callback_map.items() if output in key
    )


def banner(app, state):
    return text(callback(app, "rv-synthetic.children")(state)[8])


def save_live_collection(service):
    """Complete the real durable collection boundary without network or a model."""
    source_id = service.add_collection_source(
        {
            "platform": "news",
            "label": "Public observatory reports",
            "locator": "https://observatory.example/feed.xml",
            "enabled": False,
        }
    )
    service.collect_source(source_id)
    claimed = service.collection.repository.claim()
    item = SourceItem(
        platform="news",
        channel_id="observatory.example",
        channel_name="Public observatory reports",
        native_id="report-1",
        text="The observatory published its opening hours for public visitors.",
        source_url="https://observatory.example/report-1",
        published_at=None,
        acquired_at="2026-11-07T08:00:00Z",
    )
    service.collection.repository.complete(claimed, CollectedBatch(items=(item,)))
    return item


def mixed_case(*, claim_check):
    payload = load_demo_bundle().model_dump(mode="json")
    payload["case"]["id"] = (
        "claim-check:banner-scope" if claim_check else "mixed-origin:banner-scope"
    )
    payload["records"][0]["metadata"]["synthetic"] = False
    payload["records"][1]["metadata"]["synthetic"] = True
    payload["records"][2]["metadata"].pop("synthetic", None)
    if claim_check:
        payload["findings"][0]["details"].update(
            domain="claim_check",
            claim_check={"source_synthetic": True},
            assessment={"status": "insufficient_evidence", "explanation": "Review saved sources."},
        )
    return payload


def test_selected_demo_banner_on_cases_does_not_label_separate_live_collection_fictional(
    tmp_path, monkeypatch
):
    service = workspace(tmp_path / "desk")
    demo_id = service.seed_demo()
    demo_before = service.get_case(demo_id)
    demo_state = session_for_case(service, demo_id)
    scenario = service.baseline_scenario(demo_id, demo_state["finding_id"])
    service.review(
        demo_id, demo_state["finding_id"], scenario["id"], "qualify", "Demo review only."
    )
    events_before = service.repository.list_events(demo_id)
    collected = save_live_collection(service)
    saved_collection = service.collection.repository.items()
    app = create_app(service)

    assert "Live sources" in text(app.layout())
    cases_tab = next(
        tab
        for node in components(app.layout())
        if getattr(node, "id", None) == "rv-page"
        for tab in node.children
        if tab.value == "cases"
    )
    assert "rv-navigation-tab--selected-cases" in cases_tab.selected_className.split()
    demo_banner = banner(app, demo_state)
    assert "Selected case: Synthetic example" in demo_banner
    assert "separately collected sources are not labeled fictional" in demo_banner
    assert "Fictional evidence" not in demo_banner
    status = callback(app, "live-summary.children")(0, 0)
    assert "Collected" in text(status[1]) and "1 saved records" in text(status[1])
    assert service.collection.repository.items() == saved_collection

    opened, message = callback(app, "live-open-message.children")(1)
    assert opened is True and "Choose and preview" in text(message)
    choices = callback(app, "live-case-sources.options")(0, 0)
    selected = [source["value"] for source in choices]
    assert len(selected) == 1
    preview = callback(app, "live-case-token.data")
    monkeypatch.setattr(
        collection_scope_ui, "ctx", SimpleNamespace(triggered_id="live-case-sources")
    )
    _, token, disabled, _ = preview(selected, 0)
    assert token is None and disabled is True
    monkeypatch.setattr(
        collection_scope_ui, "ctx", SimpleNamespace(triggered_id="live-case-preview-action")
    )
    preview_card, token, disabled, preview_message = preview(selected, 1)
    assert disabled is False and token["source_ids"] == selected
    assert "1 saved record(s)" in text(preview_card)
    assert "Preview only" in text(preview_message)
    assert [case["id"] for case in service.list_cases()] == [demo_id]
    assert service.repository.list_events(demo_id) == events_before
    assert service.collection.repository.items() == saved_collection
    create_case = next(
        item["callback"].__wrapped__
        for key, item in app.callback_map.items()
        if "rv-case.options" in key and "live-case-message.children" in key
    )
    options, collected_id, collected_state, page, _, error = create_case(1, token, selected)
    assert collected_id != demo_id and collected_state["case_id"] == collected_id
    assert page == "review" and error == ""
    assert {item["value"] for item in options} == {demo_id, collected_id}
    saved_case = service.load_case(collected_id)
    assert saved_case.case.synthetic is False
    assert saved_case.records[0].metadata["synthetic"] is False
    assert saved_case.records[0].text == collected.text
    assert saved_case.records[0].published_at is None
    assert banner(app, collected_state) == ""
    assert banner(app, demo_state) == demo_banner
    assert banner(app, {}) == ""

    cards = callback(app, "rv-case-list.children")(demo_state)[1]
    collected_card = next(
        node
        for node in components(cards)
        if isinstance(node, html.Section) and saved_case.case.title in text(node)
    )
    assert "Synthetic example" not in text(collected_card)
    assert service.get_case(demo_id) == demo_before
    assert service.repository.list_events(demo_id) == events_before
    assert service.collection.repository.items() == saved_collection


@pytest.mark.parametrize("claim_check", [False, True])
def test_synthetic_case_banner_preserves_real_and_unknown_records_through_switching_and_package(
    tmp_path, claim_check
):
    service = workspace(tmp_path / "desk")
    payload = mixed_case(claim_check=claim_check)
    case_id = service.import_bundle(payload)
    state = session_for_case(service, case_id)
    baseline = service.baseline_scenario(case_id, state["finding_id"])
    review = service.review(
        case_id,
        state["finding_id"],
        baseline["id"],
        "qualify",
        "Preserve each source's own origin.",
    )
    before = service.get_case(case_id)
    fingerprint = service.case_fingerprint(case_id)
    events = service.repository.list_events(case_id)
    app = create_app(service)

    displayed = banner(app, state)
    assert "Selected case: Synthetic example" in displayed
    assert "Fictional evidence" not in displayed
    assert "Selected case: Test claim" not in displayed
    assert "The original claim is synthetic" not in displayed
    assert "This label applies to the selected case" in displayed
    assert "Saved records retain their own provenance" in displayed
    if claim_check:
        selected_claim = {**state, "finding_id": payload["findings"][0]["snapshot_id"]}
        heading = text(callback(app, "rv-synthetic.children")(selected_claim)[2])
        assert "Claim origin not specified" in heading
        assert "Synthetic test claim" not in heading
    # Selecting a real or unknown source cannot change its origin based on the case banner.
    for record in payload["records"][:3]:
        selected = {**state, "record_id": record["id"]}
        assert banner(app, selected) == displayed
    assert service.get_case(case_id) == before
    assert service.case_fingerprint(case_id) == fingerprint
    assert service.repository.list_events(case_id) == events

    exported = service.export_bundle(case_id)
    package = service.export_package(case_id)
    target = workspace(tmp_path / "restored")
    assert target.import_package(package) == case_id
    restored = target.load_case(case_id)
    assert restored.model_dump(mode="json") == exported.model_dump(mode="json")
    assert restored.records[0].metadata["synthetic"] is False
    assert restored.records[1].metadata["synthetic"] is True
    assert "synthetic" not in restored.records[2].metadata
    assert any(
        saved.id == review["id"] and saved.reason == review["reason"] for saved in restored.reviews
    )
    if claim_check:
        assert restored.findings[0].details["claim_check"]["source_synthetic"] is True
    reopened_app = create_app(target)
    assert banner(reopened_app, session_for_case(target, case_id)) == displayed
    assert target.import_package(package) == case_id
    assert target.load_case(case_id).reviews == restored.reviews
    assert service.repository.list_events(case_id) == events
