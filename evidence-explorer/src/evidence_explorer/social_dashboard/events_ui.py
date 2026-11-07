"""Read-only event candidates and exact contributor navigation in Compare."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date

from dash import Dash, Input, Output, html

from ..review.integrations.snapshots import workspace_snapshot
from ..review.navigation import NextAction, navigation_context
from ..review.presentation import record_origin_label
from ..source_contracts import PLATFORM_LABELS
from .contracts import DashboardFilters, DashboardProjection, DashboardRow
from .event_differences import EventDifferences, possible_event_differences
from .events import MATCHING_RULE, EventCluster, EventProjection, cluster_events
from .projection import project_dashboard

PANEL_ID = "event-clusters-panel"
OUTPUT_ID = "event-clusters-output"
EVENT_LIMIT = 12
UNCLUSTERED_LIMIT = 10


def event_clusters_panel() -> html.Details:
    """Mount in the existing Compare tools alongside the saved activity dashboard."""
    return html.Details(
        [
            html.Summary("Cross-platform event clusters"),
            html.Div(id=OUTPUT_ID, **{"aria-live": "polite"}),
        ],
        id=PANEL_ID,
        className="rv-details event-clusters",
    )


def _platform(row: DashboardRow) -> str:
    return PLATFORM_LABELS.get(row.platform, "Unknown platform")


def _records(
    record_ids: Sequence[str], rows: dict[str, DashboardRow], context: str, location: str
) -> html.Ul:
    return html.Ul(
        [
            html.Li(
                [
                    html.Button(
                        row.title,
                        id=NextAction(
                            kind="source",
                            label=row.title,
                            explanation="Inspect this exact saved record.",
                            context=context,
                            record_id=row.id,
                            location=f"event-clusters-{location}-{index}",
                        ).button_id(),
                        n_clicks=0,
                        className="rv-text-button",
                    ),
                    html.Span(
                        f"{_platform(row)} · {record_origin_label(row.synthetic)}",
                        className="rv-small rv-muted",
                    ),
                ],
                className="event-clusters-record",
            )
            for index, row in enumerate(rows[record_id] for record_id in record_ids)
        ],
        className="event-clusters-records",
    )


def _platform_counts(record_ids: Sequence[str], rows: dict[str, DashboardRow]) -> str:
    counts = Counter(_platform(rows[record_id]) for record_id in record_ids)
    return " · ".join(f"{platform} {count}" for platform, count in sorted(counts.items()))


def _point_date(label: str) -> html.Time | html.Span:
    if label == "Date unknown":
        return html.Span(label, className="event-clusters-date")
    day = date.fromisoformat(label)
    return html.Time(f"{day.day} {day:%b %Y}", dateTime=label, className="event-clusters-date")


def _wording_differences(
    differences: EventDifferences, rows: dict[str, DashboardRow], context: str, index: int
) -> html.Details:
    return html.Details(
        [
            html.Summary(f"Possible wording differences ({len(differences.candidates)})"),
            html.P(
                f"Examined {differences.compared_pairs} of {differences.eligible_pairs} eligible "
                "pairs of distinct saved texts."
                if differences.eligible_pairs
                else "No pair of distinct saved texts is available.",
                className="rv-small rv-muted",
            ),
            html.Details(
                [
                    html.Summary(
                        f"Source limits left text unexamined in {differences.partial_pairs} "
                        "of the selected pairs."
                    ),
                    html.Ul(
                        [
                            html.Li(
                                [
                                    html.Strong(rows[record_id].title),
                                    html.Div(
                                        f"{coverage.examined_chars:,} of "
                                        f"{coverage.total_chars:,} characters examined. "
                                        + (
                                            f"{coverage.examined_sentences:,} sentences examined."
                                            if coverage.total_sentences is None
                                            else f"{coverage.examined_sentences:,} of "
                                            f"{coverage.total_sentences:,} sentences examined."
                                        )
                                    ),
                                ]
                            )
                            for record_id, coverage in differences.source_coverage
                            if coverage.truncated
                        ]
                    ),
                ],
                className="rv-small event-clusters-reference",
            )
            if differences.partial_pairs
            else None,
            html.P("No differences flagged in the examined text.", className="rv-small rv-muted")
            if differences.compared_pairs and not differences.candidates
            else None,
            html.P("Additional candidates are outside the display limit.", className="rv-small")
            if differences.candidates_truncated
            else None,
            *[
                html.Section(
                    [
                        html.H4(candidate.label),
                        html.P(candidate.basis, className="rv-small rv-muted"),
                        html.Div(
                            [
                                html.Div(
                                    [
                                        html.Blockquote(passage.quote),
                                        _records(
                                            (passage.record_id,),
                                            rows,
                                            context,
                                            f"{index}-difference-{candidate_index}-{side}",
                                        ),
                                    ],
                                    className="event-clusters-passage",
                                )
                                for side, passage in (
                                    ("left", candidate.left),
                                    ("right", candidate.right),
                                )
                            ],
                            className="event-clusters-passages",
                        ),
                    ],
                    className="event-clusters-difference",
                )
                for candidate_index, candidate in enumerate(differences.candidates)
            ],
        ],
        className="event-clusters-related",
    )


def _event(
    event: EventCluster,
    rows: dict[str, DashboardRow],
    context: str,
    index: int,
    differences: EventDifferences | None,
) -> html.Details:
    peak = max(
        (len(point.record_ids) for point in event.points if point.label != "Date unknown"),
        default=1,
    )
    platforms = _platform_counts(event.record_ids, rows)
    points = [
        html.Details(
            [
                html.Summary(
                    [
                        _point_date(point.label),
                        html.Span(
                            html.Span(style={"width": f"{len(point.record_ids) / peak:.1%}"}),
                            className="event-clusters-bar",
                            **{"aria-hidden": "true"},
                        )
                        if point.label != "Date unknown"
                        else None,
                        html.Span(
                            f"{len(point.record_ids)} saved", className="event-clusters-count"
                        ),
                    ]
                ),
                html.P(_platform_counts(point.record_ids, rows), className="rv-small rv-muted"),
                _records(point.record_ids, rows, context, f"{index}-point-{point_index}"),
            ],
            className="event-clusters-point",
            **{"data-undated": str(point.label == "Date unknown").lower()},
        )
        for point_index, point in enumerate(event.points)
    ]
    repeats = [
        html.Details(
            [
                html.Summary(f"{group.label} · {len(group.record_ids)} saved"),
                html.P(group.basis, className="rv-small rv-muted"),
                html.P("Source first, repost second, as recorded.", className="rv-small rv-muted")
                if group.kind == "documented_repost"
                else None,
                _records(group.record_ids, rows, context, f"{index}-repeat-{group_index}"),
            ],
            className="event-clusters-related",
        )
        for group_index, group in enumerate(event.repeat_groups)
    ]
    claims = [
        html.Details(
            [
                html.Summary(claim.statement),
                html.P("No support links saved within this event.", className="rv-small rv-muted")
                if not claim.supports
                else None,
                *(
                    html.Div(
                        [
                            html.P(label, className="event-clusters-role"),
                            _records(ids, rows, context, f"{index}-claim-{claim_index}-{role}"),
                        ]
                    )
                    for role, label, ids in (
                        ("supports", "Saved links marked as support", claim.supports),
                        ("contradicts", "Saved links marked as contradiction", claim.contradicts),
                    )
                    if ids
                ),
                html.Details(
                    [
                        html.Summary("Claim reference"),
                        html.P(["Saved finding: ", html.Code(claim.finding_id)])
                        if claim.finding_id
                        else None,
                        html.P(["Saved run: ", html.Code(claim.run_id)]) if claim.run_id else None,
                    ],
                    className="rv-small event-clusters-reference",
                )
                if claim.finding_id or claim.run_id
                else None,
            ],
            className="event-clusters-related",
        )
        for claim_index, claim in enumerate(event.claims)
    ]
    return html.Details(
        [
            html.Summary(
                [
                    html.Strong(event.title),
                    html.Span(
                        f"{len(event.record_ids)} saved · {platforms}",
                        className="rv-small rv-muted event-clusters-event-counts",
                    ),
                ]
            ),
            html.Div(
                [
                    html.P(
                        "Shared wording: " + ", ".join(event.matching_terms),
                        className="rv-small rv-muted",
                    )
                    if event.matching_terms
                    else None,
                    html.H3("Saved publication timeline"),
                    html.P(
                        "Open a point to see every saved record it counts.", className="rv-small"
                    ),
                    html.Div(points, className="event-clusters-timeline"),
                    html.H3("Reposts and repeated text"),
                    *(
                        repeats
                        or [html.P("No saved repost or repeated-text groups in this event.")]
                    ),
                    html.H3("Saved claims and conflicting evidence"),
                    *(
                        claims
                        or [html.P("No support or contradiction links were saved for this event.")]
                    ),
                    _wording_differences(differences, rows, context, index)
                    if differences is not None
                    else None,
                ],
                className="event-clusters-event-body",
            ),
        ],
        className="event-clusters-event",
        open=index == 0,
    )


def render_event_clusters(
    projection: EventProjection,
    dashboard: DashboardProjection,
    context: str,
    differences: dict[str, EventDifferences] | None = None,
) -> html.Div:
    """Render bounded event cards and every contributor for each visible timeline point."""
    rows = {row.id: row for row in dashboard.rows}
    events = projection.events[:EVENT_LIMIT]
    unclustered = projection.unclustered_record_ids
    unclustered_shown = min(len(unclustered), UNCLUSTERED_LIMIT)
    differences = differences or {}
    return html.Div(
        [
            html.P(
                "Suggested events group related wording across social platforms.",
                className="rv-small rv-muted",
            ),
            html.P(
                (
                    f"{len(events)} suggested {'event' if len(events) == 1 else 'events'} · "
                    if len(events) == len(projection.events)
                    else f"{len(events)} of {len(projection.events)} suggested events shown · "
                )
                + f"{len(rows)} saved records considered · "
                f"{dashboard.coverage.excluded_records} excluded by this assessment",
                className="event-clusters-coverage",
            ),
            html.P(
                "The timeline shows UTC publication days with saved records. Records without "
                "a usable publication date appear separately.",
                className="rv-small rv-muted",
            ),
            html.Details(
                [
                    html.Summary("How suggestions are grouped"),
                    html.P(MATCHING_RULE, className="rv-small rv-muted"),
                    html.P(
                        "Review the original records before drawing conclusions about origin, "
                        "corroboration, or coordination.",
                        className="rv-small rv-muted",
                    ),
                ],
                className="event-clusters-related",
            ),
            *[
                _event(event, rows, context, index, differences.get(event.id))
                for index, event in enumerate(events)
            ],
            html.P("No cross-platform event candidates found in this assessment.", role="status")
            if not events
            else None,
            html.Details(
                [
                    html.Summary(f"Unclustered saved records ({len(unclustered)})"),
                    html.P(
                        f"Showing {unclustered_shown} of {len(unclustered)}. "
                        "Use Browse all matching records in the social dashboard to see more.",
                        className="rv-small rv-muted",
                    ),
                    _records(unclustered[:UNCLUSTERED_LIMIT], rows, context, "unclustered"),
                ],
                className="event-clusters-related",
            )
            if unclustered
            else None,
        ],
        className="event-clusters-content",
    )


def register_event_callbacks(app: Dash, service) -> None:
    """Capture and render a consistent case snapshot only while Compare is open."""

    @app.callback(
        Output(OUTPUT_ID, "children"),
        Input("rv-page", "value"),
        Input("rv-session", "data"),
    )
    def render(page, state):
        if page != "compare":
            return None
        if not isinstance(state, dict) or not state.get("case_id"):
            return html.P("Choose a saved case to look for cross-platform events.", role="status")
        try:
            with workspace_snapshot(service) as snapshot:
                view = snapshot.view(state["case_id"], state.get("scenario_id"))
                context = navigation_context(view, state)
                bundle = snapshot.export_bundle(state["case_id"])
                excluded = (view.get("impact") or {}).get("excluded_record_ids", [])
            dashboard = project_dashboard(
                bundle, excluded_record_ids=excluded, filters=DashboardFilters()
            )
            projection = cluster_events(bundle, dashboard)
            differences = {
                event.id: possible_event_differences(bundle, event.record_ids)
                for event in projection.events[:EVENT_LIMIT]
            }
            return render_event_clusters(projection, dashboard, context, differences)
        except (KeyError, TypeError, ValueError):
            return html.P(
                "Event clusters are unavailable for this assessment. Choose a saved case or "
                "reopen this section after the assessment finishes loading.",
                className="rv-notice",
                role="status",
            )
