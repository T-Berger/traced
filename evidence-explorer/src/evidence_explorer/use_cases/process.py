"""Read-only progress through a declared use case and the existing review context."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..review.workflow import assessment_context, brief_projection
from .catalog import get_use_case
from .contracts import (
    BriefScopeContext,
    CaseProgress,
    ProcessStepDefinition,
    ProgressFacts,
    StepProgress,
)
from .reporting import _audit_evidence, project_dossier_readiness


def _current_bound_scope(brief, binding, view, use_case_id: str) -> bool:
    """Reject unbound, cross-case or stale plans without changing saved state."""
    if brief is None or binding is None or not brief.confirmed:
        return False
    # Local imports keep the strict portable planning types independent of this
    # package's catalog/projection initialization.
    from ..planning_briefs.models import digest
    from ..planning_briefs.scope import scope_identity

    try:
        projected = view.get("planning_scope")
        scope = (
            BriefScopeContext.model_validate(projected)
            if projected is not None
            else scope_identity(view.get("bundle", {}))
        )
    except (ValueError, TypeError, KeyError):
        return False
    return (
        brief.use_case_id == use_case_id
        and binding.brief_id == brief.id
        and binding.case_id == scope.case_id
        and scope.case_id == view.get("bundle", {}).get("case", {}).get("id")
        and view.get("evidence_fingerprint", scope.evidence_fingerprint)
        == scope.evidence_fingerprint
        and binding.policy == scope.policy
        and binding.evidence_fingerprint == scope.evidence_fingerprint
        and binding.scope_fingerprint == scope.scope_fingerprint
        and binding.plan_fingerprint == digest(brief.model_dump(mode="json"))
        and sorted(brief.source_selection) == sorted(scope.source_refs)
    )


_STEPS = (
    ProcessStepDefinition(
        id="scope",
        title="Define the scope",
        purpose="Agree the public-interest question and boundaries.",
        completion_evidence=(
            "A confirmed brief with purpose, public scope, sources, reviewer, and required windows."
        ),
    ),
    ProcessStepDefinition(
        id="collect",
        title="Collect saved evidence",
        purpose="Preserve original source material and provenance.",
        completion_evidence="Saved records and a collection result for the declared scope.",
    ),
    ProcessStepDefinition(
        id="signals",
        title="Inspect baselines and signals",
        purpose="Inspect recorded method outputs and comparable activity.",
        completion_evidence="A specialist result or explicit valid activity-window comparison.",
    ),
    ProcessStepDefinition(
        id="evidence_check",
        title="Check exact evidence",
        purpose="Resolve citations and inspect source context.",
        completion_evidence="Saved references resolve; unsupported statements need review.",
    ),
    ProcessStepDefinition(
        id="human_review",
        title="Record a human decision",
        purpose="Use, qualify, or withhold each current finding.",
        completion_evidence="Current decisions pinned to this evidence and assessment version.",
    ),
    ProcessStepDefinition(
        id="export",
        title="Prepare the reviewed export",
        purpose="Share evidence, decisions, and remaining questions.",
        completion_evidence="An export fingerprint matching the current reviewed assessment.",
    ),
)


def process_definition(use_case_id: str) -> tuple[ProcessStepDefinition, ...]:
    get_use_case(use_case_id)
    return _STEPS


def project_case_progress(
    use_case_id: str,
    view: Mapping[str, Any],
    *,
    facts: ProgressFacts | None = None,
) -> CaseProgress:
    """Describe observed progress without creating scope, jobs, decisions, or files."""
    definition = get_use_case(use_case_id)
    facts = ProgressFacts.model_validate(facts) if facts is not None else ProgressFacts()
    bundle = view.get("bundle", {})
    case_id = bundle.get("case", {}).get("id")
    record_ids = tuple(record["id"] for record in bundle.get("records", []))
    context = assessment_context(view)
    findings = [
        finding
        for finding in bundle.get("findings", [])
        if finding.get("run_id") == context["base_run_id"]
    ]
    dossier = project_dossier_readiness(view)
    statuses: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    brief = facts.brief
    scope_complete = _current_bound_scope(brief, facts.brief_binding, view, use_case_id)
    if scope_complete and definition.requires_activity_baseline:
        scope_complete = all(
            window is not None and window.start is not None and window.end is not None
            for window in (brief.baseline_window, brief.observation_window)
        )
    statuses["scope"] = (
        "completed" if scope_complete else "needs_review",
        "The analyst confirmed a public-information scope."
        if scope_complete
        else "Save, bind and confirm a complete planning revision for this case's saved scope.",
        (),
    )
    collection = facts.collection_state
    if collection == "completed" and record_ids:
        statuses["collect"] = (
            "completed",
            "A collection result and saved records are supplied.",
            (),
        )
    elif collection in {"queued", "running"}:
        statuses["collect"] = (
            "pending" if collection == "queued" else "in_progress",
            f"Collection is reported {collection}; it is not complete.",
            (),
        )
    elif collection in {"failed", "unavailable"} or not record_ids:
        statuses["collect"] = ("unavailable", "No completed collection evidence is available.", ())
    else:
        statuses["collect"] = (
            "needs_review",
            f"{len(record_ids)} saved records are present; scope coverage is not confirmed.",
            (),
        )
    signal = facts.signal_evaluation
    if signal is not None:
        missing = set(signal.record_ids) - set(record_ids)
        status = (
            "unavailable"
            if signal.status == "unavailable" or missing
            else "needs_review"
            if signal.status == "review_needed"
            else "completed"
        )
        statuses["signals"] = (
            status,
            "Signal references records outside this case." if missing else signal.summary,
            signal.limitations,
        )
    elif definition.requires_activity_baseline:
        statuses["signals"] = (
            "unavailable",
            "No explicit baseline/observation evaluation is supplied.",
            (),
        )
    elif findings and any(
        run.get("id") == context["base_run_id"] and run.get("status") == "completed"
        for run in bundle.get("runs", [])
    ):
        statuses["signals"] = (
            "completed",
            "Stored findings from a declared completed run are available.",
            ("This does not establish that models or all platform connections are ready.",),
        )
    else:
        statuses["signals"] = ("unavailable", "No recorded specialist findings are available.", ())
    try:
        projection = brief_projection(view)
        audit = _audit_evidence(view, projection)
        missing = audit["all_missing"]
        invalid = audit["invalid"]
        status = (
            "unavailable" if invalid or not findings else "needs_review" if missing else "completed"
        )
        statuses["evidence_check"] = (
            status,
            "Exact reference checks are complete; semantic support remains a reviewer judgment."
            if status == "completed"
            else "Some findings or saved references need evidence review.",
            ("Text-reference validity establishes neither truth nor local media availability.",),
        )
    except (KeyError, TypeError, ValueError):
        statuses["evidence_check"] = ("unavailable", "Evidence references cannot be resolved.", ())
    human_done = (
        bool(findings) and not dossier.unreviewed_finding_ids and dossier.current_decision_count > 0
    )
    statuses["human_review"] = (
        "completed" if human_done else "needs_review" if findings else "unavailable",
        "Current human decisions are recorded for the visible findings."
        if human_done
        else "Record current Use, Qualify, or Withhold decisions; earlier reviews do not transfer.",
        (),
    )
    exported = (
        dossier.status == "ready_for_export"
        and facts.exported_assessment_fingerprint is not None
        and facts.exported_assessment_fingerprint == dossier.assessment_fingerprint
    )
    statuses["export"] = (
        "completed"
        if exported
        else "pending"
        if dossier.status == "ready_for_export"
        else "needs_review",
        "A matching current-assessment export is reported."
        if exported
        else "Ready to export; no matching export is reported."
        if dossier.status == "ready_for_export"
        else dossier.summary,
        ("The projection creates no export and submits no platform report.",),
    )
    steps = tuple(
        StepProgress(
            id=step.id,
            title=step.title,
            status=statuses[step.id][0],
            summary=statuses[step.id][1],
            record_ids=record_ids if step.id in {"collect", "evidence_check"} else (),
            notes=statuses[step.id][2],
        )
        for step in _STEPS
    )
    return CaseProgress(
        use_case_id=use_case_id,
        case_id=case_id,
        title=definition.title,
        steps=steps,
        next_step_id=next((step.id for step in steps if step.status != "completed"), None),
        model_readiness=facts.model_readiness,
        platform_readiness=facts.platform_readiness,
        limitations=(
            "This projection is read-only and does not configure collection or live alerts.",
            "Unreported model and platform readiness remain unknown.",
        ),
    )
