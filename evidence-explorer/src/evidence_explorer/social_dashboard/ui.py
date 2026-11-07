"""Additive, snapshot-bound views of saved social records and local annotations.

This module owns no page/session writer. Exact source links use the shared
NextAction ingress. Rendering performs no network or model work.
"""

from __future__ import annotations

import hashlib
import json
import math
from html import escape

import plotly.graph_objects as go
from dash import Dash, Input, Output, ctx, dcc, html
from dash.exceptions import PreventUpdate
from plotly.subplots import make_subplots

from ..review.integrations.snapshots import workspace_snapshot
from ..review.navigation import NextAction, navigation_context
from ..review.presentation import record_origin_label
from .contracts import DashboardFilters
from .projection import project_dashboard

PREFIX = "social-dashboard-"
TABLE_LIMIT = 50
GRAPH_LIMIT = 80
COLORS = {
    "positive": "#276B63",
    "negative": "#142229",
    "neutral": "#52636C",
    "mixed": "#754B05",
    "unclear": "#254F64",
    "unknown": "#D9E0DE",
}
FILTER_NAMES = (
    "text_query",
    "platform",
    "channel_key",
    "author_key",
    "time_basis",
    "bucket",
    "start",
    "end",
    "sentiment_run_id",
)


def _id(name):
    return PREFIX + name


def _field(label, name, control):
    return html.Div([html.Label(label, htmlFor=_id(name)), control], className="sd-field")


def _dropdown(name, options=(), value=None, *, clearable=True, placeholder=None):
    return dcc.Dropdown(
        id=_id(name),
        options=list(options),
        value=value,
        clearable=clearable,
        placeholder=placeholder,
        searchable=True,
    )


def dashboard_panel() -> html.Details:
    """Mount explicitly in Compare; callbacks are registered separately."""
    return html.Details(
        [
            html.Summary("Topics, public accounts and saved activity"),
            html.Div(
                [
                    html.H2("Explore saved social records", id=_id("heading")),
                    html.P(
                        "Count saved record snapshots by topic wording, platform, public channel "
                        "or account. These counts are not audience reach, public opinion or "
                        "independent sources. Account labels do not establish a person's identity.",
                        className="rv-small rv-muted",
                    ),
                    _field(
                        "Explore",
                        "lens",
                        _dropdown(
                            "lens",
                            [
                                {
                                    "label": "Topic / narrative wording",
                                    "value": "topics",
                                },
                                {
                                    "label": "Public channels / accounts",
                                    "value": "accounts",
                                },
                            ],
                            "topics",
                            clearable=False,
                        ),
                    ),
                    _field(
                        "Account view grouped by",
                        "group_by",
                        _dropdown(
                            "group_by",
                            [
                                {"label": "Public channel", "value": "channel"},
                                {
                                    "label": "Public account / author",
                                    "value": "account",
                                },
                            ],
                            "channel",
                            clearable=False,
                        ),
                    ),
                    html.Div(
                        [
                            _field(
                                "Topic / narrative text · literal match",
                                "text_query",
                                dcc.Input(
                                    id=_id("text_query"),
                                    type="text",
                                    value="",
                                    debounce=True,
                                    maxLength=500,
                                    placeholder="Search saved wording",
                                ),
                            ),
                            _field(
                                "Platform",
                                "platform",
                                _dropdown("platform", placeholder="All platforms"),
                            ),
                            _field(
                                "Public channel",
                                "channel_key",
                                _dropdown("channel_key", placeholder="All channels"),
                            ),
                            _field(
                                "Public account / author",
                                "author_key",
                                _dropdown("author_key", placeholder="All saved accounts"),
                            ),
                            _field(
                                "Timestamp basis",
                                "time_basis",
                                _dropdown(
                                    "time_basis",
                                    [
                                        {
                                            "label": "Publication time",
                                            "value": "publication",
                                        },
                                        {
                                            "label": "Acquisition time",
                                            "value": "acquisition",
                                        },
                                    ],
                                    "publication",
                                    clearable=False,
                                ),
                            ),
                            _field(
                                "Time bucket · UTC",
                                "bucket",
                                _dropdown(
                                    "bucket",
                                    [
                                        {"label": label, "value": value}
                                        for label, value in (
                                            ("Day", "day"),
                                            ("Week", "week"),
                                            ("Month", "month"),
                                        )
                                    ],
                                    "day",
                                    clearable=False,
                                ),
                            ),
                            _field(
                                "From date · inclusive",
                                "start",
                                dcc.Input(
                                    id=_id("start"),
                                    type="text",
                                    placeholder="YYYY-MM-DD",
                                    debounce=True,
                                ),
                            ),
                            _field(
                                "Through date · inclusive",
                                "end",
                                dcc.Input(
                                    id=_id("end"),
                                    type="text",
                                    placeholder="YYYY-MM-DD",
                                    debounce=True,
                                ),
                            ),
                            _field(
                                "Saved sentiment annotation run",
                                "sentiment_run_id",
                                _dropdown(
                                    "sentiment_run_id",
                                    placeholder="No run selected · sentiment unknown",
                                ),
                            ),
                        ],
                        className="sd-filters",
                    ),
                    html.P(
                        "Literal text matching does not discover or classify narratives. Unknown "
                        "times stay separate; acquisition time is not publication time. Select "
                        "one saved sentiment run to view its labels; runs are never combined.",
                        className="rv-small rv-muted",
                    ),
                    dcc.Store(id=_id("capture")),
                    html.Div(id=_id("output"), **{"aria-live": "polite"}),
                    html.Button(
                        "Browse all matching records",
                        id=_id("browse"),
                        n_clicks=0,
                        className="rv-button rv-button--outline",
                    ),
                    html.Div(id=_id("drilldown"), **{"aria-live": "polite"}),
                    dcc.Store(id=_id("selection")),
                    _field(
                        "Inspect any contributing record",
                        "selected-record",
                        _dropdown(
                            "selected-record",
                            placeholder="Select a chart point or browse matching records",
                        ),
                    ),
                    html.Div(id=_id("selected-open"), **{"aria-live": "polite"}),
                ],
                className="sd-content",
            ),
        ],
        id=_id("panel"),
        className="social-dashboard rv-details",
        **{"aria-labelledby": _id("heading")},
    )


