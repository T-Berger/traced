"""Read-only saved analysis context for the compliance review prototype.

No model, worker, coverage assertion, review, or platform action is created.
"""

from __future__ import annotations

from dash import Dash, Input, Output, dcc, html
from dash.exceptions import PreventUpdate

from ..review.integrations.snapshots import workspace_snapshot
from ..review.navigation import NextAction, navigation_context
from .evidence_bridge import project_saved_bundle
from .theory import theory_review_availability

PREFIX = "compliance-saved-"
MAX_ROWS = 50


def saved_context_panel():
    theory = theory_review_availability()
    return html.Details(
        [
            html.Summary("Saved analysis and Swarm context for evidence review"),
            html.Details(
                [
                    html.Summary("Swarm theory pipeline · unavailable for account review"),
                    html.P(theory["reason"]),
                    html.P(theory["next_steps"]),
                    html.P(
                        f"Implementation reviewed at commit {theory['reviewed_commit']}; "
                        "this is not a live run or live availability check.",
                        className="rv-small rv-muted",
                    ),
                ]
            ),
            html.P(
                "Choose saved analysis runs to inspect their linked evidence. These are "
                "baseline finding snapshots, not category decisions, verified violations, "
                "or platform enforcement outcomes.",
                className="rv-small rv-muted",
            ),
            html.Label("Saved analysis runs", htmlFor=PREFIX + "runs"),
            dcc.Dropdown(
                id=PREFIX + "runs",
                options=[],
                value=[],
                multi=True,
                placeholder="Select saved runs to inspect",
            ),
            html.Div(id=PREFIX + "output", **{"aria-live": "polite"}),
        ],
        className="rv-details",
    )


def capture_saved_context(service, state, run_ids):
    """Read and validate case, assessment, run selection and evidence in one snapshot."""
    if not isinstance(state, dict) or not state.get("case_id"):
        raise ValueError("Choose a saved case first.")
    if not isinstance(run_ids, list) or any(not isinstance(item, str) for item in run_ids):
        raise ValueError("Select saved analysis runs from this case.")
    with workspace_snapshot(service) as snapshot:
        view = snapshot.view(state["case_id"], state.get("scenario_id"))
        context = navigation_context(view, state)
        bundle = snapshot.export_bundle(state["case_id"])
        selected = project_saved_bundle(bundle, run_ids=run_ids)
        options = [{"label": f"{run.method} · {run.id}", "value": run.id} for run in bundle.runs]
    return selected, context, options


def _source_button(row, context, index):
    label = row.get("source_label") or row["id"]
    return html.Button(
        label,
        id=NextAction(
            kind="source",
            label=label,
            explanation="Inspect this saved record.",
            context=context,
            record_id=row["id"],
            location=f"compliance-saved-record-{index}",
        ).button_id(),
        n_clicks=0,
        className="rv-text-button",
    )


def _finding(item):
    availability = (
        "Producer reports complete inputs"
        if item["producer_reports_full_inputs"] is True
        else "Complete inputs not established"
    )
    fragments = [
        html.Li(f"{link['relation']}: {fragment['quote']}")
        for link in item["evidence_links"]
        for fragment in link["fragments"]
    ]
    return html.Li(
        [
            html.Strong("Swarm context" if item["type"] == "swarm" else "Analysis context"),
            html.P(item["statement"]),
            html.P(
                f"Run: {item['run_id']} · Method: {item['method']} · {availability} · "
                f"Status: {item['status']}",
                className="rv-small rv-muted",
            ),
            html.Ul(fragments)
            if fragments
            else html.P("No exact fragment supplied for this link."),
        ]
    )


def render_saved_context(data, context):
    """Render only linked source context; no category or compliance counts."""
    if not data["selected_run_ids"]:
        return html.P("Select a saved run to see its linked evidence.", className="rv-small")
    linked = [row for row in data["records"] if row["analysis"] or row["swarm"]]
    rows = []
    for index, row in enumerate(linked[:MAX_ROWS]):
        rows.append(
            html.Li(
                [
                    _source_button(row, context, index),
                    html.P(f"Saved record: {row['id']} · kind: {row.get('kind') or 'unknown'}"),
                    html.Ul([_finding(item) for item in row["analysis"] + row["swarm"]]),
                ]
            )
        )
    unsupported = [
        html.Li(f"{item['statement']} · run {item['run_id']} · no linked saved record")
        for item in data["unlinked_findings"][:MAX_ROWS]
    ]
    return html.Div(
        [
            html.P(
                "Baseline snapshots shown even when another assessment is selected; their "
                "reviews are not transferred to that assessment. Input availability is the "
                "producer's declaration. Findings do not establish bots or violations.",
                className="rv-notice",
            ),
            html.P(f"Showing {min(len(linked), MAX_ROWS)} of {len(linked)} linked saved records."),
            html.Ul(rows) if rows else html.P("No saved records linked to the selected runs."),
            html.Details(
                [
                    html.Summary(
                        f"Findings without linked saved records ({len(data['unlinked_findings'])})"
                    ),
                    html.Ul(unsupported),
                ]
            ),
        ]
    )


def register_saved_context_callbacks(app: Dash, service):
    @app.callback(
        Output(PREFIX + "runs", "options"),
        Output(PREFIX + "output", "children"),
        Input("rv-session", "data"),
        Input("rv-page", "value"),
        Input(PREFIX + "runs", "value"),
    )
    def show(state, page, run_ids):
        if page != "compare":
            raise PreventUpdate
        try:
            # Options are drawn from the same snapshot as the projected result.
            data, context, options = capture_saved_context(service, state, run_ids or [])
            return options, render_saved_context(data, context)
        except (ValueError, KeyError, TypeError) as exc:
            # A prior selection can become stale after a case change. Present new
            # options without displaying or carrying over the prior case's data.
            try:
                _, _, options = capture_saved_context(service, state, [])
            except (ValueError, KeyError, TypeError):
                options = []
            return options, html.P(str(exc), className="rv-notice", role="status")
