"""Handoffs preserve assessment scope, omissions, saved evidence and review authority."""

import base64
import hashlib
import io
import json
import zipfile
from copy import deepcopy

import pytest
from dash import html
from dash.development.base_component import Component

from evidence_explorer.config import Settings
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.drafts import draft_key
from evidence_explorer.review.integrations.handoff import (
    project_handoff,
    project_import_receipt,
    render_dossier,
)
from evidence_explorer.review.integrations.handoff_service import (
    load_handoff_context,
    prepare_export,
)
from evidence_explorer.review.integrations.handoff_ui import render_handoff, render_import_receipt
from evidence_explorer.review.integrations.snapshots import workspace_snapshot
from evidence_explorer.review.integrations.web import create_app
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.navigation import apply_navigation, navigation_context
from evidence_explorer.review.selectors import source_for_finding
from evidence_explorer.review.workflow import assessment_context

PURPOSE = "cited_claim_investigation"
UNSUPPORTED = "handoff:unsupported"


def workspace(path):
    settings = Settings(data_dir=path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    return WorkspaceService(repository, settings, path / "artifacts")


def state_for(service, case_id, finding_id="harbor:c1-v1", scenario_id=None):
    view = service.view(case_id, scenario_id)
    context = assessment_context(view)
    return {
        **session_for_case(service, case_id),
        "finding_id": finding_id,
        "record_id": source_for_finding(view["bundle"], finding_id),
        "scenario_id": scenario_id,
        "scenario_version": context["version"],
        "assessment_kind": "baseline" if context["is_baseline"] else "scenario",
    }


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


def post_callback(app, output_fragment, trigger, values):
    """Invoke the registered Dash HTTP contract without duplicating argument order."""
    key = next(
        key
        for key, definition in app.callback_map.items()
        if output_fragment in key and any(item["id"] == trigger for item in definition["inputs"])
    )
    definition = app.callback_map[key]
    declared = definition["output"]

    def output(item):
        return {"id": item.component_id, "property": item.component_property}

    outputs = (
        [output(item) for item in declared]
        if isinstance(declared, (list, tuple))
        else output(declared)
    )
    inputs = [
        {
            **item,
            "value": int(item["id"] == trigger)
            if item["property"] == "n_clicks"
            else values.get(item["id"]),
        }
        for item in definition["inputs"]
    ]
    response = app.server.test_client().post(
        "/_dash-update-component",
        json={
            "output": key,
            "outputs": outputs,
            "inputs": inputs,
            "state": [{**item, "value": values.get(item["id"])} for item in definition["state"]],
            "changedPropIds": [
                trigger + "." + next(item["property"] for item in inputs if item["id"] == trigger)
            ],
        },
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["response"]


@pytest.fixture
def desk(tmp_path):
    service = workspace(tmp_path / "source")
    payload = load_demo_bundle().model_dump(mode="json")
    payload["findings"].append(
        {
            "snapshot_id": UNSUPPORTED,
            "lineage_id": UNSUPPORTED,
            "version": 1,
            "run_id": payload["runs"][0]["id"],
            "kind": "assumption",
            "statement": "An unsupported test assertion has no saved source reference.",
        }
    )
    case_id = service.import_bundle(payload)
    baseline = service.baseline_scenario(case_id, "harbor:c1-v1")
    service.review(case_id, "harbor:c1-v1", baseline["id"], "use", "Baseline wording approved.")
    service.review(
        case_id, "harbor:c2-v1", baseline["id"], "withhold", "Attribution remains unproven."
    )
    service.review(
        case_id, UNSUPPORTED, baseline["id"], "qualify", "Assumption; source support missing."
    )
    return service, case_id


def scenario_with_stale_review(service, case_id):
    scenario = service.start_scenario(case_id, "harbor:c1-v1")
    service.review(case_id, "harbor:c1-v1", scenario["id"], "qualify", "Earlier scenario wording.")
    scenario = service.exclude(
        scenario["id"], "harbor:r03", True, "Inspect without this post.", scenario["version"]
    )
    service.review(
        case_id, "harbor:c2-v1", scenario["id"], "withhold", "Scenario attribution withheld."
    )
    return scenario


def test_handoff_uses_selected_assessment_and_explains_omissions_without_changing_reviews(desk):
    service, case_id = desk
    baseline = project_handoff(service.view(case_id), purpose_id=PURPOSE)
    scenario = scenario_with_stale_review(service, case_id)
    events = service.repository.list_events(case_id)
    fingerprint = service.case_fingerprint(case_id)
    selected = project_handoff(service.view(case_id, scenario["id"]), purpose_id=PURPOSE)

    assert baseline["assessment"]["label"] == "Baseline"
    assert selected["assessment"]["id"] == scenario["id"]
    assert selected["assessment"]["version"] == scenario["version"]
    assert selected["assessment"]["label"] != baseline["assessment"]["label"]
    assert {row["finding_id"] for row in baseline["brief"]["included"]} == {
        "harbor:c1-v1",
        UNSUPPORTED,
    }
    assert selected["brief"]["included"] == []
    omitted = {row["finding_id"]: row for row in selected["brief"]["omitted"]}
    assert omitted["harbor:c1-v1"]["status"] == "needs_review"
    assert "Earlier decision" in omitted["harbor:c1-v1"]["reason"]
    assert omitted["harbor:c2-v1"]["status"] == "withheld"
    assert omitted["harbor:c2-v1"]["reason"] == "Scenario attribution withheld."
    assert omitted[UNSUPPORTED]["status"] == "not_reviewed"
    assert UNSUPPORTED in baseline["readiness"]["missing_evidence_finding_ids"]
    assert any(gap["finding_id"] == UNSUPPORTED for gap in baseline["gaps"])
    assert selected["counts"]["current_decisions"] == 1
    assert selected["counts"]["withheld"] == 1
    assert selected["counts"]["stale"] >= 1
    assert project_handoff(service.view(case_id), purpose_id=PURPOSE) == baseline
    assert service.repository.list_events(case_id) == events
    assert service.case_fingerprint(case_id) == fingerprint


def test_draft_dossier_keeps_scope_missing_requirements_and_dated_references_explicit(desk):
    service, case_id = desk
    view = service.view(case_id)
    unscoped_dossier = render_dossier(project_handoff(view, purpose_id=PURPOSE))
    assert (
        "Native collection source selection: Not recorded in native evidence." in unscoped_dossier
    )
    assert "Native collection source selection: `null`" not in unscoped_dossier
    view["bundle"]["case"]["coverage"]["collection_scope"] = {
        "source_ids": ["saved-source-a"],
        "record_count": 10,
        "unknown_publication_times": 2,
    }
    handoff = project_handoff(view, purpose_id="platform_evidence_dossier")
    assert handoff["purpose"]["confirmed"] is False
    assert handoff["purpose"]["persisted"] is False
    assert handoff["purpose"]["status"] == "session_only_unconfirmed"
    assert (
        handoff["scope"]["collection_scope"]
        == view["bundle"]["case"]["coverage"]["collection_scope"]
    )
    assert handoff["readiness"]["policy_status"] == "unknown"
    assert handoff["readiness"]["submission_status"] == "not_submitted"
    assert handoff["readiness"]["status"] == "needs_review"
    assert handoff["export_allowed"] is True, "A draft may describe unresolved requirements"
    dossier = render_dossier(handoff)
    assert "draft" in dossier.lower()
    assert "Baseline wording approved." in dossier
    assert "Attribution remains unproven." in dossier
    assert UNSUPPORTED in dossier
    assert "not submitted" in dossier.lower() or "not_submitted" in dossier
    assert handoff["references"], "A platform dossier must show the dated reference metadata"
    for reference in handoff["references"]:
        assert reference["applicability"] == "unknown"
        assert reference["retrieved_at"] and reference["retrieved_at"] in dossier
        assert reference["url"] in dossier
    dirty = project_handoff(view, purpose_id=PURPOSE, dirty=True)
    assert dirty["export_allowed"] is False
    with pytest.raises(ValueError):
        render_dossier(dirty)


def test_handoff_actions_reopen_omitted_findings_in_same_assessment_without_writes(desk):
    service, case_id = desk
    scenario = scenario_with_stale_review(service, case_id)
    state = state_for(service, case_id, scenario_id=scenario["id"])
    view = service.view(case_id, scenario["id"])
    handoff = project_handoff(view, purpose_id=PURPOSE)
    rendered = render_handoff(handoff, view, state)
    buttons = [
        node
        for node in components(rendered)
        if isinstance(node, html.Button)
        and isinstance(getattr(node, "id", None), dict)
        and node.id.get("type") == "rv-next-action"
    ]
    ids = [json.dumps(button.id, sort_keys=True) for button in buttons]
    assert len(ids) == len(set(ids)), "Multiple gaps must not create duplicate Dash IDs"
    finding_actions = [button for button in buttons if button.id["fid"]]
    assert {row["finding_id"] for row in handoff["brief"]["omitted"]} <= {
        button.id["fid"] for button in finding_actions
    }
    before = service.repository.list_events(case_id)
    for button in finding_actions:
        navigated = apply_navigation(service, state, button.id)
        assert navigated["case_id"] == case_id
        assert navigated["scenario_id"] == scenario["id"]
        assert navigated["scenario_version"] == scenario["version"]
        assert navigated["finding_id"] == button.id["fid"]
        assert navigated["navigate_to"] == "review"
    assert service.repository.list_events(case_id) == before


@pytest.mark.parametrize("trigger", ["trail-brief", "trail-package", "trail-json", "trail-dossier"])
@pytest.mark.parametrize("draft_location", ["selected", "other-finding"])
def test_registered_exports_block_unsaved_approval_in_the_selected_assessment(
    desk, trigger, draft_location
):
    service, case_id = desk
    app = create_app(service)
    state = state_for(service, case_id)
    view = service.view(case_id)
    draft_state = state if draft_location == "selected" else {**state, "finding_id": "harbor:c3-v1"}
    unsaved = "Unsaved wording must not become approved handoff content."
    drafts = {"notes": {draft_key(draft_state, view): unsaved}}
    before = service.repository.list_events(case_id)
    response = post_callback(
        app,
        "trail-download.data",
        trigger,
        {
            "rv-session": state,
            "rv-draft-store": drafts,
            "rv-review-reason": unsaved
            if draft_location == "selected"
            else "Baseline wording approved.",
            "trail-use-case-purpose": {"purpose_id": PURPOSE},
        },
    )
    assert not response.get("trail-download", {}).get("data")
    assert "unsaved" in json.dumps(response).lower()
    assert unsaved not in json.dumps(response)
    assert service.repository.list_events(case_id) == before


def test_registered_dossier_download_names_the_selected_scenario_and_retains_gaps(desk):
    service, case_id = desk
    scenario = scenario_with_stale_review(service, case_id)
    state = state_for(service, case_id, "harbor:c2-v1", scenario["id"])
    response = post_callback(
        create_app(service),
        "trail-download.data",
        "trail-dossier",
        {
            "rv-session": state,
            "rv-draft-store": {},
            "rv-review-reason": "Scenario attribution withheld.",
            "trail-use-case-purpose": {"purpose_id": PURPOSE},
            "trail-handoff-context": load_handoff_context(service, state)[1]["download_context"],
        },
    )
    download = response["trail-download"]["data"]
    assert download["filename"].endswith(".md")
    assert "draft" in download["content"].lower()
    assert "Scenario attribution withheld." in download["content"]
    assert "Earlier decision" in download["content"]
    assert "Baseline wording approved." not in download["content"]


def test_dossier_prepared_notice_clears_when_its_context_changes(desk):
    service, case_id = desk
    app = create_app(service)
    state = state_for(service, case_id)
    values = {
        "rv-session": state,
        "rv-draft-store": {},
        "rv-review-reason": "Baseline wording approved.",
        "trail-use-case-purpose": {"purpose_id": PURPOSE},
        "trail-handoff-context": load_handoff_context(service, state)[1]["download_context"],
    }
    before = service.repository.list_events(case_id)
    prepared = post_callback(app, "trail-download.data", "trail-dossier", values)
    assert prepared["trail-dossier-message"]["children"]
    changes = {
        "rv-session": {**state, "revision": state.get("revision", 0) + 1},
        "rv-draft-store": {"dirty": True},
        "rv-review-reason": "A newly edited note has not been saved.",
        "trail-use-case-purpose": {"purpose_id": "platform_evidence_dossier"},
    }
    for trigger, value in changes.items():
        cleared = post_callback(
            app, "trail-dossier-message.children", trigger, {**values, trigger: value}
        )
        assert cleared["trail-dossier-message"]["children"] == ""
        assert "trail-download" not in cleared
    assert service.repository.list_events(case_id) == before


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_missing_or_corrupt_media_is_explicit_and_blocks_portable_package(desk, damage):
    service, case_id = desk
    payload = service.get_case(case_id)
    payload["case"]["id"] = "handoff:media"
    content = b'<svg xmlns="http://www.w3.org/2000/svg"><text>Test frame</text></svg>'
    path = service.artifact_root / "frames/frame.svg"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    payload["records"].append(
        {
            "id": "handoff:image",
            "kind": "image",
            "source_label": "Saved test frame",
            "artifact_ref": "frames/frame.svg",
            "content_hash": hashlib.sha256(content).hexdigest(),
        }
    )
    media_id = service.import_bundle(payload)
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"changed frame")
    state = state_for(service, media_id)
    app = create_app(service)
    status = post_callback(
        app,
        "trail-package.disabled",
        "rv-session",
        {
            "rv-session": state,
            "rv-draft-store": {},
            "rv-review-reason": "",
            "trail-handoff-context": load_handoff_context(service, state)[1]["download_context"],
        },
    )
    assert status["trail-package"]["disabled"] is True
    assert "artifact" in json.dumps(status).lower() or "media" in json.dumps(status).lower()
    with pytest.raises(ValueError):
        service.export_package(media_id)
    handoff = project_handoff(
        service.view(media_id), artifact_availability={"handoff:image": False}
    )
    assert handoff["counts"]["unavailable_media"] == 1
    assert "handoff:image" in handoff["readiness"]["unavailable_record_ids"]
    assert any(gap["record_id"] == "handoff:image" for gap in handoff["gaps"])
    unknown = project_handoff(service.view(media_id))
    assert unknown["counts"]["unknown_media"] == 1


def test_validated_package_receipt_preserves_counts_decisions_and_exact_references(desk, tmp_path):
    service, case_id = desk
    scenario_with_stale_review(service, case_id)
    exported = service.export_bundle(case_id)
    package = service.export_package(case_id)
    target = workspace(tmp_path / "recipient")
    app = create_app(target)
    response = post_callback(
        app,
        "trail-import-message.children",
        "trail-upload",
        {"trail-upload": "data:application/zip;base64," + base64.b64encode(package).decode()},
    )
    assert response["rv-case"]["value"] == case_id
    reopened = target.load_case(case_id)
    assert reopened.model_dump() == exported.model_dump()
    receipt = project_import_receipt(reopened)
    assert receipt["validation"]["status"] == "validated_saved_snapshot"
    assert receipt["validation"]["recipient_acceptance"] == "not_recorded"
    for name in ("records", "fragments", "runs", "reviews"):
        assert receipt["counts"][name] == len(getattr(exported, name))
    assert receipt["counts"]["assessments"] == len(exported.scenarios)
    assert receipt["recomputation"]["available"] is False
    rendered = text(render_import_receipt(receipt))
    assert "recomputation" in rendered.lower()
    receipt_text = json.dumps(response["trail-import-message"])
    assert "text hashes" in receipt_text and "source references validate" in receipt_text
    assert "Recipient acceptance is not recorded" in receipt_text
    records = {record.id: record for record in reopened.records}
    for fragment in reopened.fragments:
        assert (
            records[fragment.record_id].text[fragment.selector.start : fragment.selector.end]
            == fragment.quote
        )
    assert {review.id for review in reopened.reviews} == {review.id for review in exported.reviews}


def test_invalid_package_never_produces_a_validated_receipt(desk, tmp_path):
    service, case_id = desk
    package = service.export_package(case_id)
    altered = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(package)) as source,
        zipfile.ZipFile(altered, "w") as destination,
    ):
        for name in source.namelist():
            content = source.read(name)
            if name == "bundle.json":
                payload = json.loads(content)
                payload["case"]["title"] = "Changed after package validation"
                content = json.dumps(payload).encode()
            destination.writestr(name, content)
    target = workspace(tmp_path / "recipient")
    response = post_callback(
        create_app(target),
        "trail-import-message.children",
        "trail-upload",
        {
            "trail-upload": "data:application/zip;base64,"
            + base64.b64encode(altered.getvalue()).decode()
        },
    )
    assert not response.get("rv-case", {}).get("value")
    assert target.list_cases() == []
    assert "verification failed" in json.dumps(response).lower()
    assert "validated_saved_snapshot" not in json.dumps(response)


