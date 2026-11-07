"""Session-local purpose guidance over the existing case and assessment workflow.

The panel projects saved evidence and review state. It never creates a case,
scenario, source subscription, model job, monitoring brief, or report submission.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from dash import Dash, Input, Output, dcc, html

from ..presentation import badge, empty

_STATUS_LABELS = {
    "completed": "Completed for this assessment",
    "needs_review": "Needs review",
    "unavailable": "Unavailable or unknown",
    "pending": "Not completed",
    "in_progress": "In progress",
}
_STATUS_TONES = {"completed": "green", "needs_review": "amber", "in_progress": "blue"}


def purpose_options() -> list[dict[str, str]]:
    """Expose the common catalog without a separately maintained UI taxonomy."""
    from ...use_cases import list_use_cases

    return [{"label": purpose.title, "value": purpose.id} for purpose in list_use_cases()]


def selected_purpose(value: object) -> dict[str, str] | None:
    """Validate browser selection; only the purpose ID is kept in session storage."""
    valid = {option["value"] for option in purpose_options()}
    return {"purpose_id": value} if isinstance(value, str) and value in valid else None


def use_cases_panel() -> html.Section:
    """Mount once on Cases; all component IDs are local to this additive panel."""
    return html.Section(
        [
            html.Div(
                [
                    html.H2("Explore workflows"),
                    html.P(
                        "Browse example approaches without changing your investigation plan. "
                        "This optional guide is session-only. "
                        "Save your purpose in Investigation plan.",
                        className="rv-small rv-muted",
                    ),
                ],
                className="rv-section-heading",
            ),
            html.Label("Workflow to explore", htmlFor="trail-use-case-select"),
            dcc.Dropdown(
                id="trail-use-case-select",
                options=purpose_options(),
                value=None,
                placeholder="Explore an example workflow",
                clearable=True,
                persistence=True,
                persistence_type="session",
            ),
            dcc.Store(id="trail-use-case-purpose", storage_type="session"),
            html.Div(
                id="trail-use-case-details",
                children=empty(
                    "Explore a workflow",
                    "The guide will show its audience, problem, process, and current case status.",
                ),
                **{"aria-live": "polite"},
            ),
        ],
        className="rv-case-card",
        **{"aria-label": "Investigation purpose and process"},
    )


def _process_steps(progress) -> html.Ol:
    return html.Ol(
        [
            html.Li(
                [
                    html.Div(
                        [
                            html.Strong(step.title),
                            badge(
                                _STATUS_LABELS.get(step.status, "Unavailable or unknown"),
                                _STATUS_TONES.get(step.status, "neutral"),
                            ),
                        ],
                        className="rv-toolbar",
                    ),
                    html.P(step.summary, className="rv-small"),
                    html.Ul(
                        [html.Li(note) for note in step.notes],
                        className="rv-small rv-muted",
                    )
                    if step.notes
                    else None,
                ],
                className="rv-case-card",
                **{"data-process-step": step.id, "data-process-status": step.status},
            )
            for step in progress.steps
        ],
        **{"aria-label": "Process progress for the selected assessment"},
    )


def _alert_example() -> html.Details:
    from ...use_cases.alerts import evaluate_activity_alert, illustrative_activity_example

    rule, baseline, observation = illustrative_activity_example()
    result = evaluate_activity_alert(rule, baseline, observation)
    status = {
        "review_needed": "Synthetic example requests review",
        "not_triggered": "Synthetic example below threshold",
        "unavailable": "Synthetic comparison unavailable",
    }.get(result.status, "Synthetic comparison unavailable")

    def window(value):
        return (
            f"{value.start.isoformat()} to {value.end.isoformat()}"
            if value.start and value.end
            else "Unknown"
        )

    def coverage(value):
        return (
            f"{value.coverage_fraction:.0%} of the declared synthetic subset"
            if value.coverage_fraction is not None
            else "Unknown"
        )

    return html.Details(
        [
            html.Summary("Synthetic alert calculation example"),
            html.Div([badge("Synthetic planning example"), badge(status)], className="rv-toolbar"),
            html.P(result.summary, className="rv-small"),
            html.Dl(
                [
                    html.Dt("Baseline"),
                    html.Dd(f"{baseline.observed_count} synthetic records · {window(baseline)}"),
                    html.Dt("Observation"),
                    html.Dd(
                        f"{observation.observed_count} synthetic records · {window(observation)}"
                    ),
                    html.Dt("Example rule"),
                    html.Dd(f"At least {rule.threshold_percent:g}% {rule.direction}"),
                    html.Dt("Relative change"),
                    html.Dd(
                        f"{result.relative_change_percent:.2f}%"
                        if result.relative_change_percent is not None
                        else "Unavailable"
                    ),
                    html.Dt("Baseline coverage"),
                    html.Dd(coverage(baseline)),
                    html.Dt("Observation coverage"),
                    html.Dd(coverage(observation)),
                    html.Dt("Time basis"),
                    html.Dd(result.coverage.timestamp_basis),
                ],
                className="rv-metadata",
            ),
            html.P(result.method, className="rv-small"),
            html.P(
                f"Illustration only: {rule.threshold_percent:g}% is an analyst-selected example, "
                "not a platform rule. This calculation uses separate fictional records, "
                "not the current case. No live alert is configured, evaluated, or triggered "
                "by this panel.",
                className="rv-small rv-muted",
            ),
            html.Ul([html.Li(note) for note in result.limitations], className="rv-small rv-muted"),
            html.Details(
                [
                    html.Summary("Synthetic input references"),
                    html.P(", ".join(result.record_ids), className="rv-small rv-mono"),
                ],
                className="rv-details",
            ),
        ],
        className="rv-details",
        **{"data-alert-origin": result.origin, "data-alert-status": result.status},
    )


def _capabilities(progress) -> html.Details:
    labels = {
        "unknown": "Unknown",
        "configured": "Configured · not verified",
        "verified": "Verified for the stated check",
        "unavailable": "Unavailable",
    }
    facts = [progress.model_readiness, *progress.platform_readiness]
    return html.Details(
        [
            html.Summary("Source and model readiness"),
            html.Ul(
                [
                    html.Li(
                        [
                            html.Strong(f"{fact.name}: "),
                            labels.get(fact.status, "Unknown"),
                            html.P(fact.evidence, className="rv-small rv-muted")
                            if fact.evidence
                            else None,
                        ]
                    )
                    for fact in facts
                ]
            ),
            html.P(
                "Platform access: unknown; no verification supplied.", className="rv-small rv-muted"
            )
            if not progress.platform_readiness
            else None,
        ],
        className="rv-details",
    )


def render_use_case_details(
    purpose_id: str | None,
    view: Mapping[str, Any] | None = None,
    *,
    context_error: str | None = None,
    unsaved_changes: bool = False,
    facts=None,
) -> html.Div:
    """Pure rendering of catalog and current-context process/report projections."""
    from ...use_cases import (
        get_use_case,
        project_case_progress,
        project_dossier_readiness,
    )

    if not purpose_id:
        return empty(
            "Explore a workflow",
            "This optional guide does not save or change your investigation plan.",
        )
    try:
        purpose = get_use_case(purpose_id)
    except (KeyError, ValueError):
        return empty("Purpose unavailable", "Choose a purpose from the current catalog.")

    introduction = [
        html.H3(purpose.title),
        html.Dl(
            [
                html.Dt("Who it serves"),
                html.Dd(", ".join(purpose.target_roles)),
                html.Dt("Problem"),
                html.Dd(purpose.problem),
                html.Dt("User story"),
                html.Dd(purpose.user_story),
                html.Dt("Intended outcome"),
                html.Dd(purpose.outcome),
            ],
            className="rv-metadata",
        ),
        html.Ul([html.Li(note) for note in purpose.scope_notes], className="rv-small rv-muted"),
    ]
    if context_error:
        return html.Div(
            [
                *introduction,
                empty("Current case unavailable", context_error),
                _alert_example(),
            ],
            className="rv-case-card",
        )
    if not view:
        return html.Div(
            [
                *introduction,
                empty(
                    "Choose a case to see its progress",
                    "Selecting a purpose does not confirm the case scope or complete a step.",
                ),
                _alert_example(),
            ],
            className="rv-case-card",
        )

    progress = project_case_progress(purpose_id, view, facts=facts)
    readiness = project_dossier_readiness(view)
    case = view.get("bundle", {}).get("case", {})
    assessment = view.get("scenario") or view.get("baseline_scenario") or {}
    assessment_label = assessment.get("label") or (
        "Baseline" if view.get("is_baseline", not view.get("scenario")) else "Scenario"
    )
    readiness_status = str(readiness.status).replace("_", " ").capitalize()
    next_step = next((step for step in progress.steps if step.id == progress.next_step_id), None)
    included = len(readiness.included_finding_ids)
    unreviewed = len(readiness.unreviewed_finding_ids)
    return html.Div(
        [
            *introduction,
            html.Div(
                [
                    html.Strong(case.get("title") or progress.title),
                    badge(str(assessment_label)),
                    badge("Synthetic case") if case.get("synthetic") else None,
                ],
                className="rv-toolbar",
            ),
            html.P(
                "Progress applies to saved state in this selected case and assessment. "
                "Configured sources alone do not establish successful collection or verification.",
                className="rv-small rv-muted",
            ),
            html.Div(
                "Unsaved review changes. Save the current decision in Review before exporting.",
                className="rv-notice",
                role="status",
            )
            if unsaved_changes
            else None,
            _process_steps(progress),
            html.P(
                f"Next: {next_step.title}" if next_step else "Inspect any remaining limitations.",
                className="rv-small",
            ),
            html.Ul(
                [html.Li(note) for note in progress.limitations], className="rv-small rv-muted"
            ),
            _capabilities(progress),
            html.Details(
                [
                    html.Summary("Evidence dossier readiness"),
                    badge("Save review changes first" if unsaved_changes else readiness_status),
                    html.P(
                        "Readiness applies to the last saved assessment; save the current "
                        "review changes before preparing the export."
                        if unsaved_changes
                        else readiness.summary,
                        className="rv-small",
                    ),
                    html.P(
                        f"{included} statement{'s' if included != 1 else ''} currently included; "
                        f"{unreviewed} findings need a current decision.",
                        className="rv-small",
                    ),
                    html.Ul(
                        [html.Li(note) for note in readiness.limitations],
                        className="rv-small rv-muted",
                    ),
                    html.P(
                        "This is a preparation aid. The existing Brief controls remain the "
                        "place to export; no report is submitted automatically.",
                        className="rv-small rv-muted",
                    ),
                ],
                className="rv-details",
            ),
            _alert_example(),
        ],
        className="rv-case-card",
    )


def register_use_case_callbacks(app: Dash, service) -> None:
    """Read the selected assessment on demand; never create parallel workflow state."""

    @app.callback(
        Output("trail-use-case-purpose", "data"),
        Input("trail-use-case-select", "value"),
    )
    def choose_purpose(value):
        return selected_purpose(value)

    @app.callback(
        Output("trail-use-case-details", "children"),
        Input("trail-use-case-purpose", "data"),
        Input("rv-session", "data"),
        Input("rv-draft-store", "data"),
        Input("rv-review-reason", "value"),
        Input("trail-planning-refresh", "data"),
    )
    def show_process(selection, state, drafts=None, note=None, _planning=None):
        selection = selection if isinstance(selection, Mapping) else {}
        state = state if isinstance(state, Mapping) else {}
        purpose = selected_purpose((selection or {}).get("purpose_id"))
        purpose_id = purpose["purpose_id"] if purpose else None
        if not purpose_id or not state or not state.get("case_id"):
            return render_use_case_details(purpose_id)
        try:
            view = service.view(state["case_id"], state.get("scenario_id"))
            if view.get("bundle", {}).get("case", {}).get("id") != state["case_id"]:
                raise ValueError("Selected case no longer matches the assessment")
        except (KeyError, ValueError, OSError):
            return render_use_case_details(
                purpose_id,
                context_error="Choose the current case and assessment again. Earlier case "
                "progress is not reused here.",
            )
        from ..drafts import unsaved_for_export

        return render_use_case_details(
            purpose_id,
            view,
            unsaved_changes=unsaved_for_export(dict(state), view, drafts, note),
            facts=(
                service.planning_progress_facts(view)
                if hasattr(service, "planning_progress_facts")
                else None
            ),
        )
