from copy import deepcopy
from hashlib import sha256

import pytest

from evidence_explorer.alerts.repository import CaseData
from evidence_explorer.alerts.settings import AlertSettings
from evidence_explorer.compliance_review import alerts_ui
from evidence_explorer.compliance_review.alerts_bridge import project_alert_snapshot
from evidence_explorer.compliance_review.matrix import build_monthly_matrix
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.controller import session_for_case
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.navigation import navigation_context


def setup_data():
    text = "A fictional statement for review."
    capture = '{"fictional":true}'
    row = {
        "id": "alert-item",
        "case_id": "analytics-case",
        "status": "classified",
        "text": text,
        "url": "https://example.com/post/1",
        "capture_sha256": sha256(capture.encode()).hexdigest(),
        "model": "test-model",
        "prompt_version": "v1",
        "disinformation": 0.9,
        "hate_speech": 0.9,
        "racism": 0.2,
        "harassment": 0.1,
        "reference_ids": [],
    }
    data = CaseData(
        case={"id": "analytics-case"},
        total_posts=1,
        labels=[row],
        references={},
        last_run=None,
        provenance={"alert-item": {"raw": capture, "run": None}},
    )
    bundle = {
        "case": {
            "id": "local-case",
            "title": "Fictional",
            "question": "Review?",
            "synthetic": True,
        },
        "records": [
            {
                "id": "saved",
                "kind": "post",
                "source_label": "Post",
                "text": text,
                "content_hash": sha256(text.encode()).hexdigest(),
                "source_url": row["url"],
                "published_at": "2026-01-01T00:00:00Z",
            }
        ],
        "fragments": [],
        "runs": [],
        "findings": [],
    }
    return data, bundle


def test_explicit_mapping_emits_overlapping_proposals_not_review_or_coverage():
    data, bundle = setup_data()
    result = project_alert_snapshot(data, bundle, {"alert-item": "saved"}, AlertSettings())
    assert {a["category"] for a in result["annotations"]} == {"disinformation", "hate_speech"}
    assert all(a["status"] == "proposed" and a["origin"] == "model" for a in result["annotations"])
    matrix = build_monthly_matrix(bundle["records"], result["annotations"], {"2026-01": "complete"})
    assert matrix["months"][0]["coverage"] == "unknown"
    assert matrix["months"][0]["categories"]["hate_speech"]["observed_proposed"] == 1
    assert matrix["months"][0]["categories"]["hate_speech"]["confirmed"] is None


@pytest.mark.parametrize("mutation", ["capture", "case", "text", "url", "model", "reference"])
def test_missing_stale_or_unresolved_inputs_never_emit_candidates(mutation):
    data, bundle = setup_data()
    if mutation == "capture":
        data.provenance["alert-item"]["raw"] = "changed"
    elif mutation == "case":
        data.labels[0]["case_id"] = "other"
    elif mutation == "text":
        data.labels[0]["text"] = "Different text"
    elif mutation == "url":
        data.labels[0]["url"] = "https://example.com/post/other"
    elif mutation == "model":
        data.labels[0]["model"] = None
    else:
        data.labels[0]["reference_ids"] = ["missing"]
    with pytest.raises(ValueError):
        project_alert_snapshot(data, bundle, {"alert-item": "saved"}, AlertSettings())


def test_explicit_ui_comparison_reloads_snapshot_and_rejects_stale_fingerprint(monkeypatch):
    data, bundle_data = setup_data()
    bundle = EvidenceBundle.model_validate(bundle_data)
    settings = AlertSettings()
    fingerprint = alerts_ui._snapshot_fingerprint(data, settings)

    class Snapshot:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def view(self, case_id, scenario_id):
            assert (case_id, scenario_id) == ("local-case", None)
            return {}

        def export_bundle(self, case_id):
            assert case_id == "local-case"
            return bundle

        def case_fingerprint(self, case_id):
            return bundle.fingerprint()

    class Repo:
        def case_snapshot(self, case_id, selected):
            assert case_id == "analytics-case" and selected == ["alert-item"]
            return data

    class Store:
        def load(self):
            return settings, None

    monkeypatch.setattr(alerts_ui, "workspace_snapshot", lambda service: Snapshot())
    monkeypatch.setattr(alerts_ui, "navigation_context", lambda view, state: "token")
    args = (
        None,
        Repo(),
        Store(),
        {"case_id": "local-case"},
        "analytics-case",
        ["alert-item"],
        "saved",
    )
    result, context, label, bundle_fingerprint = alerts_ui.compare_selected(*args, fingerprint)
    assert len(result["annotations"]) == 2 and context == "token" and label == "Post"
    assert bundle_fingerprint == bundle.fingerprint()
    rendered = alerts_ui._render(result, context, label)
    assert "Unreviewed model proposals" in str(rendered)
    button = next(item for item in rendered.children if getattr(item, "id", None))
    assert button.id["rid"] == "saved" and button.id["context"] == "token"
    with pytest.raises(ValueError, match="changed"):
        alerts_ui.compare_selected(*args, "stale")


