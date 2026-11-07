"""The same review-to-export contract applies to investigations, swarms and media."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from evidence_explorer.config import Settings
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.integrations.media import (
    build_media_demo_bundle,
    media_artifact_root,
)
from evidence_explorer.review.integrations.packages import export_case_package
from evidence_explorer.review.integrations.web import create_app
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.selectors import initial_selection
from evidence_explorer.review.workflow import assessment_decisions


def workspace(path: Path) -> WorkspaceService:
    settings = Settings(data_dir=path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    return WorkspaceService(repository, settings, path / "artifacts")


@pytest.fixture(scope="module")
def source_bundles():
    return {
        "investigation": load_demo_bundle(),
        "media": build_media_demo_bundle(),
    }


@pytest.fixture(scope="module")
def swarm_bundle():
    """Run the small deterministic detector once, outside the parameterized flows."""
    from evidence_explorer.review.integrations.swarm import build_swarm_bundle

    detector = pytest.importorskip("swarm_detector.detector")
    sample_data = pytest.importorskip("swarm_detector.sample_data")
    dataset = sample_data.build_sample()
    return build_swarm_bundle(dataset, detector.analyze(dataset, {"randomizations": 3, "seed": 42}))


@pytest.fixture(params=["investigation", "swarm", "media"])
def case_workspace(request, source_bundles, tmp_path):
    service = workspace(tmp_path / "original")
    source = (
        request.getfixturevalue("swarm_bundle")
        if request.param == "swarm"
        else source_bundles[request.param]
    )
    if request.param == "media":
        service.import_package(export_case_package(source, media_artifact_root()))
    else:
        service.import_bundle(source.model_dump(mode="json"))
    return request.param, service, source.case.id


def test_initial_review_opens_a_record_in_the_selected_findings_evidence(case_workspace):
    """Indirect media premises must be as inspectable as a directly cited post."""
    _, service, case_id = case_workspace
    view = service.view(case_id)
    finding_id, record_id = initial_selection(view["bundle"])
    assert finding_id is not None
    assert record_id is not None, "The default finding needs an inspectable source selection"
    assert record_id in view["impact"]["remaining_evidence"][finding_id]
    record = next(record for record in view["bundle"]["records"] if record["id"] == record_id)
    assert record.get("text") or record.get("artifact_ref")


def test_complete_review_to_export_preserves_assessment_and_exact_evidence(
    case_workspace, tmp_path
):
    kind, service, case_id = case_workspace
    original_view = service.view(case_id)
    finding_id, record_id = initial_selection(original_view["bundle"])
    assert finding_id and record_id
    fingerprint = original_view["case_fingerprint"]

    # Baseline decisions have a durable home but opening the baseline stays read-only.
    baseline = service.baseline_scenario(case_id, finding_id)
    baseline_reason = f"Baseline qualification for {kind}: the supplied records have limits."
    original_review = service.review(
        case_id,
        finding_id,
        baseline["id"],
        "qualify",
        baseline_reason,
        expected_version=baseline["version"],
        expected_fingerprint=fingerprint,
    )
    assert service.baseline_scenario(case_id, finding_id)["id"] == baseline["id"]
    baseline_decisions = assessment_decisions(service.view(case_id))
    assert baseline_decisions[finding_id]["id"] == original_review["id"]
    baseline_events = service.repository.list_events(case_id)
    service.view(case_id)
    service.view(case_id)
    assert service.repository.list_events(case_id) == baseline_events

    # A source test creates a separate assessment and never transfers the baseline decision.
    scenario = service.start_scenario(case_id, finding_id)
    assert scenario["id"] != baseline["id"]
    excluded = service.exclude(
        scenario["id"], record_id, True, "Check dependence on this supplied record.", 1
    )
    scenario_view = service.view(case_id, scenario["id"])
    assert finding_id in scenario_view["impact"]["affected_finding_ids"]
    assert record_id not in scenario_view["impact"]["remaining_evidence"][finding_id]
    assert finding_id not in assessment_decisions(scenario_view)
    assert assessment_decisions(service.view(case_id))[finding_id]["id"] == original_review["id"]
    scenario_reason = f"Scenario withholding for {kind}: further corroboration is needed."
    revised_review = service.review(
        case_id,
        finding_id,
        scenario["id"],
        "withhold",
        scenario_reason,
        expected_version=excluded["version"],
        expected_fingerprint=fingerprint,
    )
    current_scenario = assessment_decisions(service.view(case_id, scenario["id"]))
    assert current_scenario[finding_id]["id"] == revised_review["id"]

    # The human-readable brief uses only the active assessment's saved decisions.
    baseline_brief = service.export_brief(case_id, scenario_id=None)
    scenario_brief = service.export_brief(case_id, scenario_id=scenario["id"])
    assert baseline_reason in baseline_brief
    assert scenario_reason not in baseline_brief
    assert scenario_reason in scenario_brief
    assert baseline_reason not in scenario_brief
    assert "withhold" in scenario_brief.lower()
    assert "omitted" in scenario_brief.lower()
    assert "Synthetic" in baseline_brief

    # Restoring the record changes the scenario version; its old decision needs review.
    restored = service.exclude(
        scenario["id"],
        record_id,
        False,
        "Restore the record for another assessment.",
        excluded["version"],
    )
    assert restored["version"] > excluded["version"]
    assert finding_id not in assessment_decisions(service.view(case_id, scenario["id"]))
    assert (
        record_id
        in service.view(case_id, scenario["id"])["impact"]["remaining_evidence"][finding_id]
    )
    assert assessment_decisions(service.view(case_id))[finding_id]["id"] == original_review["id"]
    assert scenario_reason not in service.export_brief(case_id, scenario_id=scenario["id"])

    # Reopening an exported case needs neither the detector nor the original artifact folder.
    exported = service.export_bundle(case_id)
    package = service.export_package(case_id)
    assert package.startswith(b"PK")
    reopened = workspace(tmp_path / "reopened")
    assert reopened.import_package(package) == case_id
    assert reopened.repository.get_bundle(case_id).model_dump() == exported.model_dump()
    assert EvidenceBundle.model_validate_json(exported.model_dump_json()) == exported
    reviews = {review.id: review for review in exported.reviews}
    assert {original_review["id"], revised_review["id"]} <= set(reviews)
    assert reviews[original_review["id"]].scenario_id != reviews[revised_review["id"]].scenario_id
    snapshots = {scenario.id: scenario for scenario in exported.scenarios}
    assert snapshots[reviews[original_review["id"]].scenario_id].excluded_record_ids == []
    assert record_id in snapshots[reviews[revised_review["id"]].scenario_id].excluded_record_ids
    assert assessment_decisions(reopened.view(case_id))[finding_id]["reason"] == baseline_reason
    assert baseline_reason in reopened.export_brief(case_id, scenario_id=None)

    for record in exported.records:
        if record.artifact_ref:
            path = reopened.artifact(case_id, record.id)
            assert hashlib.sha256(path.read_bytes()).hexdigest() == record.content_hash
    records = {record.id: record for record in exported.records}
    for fragment in exported.fragments:
        assert fragment.record_id in records
        if fragment.selector.kind == "text":
            selector = fragment.selector
            assert records[fragment.record_id].text[selector.start : selector.end] == fragment.quote

    # Unchanged reimport does not duplicate the preserved reviews or alter source identities.
    reopened.import_package(package)
    assert reopened.repository.get_bundle(case_id).model_dump() == exported.model_dump()


def _post_action(app, output_fragment, trigger, values):
    """Exercise registered Dash callbacks through Flask's public HTTP transport."""
    key = next(
        key
        for key, definition in app.callback_map.items()
        if output_fragment in key and any(item["id"] == trigger for item in definition["inputs"])
    )
    definition = app.callback_map[key]
    declared_outputs = definition["output"]
    if isinstance(declared_outputs, (list, tuple)):
        outputs = [
            {"id": output.component_id, "property": output.component_property}
            for output in declared_outputs
        ]
    else:
        outputs = {
            "id": declared_outputs.component_id,
            "property": declared_outputs.component_property,
        }
    inputs = []
    for item in definition["inputs"]:
        value = values.get(item["id"])
        if item["property"] == "n_clicks":
            value = [] if item["id"].startswith("{") else int(item["id"] == trigger)
        inputs.append({**item, "value": value})
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


