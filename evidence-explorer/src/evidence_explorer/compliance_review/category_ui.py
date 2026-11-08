"""Selected-assessment category review with explicit target and kind scope."""

from __future__ import annotations

from dash import Dash, Input, Output, dcc, html
from dash.exceptions import PreventUpdate

from ..review.integrations.snapshots import workspace_snapshot
from ..review.navigation import navigation_context
from ..review.workflow import assessment_context
from .review_ui import render_category_review
from .swarm_context import project_category_swarm_context

PREFIX = "compliance-category-"


def category_review_panel():
    return html.Details(
        [
            html.Summary("Review saved category findings over time"),
            html.P(
                "Choose the exact target and record kinds for the selected assessment. "
                "The view counts saved records with category proposals and current human "
                "decisions; it does not establish platform-wide coverage or enforcement.",
                className="rv-small rv-muted",
            ),
            html.Label("Analysis target", htmlFor=PREFIX + "target"),
            dcc.Dropdown(id=PREFIX + "target", options=[], placeholder="Select a target"),
            html.Label("Saved record kinds", htmlFor=PREFIX + "kinds"),
            dcc.Dropdown(
                id=PREFIX + "kinds",
                options=[],
                value=[],
                multi=True,
                placeholder="Select record kinds to include",
            ),
            html.Label("Saved Swarm runs (optional)", htmlFor=PREFIX + "swarm-runs"),
            dcc.Dropdown(
                id=PREFIX + "swarm-runs",
                options=[],
                value=[],
                multi=True,
                placeholder="Select explicitly linked behavioral context",
            ),
            html.P(
                "Only the same saved record can connect a selected Swarm finding to a category "
                "row. Swarm observations and their reviews remain separate.",
                className="rv-small rv-muted",
            ),
            html.Div(id=PREFIX + "output", **{"aria-live": "polite"}),
        ],
        className="rv-details",
    )


def capture_category_view(service, state):
    """Capture the selected view, its context, and its complete bundle together."""
    if not isinstance(state, dict) or not state.get("case_id"):
        raise ValueError("Choose a saved case and assessment first.")
    with workspace_snapshot(service) as snapshot:
        view = snapshot.view(state["case_id"], state.get("scenario_id"))
        context = navigation_context(view, state)
        bundle = snapshot.export_bundle(state["case_id"])
    return view, bundle, context


def category_scope_options(view, bundle):
    """Expose saved values as choices without selecting or inferring scope."""
    run_id = assessment_context(view).get("base_run_id")
    candidates = [
        finding
        for finding in bundle.findings
        if finding.run_id == run_id and finding.details.get("domain") == "compliance_category"
    ]
    targets = sorted(
        {
            finding.details["target"]
            for finding in candidates
            if isinstance(finding.details.get("target"), str) and finding.details["target"].strip()
        }
    )
    target_records = {finding.details.get("record_id") for finding in candidates}
    kinds = sorted({record.kind for record in bundle.records if record.id in target_records})
    return (
        [{"label": target, "value": target} for target in targets],
        [{"label": kind, "value": kind} for kind in kinds],
    )


def swarm_run_options(bundle):
    """Offer saved detector runs; do not imply that complete inputs are present."""
    return [
        {"label": f"{run.method} · {run.id}", "value": run.id}
        for run in bundle.runs
        if run.method.startswith("swarm_detector.")
    ]


def register_category_review_callbacks(app: Dash, service):
    @app.callback(
        Output(PREFIX + "target", "options"),
        Output(PREFIX + "target", "value"),
        Output(PREFIX + "kinds", "options"),
        Output(PREFIX + "kinds", "value"),
        Output(PREFIX + "swarm-runs", "options"),
        Output(PREFIX + "swarm-runs", "value"),
        Input("rv-session", "data"),
        Input("rv-page", "value"),
    )
    def scope_options(state, page):
        if page != "compare":
            raise PreventUpdate
        try:
            view, bundle, _ = capture_category_view(service, state)
            targets, kinds = category_scope_options(view, bundle)
            swarm_runs = swarm_run_options(bundle)
        except (ValueError, KeyError, TypeError):
            targets, kinds, swarm_runs = [], [], []
        # Case or assessment changes invalidate prior selections.
        return targets, None, kinds, [], swarm_runs, []

    @app.callback(
        Output(PREFIX + "output", "children"),
        Input("rv-session", "data"),
        Input("rv-page", "value"),
        Input(PREFIX + "target", "value"),
        Input(PREFIX + "kinds", "value"),
        Input(PREFIX + "swarm-runs", "value"),
    )
    def category_result(state, page, target, kinds, swarm_runs):
        if page != "compare":
            raise PreventUpdate
        if not target or not kinds:
            return html.P(
                "Choose a target and at least one saved record kind.", className="rv-small"
            )
        try:
            view, bundle, context = capture_category_view(service, state)
            targets, available_kinds = category_scope_options(view, bundle)
            if target not in {item["value"] for item in targets} or not set(kinds) <= {
                item["value"] for item in available_kinds
            }:
                raise ValueError("The selected scope is no longer available. Choose again.")
            result = project_category_swarm_context(
                view,
                bundle,
                record_kinds=kinds,
                target=target,
                swarm_run_ids=swarm_runs or [],
            )
            return render_category_review(result, context)
        except (ValueError, KeyError, TypeError) as exc:
            return html.P(str(exc), className="rv-notice", role="status")
