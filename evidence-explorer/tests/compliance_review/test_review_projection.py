from copy import deepcopy
from hashlib import sha256

import pytest

from evidence_explorer.compliance_review.category_ui import (
    capture_category_view,
    category_scope_options,
)
from evidence_explorer.compliance_review.producer import category_proposal_bundle
from evidence_explorer.compliance_review.proposals import PROMPT_VERSION
from evidence_explorer.compliance_review.review_projection import project_category_review
from evidence_explorer.compliance_review.review_ui import render_category_review
from evidence_explorer.config import Settings
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.workflow import evidence_fingerprint


def example():
    text = "An invented statement for category review."
    digest = sha256(text.encode()).hexdigest()
    bundle = EvidenceBundle.model_validate(
        {
            "case": {"id": "case", "title": "Fiction", "question": "Review?", "synthetic": True},
            "records": [
                {
                    "id": "record",
                    "kind": "post",
                    "source_label": "Example",
                    "text": text,
                    "content_hash": digest,
                    "published_at": "2026-02-01T00:00:00Z",
                }
            ],
            "fragments": [
                {
                    "id": "fragment",
                    "record_id": "record",
                    "selector": {"kind": "text", "start": 3, "end": 11},
                    "quote": "invented",
                }
            ],
            "runs": [
                {
                    "id": "run",
                    "input_fingerprint": "input",
                    "input_manifest": ["record"],
                    "method": "example",
                    "method_version": "1",
                    "created_at": "2026-02-02",
                }
            ],
            "findings": [
                {
                    "snapshot_id": "finding",
                    "lineage_id": "lineage",
                    "version": 1,
                    "run_id": "run",
                    "kind": "inference",
                    "statement": "Review candidate",
                    "details": {
                        "domain": "compliance_category",
                        "category": "racism",
                        "record_id": "record",
                        "record_content_hash": digest,
                        "target": "invented target",
                        "origin": "model",
                        "model": "ollama:test",
                    },
                }
            ],
            "links": [
                {
                    "id": "link",
                    "source": {"type": "fragment", "id": "fragment"},
                    "target": {"type": "finding", "id": "finding"},
                    "relation": "supports",
                    "basis": "quote",
                    "run_id": "run",
                }
            ],
        }
    )
    data = bundle.model_dump(mode="json")
    view = {
        "bundle": data,
        "evidence_fingerprint": evidence_fingerprint(data),
        "case_fingerprint": "saved-case-hash",
        "scenario": {"id": "assessment", "version": 1, "base_run_id": "run"},
        "is_baseline": False,
        "impact": {"excluded_record_ids": []},
        "events": [
            {
                "event_type": "finding_reviewed",
                "payload": {
                    "finding_id": "finding",
                    "scenario_id": "assessment",
                    "scenario_version": 1,
                    "case_fingerprint": "saved-case-hash",
                    "disposition": "use",
                    "reason": "Review",
                },
            }
        ],
    }
    return view, bundle


def test_current_decision_is_bound_to_exact_finding_and_source():
    view, bundle = example()
    projection = project_category_review(
        view, bundle, record_kinds=["post"], target="invented target"
    )
    assert projection["rows"][0]["fragments"][0]["quote"] == "invented"
    assert projection["rows"][0]["disposition"] == "use"
    month = projection["months"][0]
    assert month["coverage"] == "unknown"
    assert month["categories"]["racism"]["use"]["observed_count"] == 1
    assert projection["count_unit"].startswith("distinct saved record IDs")
    rendered = str(render_category_review(projection, "context-token"))
    assert "context-token" in rendered
    assert "Review this finding" in rendered
    assert "absent months do not mean zero activity" in rendered


def test_stale_and_excluded_decisions_never_become_current_counts():
    view, bundle = example()
    view["scenario"]["version"] = 2
    projection = project_category_review(
        view, bundle, record_kinds=["post"], target="invented target"
    )
    assert projection["rows"][0]["disposition"] is None
    assert projection["months"][0]["categories"]["racism"]["unreviewed"]["observed_count"] == 1
    view["impact"]["excluded_record_ids"] = ["record"]
    assert (
        project_category_review(view, bundle, record_kinds=["post"], target="invented target")[
            "months"
        ]
        == []
    )