def test_commit_adds_separate_reviewable_assessment_and_rejects_stale(settings, repo):
    data, bundle_data = setup_data()
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    bundle = EvidenceBundle.model_validate(bundle_data)
    repository.import_bundle(bundle)
    service = WorkspaceService(repository, settings, settings.data_dir / "artifacts")
    projection = project_alert_snapshot(data, bundle, {"alert-item": "saved"}, AlertSettings())
    original = repository.get_case_fingerprint("local-case")
    state = session_for_case(service, "local-case")
    context = navigation_context(service.view("local-case"), state)
    assessment = alerts_ui.commit_alert_proposals(
        service, state, projection, original, context, "subject"
    )
    updated = repository.get_bundle("local-case")
    assert len(updated.findings) == 2
    assert assessment.base_run_id == updated.runs[-1].id
    assert all(f.details["domain"] == "compliance_category" for f in updated.findings)
    assert not updated.reviews
    with pytest.raises(ValueError, match="changed"):
        alerts_ui.commit_alert_proposals(service, state, projection, original, context, "subject")
    assert len(repository.get_bundle("local-case").findings) == 2


def test_oversize_alert_handoff_rolls_back_proposals_and_assessment(settings, repo, monkeypatch):
    from evidence_explorer.review.integrations import packages

    data, bundle_data = setup_data()
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    bundle = EvidenceBundle.model_validate(bundle_data)
    repository.import_bundle(bundle)
    service = WorkspaceService(repository, settings, settings.data_dir / "artifacts")
    projection = project_alert_snapshot(data, bundle, {"alert-item": "saved"}, AlertSettings())
    original = repository.get_case_fingerprint("local-case")
    state = session_for_case(service, "local-case")
    context = navigation_context(service.view("local-case"), state)
    before_assessments = service.assessments("local-case")
    existing_export = service.export_bundle("local-case")
    monkeypatch.setattr(
        packages, "MAX_FILE_BYTES", len(packages._json(existing_export.model_dump(mode="json"))) + 1
    )

    with pytest.raises(ValueError, match="too large"):
        alerts_ui.commit_alert_proposals(service, state, projection, original, context, "subject")
    assert repository.get_case_fingerprint("local-case") == original
    assert not repository.get_bundle("local-case").findings
    assert service.assessments("local-case") == before_assessments


def test_commit_rejects_exclusion_added_after_preview(settings, repo):
    data, bundle_data = setup_data()
    bundle_data["runs"] = [
        {
            "id": "base",
            "input_fingerprint": "input",
            "input_manifest": ["saved"],
            "method": "fixture",
            "method_version": "1",
            "created_at": "2026-01-01T00:00:00Z",
        }
    ]
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    bundle = EvidenceBundle.model_validate(bundle_data)
    repository.import_bundle(bundle)
    service = WorkspaceService(repository, settings, settings.data_dir / "artifacts")
    scenario = repository.create_scenario_state("local-case", "base")
    state = session_for_case(service, "local-case")
    state.update(scenario_id=scenario.id, scenario_version=scenario.version)
    context = navigation_context(service.view("local-case", scenario.id), state)
    projection = project_alert_snapshot(data, bundle, {"alert-item": "saved"}, AlertSettings())
    fingerprint = repository.get_case_fingerprint("local-case")
    service.set_source_exclusion(scenario.id, "saved", True, "Test exclusion", scenario.version)
    with pytest.raises(ValueError):
        alerts_ui.commit_alert_proposals(
            service, state, projection, fingerprint, context, "subject"
        )
    assert not repository.get_bundle("local-case").findings


def test_commit_preserves_existing_review_history(settings, repo):
    data, bundle_data = setup_data()
    bundle_data["runs"] = [
        {
            "id": "base",
            "input_fingerprint": "input",
            "input_manifest": ["saved"],
            "method": "fixture",
            "method_version": "1",
            "created_at": "2026-01-01T00:00:00Z",
        }
    ]
    bundle_data["findings"] = [
        {
            "snapshot_id": "old",
            "lineage_id": "old",
            "version": 1,
            "run_id": "base",
            "kind": "observation",
            "statement": "Old finding",
        }
    ]
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    repository.import_bundle(EvidenceBundle.model_validate(bundle_data))
    service = WorkspaceService(repository, settings, settings.data_dir / "artifacts")
    scenario = repository.create_scenario_state("local-case", "base")
    repository.save_review_state("local-case", "old", scenario.id, "Tester", "qualify", "Context")
    state = session_for_case(service, "local-case")
    state.update(scenario_id=scenario.id, scenario_version=scenario.version)
    context = navigation_context(service.view("local-case", scenario.id), state)
    with alerts_ui.workspace_snapshot(service) as snapshot:
        export = snapshot.export_bundle("local-case")
        fingerprint = snapshot.case_fingerprint("local-case")
    assert export.fingerprint() != fingerprint
    projection = project_alert_snapshot(data, export, {"alert-item": "saved"}, AlertSettings())
    assessment = alerts_ui.commit_alert_proposals(
        service, state, projection, fingerprint, context, "subject"
    )
    output = service.export_bundle("local-case")
    assert any(review.finding_snapshot_id == "old" for review in output.reviews)
    assert not any(review.run_id == assessment.base_run_id for review in output.reviews)


