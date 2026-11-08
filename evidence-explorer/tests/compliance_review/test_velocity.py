from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from evidence_explorer.compliance_review.velocity import category_metric, evaluate_category_velocity
from evidence_explorer.compliance_review.velocity_ui import (
    VelocityCapture,
    capture_velocity_context,
    evaluate_velocity_selection,
    render_velocity_result,
    velocity_input_context,
)
from evidence_explorer.config import Settings
from evidence_explorer.planning_briefs.models import (
    BriefBinding,
    BriefConfirmation,
    BriefRevision,
    BriefState,
    DraftFields,
    digest,
    revision_content_hash,
)
from evidence_explorer.planning_briefs.scope import scope_identity
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.workflow import evidence_fingerprint
from evidence_explorer.use_cases.contracts import AlertRule, WindowDefinition
from evidence_explorer.use_cases.saved_activity import SavedActivityRequest


def sample():
    windows = (
        WindowDefinition(id="baseline", start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z"),
        WindowDefinition(id="current", start="2026-01-02T00:00:00Z", end="2026-01-03T00:00:00Z"),
    )
    rule = AlertRule(
        id="fixture-rule",
        title="Fictional review cue",
        metric=category_metric("racism", "fictional target"),
        threshold_percent=25,
        threshold_basis="analyst_configured",
        direction="increase",
        comparison_basis="equal_duration_counts",
        min_baseline_count=2,
        min_current_count=1,
        min_coverage_fraction=1,
    )
    records, fragments, findings, links, events = [], [], [], [], []
    for index in range(5):
        rid, fid, fragment = f"record:{index}", f"finding:{index}", f"fragment:{index}"
        text = f"Fictional observation {index}."
        content_hash = sha256(text.encode()).hexdigest()
        records.append(
            {
                "id": rid,
                "kind": "post",
                "source_label": "Fictional source",
                "text": text,
                "content_hash": content_hash,
                "published_at": f"2026-01-0{1 if index < 2 else 2}T12:00:00Z",
                "metadata": {"synthetic": True},
            }
        )
        fragments.append(
            {
                "id": fragment,
                "record_id": rid,
                "selector": {"kind": "text", "start": 0, "end": 9},
                "quote": "Fictional",
            }
        )
        findings.append(
            {
                "snapshot_id": fid,
                "lineage_id": fid,
                "version": 1,
                "run_id": "run",
                "kind": "inference",
                "statement": "Fictional candidate",
                "details": {
                    "domain": "compliance_category",
                    "category": "racism",
                    "target": "fictional target",
                    "origin": "model",
                    "model": "fixture",
                    "record_id": rid,
                    "record_content_hash": content_hash,
                },
            }
        )
        links.append(
            {
                "id": f"link:{index}",
                "source": {"type": "fragment", "id": fragment},
                "target": {"type": "finding", "id": fid},
                "relation": "supports",
                "basis": "Fictional quote",
                "run_id": "run",
            }
        )
        events.append(
            {
                "event_type": "finding_reviewed",
                "payload": {
                    "id": f"decision:{index}",
                    "finding_id": fid,
                    "scenario_id": "scenario",
                    "scenario_version": 1,
                    "case_fingerprint": "fixture-case-hash",
                    "disposition": "use",
                    "reviewer": "Fictional reviewer",
                    "reason": "Reviewed",
                },
            }
        )
    refs = [record["id"] for record in records]
    coverage = {
        "policy": "saved-activity-coverage/v1",
        "timestamp_basis": "publication",
        "record_kind": "post",
        "source_refs": refs,
    }
    for name, window in zip(("baseline", "observation"), windows, strict=True):
        coverage[name] = {
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "coverage_status": "complete",
            "coverage_fraction": 1,
            "evidence": "Closed fictional sample declaration.",
        }
    bundle = EvidenceBundle.model_validate(
        {
            "case": {
                "id": "case",
                "title": "Fictional",
                "question": "Review?",
                "synthetic": True,
                "coverage": {"saved_activity_coverage": coverage},
            },
            "records": records,
            "fragments": fragments,
            "runs": [
                {
                    "id": "run",
                    "input_fingerprint": "input",
                    "input_manifest": refs,
                    "method": "fixture",
                    "method_version": "1",
                    "created_at": "2026-01-04",
                }
            ],
            "findings": findings,
            "links": links,
        }
    )
    scope = scope_identity(bundle)
    fields = DraftFields(
        use_case_id="public_information_monitoring",
        purpose="Fictional review sample",
        public_subjects_or_topics=("fictional target",),
        source_selection=scope.source_refs,
        reviewer_role="Fictional analyst",
        expected_output="reviewed_brief",
        baseline_window=windows[0],
        observation_window=windows[1],
        alert_rule=rule,
    )
    binding = BriefBinding(
        case_id=bundle.case.id, source_case_fingerprint=bundle.fingerprint(), scope=scope
    )
    revision = BriefRevision(
        brief_id="brief",
        revision_id="revision",
        number=1,
        fields=fields,
        binding=binding,
        content_hash=revision_content_hash(fields, binding),
        author="Fictional analyst",
        created_at=datetime(2026, 1, 4, tzinfo=UTC),
    )
    confirmation = BriefConfirmation(
        confirmation_id="confirmation",
        brief_id="brief",
        revision_id="revision",
        binding_digest=digest(binding.model_dump(mode="json")),
        reviewer="Fictional reviewer",
        confirmed_at=datetime(2026, 1, 4, tzinfo=UTC),
    )
    state = BriefState(
        revision=revision,
        confirmation=confirmation,
        status="confirmed",
        head_revision_id="revision",
        is_head=True,
        is_active_for_case=True,
        imported=False,
        confirmation_is_local=True,
    )
    data = bundle.model_dump(mode="json")
    view = {
        "bundle": data,
        "evidence_fingerprint": evidence_fingerprint(data),
        "case_fingerprint": "fixture-case-hash",
        "scenario": {"id": "scenario", "version": 1, "base_run_id": "run"},
        "is_baseline": False,
        "impact": {"excluded_record_ids": []},
        "events": events,
    }
    return (
        bundle,
        state,
        view,
        SavedActivityRequest(rule=rule, record_kind="post", timestamp_basis="publication"),
    )


def evaluate(bundle, state, view, request):
    return evaluate_category_velocity(
        bundle, state, view, request, category="racism", target="fictional target"
    )


def test_declared_closed_sample_and_current_reviewed_evidence_trigger():
    bundle, state, view, request = sample()
    before = deepcopy(view)
    result = evaluate(bundle, state, view, request)
    assert result["status"] == "review_needed"
    assert result["evaluation"]["relative_change_percent"] == 50
    assert len(result["reviewed_findings"]) == 5
    assert result["reviewed_findings"][0]["fragment_ids"]
    assert "not verified violations" in result["limitations"][0]
    assert view == before


def test_missing_review_qualified_and_conflicting_reviews_are_unavailable():
    bundle, state, view, request = sample()
    view["events"].pop()
    assert evaluate(bundle, state, view, request)["status"] == "unavailable"
    bundle, state, view, request = sample()
    view["events"][-1]["payload"]["disposition"] = "qualify"
    assert evaluate(bundle, state, view, request)["status"] == "unavailable"
    bundle, state, view, request = sample()
    view["impact"]["excluded_record_ids"] = ["record:0"]
    assert evaluate(bundle, state, view, request)["status"] == "unavailable"


def test_no_coverage_no_current_plan_and_stale_view_are_unavailable():
    bundle, state, view, request = sample()
    assert evaluate(bundle, None, view, request)["status"] == "unavailable"
    view["evidence_fingerprint"] = "stale"
    assert evaluate(bundle, state, view, request)["status"] == "unavailable"
    bundle, state, view, request = sample()
    updated = bundle.model_dump(mode="json")
    updated["case"]["coverage"] = {}
    no_coverage = EvidenceBundle.model_validate(updated)
    assert evaluate(no_coverage, state, view, request)["status"] == "unavailable"


def test_general_activity_rule_cannot_be_used_for_a_category_subset():
    bundle, state, view, request = sample()
    general_rule = request.rule.model_copy(update={"metric": "all saved records"})
    mismatch = request.model_copy(update={"rule": general_rule})
    result = evaluate(bundle, state, view, mismatch)
    assert result["status"] == "unavailable"
    assert "metric does not explicitly match" in result["summary"]


def test_withhold_is_exclusion_from_use_count_never_negative_category():
    bundle, state, view, request = sample()
    view["events"][-1]["payload"]["disposition"] = "withhold"
    result = evaluate(bundle, state, view, request)
    assert result["status"] == "not_triggered"
    assert len(result["observation_record_ids"]) == 2
    assert any(item["disposition"] == "withhold" for item in result["reviewed_findings"])


def test_explicit_ui_selection_uses_saved_rule_and_rejects_stale_or_dirty_inputs():
    bundle, state, view, _ = sample()
    captured = VelocityCapture(bundle, state, view, "navigation-token", "snapshot-token")
    expected = velocity_input_context(
        captured, "racism", "fictional target", ["post"], "publication"
    )
    result = evaluate_velocity_selection(
        captured, expected, "racism", "fictional target", ["post"], "publication"
    )
    assert result["status"] == "review_needed"
    rendered = str(render_velocity_result(result, captured.navigation))
    assert "navigation-token" in rendered
    assert "not verified violations" in rendered
    with pytest.raises(ValueError, match="changed"):
        evaluate_velocity_selection(
            captured,
            {**expected, "snapshot": "stale"},
            "racism",
            "fictional target",
            ["post"],
            "publication",
        )
    with pytest.raises(ValueError, match="planning draft"):
        evaluate_velocity_selection(
            captured, expected, "racism", "fictional target", ["post"], "publication", dirty=True
        )


def test_workspace_snapshot_reports_missing_plan_without_writes(tmp_path):
    bundle, _, _, _ = sample()
    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    service.import_bundle(bundle.model_dump(mode="json"))
    view = service.view("case")
    session = {
        "case_id": "case",
        "case_fingerprint": view["case_fingerprint"],
        "scenario_id": None,
        "scenario_version": 1,
    }
    before = service.repository.list_events("case")
    captured = capture_velocity_context(service, session)
    assert captured.planning is None
    expected = velocity_input_context(
        captured, "racism", "fictional target", ["post"], "publication"
    )
    with pytest.raises(ValueError, match="locally confirm"):
        evaluate_velocity_selection(
            captured, expected, "racism", "fictional target", ["post"], "publication"
        )
    assert service.repository.list_events("case") == before