def capture_dashboard(service, state, filters=None):
    """Read the selected assessment and its binding in one SQLite snapshot."""
    if not isinstance(state, dict) or not state.get("case_id"):
        raise ValueError("Choose a saved case to explore its records.")
    with workspace_snapshot(service) as snapshot:
        view = snapshot.view(state["case_id"], state.get("scenario_id"))
        context = navigation_context(view, state)
        bundle = snapshot.export_bundle(state["case_id"]).model_dump(mode="json")
        projection = project_dashboard(
            bundle,
            excluded_record_ids=(view.get("impact") or {}).get("excluded_record_ids", []),
            filters=filters,
        ).model_dump(mode="json")
        selected_run = projection["filters"]["sentiment_run_id"]
        projection["annotation_metadata"] = {
            "run": next((run for run in bundle["runs"] if run["id"] == selected_run), None),
            "annotations": [
                finding["details"]["annotation"]
                for finding in bundle["findings"]
                if finding["run_id"] == selected_run
                and finding.get("details", {}).get("domain") == "social_sentiment"
                and isinstance(finding["details"].get("annotation"), dict)
            ],
        }
    return projection, view, context


def _presence_groups(data, group_by):
    """Bound chart traces without losing records in the remaining-groups trace."""
    summaries = data["channels" if group_by == "channel" else "authors"]
    ordered = sorted(summaries, key=lambda group: (-group["count"], group["label"]))
    groups = [
        {"key": g["key"], "label": g["label"], "ids": set(g["record_ids"])} for g in ordered[:8]
    ]
    if len(ordered) > 8:
        groups.append(
            {
                "key": "__remaining__",
                "label": "Remaining groups",
                "ids": {rid for group in ordered[8:] for rid in group["record_ids"]},
            }
        )
    return groups