def _wire_components(value):
    if isinstance(value, dict) and "props" in value:
        yield value["props"]
        yield from _wire_components(value["props"].get("children"))
    elif isinstance(value, (tuple, list)):
        for child in value:
            yield from _wire_components(child)


def _wire_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _wire_text(value.get("props", {}).get("children", ""))
    if isinstance(value, (tuple, list)):
        return " ".join(_wire_text(child) for child in value)
    return ""


def test_registered_workspace_renders_metadata_and_active_brief(case_workspace):
    """Typed repository metadata must cross the real Dash rendering boundary."""
    kind, service, case_id = case_workspace
    original = service.view(case_id)
    finding_id, record_id = initial_selection(original["bundle"])
    baseline = service.baseline_scenario(case_id, finding_id)
    note = f"Saved {kind} qualification shown in the active brief preview."
    service.review(case_id, finding_id, baseline["id"], "qualify", note)
    scenario = service.start_scenario(case_id, finding_id)
    scenario = service.exclude(
        scenario["id"], record_id, True, "Test a separate assessment.", scenario["version"]
    )
    state = {
        "case_id": case_id,
        "case_fingerprint": service.case_fingerprint(case_id),
        "finding_id": finding_id,
        "record_id": record_id,
        "scenario_id": baseline["id"],
        "scenario_version": baseline["version"],
        "assessment_kind": "baseline",
    }
    app = create_app(service)
    rendered = _post_action(app, "rv-context.children", "rv-session", {"rv-session": state})
    case = service.repository.get_case_metadata(case_id).case
    context = _wire_text(rendered["rv-context"]["children"])
    assert case.title in context
    assert "Baseline" in context
    assert baseline["base_run_id"] in context
    cards = rendered["rv-case-list"]["children"]
    assert case.title in _wire_text(cards)
    assert case.question in _wire_text(cards)
    expected_labels = {
        "investigation": "Agent investigation",
        "swarm": "Behavioral analysis",
        "media": "Historical media",
    }
    assert expected_labels[kind] in _wire_text(cards)
    card_ids = [props.get("id") for props in _wire_components(cards) if props.get("id")]
    assert {"type": "rv-open-case", "cid": case_id} in card_ids
    assert "Continue review" in _wire_text(cards)
    preview = _wire_text(rendered["rv-brief-preview"]["children"])
    assert note in preview
    assert "Use with qualification" in preview
    assert "1 included" in preview
    assert {option["value"] for option in rendered["rv-scenario"]["options"]} >= {
        "baseline",
        scenario["id"],
    }
    assert rendered["rv-back-baseline"]["disabled"]

    # Switching assessments changes context and preview together; baseline approval stays separate.
    scenario_state = {
        **state,
        "scenario_id": scenario["id"],
        "scenario_version": scenario["version"],
        "assessment_kind": "scenario",
    }
    switched = _post_action(
        app, "rv-context.children", "rv-session", {"rv-session": scenario_state}
    )
    assert "Scenario 1" in _wire_text(switched["rv-context"]["children"])
    assert note not in _wire_text(switched["rv-brief-preview"]["children"])
    assert "0 included" in _wire_text(switched["rv-brief-preview"]["children"])
    assert not switched["rv-back-baseline"]["disabled"]


