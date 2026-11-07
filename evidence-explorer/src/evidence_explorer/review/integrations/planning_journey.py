"""Shared presentation of the explicit plan-to-evidence handoff.

These states describe saved facts and available controls. They never bind a plan,
confirm a scope, infer collection completeness, or approve a finding.
"""

from __future__ import annotations

from dash import html

from .brief_editor_state import EditorDraft, EditorStore, _remember_state


def current_draft(service, draft: EditorDraft) -> EditorDraft:
    """Refresh a presentation copy while retaining the captured write expectations."""
    result = draft.model_copy(deep=True)
    if result.brief_id and result.selected_revision_id:
        _remember_state(
            result, service.planning_briefs.load(result.brief_id, result.selected_revision_id)
        )
    if result.binding_case_id and result.binding_case_id != result.case_id:
        result.status = "needs_review"
        result.is_active_for_case = False
        result.reasons = (
            "This plan belongs to another case. Open that case or copy the fields into a new plan.",
        )
    return result


def draft_stage(draft: EditorDraft, source_refs=(), *, current_case=False) -> str:
    if draft.binding_case_id and draft.binding_case_id != draft.case_id:
        return "history"
    if draft.imported or (draft.brief_id and not draft.is_head):
        return "history"
    if draft.dirty:
        return "draft"
    if not draft.brief_id:
        return "start"
    if not current_case:
        return "saved"
    if draft.binding_case_id:
        return "confirmed" if draft.status == "confirmed" else "confirm"
    sources = {line.strip() for line in str(draft.fields.get("sources") or "").splitlines()}
    sources.discard("")
    if not sources:
        return "scope" if draft.evidence_requested else "saved"
    return "attach_ready" if sources == set(source_refs) else "scope"


def continuation_state(service, store: EditorStore, case_id: str | None):
    """Resolve only the exact user-saved reference, without substituting a head."""
    pending = store.continuation
    if not pending or not case_id or pending.origin_case_id == case_id:
        return None
    saved = service.planning_briefs.load(pending.brief_id, pending.revision_id)
    if saved.imported or not saved.is_head or saved.revision.binding:
        return None
    return saved


def journey_steps(stage: str | None, *, has_evidence: bool, has_findings: bool):
    """A compact sequence with words for every state, including unknown progress."""
    if stage is None:
        return None
    plan_saved = stage not in {"start", "draft", "history"}
    confirmed = stage == "confirmed"
    current = (
        0
        if stage in {"start", "draft", "history", "resume"}
        else 1
        if stage == "saved"
        else 3
        if confirmed
        else 2
    )
    rows = (
        ("Plan", "Saved" if plan_saved else "Review draft"),
        ("Evidence", "Saved case available" if has_evidence else "Collect or import"),
        ("Scope", "Confirmed" if confirmed else "Needs confirmation"),
        (
            "Findings",
            "Ready to inspect"
            if confirmed and has_findings
            else "Inspect saved evidence"
            if confirmed
            else "Inspect after scope",
        ),
    )
    return html.Ol(
        [
            html.Li(
                [html.Strong(f"{index + 1}. {label}"), html.Span(status)],
                **({"aria-current": "step"} if index == current else {}),
            )
            for index, (label, status) in enumerate(rows)
        ],
        className="rv-planning-journey",
        **{"aria-label": "Investigation steps"},
    )