def activity_figure(data, scope="", lens="topics", group_by="channel"):
    """Both rows count saved records; unknown labels are never painted neutral."""
    buckets = data["time_buckets"]
    labels = [bucket["label"] for bucket in buckets]
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.14,
        subplot_titles=("Saved activity", "Sentiment in one selected run"),
    )

    def points(sentiment, group=None):
        return [
            {
                "scope": scope,
                "kind": "bucket",
                "key": b["start"],
                "sentiment": sentiment,
                **({"group_by": group_by, "group": group["key"]} if group else {}),
            }
            for b in buckets
        ]

    if lens == "accounts":
        palette = ("#276B63", "#52636C", "#142229", "#F36643")
        for index, group in enumerate(_presence_groups(data, group_by)):
            figure.add_trace(
                go.Bar(
                    x=labels,
                    y=[len(set(b["record_ids"]) & group["ids"]) for b in buckets],
                    customdata=points("all", group),
                    name=group["label"],
                    showlegend=False,
                    text=[escape(group["label"]).replace(" · ", "<br>")] * len(labels),
                    textposition="none",
                    hovertemplate="%{text}<br>%{x}: %{y} saved records<extra></extra>",
                    marker_color=palette[index % len(palette)],
                    marker_pattern_shape="/" if index >= len(palette) else "",
                ),
                row=1,
                col=1,
            )
    else:
        figure.add_trace(
            go.Bar(
                x=labels,
                y=[bucket["count"] for bucket in buckets],
                customdata=points("all"),
                name="Saved records",
                marker_color="#52636C",
            ),
            row=1,
            col=1,
        )
    if data.get("sentiment_comparable", True):
        for label, color in COLORS.items():
            figure.add_trace(
                go.Bar(
                    x=labels,
                    y=[bucket["sentiment_counts"][label] for bucket in buckets],
                    customdata=points(label),
                    name=label.title(),
                    marker_color=color,
                ),
                row=2,
                col=1,
            )
    else:
        figure.add_annotation(
            text="Different sentiment targets: use the target table or narrower filters",
            x=0.5,
            y=0.18,
            xref="paper",
            yref="paper",
            showarrow=False,
        )
    figure.update_layout(
        barmode="stack",
        height=440,
        margin={"l": 48, "r": 16, "t": 55, "b": 55},
        paper_bgcolor="#FFFFFF",
        plot_bgcolor="#FFFFFF",
        font={"family": "Manrope, sans-serif", "color": "#142229", "size": 12},
        legend={"orientation": "h", "y": -0.15},
        hovermode="closest",
    )
    figure.update_yaxes(title_text="Records", rangemode="tozero", dtick=1)
    if not buckets:
        figure.add_annotation(
            text="No matching records with a usable timestamp",
            showarrow=False,
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
        )
    return figure


def relationship_figure(data, scope=""):
    """A bounded record graph, never an inferred account or coordination graph."""
    rows = data["rows"][:GRAPH_LIMIT]
    positions = {
        row["id"]: (
            math.cos(2 * math.pi * i / max(len(rows), 1)),
            math.sin(2 * math.pi * i / max(len(rows), 1)),
        )
        for i, row in enumerate(rows)
    }
    edges = [
        edge
        for edge in data["edges"]
        if edge["source_record_id"] in positions and edge["target_record_id"] in positions
    ]
    figure = go.Figure()
    for relation in sorted({edge["relation"] for edge in edges}):
        x, y = [], []
        for edge in edges:
            if edge["relation"] == relation:
                a, b = (
                    positions[edge["source_record_id"]],
                    positions[edge["target_record_id"]],
                )
                x.extend([a[0], b[0], None])
                y.extend([a[1], b[1], None])
        figure.add_trace(
            go.Scatter(
                x=x,
                y=y,
                mode="lines",
                name=relation.replace("_", " "),
                line={"color": "#52636C", "width": 1},
                hoverinfo="skip",
            )
        )
    if edges:
        figure.add_trace(
            go.Scatter(
                x=[
                    (positions[e["source_record_id"]][0] + positions[e["target_record_id"]][0]) / 2
                    for e in edges
                ],
                y=[
                    (positions[e["source_record_id"]][1] + positions[e["target_record_id"]][1]) / 2
                    for e in edges
                ],
                mode="markers",
                name="Inspect relationship",
                marker={"size": 8, "color": "#52636C", "symbol": "diamond"},
                text=[e["relation"].replace("_", " ") for e in edges],
                customdata=[{"scope": scope, "kind": "edge", "key": e["id"]} for e in edges],
                hovertemplate="%{text}<extra>Select to inspect both records</extra>",
            )
        )
    figure.add_trace(
        go.Scatter(
            x=[positions[row["id"]][0] for row in rows],
            y=[positions[row["id"]][1] for row in rows],
            mode="markers",
            name="Saved record",
            text=[row["title"] for row in rows],
            customdata=[{"scope": scope, "kind": "record", "key": row["id"]} for row in rows],
            hovertemplate="%{text}<extra>Saved record</extra>",
            marker={
                "size": 11,
                "color": "#276B63",
                "line": {"width": 1, "color": "#142229"},
            },
        )
    )
    figure.update_layout(
        height=330,
        margin={"l": 10, "r": 10, "t": 12, "b": 35},
        paper_bgcolor="#FFFFFF",
        plot_bgcolor="#FFFFFF",
        font={"family": "Manrope, sans-serif", "color": "#142229"},
        legend={"orientation": "h"},
        xaxis={"visible": False},
        yaxis={"visible": False, "scaleanchor": "x"},
    )
    return figure, rows, edges


