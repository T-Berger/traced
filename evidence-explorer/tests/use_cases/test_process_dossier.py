"""Use-case progress reuses exact evidence and current assessment decisions."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from evidence_explorer.planning_briefs.models import digest
from evidence_explorer.planning_briefs.scope import scope_identity
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.use_cases import (
    BriefBindingFact,
    MonitoringBrief,
    ProgressFacts,
    ReadinessFact,
    WindowDefinition,
    evaluate_activity_alert,
    get_use_case,
    illustrative_activity_example,
    list_use_cases,
    platform_reporting_references,
    process_definition,
    project_case_progress,
    project_dossier_readiness,
)


def _synthetic_view():
    """An explicitly fictional complete scope, saved source set, and count result."""
    rule, baseline, observation = illustrative_activity_example()
    records, fragments, links = [], [], []
    for window in (baseline, observation):
        for record_id in window.record_ids:
            text = f"Fictional public-topic observation {record_id}."
            fragment_id = f"quote:{record_id}"
            records.append(
                {
                    "id": record_id,
                    "kind": "synthetic_public_post",
                    "source_label": "Fictional public source",
                    "text": text,
                    "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                    "published_at": window.start.isoformat(),
                    "metadata": {"synthetic": True},
                }
            )
            fragments.append(
                {
                    "id": fragment_id,
                    "record_id": record_id,
                    "selector": {"kind": "text", "start": 0, "end": len(text)},
                    "quote": text,
                }
            )
            links.append(
                {
                    "id": f"support:{record_id}",
                    "source": {"type": "fragment", "id": fragment_id},
                    "target": {"type": "finding", "id": "synthetic:count-change"},
                    "relation": "supports",
                    "basis": "Each cited fictional record contributes to one declared count.",
                }
            )
    bundle = EvidenceBundle.model_validate(
        {
            "case": {
                "id": "synthetic:activity-example",
                "title": "Fictional activity illustration",
                "question": "Which declared fictional sample needs review?",
                "synthetic": True,
                "limitations": ["No real activity, people, or validated threshold."],
            },
            "records": records,
            "fragments": fragments,
            "runs": [
                {
                    "id": "synthetic:count-run",
                    "input_fingerprint": hashlib.sha256(b"fictional fixture v1").hexdigest(),
                    "input_manifest": [record["id"] for record in records],
                    "method": "Explicit illustrative count comparison",
                    "method_version": "1",
                    "created_at": "2026-11-06T12:00:00+00:00",
                    "configuration": {"rule": rule.model_dump(mode="json")},
                }
            ],
            "findings": [
                {
                    "snapshot_id": "synthetic:count-change",
                    "lineage_id": "synthetic:count-change",
                    "version": 1,
                    "run_id": "synthetic:count-run",
                    "kind": "observation",
                    "statement": "The supplied fictional counts changed from 5 to 6 (20%).",
                }
            ],
            "links": links,
        }
    )
    view = {
        "bundle": bundle.model_dump(mode="json"),
        "baseline_scenario": {
            "id": "synthetic:baseline",
            "base_run_id": "synthetic:count-run",
            "version": 1,
        },
        "is_baseline": True,
        "case_fingerprint": bundle.fingerprint(),
        "events": [],
    }
    brief = MonitoringBrief(
        id="synthetic:brief",
        use_case_id="public_information_monitoring",
        purpose="Review a declared fictional public-topic activity change.",
        public_subjects_or_topics=("Fictional topic",),
        source_selection=scope_identity(bundle).source_refs,
        baseline_window=WindowDefinition(id=baseline.id, start=baseline.start, end=baseline.end),
        observation_window=WindowDefinition(
            id=observation.id, start=observation.start, end=observation.end
        ),
        alert_rule=rule,
        reviewer_role="OSINT analyst",
        expected_output="reviewed_brief",
        confirmed=True,
    )
    facts = ProgressFacts(
        brief=brief,
        brief_binding=BriefBindingFact(
            brief_id=brief.id,
            revision_id="synthetic:brief:revision-1",
            case_id=bundle.case.id,
            plan_fingerprint=digest(brief.model_dump(mode="json")),
            evidence_fingerprint=scope_identity(bundle).evidence_fingerprint,
            scope_fingerprint=scope_identity(bundle).scope_fingerprint,
        ),
        collection_state="completed",
        signal_evaluation=evaluate_activity_alert(rule, baseline, observation),
    )
    return view, facts


def test_confirmed_flag_without_current_bound_revision_does_not_complete_scope():
    view, facts = _synthetic_view()
    unbound = facts.model_copy(update={"brief_binding": None})
    assert (
        _statuses(project_case_progress(facts.brief.use_case_id, view, facts=unbound))["scope"]
        == "needs_review"
    )


def test_binding_to_another_case_or_edited_plan_is_not_current_scope():
    view, facts = _synthetic_view()
    wrong_case = facts.model_copy(
        update={"brief_binding": facts.brief_binding.model_copy(update={"case_id": "other-case"})}
    )
    changed_plan = facts.model_copy(
        update={"brief": facts.brief.model_copy(update={"purpose": "A different question."})}
    )
    for supplied in (wrong_case, changed_plan):
        assert (
            _statuses(project_case_progress(facts.brief.use_case_id, view, facts=supplied))["scope"]
            == "needs_review"
        )


def test_source_subset_cannot_silently_confirm_a_broader_saved_case():
    view, facts = _synthetic_view()
    narrowed = facts.brief.model_copy(
        update={"source_selection": facts.brief.source_selection[:-1]}
    )
    supplied = facts.model_copy(
        update={
            "brief": narrowed,
            "brief_binding": facts.brief_binding.model_copy(
                update={"plan_fingerprint": digest(narrowed.model_dump(mode="json"))}
            ),
        }
    )
    assert (
        _statuses(project_case_progress(narrowed.use_case_id, view, facts=supplied))["scope"]
        == "needs_review"
    )


def test_bound_scope_changes_with_evidence_but_not_packaged_scenario_history():
    view, facts = _synthetic_view()
    history_only = deepcopy(view)
    history_only["bundle"]["scenarios"].append(
        {
            "id": "packaged:scenario",
            "base_run_id": "synthetic:count-run",
            "excluded_record_ids": [],
            "reason": "Saved scenario history.",
            "status": "dependency_preview",
        }
    )
    assert (
        _statuses(project_case_progress(facts.brief.use_case_id, history_only, facts=facts))[
            "scope"
        ]
        == "completed"
    )
    changed = deepcopy(view)
    record = changed["bundle"]["records"][0]
    record["text"] += " Changed supplied evidence."
    record["content_hash"] = hashlib.sha256(record["text"].encode()).hexdigest()
    assert (
        _statuses(project_case_progress(facts.brief.use_case_id, changed, facts=facts))["scope"]
        == "needs_review"
    )


def _review(view, *, disposition="qualify"):
    result = deepcopy(view)
    scenario = result.get("scenario") or result["baseline_scenario"]
    result["events"].append(
        {
            "event_type": "finding_reviewed",
            "payload": {
                "finding_id": "synthetic:count-change",
                "scenario_id": scenario["id"],
                "scenario_version": scenario["version"],
                "case_fingerprint": result["case_fingerprint"],
                "reviewer": "Fictional reviewer",
                "disposition": disposition,
                "reason": "Use only as a fictional count illustration, with limits.",
                "created_at": "2026-11-06T12:01:00+00:00",
            },
        }
    )
    return result


def _statuses(progress):
    return {step.id: step.status for step in progress.steps}


def test_catalog_is_one_public_information_workflow_with_five_distinct_jobs():
    assert len(list_use_cases()) == 5
    assert len({item.id for item in list_use_cases()}) == 5
    expected = ["scope", "collect", "signals", "evidence_check", "human_review", "export"]
    for item in list_use_cases():
        assert get_use_case(item.id) == item
        assert item.target_roles and item.problem and item.user_story
        assert [step.id for step in process_definition(item.id)] == expected
    with pytest.raises(ValueError, match="Unknown use case"):
        get_use_case("undeclared purpose")


def test_template_selection_is_not_scope_collection_or_verified_readiness():
    view, _ = _synthetic_view()
    original = deepcopy(view)
    progress = project_case_progress("public_information_monitoring", view)
    assert _statuses(progress) == {
        "scope": "needs_review",
        "collect": "needs_review",
        "signals": "unavailable",
        "evidence_check": "completed",
        "human_review": "needs_review",
        "export": "needs_review",
    }
    assert progress.model_readiness.status == "unknown"
    assert progress.platform_readiness == ()
    assert progress.next_step_id == "scope"
    assert view == original


def test_synthetic_scope_counts_review_and_dossier_require_current_human_decision():
    view, facts = _synthetic_view()
    original = deepcopy(view)
    assert MonitoringBrief.model_validate_json(facts.brief.model_dump_json()) == facts.brief
    before = project_case_progress("public_information_monitoring", view, facts=facts)
    assert _statuses(before)["scope"] == "completed"
    assert _statuses(before)["collect"] == "completed"
    assert _statuses(before)["signals"] == "needs_review"
    assert _statuses(before)["human_review"] == "needs_review"
    assert facts.signal_evaluation.origin == "synthetic"
    assert facts.signal_evaluation.relative_change_percent == 20
    assert len(facts.signal_evaluation.record_ids) == 11

    reviewed = _review(view)
    dossier = project_dossier_readiness(reviewed)
    assert dossier.status == "ready_for_export"
    assert dossier.current_decision_count == 1
    assert dossier.included_finding_ids == ("synthetic:count-change",)
    after = project_case_progress("public_information_monitoring", reviewed, facts=facts)
    assert _statuses(after)["human_review"] == "completed"
    assert _statuses(after)["export"] == "pending"
    exported = ProgressFacts.model_validate(
        {
            **facts.model_dump(mode="json"),
            "exported_assessment_fingerprint": dossier.assessment_fingerprint,
        }
    )
    assert (
        _statuses(project_case_progress("public_information_monitoring", reviewed, facts=exported))[
            "export"
        ]
        == "completed"
    )
    assert view == original


@pytest.mark.parametrize("changed", ["scenario_version", "case_fingerprint"])
def test_historical_decisions_cannot_make_changed_context_ready(changed):
    view, facts = _synthetic_view()
    reviewed = _review(view)
    prior = project_dossier_readiness(reviewed)
    if changed == "scenario_version":
        reviewed["baseline_scenario"]["version"] = 2
    else:
        reviewed["case_fingerprint"] = "changed evidence identity"
    dossier = project_dossier_readiness(reviewed)
    assert dossier.status == "needs_review"
    assert dossier.current_decision_count == 0
    assert dossier.unreviewed_finding_ids == ("synthetic:count-change",)
    assert dossier.assessment_fingerprint != prior.assessment_fingerprint
    assert (
        _statuses(project_case_progress("public_information_monitoring", reviewed, facts=facts))[
            "human_review"
        ]
        == "needs_review"
    )


def test_exact_quote_and_saved_content_hash_are_required_before_dossier_export():
    view, _ = _synthetic_view()
    reviewed = _review(view)
    reviewed["bundle"]["fragments"][0]["quote"] = "A quotation never saved in this record"
    invalid_quote = project_dossier_readiness(reviewed)
    assert invalid_quote.status == "unavailable"
    assert invalid_quote.invalid_reference_ids == (reviewed["bundle"]["fragments"][0]["id"],)

    reviewed = _review(view)
    reviewed["bundle"]["records"][0]["text"] = "Changed without updating the content hash"
    invalid_hash = project_dossier_readiness(reviewed)
    assert invalid_hash.status == "unavailable"
    assert reviewed["bundle"]["records"][0]["id"] in invalid_hash.invalid_reference_ids


def test_unknown_artifact_availability_and_policy_links_do_not_establish_readiness():
    view, _ = _synthetic_view()
    reviewed = _review(view)
    record = reviewed["bundle"]["records"][0]
    record["artifact_ref"] = "saved/reference.png"
    references = platform_reporting_references()
    assert len(references) == 5
    assert all(ref.version is None and ref.applicability == "unknown" for ref in references)
    unknown = project_dossier_readiness(reviewed, policy_references=references)
    assert unknown.status == "needs_review"
    assert unknown.unknown_availability_record_ids == (record["id"],)
    unavailable = project_dossier_readiness(reviewed, artifact_availability={record["id"]: False})
    assert unavailable.status == "unavailable"
    verified = project_dossier_readiness(
        reviewed, artifact_availability={record["id"]: True}, policy_references=references
    )
    assert verified.status == "ready_for_export"
    assert verified.policy_status == "unknown"
    assert verified.legal_status == "not_assessed"
    assert verified.submission_status == "not_submitted"


def test_all_withheld_is_a_reviewed_omission_package_not_an_included_claim():
    view, _ = _synthetic_view()
    dossier = project_dossier_readiness(_review(view, disposition="withhold"))
    assert dossier.status == "ready_for_export"
    assert dossier.included_finding_ids == ()
    assert dossier.withheld_finding_ids == ("synthetic:count-change",)
    assert "omissions" in dossier.summary


def test_selected_run_cannot_borrow_completed_signals_or_reviews_from_another_run():
    view, _ = _synthetic_view()
    reviewed = _review(view)
    result = deepcopy(reviewed["bundle"]["runs"][0])
    result.update(id="synthetic:result-run", status="failed")
    finding = deepcopy(reviewed["bundle"]["findings"][0])
    finding.update(snapshot_id="synthetic:result-finding", run_id=result["id"])
    reviewed["bundle"]["runs"].append(result)
    reviewed["bundle"]["findings"].append(finding)
    reviewed["scenario"] = {
        "id": "synthetic:result-scenario",
        "label": "Result assessment",
        "version": 1,
        "base_run_id": result["id"],
    }
    reviewed["is_baseline"] = False
    progress = project_case_progress("cited_claim_investigation", reviewed)
    assert _statuses(progress)["signals"] == "unavailable"
    assert _statuses(progress)["human_review"] == "needs_review"
    dossier = project_dossier_readiness(reviewed)
    assert dossier.current_decision_count == 0
    assert dossier.unreviewed_finding_ids == (finding["snapshot_id"],)
    reviewed["scenario"]["base_run_id"] = "missing-run"
    assert project_dossier_readiness(reviewed).status == "unavailable"


def test_verified_readiness_requires_a_dated_supporting_observation():
    with pytest.raises(ValidationError, match="requires a check time"):
        ReadinessFact(name="Model", status="verified")
    checked = ReadinessFact(
        name="One declared connector",
        status="verified",
        checked_at=datetime(2026, 11, 6, tzinfo=UTC),
        evidence="Synthetic fixture successfully fetched this declared test source.",
    )
    assert checked.status == "verified"
    assert ProgressFacts().model_readiness.status == "unknown"