@pytest.mark.parametrize("context", [None, {"baseline_run_id": "harbor:run-v1", "dataset": {}}])
def test_receipt_does_not_invent_recomputation_from_capability_labels_alone(desk, context):
    service, case_id = desk
    payload = deepcopy(service.get_case(case_id))
    payload["runs"][0].update(
        method="swarm_detector.detector.analyze",
        recomputation_capability="swarm_full_subset",
        full_inputs_available=True,
    )
    if context is not None:
        payload["swarm_context"] = context
    receipt = project_import_receipt(EvidenceBundle.model_validate(payload))
    assert receipt["recomputation"]["available"] is False
    assert receipt["recomputation"]["reason"]


def change_saved_context(service, case_id, scenario_id, change):
    """Commit a realistic competing edit through a separate SQLite connection."""
    if change == "records":
        payload = service.repository.get_bundle(case_id).model_dump(mode="json")
        added = {**payload["records"][0], "id": "handoff:concurrent-source"}
        payload["records"].append(added)
        service.repository.import_bundle(EvidenceBundle.model_validate(payload))
    elif change == "scenario":
        scenario = service.load_scenario(scenario_id)
        service.exclude(
            scenario_id,
            "harbor:r03",
            True,
            "Concurrent source exclusion.",
            scenario.version,
        )
    else:
        assert change == "review"
        service.review(
            case_id,
            "harbor:c1-v1",
            scenario_id,
            "withhold",
            "Concurrent saved decision.",
        )