def _table(headers, rows, caption):
    return html.Div(
        html.Table(
            [
                html.Caption(caption),
                html.Thead(html.Tr([html.Th(label, scope="col") for label in headers])),
                html.Tbody(
                    [
                        html.Tr(
                            [
                                html.Td(value, **{"data-label": label})
                                for label, value in zip(headers, row, strict=True)
                            ]
                        )
                        for row in rows
                    ]
                ),
            ],
            className="sd-table",
        ),
        className="sd-table-wrap",
    )


def _source_button(row, context, index):
    return html.Button(
        row["title"],
        id=NextAction(
            kind="source",
            label=row["title"],
            explanation="Inspect this counted saved record.",
            context=context,
            record_id=row["id"],
            location=f"dashboard-record-{index}",
        ).button_id(),
        n_clicks=0,
        className="rv-text-button",
        title=f"Inspect saved record {row['id']}",
    )


def _annotation_details(data):
    metadata = data.get("annotation_metadata") or {}
    run = metadata.get("run")
    if not run:
        return html.P(
            "No saved sentiment run selected. No sentiment model is run by this view.",
            className="rv-small rv-muted",
        )
    facts = []
    for key, title in (
        ("sentiment_model", "Model"),
        ("sentiment_prompt_version", "Prompt / method version"),
        ("sentiment_target", "Target"),
    ):
        values = sorted({str(row[key]) for row in data["rows"] if row.get(key) is not None})
        facts.append(html.Li(f"{title}: {', '.join(values) if values else 'Not recorded'}"))
    return html.Details(
        [
            html.Summary("Saved sentiment attribution and denominator"),
            html.P(
                f"Run: {run['id']} · Method: {run['method']} · "
                f"Saved: {run.get('created_at') or 'Unknown'}"
            ),
            html.Ul(facts),
            html.P(
                f"Denominator: {data['coverage']['matching_records']} matching saved records; "
                f"{data['coverage']['unknown_sentiment_records']} have unknown sentiment. "
                "Only timestamped records enter the time chart. Labels describe saved wording "
                "toward their declared targets; they do not measure population opinion or truth."
            ),
        ],
        className="rv-details",
    )