def test_specialist_result_transition_synchronizes_the_assessment_picker(swarm_bundle, tmp_path):
    service = workspace(tmp_path / "specialist-picker")
    case_id = service.import_bundle(swarm_bundle.model_dump(mode="json"))
    baseline = service.repository.get_bundle(case_id)
    finding = baseline.findings[0]
    scenario = service.start_scenario(case_id, finding.snapshot_id)
    payload = baseline.model_dump(mode="json")
    result_run_id, result_finding_id = "picker:result-run", "picker:result-finding"
    payload["runs"].append({**payload["runs"][0], "id": result_run_id})
    payload["findings"].append(
        {
            **payload["findings"][0],
            "snapshot_id": result_finding_id,
            "run_id": result_run_id,
            "version": 2,
        }
    )
    service.import_bundle(payload)
    job = service.repository.enqueue_job(
        case_id, finding.snapshot_id, scenario["id"], "swarm_recompute"
    )
    running = service.repository.claim_job()
    service.repository.finish_job(
        job["id"], {"result_run_id": result_run_id}, expected_attempt=running["attempts"]
    )
    app = create_app(service)
    state = {
        "case_id": case_id,
        "case_fingerprint": baseline.fingerprint(),
        "finding_id": finding.snapshot_id,
        "record_id": None,
        "scenario_id": scenario["id"],
        "scenario_version": scenario["version"],
    }
    transition = _post_action(
        app, "trail-operation-message.children", "trail-result", {"rv-session": state}
    )
    active = transition["rv-session"]["data"]
    assert active["finding_id"] == result_finding_id
    assert service.repository.get_scenario(active["scenario_id"])["base_run_id"] == result_run_id
    picker = _post_action(
        app,
        "rv-scenario.value",
        "trail-operation-message",
        {
            "rv-session": active,
            "trail-operation-message": transition["trail-operation-message"]["children"],
        },
    )
    assert picker["rv-scenario"]["value"] == active["scenario_id"]
    rendered = _post_action(app, "rv-context.children", "rv-session", {"rv-session": active})
    assert active["scenario_id"] in {
        option["value"] for option in rendered["rv-scenario"]["options"]
    }
    restored = _post_action(
        app, "trail-operation-message.children", "trail-original", {"rv-session": active}
    )
    picker = _post_action(
        app,
        "rv-scenario.value",
        "trail-operation-message",
        {
            "rv-session": restored["rv-session"]["data"],
            "trail-operation-message": restored["trail-operation-message"]["children"],
        },
    )
    assert picker["rv-scenario"]["value"] == "baseline"


