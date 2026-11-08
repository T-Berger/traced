"""Read-only rendering for the saved-assessment category projection."""

from __future__ import annotations

from dash import html

from ..review.navigation import NextAction
from ..review.presentation import record_origin_label


def _swarm_context(finding):
    evidence = [
        html.Li(
            [
                html.Strong(link["relation"].title() + ": "),
                link["basis"],
                html.Ul(
                    [
                        html.Li(fragment["quote"])
                        for fragment in link["fragments"]
                        if fragment.get("quote")
                    ]
                ),
            ]
        )
        for link in finding["evidence_links"]
    ]
    availability = (
        "Producer reports complete inputs"
        if finding["producer_reports_full_inputs"] is True
        else "Complete inputs not established"
    )
    return html.Li(
        [
            html.P(finding["statement"]),
            html.P(
                f"Run: {finding['run_id']} · Method: {finding['method']} "
                f"· Input: {finding['input_fingerprint']} · {availability}",
                className="rv-small rv-muted",
            ),
            html.Ul(evidence) if evidence else html.P("No linked evidence was supplied."),
        ]
    )


def render_category_review(projection: dict, context: str, *, row_limit: int = 50):
    """Render observed records and links through the shared evidence inspector.

    This function does not register callbacks, create findings, or save review
    decisions. The caller supplies a context captured with the projection.
    """
    months = []
    for month in projection["months"]:
        details = []
        for category, states in month["categories"].items():
            nonzero = [
                f"{state}: {summary['observed_count']}"
                for state, summary in states.items()
                if summary["observed_count"]
            ]
            if nonzero:
                details.append(html.Li(f"{category.replace('_', ' ')} · " + " · ".join(nonzero)))
        months.append(
            html.Details(
                [
                    html.Summary(f"{month['month']} · sample coverage unknown"),
                    html.Ul(details),
                ]
            )
        )

    rows = []
    for index, row in enumerate(projection["rows"][:row_limit]):
        source_button = html.Button(
            "Inspect saved record",
            id=NextAction(
                kind="source",
                label="Inspect saved record",
                explanation="Open this exact saved record.",
                context=context,
                finding_id=row["finding_id"],
                record_id=row["record_id"],
                location=f"compliance-matrix-source-{index}",
            ).button_id(),
            n_clicks=0,
            className="rv-button rv-button--outline",
        )
        finding_button = html.Button(
            "Review this finding",
            id=NextAction(
                kind="review",
                label="Review this finding",
                explanation="Review the model proposal and its exact evidence.",
                context=context,
                finding_id=row["finding_id"],
                record_id=row["record_id"],
                location=f"compliance-matrix-review-{index}",
            ).button_id(),
            n_clicks=0,
            className="rv-button rv-button--outline",
        )
        status = row["disposition"] or "unreviewed"
        decision = row.get("decision") or {}
        observations = row.get("swarm_context", [])
        rows.append(
            html.Details(
                [
                    html.Summary(
                        f"{row['source_label']} · {row['category'].replace('_', ' ')} · {status}"
                        + (" · excluded" if row["excluded"] else "")
                    ),
                    html.P(
                        f"Saved record: {row['record_id']} · publication: "
                        f"{row['published_at'] or 'unknown'} · run: {row['run_id']} · "
                        f"{record_origin_label(row.get('record_synthetic'))}",
                        className="rv-small rv-muted",
                    ),
                    html.P(f"Model proposal: {row['model']} · target: {row['target']}"),
                    html.Ul([html.Li(fragment["quote"]) for fragment in row["fragments"]]),
                    html.P(f"Decision rationale: {decision['reason']}")
                    if decision.get("reason")
                    else html.P("No current decision for this finding and assessment."),
                    html.Details(
                        [
                            html.Summary(f"Linked Swarm observations ({len(observations)})"),
                            html.P(
                                "Separate saved behavioral findings linked through this exact "
                                "record. Category decisions do not carry over. Coordination "
                                "alone does not establish automation, intent or a violation.",
                                className="rv-small rv-muted",
                            ),
                            html.Ul([_swarm_context(item) for item in observations])
                            if observations
                            else html.P("No selected Swarm observation links to this record."),
                        ]
                    )
                    if projection.get("selected_swarm_run_ids")
                    else None,
                    html.Div([source_button, finding_button], className="rv-toolbar"),
                ]
            )
        )
    invalid = [
        html.Li(f"{item['finding_id']} · {item['reason']}")
        for item in projection["invalid_findings"][:row_limit]
    ]
    return html.Section(
        [
            html.H3("Saved category findings over time"),
            html.P(
                f"Target: {projection['target']} · record kinds: "
                f"{', '.join(projection['record_kinds'])} · {projection['time_basis']}"
            ),
            html.P(
                "Counts describe only observed saved record IDs in this assessment. "
                "Coverage is unknown; absent months do not mean zero activity. The same "
                "record can appear in multiple categories or review states.",
                className="rv-notice",
            ),
            *months,
            html.P("No dated category findings in the selected scope.") if not months else None,
            html.H4("Evidence matrix"),
            html.P(f"Showing {len(rows)} of {len(projection['rows'])} matching findings."),
            *rows,
            html.P(
                "Undated record IDs: " + (", ".join(projection["undated_record_ids"]) or "none")
            ),
            html.Details(
                [
                    html.Summary(
                        f"Unavailable category findings ({len(projection['invalid_findings'])})"
                    ),
                    html.Ul(invalid),
                ]
            ),
            html.Details(
                [
                    html.Summary(
                        "Selected Swarm findings without a linked saved record "
                        f"({len(projection.get('unlinked_swarm_findings', []))})"
                    ),
                    html.Ul(
                        [
                            html.Li(f"{item['statement']} · run {item['run_id']}")
                            for item in projection.get("unlinked_swarm_findings", [])[:row_limit]
                        ]
                    ),
                ]
            )
            if projection.get("selected_swarm_run_ids")
            else None,
            html.P(
                "Review decisions apply to exact findings, not a legal violation or proof of "
                "platform enforcement.",
                className="rv-small rv-muted",
            ),
        ],
        className="rv-case-card",
    )