def render_dashboard(data, context, scope="", lens="topics", group_by="channel"):
    coverage = data["coverage"]
    graph, graph_rows, graph_edges = relationship_figure(data, scope)
    row_by_id = {row["id"]: row for row in data["rows"]}
    summaries = []
    groups = (
        ("platforms", "Platform"),
        ("channels", "Public channel"),
        ("authors", "Public account / author"),
    )
    if lens == "accounts":
        groups = (groups[1], groups[2], groups[0])
    for key, title in groups:
        values = data[key]
        summaries.append(
            _table(
                [title, "Saved records"],
                [[v["label"], v["count"]] for v in values[:TABLE_LIMIT]],
                f"{title}: showing {min(len(values), TABLE_LIMIT)} of {len(values)} groups",
            )
        )
    records = [
        [
            _source_button(row, context, i),
            row["platform"],
            row["channel_label"],
            row["author_label"],
            row["published_at"]
            if data["filters"]["time_basis"] == "publication"
            else row["acquired_at"],
            row["sentiment"].title(),
            row["sentiment_target"] or "Not specified",
            record_origin_label(row["synthetic"]),
        ]
        for i, row in enumerate(data["rows"][:TABLE_LIMIT])
    ]
    return html.Div(
        [
            html.H3(
                "Topic / narrative wording presence"
                if lens == "topics"
                else "Public account and channel presence"
            ),
            html.P(
                f"{coverage['matching_records']} matching saved records · "
                f"{coverage['total_records']} "
                f"in this case · {coverage['excluded_records']} excluded in this assessment.",
                className="sd-count",
            ),
            html.P(
                f"Unknown timestamp: {coverage['unknown_timestamp_records']} · Unknown sentiment: "
                f"{coverage['unknown_sentiment_records']} · "
                f"Unknown channel: {coverage['unknown_channel_records']} "
                f"· Unknown account: {coverage['unknown_author_records']}.",
                className="rv-small rv-muted",
            ),
            html.P(
                "Records with unknown, date-only, naive or invalid timestamps remain listed even "
                "when date filters are applied, and are excluded from the time chart. "
                "Unknown sentiment is not neutral sentiment.",
                className="rv-small rv-muted",
            ),
            html.P(
                f"Timestamp gaps: {coverage.get('date_only_timestamp_records', 0)} date-only, "
                f"{coverage.get('missing_timestamp_records', 0)} missing, "
                f"{coverage.get('invalid_timestamp_records', 0)} invalid and "
                f"{coverage.get('naive_timestamp_records', 0)} without a timezone. "
                "Invalid/conflicting annotation records: "
                f"{coverage['invalid_annotation_records']}.",
                className="rv-small rv-muted",
            ),
            html.P(
                "Sentiment is unknown until a valid saved annotation run is selected."
                if not data["filters"]["sentiment_run_id"]
                else (
                    "Sentiment labels come only from the selected saved run "
                    "and need analyst review."
                ),
                className="rv-notice",
            ),
            _annotation_details(data),
            _table(
                [
                    "Sentiment target",
                    "Assessed records",
                    *[label.title() for label in COLORS],
                ],
                [
                    [
                        target["target"],
                        target["count"],
                        *[target["sentiment_counts"][label] for label in COLORS],
                    ]
                    for target in data.get("sentiment_targets", [])
                ],
                "Saved sentiment stays separated by its declared target.",
            )
            if not data.get("sentiment_comparable", True)
            else None,
            html.P(
                "Channel / account stacks count the same saved records by their platform-scoped "
                "labels. The largest eight groups and any remaining groups are shown."
                if lens == "accounts"
                else "Presence follows your literal topic wording filter.",
                className="rv-small rv-muted",
            ),
            dcc.Graph(
                figure=activity_figure(data, scope, lens, group_by),
                config={"displayModeBar": False},
                responsive=True,
                className="sd-chart",
                id=_id("activity-chart"),
            ),
            html.P(
                f"Time basis: {data['filters']['time_basis'].capitalize()} timestamps · "
                f"UTC {data['filters']['bucket']} buckets. "
                "Hover over or select a segment for its full identity and contributing records.",
                className="rv-small rv-muted",
            ),
            html.P(
                "Dates without bars have no saved observations; source coverage is unknown.",
                className="rv-small rv-muted",
            ),
            html.Details(
                [
                    html.Summary("Activity and sentiment as a table"),
                    _table(
                        [
                            "UTC bucket",
                            "Saved records",
                            *(
                                [label.title() for label in COLORS]
                                if data.get("sentiment_comparable", True)
                                else []
                            ),
                        ],
                        [
                            [
                                b["label"],
                                b["count"],
                                *(
                                    [b["sentiment_counts"][label] for label in COLORS]
                                    if data.get("sentiment_comparable", True)
                                    else []
                                ),
                            ]
                            for b in data["time_buckets"]
                        ],
                        "Records with unknown timestamps are reported separately, "
                        "outside time buckets.",
                    ),
                ],
                className="rv-details",
            ),
            html.Div(summaries, className="sd-breakdowns"),
            html.H3("Saved evidence relationships"),
            html.P(
                f"Graph shows {len(graph_rows)} of {len(data['rows'])} matching records and "
                f"{len(graph_edges)} of {coverage['relationship_total']} saved relationships. "
                "Links mean documented repost, shared URL or matching content; "
                "they do not establish "
                "coordination, common control or independent corroboration.",
                className="rv-small rv-muted",
            ),
            dcc.Graph(
                figure=graph,
                config={"displayModeBar": False},
                responsive=True,
                className="sd-chart sd-relationship-chart",
                id=_id("relationship-chart"),
            ),
            html.Details(
                [
                    html.Summary("Relationship table and basis"),
                    _table(
                        ["From record", "To record", "Relationship", "Saved basis"],
                        [
                            [
                                html.Div(
                                    [
                                        _source_button(
                                            row_by_id[e["source_record_id"]],
                                            context,
                                            f"relationship-from-{index}",
                                        ),
                                        html.Code(
                                            e["source_record_id"], className="rv-small rv-mono"
                                        ),
                                    ]
                                ),
                                html.Div(
                                    [
                                        _source_button(
                                            row_by_id[e["target_record_id"]],
                                            context,
                                            f"relationship-to-{index}",
                                        ),
                                        html.Code(
                                            e["target_record_id"], className="rv-small rv-mono"
                                        ),
                                    ]
                                ),
                                e["relation"].replace("_", " "),
                                e["basis"],
                            ]
                            for index, e in enumerate(data["edges"])
                        ],
                        f"Showing {len(data['edges'])} of {coverage['relationship_total']} "
                        "saved relationships, including records outside the graph display limit.",
                    ),
                ],
                className="rv-details sd-relationship-table",
                open=True,
            ),
            _table(
                [
                    "Inspect source",
                    "Platform",
                    "Channel",
                    "Account / author",
                    "Selected timestamp",
                    "Sentiment",
                    "Sentiment target",
                    "Record origin",
                ],
                [[value if value is not None else "Unknown" for value in row] for row in records],
                f"Showing {len(records)} of {len(data['rows'])} matching records. "
                "Use Browse all matching records below to inspect any record.",
            ),
            html.Details(
                [
                    html.Summary("Coverage and limitations"),
                    html.Ul([html.Li(value) for value in data["limitations"]]),
                ],
                className="rv-details",
            ),
        ]
    )