def test_missing_support_and_wrong_snapshot_are_unavailable():
    view, bundle = example()
    no_links = bundle.model_copy(update={"links": []})
    altered = no_links.model_dump(mode="json")
    view["bundle"] = altered
    view["evidence_fingerprint"] = evidence_fingerprint(altered)
    projection = project_category_review(
        view, no_links, record_kinds=["post"], target="invented target"
    )
    assert projection["rows"] == []
    assert projection["invalid_findings"][0]["reason"] == "No exact supporting text fragment"
    mismatched = deepcopy(view)
    mismatched["evidence_fingerprint"] = "stale"
    with pytest.raises(ValueError, match="snapshot differ"):
        project_category_review(
            mismatched, no_links, record_kinds=["post"], target="invented target"
        )


def test_kind_scope_must_be_explicit_and_does_not_guess():
    view, bundle = example()
    with pytest.raises(ValueError, match="Explicitly select"):
        project_category_review(view, bundle, record_kinds=[], target="invented target")
    projection = project_category_review(
        view, bundle, record_kinds=["telegram_message"], target="invented target"
    )
    assert not projection["rows"]
    assert "outside the selected record kinds" in projection["invalid_findings"][0]["reason"]


def test_target_and_exclusion_scope_are_explicit():
    view, bundle = example()
    projection = project_category_review(view, bundle, record_kinds=["post"], target="other")
    assert not projection["rows"]
    view.pop("impact")
    with pytest.raises(ValueError, match="excluded-record scope"):
        project_category_review(view, bundle, record_kinds=["post"], target="invented target")


def test_actual_workspace_decision_is_projected_from_one_snapshot(tmp_path):
    _, bundle = example()
    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    service.import_bundle(bundle.model_dump(mode="json"))
    baseline = service.baseline_scenario("case", "finding")
    service.review("case", "finding", baseline["id"], "qualify", "Needs additional context")
    saved = service.view("case")
    state = {
        "case_id": "case",
        "case_fingerprint": saved["case_fingerprint"],
        "scenario_id": None,
        "scenario_version": 1,
    }
    view, captured, context = capture_category_view(service, state)
    targets, kinds = category_scope_options(view, captured)
    assert targets == [{"label": "invented target", "value": "invented target"}]
    assert kinds == [{"label": "post", "value": "post"}]
    result = project_category_review(
        view, captured, record_kinds=["post"], target="invented target"
    )
    assert result["rows"][0]["disposition"] == "qualify"
    assert result["rows"][0]["decision"]["reason"] == "Needs additional context"
    assert result["months"][0]["coverage"] == "unknown"
    assert context.startswith("nav-v1:")


def test_saved_model_proposal_review_and_matrix_share_one_assessment(tmp_path):
    _, base = example()
    record = base.records[0]
    annotation = {
        "record_id": record.id,
        "record_content_hash": record.content_hash,
        "category": "harassment",
        "status": "proposed",
        "origin": "model",
        "quote": "invented",
        "start": 3,
        "end": 11,
        "run_id": "new-run",
        "target": "invented target",
        "model": "ollama:fixture",
        "rationale": "A fictional candidate for review.",
    }
    updated = category_proposal_bundle(
        base,
        record.id,
        {
            "record_id": record.id,
            "record_content_hash": record.content_hash,
            "target": "invented target",
            "run_id": "new-run",
            "model": "ollama:fixture",
            "provider": "ollama",
            "method": PROMPT_VERSION,
            "annotations": [annotation],
        },
    )
    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    service.import_bundle(updated.model_dump(mode="json"))
    finding = "new-run:0:finding"
    baseline = service.baseline_scenario("case", finding)
    service.review("case", finding, baseline["id"], "use", "Reviewed exact fictional excerpt")
    saved = service.view("case", baseline["id"])
    state = {
        "case_id": "case",
        "case_fingerprint": saved["case_fingerprint"],
        "scenario_id": baseline["id"],
        "scenario_version": 1,
    }
    view, captured, _ = capture_category_view(service, state)
    result = project_category_review(
        view, captured, record_kinds=["post"], target="invented target"
    )
    assert [(row["finding_id"], row["disposition"]) for row in result["rows"]] == [(finding, "use")]
    assert result["rows"][0]["fragments"][0]["quote"] == "invented"
    assert result["months"][0]["coverage"] == "unknown"