def test_package_guard_includes_saved_review_history(settings, repo, monkeypatch):
    from evidence_explorer.compliance_review.package_guard import ensure_transaction_bundle_fits
    from evidence_explorer.review.integrations import packages

    _, bundle_data = setup_data()
    bundle_data["runs"] = [
        {
            "id": "base",
            "input_fingerprint": "input",
            "input_manifest": ["saved"],
            "method": "fixture",
            "method_version": "1",
            "created_at": "2026-01-01T00:00:00Z",
        }
    ]
    bundle_data["findings"] = [
        {
            "snapshot_id": "old",
            "lineage_id": "old",
            "version": 1,
            "run_id": "base",
            "kind": "observation",
            "statement": "Old",
        }
    ]
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    repository.import_bundle(EvidenceBundle.model_validate(bundle_data))
    service = WorkspaceService(repository, settings, settings.data_dir / "artifacts")
    scenario = repository.create_scenario_state("local-case", "base")
    repository.save_review_state("local-case", "old", scenario.id, "Tester", "qualify", "Context")
    bare = repository.get_bundle("local-case")
    exported = service.export_bundle("local-case")
    bare_size = len(packages._json(bare.model_dump(mode="json")))
    exported_size = len(packages._json(exported.model_dump(mode="json")))
    assert exported_size > bare_size
    monkeypatch.setattr(packages, "MAX_FILE_BYTES", bare_size)
    with pytest.raises(ValueError, match="too large"):
        with repository._connection(write=True) as connection:
            ensure_transaction_bundle_fits(repository, connection, settings, "local-case")


def test_multiple_explicit_mappings_make_one_reviewable_run(settings, repo):
    data, bundle_data = setup_data()
    row = deepcopy(data.labels[0])
    row.update(
        id="alert-item-2",
        text="A different fictional statement.",
        url="https://example.com/post/2",
        disinformation=0.1,
        harassment=0.9,
    )
    data.labels.append(row)
    data.provenance["alert-item-2"] = deepcopy(data.provenance["alert-item"])
    second = deepcopy(bundle_data["records"][0])
    second.update(
        id="saved-2",
        text=row["text"],
        content_hash=sha256(row["text"].encode()).hexdigest(),
        source_url=row["url"],
        published_at="2026-02-01T00:00:00Z",
    )
    bundle_data["records"].append(second)
    bundle = EvidenceBundle.model_validate(bundle_data)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    repository.import_bundle(bundle)
    service = WorkspaceService(repository, settings, settings.data_dir / "artifacts")
    state = session_for_case(service, "local-case")
    view = service.view("local-case")
    projection = project_alert_snapshot(
        data, bundle, {"alert-item": "saved", "alert-item-2": "saved-2"}, AlertSettings()
    )
    assessment = alerts_ui.commit_alert_proposals(
        service,
        state,
        projection,
        repository.get_case_fingerprint("local-case"),
        navigation_context(view, state),
        "subject",
    )
    updated = repository.get_bundle("local-case")
    run = next(run for run in updated.runs if run.id == assessment.base_run_id)
    assert set(run.input_manifest) == {"saved", "saved-2"}
    assert {f.details["record_id"] for f in updated.findings if f.run_id == run.id} == {
        "saved",
        "saved-2",
    }
    assert not updated.reviews


@pytest.mark.parametrize(
    "selected,ids,values",
    [
        (["a", "b"], [{"type": alerts_ui.MAPPING_TYPE, "item": "a"}], ["record-a"]),
        (
            ["a", "b"],
            [
                {"type": alerts_ui.MAPPING_TYPE, "item": "a"},
                {"type": alerts_ui.MAPPING_TYPE, "item": "b"},
            ],
            ["same", "same"],
        ),
        (
            ["a", "b"],
            [
                {"type": alerts_ui.MAPPING_TYPE, "item": "a"},
                {"type": alerts_ui.MAPPING_TYPE, "item": "other"},
            ],
            ["one", "two"],
        ),
        ([str(i) for i in range(21)], [], []),
    ],
)
def test_browser_mapping_rejects_missing_duplicate_stale_or_unbounded_selection(
    selected, ids, values
):
    with pytest.raises(ValueError):
        alerts_ui._selected_mapping(selected, ids, values)


