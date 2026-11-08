from hashlib import sha256
from types import SimpleNamespace

import pytest
from dash.development.base_component import Component

from evidence_explorer.compliance_review import saved_ui
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.integrations.web import create_app
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService


def saved_bundle():
    text = "A fictional saved statement."
    return {
        "case": {"id": "case", "title": "Example", "question": "Review?", "synthetic": True},
        "records": [
            {
                "id": "record",
                "kind": "post",
                "source_label": "Fictional post",
                "text": text,
                "content_hash": sha256(text.encode()).hexdigest(),
            }
        ],
        "fragments": [
            {
                "id": "fragment",
                "record_id": "record",
                "selector": {"kind": "text", "start": 2, "end": 11},
                "quote": "fictional",
            }
        ],
        "runs": [
            {
                "id": "run",
                "input_fingerprint": "input",
                "input_manifest": ["record"],
                "method": "swarm_detector.detector.analyze",
                "method_version": "1",
                "created_at": "2026-01-05T00:00:00Z",
                "full_inputs_available": False,
            }
        ],
        "findings": [
            {
                "snapshot_id": "finding",
                "lineage_id": "lineage",
                "version": 1,
                "run_id": "run",
                "kind": "observation",
                "statement": "Timing pattern",
            },
            {
                "snapshot_id": "unsupported",
                "lineage_id": "other",
                "version": 1,
                "run_id": "run",
                "kind": "inference",
                "statement": "Unsupported candidate",
            },
        ],
        "links": [
            {
                "id": "link",
                "source": {"type": "fragment", "id": "fragment"},
                "target": {"type": "finding", "id": "finding"},
                "relation": "supports",
                "fragment_ids": ["fragment"],
                "basis": "Exact excerpt",
                "run_id": "run",
            }
        ],
    }


def walk(node):
    if isinstance(node, Component):
        yield node
        yield from walk(getattr(node, "children", None))
    elif isinstance(node, (tuple, list)):
        for item in node:
            yield from walk(item)


def test_saved_context_preserves_exact_source_navigation_and_unlinked_findings(monkeypatch):
    bundle = EvidenceBundle.model_validate(saved_bundle())

    class Snapshot:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def view(self, case_id, scenario_id):
            assert (case_id, scenario_id) == ("case", "scenario")
            return {"case": case_id}

        def export_bundle(self, case_id):
            assert case_id == "case"
            return bundle

    monkeypatch.setattr(saved_ui, "workspace_snapshot", lambda service: Snapshot())
    monkeypatch.setattr(saved_ui, "navigation_context", lambda view, state: "snapshot-token")
    data, context, options = saved_ui.capture_saved_context(
        SimpleNamespace(), {"case_id": "case", "scenario_id": "scenario"}, ["run"]
    )
    assert options[0]["value"] == "run"
    nodes = list(walk(saved_ui.render_saved_context(data, context)))
    buttons = [node for node in nodes if getattr(node, "id", None)]
    assert len(buttons) == 1
    assert buttons[0].id["rid"] == "record"
    assert buttons[0].id["context"] == "snapshot-token"
    text = str(saved_ui.render_saved_context(data, context))
    assert "fictional" in text
    assert "Complete inputs not established" in text
    assert "Unsupported candidate" in text
    assert "reviews are not transferred" in text


def test_no_selection_and_invalid_state_are_explicit():
    with pytest.raises(ValueError, match="Choose a saved case"):
        saved_ui.capture_saved_context(None, {}, [])
    panel = saved_ui.render_saved_context({"selected_run_ids": []}, "unused")
    assert "Select a saved run" in str(panel)
    assert "unavailable" in str(saved_ui.saved_context_panel())


def test_workspace_mounts_both_panels_without_creating_cases(settings, repo):
    reviews = WorkspaceRepository(settings.db_path)
    reviews.initialize()
    service = WorkspaceService(reviews, settings, settings.data_dir / "artifacts")
    app = create_app(service)
    layout = app.server.test_client().get("/_dash-layout")
    assert layout.status_code == 200
    text = layout.get_data(as_text=True)
    assert "Fictional category timeline and evidence matrix" in text
    assert "compliance-saved-runs" in text
    assert any("compliance-saved-output.children" in key for key in app.callback_map)
    assert service.list_cases() == []


def test_policy_page_defaults_to_general_not_a_specific_person():
    from evidence_explorer.policies.ui import GENERAL, policies_page

    dropdown = next(
        node for node in walk(policies_page()) if getattr(node, "id", None) == "pg-case"
    )
    assert dropdown.value == GENERAL
