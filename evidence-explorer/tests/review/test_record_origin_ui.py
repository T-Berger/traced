"""Inspector and brief preview label explicit record provenance without changing it."""

from copy import deepcopy

import pytest
from dash import html
from dash.development.base_component import Component

from evidence_explorer.config import Settings
from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.presentation import source_inspector
from evidence_explorer.review.workspace_views import brief_preview


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


@pytest.mark.parametrize("case_synthetic", [True, False])
def test_per_record_labels_match_across_inspector_and_brief_without_mutation(
    tmp_path, case_synthetic
):
    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    payload = load_demo_bundle().model_dump(mode="json")
    payload["case"]["synthetic"] = case_synthetic
    payload["records"][0]["metadata"]["synthetic"] = True
    payload["records"][1]["metadata"]["synthetic"] = False
    payload["records"][2]["metadata"].pop("synthetic", None)
    case_id = service.import_bundle(payload)
    finding_id = "harbor:c3-v1"  # Includes all three origin variants through its evidence links.
    baseline = service.baseline_scenario(case_id, finding_id)
    service.review(case_id, finding_id, baseline["id"], "qualify", "Check every source's origin.")
    view = service.view(case_id)
    before = deepcopy(view)
    stored_before = service.get_case(case_id)
    events_before = service.repository.list_events(case_id)
    expected = {
        "harbor:r01": "Synthetic record",
        "harbor:r02": "Collected source; factual accuracy requires review",
        "harbor:r03": "Record origin not specified",
    }
    for record_id, label in expected.items():
        panel = source_inspector(view["bundle"], record_id, finding_id, set())
        assert f"Record provenance: {label}." in text(panel)

    preview = brief_preview(view)
    rows = {}
    for row in components(preview):
        if not isinstance(row, html.Li):
            continue
        for button in components(row):
            identity = getattr(button, "id", None)
            if isinstance(identity, dict) and identity.get("type") == "rv-citation":
                rows[identity["rid"]] = text(row)
    for record_id, label in expected.items():
        assert label in rows[record_id]
    assert view == before
    assert service.get_case(case_id) == stored_before
    assert service.repository.list_events(case_id) == events_before
    assert "synthetic" not in service.get_case(case_id)["records"][2]["metadata"]
