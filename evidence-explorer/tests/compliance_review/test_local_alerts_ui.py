from hashlib import sha256

from dash import Dash, html

from evidence_explorer.compliance_review.category_ui import capture_category_view
from evidence_explorer.compliance_review.local_alerts_ui import (
    local_alerts_panel,
    register_local_alerts_callbacks,
    render_local_alerts,
    scope_options,
)
from evidence_explorer.config import Settings
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService


def fixture_bundle(*, with_finding=True):
    text = "A fictional saved statement."
    other = "A second saved statement."
    run = {
        "id": "run",
        "input_fingerprint": "fixture",
        "input_manifest": ["one", "two"],
        "method": "fixture",
        "method_version": "1",
        "created_at": "2026-01-01T00:00:00Z",
    }
    finding = {
        "snapshot_id": "finding",
        "lineage_id": "finding",
        "version": 1,
        "run_id": "run",
        "kind": "inference",
        "statement": "Model candidate",
        "details": {
            "domain": "compliance_category",
            "category": "harassment",
            "target": "fictional target",
            "origin": "model",
            "model": "local:test",
            "record_id": "one",
            "record_content_hash": sha256(text.encode()).hexdigest(),
        },
    }
    return EvidenceBundle.model_validate(
        {
            "case": {"id": "case", "title": "Fictional", "question": "Review?", "synthetic": True},
            "records": [
                {
                    "id": "one",
                    "kind": "post",
                    "source_label": "Fictional one",
                    "text": text,
                    "content_hash": sha256(text.encode()).hexdigest(),
                    "published_at": "2026-01-01T00:00:00Z",
                    "metadata": {"synthetic": True},
                },
                {
                    "id": "two",
                    "kind": "post",
                    "source_label": "Fictional two",
                    "text": other,
                    "content_hash": sha256(other.encode()).hexdigest(),
                    "published_at": "2026-02-01T00:00:00Z",
                },
            ],
            "fragments": [
                {
                    "id": "fragment",
                    "record_id": "one",
                    "selector": {"kind": "text", "start": 2, "end": 11},
                    "quote": "fictional",
                }
            ]
            if with_finding
            else [],
            "runs": [run],
            "findings": [finding] if with_finding else [],
            "links": [
                {
                    "id": "link",
                    "source": {"type": "fragment", "id": "fragment"},
                    "target": {"type": "finding", "id": "finding"},
                    "relation": "supports",
                    "basis": "quotation",
                    "run_id": "run",
                }
            ]
            if with_finding
            else [],
        }
    )


def workspace(tmp_path, bundle):
    settings = Settings(data_dir=tmp_path, ollama_url="http://127.0.0.1:1")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    service.import_bundle(bundle.model_dump(mode="json"))
    return service, repository


def test_saved_candidates_preserve_current_decisions_and_unknown_records(tmp_path):
    service, repository = workspace(tmp_path, fixture_bundle())
    baseline = service.baseline_scenario("case", "finding")
    service.review("case", "finding", baseline["id"], "qualify", "Needs context")
    state = session_for_case(service, "case")
    state.update(scenario_id=baseline["id"], scenario_version=baseline["version"])
    fingerprint = repository.get_case_fingerprint("case")
    view, bundle, context = capture_category_view(service, state)
    targets, kinds = scope_options(view, bundle)
    assert targets == [{"label": "fictional target", "value": "fictional target"}]
    assert kinds == [{"label": "post", "value": "post"}]
    rendered = str(
        render_local_alerts(view, bundle, context, target="fictional target", kinds=["post"])
    )
    assert "harassment · qualify" in rendered
    assert "Needs context" in rendered and "fictional" in rendered
    assert "1 included saved text record(s) have no source-linked category" in rendered
    assert "classification is unknown" in rendered and "Sample coverage is unknown" in rendered
    assert "local-alert-source-0" in rendered and "local-alert-review-0" in rendered
    assert repository.get_case_fingerprint("case") == fingerprint


def test_no_saved_labels_is_explicitly_unknown_without_postgres(tmp_path):
    service, repository = workspace(tmp_path, fixture_bundle(with_finding=False))
    state = session_for_case(service, "case")
    app = Dash(__name__)
    app.layout = html.Div(local_alerts_panel())
    register_local_alerts_callbacks(app, service)
    output = "compliance-local-alerts-output.children"
    result = app.server.test_client().post(
        "/_dash-update-component",
        json={
            "output": output,
            "outputs": {"id": "compliance-local-alerts-output", "property": "children"},
            "inputs": [
                {"id": "rv-session", "property": "data", "value": state},
                {"id": "rv-page", "property": "value", "value": "alerts"},
                {"id": "compliance-local-alerts-target", "property": "value", "value": None},
                {"id": "compliance-local-alerts-kinds", "property": "value", "value": []},
            ],
            "state": [],
            "changedPropIds": ["rv-session.data"],
        },
    )
    assert result.status_code == 200
    assert "classification of its saved records is unknown" in result.get_data(as_text=True)
    assert not repository.get_bundle("case").findings