def scope_token(context, filters, lens, group_by="channel"):
    payload = json.dumps([context, filters, lens, group_by], sort_keys=True, ensure_ascii=False)
    return "dashboard-v1:" + hashlib.sha256(payload.encode()).hexdigest()


def derive_selection(data, point, scope):
    """Resolve a current saved match set, time bucket, graph node, or graph edge."""
    selected = point.get("customdata") if isinstance(point, dict) else None
    if not isinstance(selected, dict) or selected.get("scope") != scope:
        raise ValueError("This chart selection is stale. Select a point in the current view.")
    rows = {row["id"]: row for row in data["rows"]}
    if selected.get("kind") == "matching":
        return list(rows)
    if selected.get("kind") == "bucket":
        bucket = next((b for b in data["time_buckets"] if b["start"] == selected.get("key")), None)
        sentiment = selected.get("sentiment")
        if bucket is None or sentiment not in {"all", *COLORS}:
            raise ValueError("This time bucket is no longer available.")
        if sentiment != "all" and not data.get("sentiment_comparable", True):
            raise ValueError("Sentiment targets cannot be combined in this chart.")
        ids = [
            rid
            for rid in bucket["record_ids"]
            if sentiment == "all" or rows[rid]["sentiment"] == sentiment
        ]
        if "group_by" in selected:
            if selected["group_by"] not in {"channel", "account"}:
                raise ValueError("This account grouping is unavailable.")
            group = next(
                (
                    g
                    for g in _presence_groups(data, selected["group_by"])
                    if g["key"] == selected.get("group")
                ),
                None,
            )
            if group is None:
                raise ValueError("This account group is no longer available.")
            ids = [rid for rid in ids if rid in group["ids"]]
        return ids
    visible = {row["id"] for row in data["rows"][:GRAPH_LIMIT]}
    if selected.get("kind") == "record" and selected.get("key") in visible:
        return [selected["key"]]
    if selected.get("kind") == "edge":
        edge = next(
            (
                e
                for e in data["edges"]
                if e["id"] == selected.get("key")
                and {e["source_record_id"], e["target_record_id"]} <= visible
            ),
            None,
        )
        if edge:
            return [edge["source_record_id"], edge["target_record_id"]]
    raise ValueError("This saved relationship or record is no longer available.")