def test_export_snapshot_rejects_writes_and_releases_access_without_invalidating_values(desk):
    service, case_id = desk
    before = service.export_bundle(case_id).model_dump(mode="json")
    events = service.repository.list_events(case_id)
    changed = service.repository.get_bundle(case_id).model_dump(mode="json")
    changed["records"].append({**changed["records"][0], "id": "handoff:forbidden-write"})
    replacement = EvidenceBundle.model_validate(changed)

    with workspace_snapshot(service) as snapshot:
        with pytest.raises(PermissionError, match="read-only"):
            snapshot.repository.import_bundle(replacement)
        captured_bundle = snapshot.export_bundle(case_id)
        captured_view = snapshot.view(case_id)

    assert service.export_bundle(case_id).model_dump(mode="json") == before
    assert service.repository.list_events(case_id) == events
    assert json.loads(captured_bundle.model_dump_json()) == before
    assert "Baseline wording approved." in render_dossier(project_handoff(captured_view))
    with pytest.raises(RuntimeError, match="snapshot has been released"):
        snapshot.view(case_id)


@pytest.mark.parametrize("kind", ["package", "json", "brief"])
@pytest.mark.parametrize("change", ["records", "scenario", "review"])
def test_export_rejects_previously_valid_context_after_saved_changes(desk, kind, change):
    service, case_id = desk
    scenario = service.start_scenario(case_id, "harbor:c1-v1")
    service.review(case_id, "harbor:c1-v1", scenario["id"], "use", "Selected scenario wording.")
    state = state_for(service, case_id, scenario_id=scenario["id"])
    context = load_handoff_context(service, state)
    change_saved_context(service, case_id, scenario["id"], change)
    if change == "review":
        # Review edits do not alter the navigation binding or scenario version.
        # The full saved view still changed and must not authorize this download.
        assert navigation_context(service.view(case_id, scenario["id"]), state) == context[2]
    events = service.repository.list_events(case_id)

    with pytest.raises(ValueError, match="(?i)(changed|stale|refresh|reopen)"):
        prepare_export(service, kind, state, context=context)

    assert service.repository.list_events(case_id) == events