def test_registered_dash_review_and_download_flow(case_workspace, tmp_path):
    """Saving in the shared UI and downloading must use the same active case."""
    kind, service, case_id = case_workspace
    view = service.view(case_id)
    finding_id, record_id = initial_selection(view["bundle"])
    note = f"Saved through the shared Dash screen for {kind}."
    state = {
        "case_id": case_id,
        "case_fingerprint": view["case_fingerprint"],
        "finding_id": finding_id,
        "record_id": record_id,
        "scenario_id": None,
        "scenario_version": None,
        "job_id": None,
        "revision": 0,
    }
    values = {
        "rv-session": state,
        "rv-case": case_id,
        "rv-review-reason": note,
        "rv-exclude-reason": "",
        "rv-challenge-mode": "demo",
        "rv-draft-store": {},
        "rv-page": "review",
        "rv-scenario": None,
    }
    app = create_app(service)
    saved = _post_action(app, "rv-action-message.children", "rv-qualify", values)
    values["rv-session"] = saved["rv-session"]["data"]
    assert assessment_decisions(service.view(case_id))[finding_id]["reason"] == note

    preview = _post_action(app, "trail-handoff-summary.children", "rv-session", values)
    values["trail-handoff-context"] = preview["trail-handoff-context"]["data"]
    brief = _post_action(app, "trail-download.data", "trail-brief", values)
    brief_data = brief["trail-download"]["data"]
    assert brief_data["filename"].endswith(".md")
    assert note in brief_data["content"]
    assert service.get_case(case_id)["case"]["title"] in brief_data["content"]

    download = _post_action(app, "trail-download.data", "trail-package", values)
    package_data = download["trail-download"]["data"]
    assert package_data["base64"]
    restored = workspace(tmp_path / "from-ui-download")
    assert restored.import_package(base64.b64decode(package_data["content"])) == case_id
    assert assessment_decisions(restored.view(case_id))[finding_id]["reason"] == note


def test_blank_review_note_reports_error_without_creating_an_assessment(case_workspace):
    _, service, case_id = case_workspace
    view = service.view(case_id)
    finding_id, record_id = initial_selection(view["bundle"])
    state = {
        "case_id": case_id,
        "case_fingerprint": view["case_fingerprint"],
        "finding_id": finding_id,
        "record_id": record_id,
        "scenario_id": None,
        "scenario_version": None,
        "job_id": None,
        "revision": 0,
    }
    before = service.repository.list_events(case_id)
    values = {
        "rv-session": state,
        "rv-case": case_id,
        "rv-review-reason": "   ",
        "rv-exclude-reason": "",
        "rv-challenge-mode": "demo",
        "rv-draft-store": {},
        "rv-page": "review",
        "rv-scenario": None,
    }
    response = _post_action(create_app(service), "rv-action-message.children", "rv-use", values)
    message = json.dumps(response["rv-action-message"]["children"])
    assert "rv-notice--error" in message
    assert "reason" in message.lower()
    assert service.repository.list_events(case_id) == before
