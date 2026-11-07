"""A compact available action over the existing purpose and review workflow.

Recommendations are navigation, never evidence that a process step is complete.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from dash import Dash, Input, Output, html

from ..navigation import NavigationContextError, contextual_next_action
from ..workflow import assessment_context
from .planning_journey import continuation_state, current_draft, draft_stage, journey_steps
from .use_cases_ui import selected_purpose


def planning_navigation(service, state, editor=None):
    """Resolve a destination, never confirmation, from current saved planning state."""
    from .brief_editor_state import EditorStore

    case_id = state.get("case_id")
    if hasattr(service, "_has_planning_storage") and not service._has_planning_storage():
        return None, None
    store = EditorStore.model_validate(editor or {})
    candidate = store.drafts.get(store.active)
    working = candidate if candidate and candidate.case_id == case_id else None
    if working and working.brief_id and working.selected_revision_id:
        saved = service.planning_briefs.load(working.brief_id, working.selected_revision_id)
        if saved.imported or not saved.is_head:
            return "history", saved.revision.fields.use_case_id
        binding = saved.revision.binding
        if binding and binding.case_id != case_id:
            return "start", None
        # Refresh only a presentation copy; never advance the editor's captured
        # head expectation or trust a browser confirmation flag.
        current = current_draft(service, working)
        refs = service.planning_briefs.case_binding(case_id).scope.source_refs if case_id else ()
        return draft_stage(
            current, refs, current_case=bool(case_id)
        ), saved.revision.fields.use_case_id
    if working and working.dirty:
        return "draft", working.fields.get("purpose")
    active = service.planning_briefs.state_for_case(case_id) if case_id else None
    if active:
        return (
            "confirmed" if active.status == "confirmed" else "confirm_saved",
            active.revision.fields.use_case_id,
        )
    pending = continuation_state(service, store, case_id)
    if pending:
        return "continue", pending.revision.fields.use_case_id
    available = service.planning_briefs.list_for_case(case_id)
    if available:
        return ("attach" if case_id else "resume"), None
    return "start", None


def render_next_action(service, state, page, selection, drafts=None, note=None, editor=None):
    state = dict(state) if isinstance(state, Mapping) else {}
    selection = selection if isinstance(selection, Mapping) else {}
    try:
        view = (
            service.view(state["case_id"], state.get("scenario_id")) if state.get("case_id") else {}
        )
        stage, saved_purpose = planning_navigation(service, state, editor)
        purpose = selected_purpose(saved_purpose) or selected_purpose(selection.get("purpose_id"))
        purpose_id = purpose["purpose_id"] if purpose else None
        progress = None
        if purpose_id and view:
            from ...use_cases import project_case_progress

            facts = (
                service.planning_progress_facts(view)
                if hasattr(service, "planning_progress_facts")
                else None
            )
            progress = project_case_progress(purpose_id, view, facts=facts)
        action = contextual_next_action(
            view,
            state,
            page=page or "cases",
            purpose_id=purpose_id,
            progress=progress,
            drafts=drafts,
            note=note,
            planning_stage=stage,
        )
    except (NavigationContextError, KeyError, ValueError, OSError, sqlite3.Error):
        return html.Div(
            [
                html.Strong("Choose the current case and assessment again. "),
                html.A("Go to the case selector", href="#rv-case"),
                html.P("Earlier navigation guidance is not reused.", className="rv-small"),
            ],
            className="rv-notice",
            role="status",
        )
    return html.Section(
        [
            journey_steps(
                stage,
                has_evidence=bool(view.get("bundle", {}).get("records")),
                has_findings=any(
                    finding.get("run_id") == assessment_context(view)["base_run_id"]
                    for finding in view.get("bundle", {}).get("findings", ())
                ),
            ),
            html.Div(
                [
                    html.Strong("Next available action", className="rv-small"),
                    html.P(action.explanation, className="rv-small rv-muted"),
                ],
                className="rv-next-action-copy",
            ),
            html.Button(
                action.label,
                id=action.button_id(),
                n_clicks=0,
                className="rv-button rv-button--outline",
            ),
        ],
        className="rv-next-action",
        **{"aria-label": "Available action for the selected case and assessment"},
    )


def register_navigation_callbacks(app: Dash, service) -> None:
    @app.callback(
        Output("trail-next-action", "children"),
        Input("rv-session", "data"),
        Input("rv-page", "value"),
        Input("trail-use-case-purpose", "data"),
        Input("rv-draft-store", "data"),
        Input("rv-review-reason", "value"),
        Input("trail-planning-refresh", "data"),
        Input("trail-planning-edit", "data"),
    )
    def guidance(state, page, purpose, drafts, note, _planning=None, editor=None):
        return render_next_action(service, state, page, purpose, drafts, note, editor)