@pytest.mark.parametrize("kind", ["package", "json", "brief"])
@pytest.mark.parametrize("change", ["records", "scenario", "review"])
def test_export_keeps_one_snapshot_when_saved_data_changes_during_capture(
    desk, monkeypatch, kind, change
):
    service, case_id = desk
    scenario = service.start_scenario(case_id, "harbor:c1-v1")
    service.review(case_id, "harbor:c1-v1", scenario["id"], "use", "Selected scenario wording.")
    state = state_for(service, case_id, scenario_id=scenario["id"])
    context = load_handoff_context(service, state)
    expected_bundle = service.export_bundle(case_id).model_dump(mode="json")
    expected_brief = service.export_brief(case_id, scenario_id=scenario["id"])
    original_view = WorkspaceService.view
    committed = False

    def view_then_commit(snapshot_service, selected_case, scenario_id=None):
        nonlocal committed
        view = original_view(snapshot_service, selected_case, scenario_id)
        if selected_case == case_id and not committed:
            committed = True
            change_saved_context(service, case_id, scenario["id"], change)
        return view

    with monkeypatch.context() as patch:
        patch.setattr(WorkspaceService, "view", view_then_commit)
        prepared = prepare_export(service, kind, state, context=context)

    assert committed, "The competing transaction must commit inside the capture window"
    assert service.export_bundle(case_id).model_dump(mode="json") != expected_bundle
    assert prepared.binding == context[2]
    if kind == "package":
        with zipfile.ZipFile(io.BytesIO(prepared.content)) as package:
            exported = json.loads(package.read("bundle.json"))
        assert exported == expected_bundle
    elif kind == "json":
        assert json.loads(prepared.content) == expected_bundle
    else:
        assert prepared.content == expected_brief
