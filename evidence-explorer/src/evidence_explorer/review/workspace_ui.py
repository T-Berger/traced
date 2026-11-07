"""Shared navigation, assessment context, and local draft callbacks.

The application factory registers these alongside the core review actions. Screen
projections stay reusable across text, behavioral, and historical media cases.
"""

from __future__ import annotations

from dash import Dash, Input, Output, State, ctx, html, no_update

from .selectors import bundle_data
from .selectors import finding_id as finding_identifier
from .service import ReviewService
from .workflow import assessment_context, assessment_decisions
from .workspace_views import (
    brief_preview,
    case_cards,
    compare_summary,
    context_header,
    finding_summary,
)


def register_workspace_callbacks(app: Dash, service: ReviewService) -> None:
    """Register page navigation, read-only projections, and persisted form drafts."""

    @app.callback(
        Output("rv-page", "value"), Input("rv-session", "data"), prevent_initial_call=True
    )
    def navigate(state):
        return (state or {}).get("navigate_to") or no_update

    app.clientside_callback(
        """function(page, state) {
            const changes = dash_clientside.callback_context.triggered || [];
            const pageChanged = changes.some(x => x.prop_id === 'rv-page.value');
            if (pageChanged || (state && state.navigate_to)) {
                requestAnimationFrame(() => requestAnimationFrame(() => {
                    const targets = new Set(['trail-use-case-select', 'live-preset',
                        'trail-planning-question', 'trail-planning-picker', 'trail-planning-scope',
                        'trail-planning-history',
                        'trail-gather-evidence', 'trail-planning-resume', 'trail-planning-save',
                        'trail-planning-bind', 'trail-planning-confirm', 'rv-finding-heading',
                        'rv-source', 'rv-source-heading', 'rv-review-reason', 'rv-brief-page',
                        'rv-compare-summary']);
                    const destination = state && state.navigate_to === page;
                    const id = destination && state.navigate_target;
                    const target = targets.has(id) && document.getElementById(id);
                    if (target) {
                        if (target.tagName === 'DETAILS') target.open = true;
                        for (let parent = target.parentElement; parent;
                                parent = parent.parentElement) {
                            if (parent.tagName === 'DETAILS') parent.open = true;
                        }
                    }
                    if (target && target.getClientRects().length &&
                            getComputedStyle(target).display !== 'none') {
                        let focus = target;
                        if (id === 'trail-use-case-select' || id === 'live-preset' ||
                                id === 'trail-planning-picker') {
                            focus = target.matches('button, input, [role="combobox"]') ?
                                target : target.querySelector('button, input, [role="combobox"]');
                            focus = focus || target;
                        }
                        if (!focus.matches('button, input, textarea, select, a, [tabindex]')) {
                            focus.setAttribute('tabindex', '-1');
                        }
                        focus.focus({preventScroll: true});
                        target.scrollIntoView({block: 'start', behavior: 'auto'});
                    } else if (pageChanged) {
                        window.scrollTo(0, 0);
                    }
                }));
            }
            return {page: page, case_id: state && state.case_id};
        }""",
        Output("rv-scroll-marker", "data"),
        Input("rv-page", "value"),
        Input("rv-session", "data"),
    )

    @app.callback(
        Output("rv-exclude-reason", "value"), Input("rv-session", "data"), prevent_initial_call=True
    )
    def clear_exclusion_reason(_state):
        return ""

    @app.callback(
        Output("rv-context", "children"),
        Output("rv-case-list", "children"),
        Output("rv-finding-summary", "children"),
        Output("rv-compare-summary", "children"),
        Output("rv-brief-preview", "children"),
        Output("rv-scenario", "options"),
        Output("rv-back-baseline", "disabled"),
        Input("rv-session", "data"),
    )
    def workspace(state):
        state = state or {}
        cases = []
        for summary in service.list_cases():
            metadata = service.repository.get_case_metadata(summary["id"])
            case = metadata.case.model_dump(mode="json")
            case_id = summary["id"]
            kind = next(
                (kind for kind in ("media", "swarm", "sources") if case_id.startswith(kind + ":")),
                "investigation",
            )
            cases.append(
                {
                    "case": case,
                    "record_count": summary["record_count"],
                    "finding_count": summary["finding_count"],
                    "case_type": kind,
                }
            )
        cards = case_cards(cases, state.get("case_id"))
        if not state.get("case_id"):
            return (
                "Choose a case",
                cards,
                "",
                "",
                "",
                [{"label": "Baseline", "value": "baseline"}],
                True,
            )
        view = service.view(state["case_id"], state.get("scenario_id"))
        bundle = bundle_data(view)
        finding = next(
            (
                f
                for f in bundle.get("findings", [])
                if finding_identifier(f) == state.get("finding_id")
            ),
            {},
        )
        assessments = [a for a in view.get("assessments", []) if not a.get("is_baseline")]
        options = [
            {"label": "Baseline", "value": "baseline"},
            *[{"label": a["label"], "value": a["id"]} for a in assessments],
        ]
        comparison_id = state.get("comparison_scenario_id")
        if not view.get("is_baseline"):
            comparison = view
        elif comparison_id:
            comparison = service.view(state["case_id"], comparison_id)
        else:
            comparison = (
                service.view(state["case_id"], assessments[-1]["id"]) if assessments else None
            )
        return (
            context_header(view),
            cards,
            finding_summary(bundle, finding),
            compare_summary(service.view(state["case_id"]), comparison, current_view=view),
            brief_preview(view),
            options,
            bool(view.get("is_baseline")),
        )

    @app.callback(
        Output("rv-draft-store", "data"),
        Output("rv-review-reason", "value"),
        Output("rv-save-state", "children"),
        Input("rv-session", "data"),
        Input("rv-review-reason", "value"),
        State("rv-draft-store", "data"),
    )
    def drafts(state, note, stored):
        from .drafts import draft_key

        state, store = state or {}, dict(stored or {})
        notes = dict(store.get("notes", {}))
        key = draft_key(state)
        if not state.get("case_id"):
            return {"active": key, "notes": notes}, "", "Choose a finding to record your decision."
        view = service.view(state["case_id"], state.get("scenario_id"))
        saved = assessment_decisions(view).get(state.get("finding_id"))
        key = draft_key(state, view)
        saved_note = (saved or {}).get("reason", "")
        if ctx.triggered_id == "rv-review-reason":
            notes[key] = str(note or "")
            value = no_update
            current_note = str(note or "")
        else:
            # On reload Dash restores session storage before hydrating the
            # textarea. Only an explicit edit may replace a persisted draft.
            current_note = notes.get(key, saved_note)
            value = current_note
        dirty = current_note != saved_note
        status = (
            "Unsaved changes · your draft stays when you switch findings."
            if dirty
            else (
                f"Reviewed · {saved['disposition'].title()} · "
                f"saved for {assessment_context(view)['label']}"
                if saved
                else "Draft · no decision saved for this assessment"
            )
        )
        return (
            {"active": key, "notes": notes, "dirty": dirty},
            value,
            html.Span(status, className="rv-small rv-muted"),
        )