def test_explicit_mapping_callback_revalidates_selection_before_save(settings, repo, monkeypatch):
    from dash import Dash, html

    data, bundle_data = setup_data()
    row = deepcopy(data.labels[0])
    row.update(id="alert-item-2", text="Another fictional post.", url="https://example.com/post/2")
    data.labels.append(row)
    data.provenance["alert-item-2"] = deepcopy(data.provenance["alert-item"])
    second = deepcopy(bundle_data["records"][0])
    second.update(
        id="saved-2",
        text=row["text"],
        content_hash=sha256(row["text"].encode()).hexdigest(),
        source_url=row["url"],
        published_at="2026-02-01T00:00:00Z",
    )
    bundle_data["records"].append(second)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    repository.import_bundle(EvidenceBundle.model_validate(bundle_data))
    service = WorkspaceService(repository, settings, settings.data_dir / "artifacts")
    state = session_for_case(service, "local-case")
    alert_settings = AlertSettings()
    fingerprint = alerts_ui._snapshot_fingerprint(data, alert_settings)

    class FakeRepo:
        def case_snapshot(self, case_id, item_ids):
            assert case_id == "analytics-case"
            assert set(item_ids) == {"alert-item", "alert-item-2"}
            return data

    class FakeStore:
        def __init__(self, *_):
            pass

        def load(self):
            return alert_settings, None

    monkeypatch.setattr(alerts_ui, "AnalyticsRepository", FakeRepo)
    monkeypatch.setattr(alerts_ui, "AlertSettingsStore", FakeStore)
    app = Dash(__name__)
    app.layout = html.Div()
    alerts_ui.register_alert_bridge_callbacks(app, service)
    client = app.server.test_client()
    pattern = '{"item":["ALL"],"type":"compliance-alert-mapping"}'
    ids = [
        {"type": alerts_ui.MAPPING_TYPE, "item": item} for item in ("alert-item", "alert-item-2")
    ]
    inputs = [
        {"id": "compliance-alert-compare", "property": "n_clicks", "value": 1},
        {"id": "rv-session", "property": "data", "value": state},
        {"id": "al-case", "property": "value", "value": "analytics-case"},
        {"id": "al-ev-select", "property": "value", "value": ["alert-item", "alert-item-2"]},
        {"id": "al-selection-snapshot", "property": "data", "value": fingerprint},
        {"id": pattern, "property": "value", "value": ["saved", "saved-2"]},
        {"id": "compliance-alert-target", "property": "value", "value": "subject"},
    ]
    output = "..compliance-alert-output.children...compliance-alert-preview.data.."
    response = client.post(
        "/_dash-update-component",
        json={
            "output": output,
            "outputs": [
                {"id": "compliance-alert-output", "property": "children"},
                {"id": "compliance-alert-preview", "property": "data"},
            ],
            "inputs": inputs,
            "state": [
                {"id": pattern, "property": "id", "value": ids},
                {"id": "rv-page", "property": "value", "value": "alerts"},
            ],
            "changedPropIds": ["compliance-alert-compare.n_clicks"],
        },
    )
    assert response.status_code == 200
    preview = response.get_json()["response"]["compliance-alert-preview"]["data"]
    assert preview["mapping"] == {"alert-item": "saved", "alert-item-2": "saved-2"}
    save_state = [
        {"id": "compliance-alert-preview", "property": "data", "value": preview},
        *inputs[1:5],
        {"id": pattern, "property": "value", "value": ["saved", "saved-2"]},
        {"id": pattern, "property": "id", "value": ids},
        {"id": "compliance-alert-target", "property": "value", "value": "subject"},
        {"id": "rv-page", "property": "value", "value": "alerts"},
    ]
    payload = {
        "output": "compliance-alert-save-status.children",
        "outputs": {"id": "compliance-alert-save-status", "property": "children"},
        "inputs": [{"id": "compliance-alert-save", "property": "n_clicks", "value": 1}],
        "state": save_state,
        "changedPropIds": ["compliance-alert-save.n_clicks"],
    }
    save_state[5]["value"] = ["saved", "saved"]
    rejected = client.post("/_dash-update-component", json=payload)
    assert "mappings changed" in rejected.get_data(as_text=True)
    assert not repository.get_bundle("local-case").findings
    save_state[5]["value"] = ["saved", "saved-2"]
    saved = client.post("/_dash-update-component", json=payload)
    assert "unreviewed proposals" in saved.get_data(as_text=True)
    updated = repository.get_bundle("local-case")
    assert {finding.details["record_id"] for finding in updated.findings} == {"saved", "saved-2"}
    assert len({finding.run_id for finding in updated.findings}) == 1
    assert not updated.reviews