def render_selection(data, record_ids, context):
    rows = {row["id"]: row for row in data["rows"]}
    return html.Section(
        [
            html.H3(f"Selected contribution · {len(record_ids)} saved records"),
            html.P(
                "Record IDs are resolved from the current saved assessment and filters. "
                "Choose a source to inspect its exact evidence.",
                className="rv-small rv-muted",
            ),
            html.Ul(
                [
                    html.Li(
                        [
                            _source_button(rows[rid], context, f"selected-{index}"),
                            html.Code(rid, className="rv-small rv-mono"),
                        ]
                    )
                    for index, rid in enumerate(record_ids[:TABLE_LIMIT])
                ]
            ),
            html.Details(
                [
                    html.Summary(f"All {len(record_ids)} contributing record IDs"),
                    html.Pre("\n".join(record_ids), className="sd-record-ids"),
                ]
            ),
            html.P(
                f"Showing {min(TABLE_LIMIT, len(record_ids))} direct inspection links. "
                "Use the contributing-record selector below to open any record.",
                className="rv-small rv-muted",
            ),
        ],
        className="sd-selection",
    )


def register_callbacks(app: Dash, service):
    """Read-only callbacks; no job services, providers, or page/session writers."""

    @app.callback(
        Output(_id("output"), "children"),
        Output(_id("capture"), "data"),
        *[
            Output(_id(name), "options")
            for name in ("platform", "channel_key", "author_key", "sentiment_run_id")
        ],
        Input("rv-session", "data"),
        Input("rv-page", "value"),
        Input(_id("lens"), "value"),
        Input(_id("group_by"), "value"),
        *[Input(_id(name), "value") for name in FILTER_NAMES],
    )
    def render(state, page, lens, group_by, *values):
        if page != "compare":
            raise PreventUpdate
        try:
            if lens not in {"topics", "accounts"}:
                raise ValueError("Choose a dashboard lens.")
            if group_by not in {"channel", "account"}:
                raise ValueError("Choose a channel or account grouping.")
            filters = _filters(values)
            data, view, context = capture_dashboard(service, state, filters)
            options = [
                [
                    {"label": f"{o['label']} ({o['count']})", "value": o["value"]}
                    for o in data["options"][key]
                ]
                for key in ("platforms", "channels", "authors", "sentiment_runs")
            ]
            filter_values = filters.model_dump(mode="json")
            scope = scope_token(context, filter_values, lens, group_by)
            capture = {
                "context": context,
                "case_id": data["case_id"],
                "case_fingerprint": view["case_fingerprint"],
                "filters": filter_values,
                "lens": lens,
                "group_by": group_by,
                "scope": scope,
            }
            return (
                render_dashboard(data, context, scope, lens, group_by),
                capture,
                *options,
            )
        except (ValueError, KeyError, TypeError) as exc:
            return (
                html.P(str(exc), className="rv-notice", role="status"),
                None,
                [],
                [],
                [],
                [],
            )

    @app.callback(
        Output(_id("drilldown"), "children"),
        Output(_id("selected-record"), "options"),
        Output(_id("selected-record"), "value"),
        Output(_id("selection"), "data"),
        # Charts mount only after a valid Compare projection; absent charts supply None.
        Input(_id("activity-chart"), "clickData", allow_optional=True),
        Input(_id("relationship-chart"), "clickData", allow_optional=True),
        Input(_id("browse"), "n_clicks"),
        Input(_id("capture"), "data"),
        Input("rv-session", "data"),
        Input("rv-page", "value"),
        Input(_id("lens"), "value"),
        Input(_id("group_by"), "value"),
        *[Input(_id(name), "value") for name in FILTER_NAMES],
    )
    def drilldown(
        activity_click, graph_click, browse_clicks, capture, state, page, lens, group_by, *values
    ):
        if page != "compare":
            raise PreventUpdate
        browsing = ctx.triggered_id == _id("browse") and bool(browse_clicks)
        if (
            ctx.triggered_id not in {_id("activity-chart"), _id("relationship-chart")}
            and not browsing
        ):
            return (
                "Select a time bar, sentiment segment or graph point to inspect its saved records.",
                [],
                None,
                None,
            )
        click = activity_click if ctx.triggered_id == _id("activity-chart") else graph_click
        if click is None and not browsing:
            return (
                "Select a time bar, sentiment segment or graph point to inspect its saved records.",
                [],
                None,
                None,
            )
        try:
            if browsing:
                if not capture:
                    raise ValueError("Choose a saved case and refresh the dashboard first.")
                click = {
                    "points": [{"customdata": {"kind": "matching", "scope": capture["scope"]}}]
                }
            if not capture or not isinstance(click, dict) or len(click.get("points", [])) != 1:
                raise ValueError("Select one chart point in the current view.")
            filters = _filters(values)
            if (
                filters.model_dump(mode="json") != capture["filters"]
                or lens != capture["lens"]
                or group_by != capture["group_by"]
            ):
                raise ValueError("The filters changed. Select a point in the refreshed chart.")
            data, view, context = capture_dashboard(service, state, filters)
            scope = scope_token(context, filters.model_dump(mode="json"), lens, group_by)
            if scope != capture["scope"] or view["case_fingerprint"] != capture["case_fingerprint"]:
                raise ValueError(
                    "The saved assessment changed. Select a point in the refreshed chart."
                )
            ids = derive_selection(data, click["points"][0], scope)
            rows = {row["id"]: row for row in data["rows"]}
            options = [{"value": rid, "label": f"{rows[rid]['title']} · {rid}"} for rid in ids]
            return (
                render_selection(data, ids, context),
                options,
                None,
                {"point": click["points"][0], "scope": scope},
            )
        except (ValueError, KeyError, TypeError) as exc:
            return (
                html.P(str(exc), className="rv-notice", role="status"),
                [],
                None,
                None,
            )

    @app.callback(
        Output(_id("selected-open"), "children"),
        Input(_id("selected-record"), "value"),
        Input(_id("selection"), "data"),
        Input("rv-session", "data"),
        Input("rv-page", "value"),
        Input(_id("capture"), "data"),
        Input(_id("lens"), "value"),
        Input(_id("group_by"), "value"),
        *[Input(_id(name), "value") for name in FILTER_NAMES],
    )
    def open_selected(record_id, selection, state, page, capture, lens, group_by, *values):
        if page != "compare":
            raise PreventUpdate
        if not record_id or not selection or not capture:
            return ""
        try:
            filters = _filters(values)
            data, _view, context = capture_dashboard(service, state, filters)
            scope = scope_token(context, filters.model_dump(mode="json"), lens, group_by)
            if scope != capture["scope"] or scope != selection["scope"]:
                raise ValueError("The selected contribution changed. Select a new chart point.")
            if record_id not in derive_selection(data, selection["point"], scope):
                raise ValueError("That record does not contribute to the selected chart point.")
            row = next(row for row in data["rows"] if row["id"] == record_id)
            return _source_button(row, context, "exact-selection")
        except (ValueError, KeyError, TypeError) as exc:
            return html.P(str(exc), className="rv-notice", role="status")


def _filters(values):
    data = dict(zip(FILTER_NAMES, values, strict=True))
    for key in ("start", "end"):
        data[key] = data[key] or None
    data["text_query"] = data["text_query"] or ""
    return DashboardFilters.model_validate(data)
