"""Saved-case category candidates for Alerts, independent of external analytics.

The panel projects existing finding snapshots and current review decisions. It
never classifies records, records decisions, or infers sample completeness.
"""

from __future__ import annotations

from dash import Dash, Input, Output, dcc, html
from dash.exceptions import PreventUpdate

from ..review.navigation import NextAction
from ..review.workflow import assessment_context
from .category_ui import capture_category_view
from .review_projection import project_category_review

PREFIX = "compliance-local-alerts-"
ROW_LIMIT = 50


def local_alerts_panel():
    return html.Details(
        [
            html.Summary("Saved case category candidates"),
            html.P(
                "Inspect existing category proposals and review decisions in the selected "
                "case and assessment. This view does not need the separate analytics database "
                "and does not classify the saved records.",
                className="rv-small rv-muted",
            ),
            html.Label("Analysis target", htmlFor=PREFIX + "target"),
            dcc.Dropdown(id=PREFIX + "target", options=[], placeholder="Select a saved target"),
            html.Label("Saved record kinds", htmlFor=PREFIX + "kinds"),
            dcc.Dropdown(
                id=PREFIX + "kinds",
                options=[],
                value=[],
                multi=True,
                placeholder="Select saved record kinds",
            ),
            html.Div(id=PREFIX + "output", role="status", **{"aria-live": "polite"}),
        ],
        className="rv-details",
    )


def scope_options(view, bundle):
    """List explicit values from this assessment without selecting a scope."""
    run_id = assessment_context(view).get("base_run_id")
    targets = sorted(
        {
            finding.details["target"]
            for finding in bundle.findings
            if finding.run_id == run_id
            and finding.details.get("domain") == "compliance_category"
            and isinstance(finding.details.get("target"), str)
            and finding.details["target"].strip()
        }
    )
    excluded = set((view.get("impact") or {}).get("excluded_record_ids", []))
    kinds = sorted(
        {record.kind for record in bundle.records if record.text and record.id not in excluded}
    )
    return (
        [{"label": item, "value": item} for item in targets],
        [{"label": item, "value": item} for item in kinds],
    )


def render_local_alerts(view, bundle, context, *, target, kinds):
    """Render exact findings from the selected saved assessment only."""
    projection = project_category_review(view, bundle, record_kinds=kinds, target=target)
    excluded_ids = set(view["impact"]["excluded_record_ids"])
    scoped = {
        record.id
        for record in bundle.records
        if record.text is not None and record.kind in kinds and record.id not in excluded_ids
    }
    included = [row for row in projection["rows"] if not row["excluded"]]
    linked = {row["record_id"] for row in included}
    unknown_count = len(scoped - linked)
    omitted = len(projection["rows"]) - len(included)
    entries = []
    for index, row in enumerate(included[:ROW_LIMIT]):
        source = NextAction(
            kind="source",
            label="Inspect saved record",
            explanation="Open exact evidence.",
            context=context,
            finding_id=row["finding_id"],
            record_id=row["record_id"],
            location=f"local-alert-source-{index}",
        )
        review = NextAction(
            kind="review",
            label="Review finding",
            explanation="Open the saved proposal.",
            context=context,
            finding_id=row["finding_id"],
            record_id=row["record_id"],
            location=f"local-alert-review-{index}",
        )
        decision = row.get("decision") or {}
        entries.append(
            html.Li(
                [
                    html.Strong(
                        f"{row['category'].replace('_', ' ')} · "
                        f"{row['disposition'] or 'unreviewed'}"
                    ),
                    html.P(
                        f"{row['source_label']} · {row['record_id']} · "
                        f"published {row['published_at'] or 'unknown'}"
                    ),
                    html.P(
                        f"Model: {row['model']} · method: {row['method']} · run: {row['run_id']}",
                        className="rv-small rv-muted",
                    ),
                    html.P(
                        "Record origin: "
                        + (
                            "synthetic"
                            if row.get("record_synthetic") is True
                            else "not marked synthetic"
                            if row.get("record_synthetic") is False
                            else "not specified"
                        ),
                        className="rv-small rv-muted",
                    ),
                    html.Ul([html.Li(fragment["quote"]) for fragment in row["fragments"]]),
                    html.P(f"Decision rationale: {decision['reason']}")
                    if decision.get("reason")
                    else None,
                    html.Div(
                        [
                            html.Button(
                                source.label,
                                id=source.button_id(),
                                n_clicks=0,
                                className="rv-button rv-button--outline",
                            ),
                            html.Button(
                                review.label,
                                id=review.button_id(),
                                n_clicks=0,
                                className="rv-button rv-button--outline",
                            ),
                        ],
                        className="rv-toolbar",
                    ),
                ]
            )
        )
    return html.Div(
        [
            html.P(
                f"{len(included)} saved finding(s) across {len(linked)} records "
                "in the selected scope."
            ),
            html.P(
                f"{unknown_count} included saved text record(s) have no source-linked category "
                "finding for this target in the selected assessment. "
                "Their classification is unknown; "
                "this includes records with no candidate or not analyzed.",
                className="rv-notice",
            ),
            html.P(
                "Sample coverage is unknown. A model proposal or a review decision does not "
                "establish a violation or platform action.",
                className="rv-small rv-muted",
            ),
            html.P(
                f"Showing {len(entries)} of {len(included)} findings; "
                f"{omitted} excluded finding(s) "
                "remain available in the assessment."
            ),
            html.Ul(entries)
            if entries
            else html.P("No source-linked category findings in this scope."),
            html.Details(
                [
                    html.Summary(f"Unavailable findings ({len(projection['invalid_findings'])})"),
                    html.Ul(
                        [
                            html.Li(f"{item['finding_id']} · {item['reason']}")
                            for item in projection["invalid_findings"][:ROW_LIMIT]
                        ]
                    ),
                ]
            ),
        ]
    )


def register_local_alerts_callbacks(app: Dash, service):
    @app.callback(
        Output(PREFIX + "target", "options"),
        Output(PREFIX + "target", "value"),
        Output(PREFIX + "kinds", "options"),
        Output(PREFIX + "kinds", "value"),
        Input("rv-session", "data"),
        Input("rv-page", "value"),
    )
    def options(state, page):
        if page != "alerts":
            raise PreventUpdate
        try:
            view, bundle, _ = capture_category_view(service, state)
            targets, kinds = scope_options(view, bundle)
            return targets, None, kinds, []
        except (ValueError, KeyError, TypeError):
            return [], None, [], []

    @app.callback(
        Output(PREFIX + "output", "children"),
        Input("rv-session", "data"),
        Input("rv-page", "value"),
        Input(PREFIX + "target", "value"),
        Input(PREFIX + "kinds", "value"),
    )
    def results(state, page, target, kinds):
        if page != "alerts":
            raise PreventUpdate
        try:
            view, bundle, context = capture_category_view(service, state)
            targets, available_kinds = scope_options(view, bundle)
            if not targets:
                return html.P(
                    "No saved category proposals in this assessment. The classification "
                    "of its saved records is unknown.",
                    className="rv-notice",
                )
            if not target or not kinds:
                return html.P("Choose a saved target and at least one record kind.")
            if target not in {option["value"] for option in targets} or not set(kinds) <= {
                option["value"] for option in available_kinds
            }:
                raise ValueError("The selected scope changed; choose it again.")
            return render_local_alerts(view, bundle, context, target=target, kinds=kinds)
        except (ValueError, KeyError, TypeError) as exc:
            return html.P(str(exc), className="rv-notice")
