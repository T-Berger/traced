"""Persistent human review and posting chronology, without layout-time writes.

Reuses the Explorer's bounded inspector, section headings and scoped panels.
Form loads and mutations are separate callbacks: failed saves never replace a
draft, and list refreshes never automatically load another review into it.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import plotly.graph_objects as go
from dash import Dash, Input, Output, State, ctx, dcc, html, no_update
from plotly.subplots import make_subplots

from ..contracts import (
    ALL_FILTER,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    REVIEW_STATUS_LABELS,
    REVIEW_STATUSES,
    UNKNOWN_FILTER,
    ExplorerStore,
    ReviewConflictError,
    ReviewServiceProtocol,
    normalize_filter,
)
from .components import (
    empty_state,
    evidence_inspector,
    filter_options,
    number,
    section_heading,
    supplied,
)

LOGGER = logging.getLogger(__name__)
FORM_FIELDS = (
    "title",
    "observation",
    "interpretation",
    "uncertainty",
    "rationale",
    "reviewer",
    "status",
)
BERLIN = ZoneInfo("Europe/Berlin")


def berlin_stamp(value: Any) -> str:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return "Time zone not supplied"
        return stamp.astimezone(BERLIN).strftime("%d %b %Y %H:%M:%S %Z")
    except (TypeError, ValueError):
        return "Time not supplied"


def _page(value: Any) -> int:
    try:
        return max(1, min(int(value or 1), 1_000_000))
    except (TypeError, ValueError, OverflowError):
        return 1


def _button(label: str, key: str, *, primary: bool = False, disabled: bool = False):
    return html.Button(
        label,
        id=key,
        n_clicks=0,
        disabled=disabled,
        className="ss-button ss-primary" if primary else "ss-button ss-secondary",
    )


def _pager(prefix: str):
    return html.Div(
        [
            _button("Previous page", f"ss-r-{prefix}-previous", disabled=True),
            html.Span(
                "No records", id=f"ss-r-{prefix}-pagination", className="ss-pagination-label"
            ),
            _button("Next page", f"ss-r-{prefix}-next", disabled=True),
        ],
        className="ss-pagination",
    )


def _field(label: str, key: str, *, multiline: bool = True, limit: int = 12000):
    control = (
        dcc.Textarea(id=f"ss-r-{key}", value="", maxLength=limit, className="ss-form-textarea")
        if multiline
        else dcc.Input(
            id=f"ss-r-{key}", value="", type="text", maxLength=limit, className="ss-form-input"
        )
    )
    return html.Div([html.Label(label, htmlFor=f"ss-r-{key}"), control], className="ss-form-field")


def _status_options(include_all: bool = False):
    choices = [{"label": REVIEW_STATUS_LABELS[key], "value": key} for key in REVIEW_STATUSES]
    return (
        ([{"label": "All review states", "value": ALL_FILTER}] + choices)
        if include_all
        else choices
    )


def create_review_layout(store: ExplorerStore, review_service: ReviewServiceProtocol) -> html.Div:
    """Pure layout. In particular, never ensure a baseline during a GET request."""
    return html.Div(
        [
            dcc.Store(id="ss-r-editor-ref"),
            dcc.Store(id="ss-r-refresh", data=0),
            dcc.Store(id="ss-r-page", data=1),
            dcc.Store(id="ss-r-history-page", data=1),
            dcc.Store(id="ss-r-events-page", data=1),
            dcc.Store(id="ss-r-exclusion-preview"),
            html.Section(
                [
                    section_heading(
                        "REVIEW WORKSPACE",
                        "Reviews & chronology",
                        "Record assessed wording with evidence. Reviewed does not certify authenticity.",
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Label("Scenario", htmlFor="ss-r-scenario"),
                                    dcc.Dropdown(
                                        id="ss-r-scenario",
                                        options=[],
                                        value=None,
                                        clearable=False,
                                        className="ss-dropdown",
                                    ),
                                ]
                            ),
                            _field(
                                "Reviewer / actor (required)",
                                "reviewer",
                                multiline=False,
                                limit=150,
                            ),
                            html.Div(
                                [
                                    html.Label("New scenario name", htmlFor="ss-r-scenario-name"),
                                    dcc.Input(
                                        id="ss-r-scenario-name",
                                        type="text",
                                        value="",
                                        maxLength=300,
                                        className="ss-form-input",
                                    ),
                                ]
                            ),
                            _button("Fork scenario", "ss-r-fork"),
                        ],
                        className="ss-review-workspace-controls",
                    ),
                    html.Div(
                        id="ss-r-scenario-status", className="ss-form-feedback", role="status"
                    ),
                    html.Div(id="ss-r-scenario-caption", className="ss-muted"),
                    html.P(
                        "Fork before excluding a source. A fork preserves its source scenario and "
                        "copies current reviews and exclusion state.",
                        className="ss-muted",
                    ),
                ],
                className="ss-panel",
            ),
            html.Section(
                [
                    section_heading(
                        "POSTING COVERAGE",
                        "Exercise week first posted",
                        "Supplied evidence counts, not events or operational activity. "
                        "Click a week to filter the review queue.",
                    ),
                    dcc.RadioItems(
                        id="ss-r-unit",
                        options=[
                            {"label": "Source videos", "value": "videos"},
                            {"label": "Clips", "value": "clips"},
                            {"label": "Frames", "value": "frames"},
                        ],
                        value="videos",
                        inline=True,
                        className="ss-unit-control",
                    ),
                    dcc.Loading(
                        dcc.Graph(
                            id="ss-r-timeline",
                            figure=empty_chronology(),
                            config={"displayModeBar": False},
                            responsive=True,
                            style={"height": "560px"},
                        )
                    ),
                    html.P(
                        "Excluded counts are a subset of supplied evidence. Review counts are shown "
                        "separately and follow the review-state filter. No capture dates are inferred.",
                        className="ss-muted",
                    ),
                    _button("Open available evidence for selected week", "ss-r-open-evidence"),
                ],
                className="ss-panel",
            ),
            html.Div(
                [
                    html.Section(
                        [
                            section_heading("REVIEW QUEUE", "Saved statements"),
                            html.Div(
                                [
                                    html.Div(
                                        [
                                            html.Label(
                                                "Review state", htmlFor="ss-r-status-filter"
                                            ),
                                            dcc.Dropdown(
                                                id="ss-r-status-filter",
                                                options=_status_options(True),
                                                value=ALL_FILTER,
                                                clearable=False,
                                                className="ss-dropdown",
                                            ),
                                        ]
                                    ),
                                    html.Div(
                                        [
                                            html.Label("Posting week", htmlFor="ss-r-week"),
                                            dcc.Dropdown(
                                                id="ss-r-week",
                                                options=filter_options([], "All posting weeks"),
                                                value=ALL_FILTER,
                                                clearable=False,
                                                className="ss-dropdown",
                                            ),
                                        ]
                                    ),
                                    html.Div(
                                        [
                                            html.Label("Find saved wording", htmlFor="ss-r-query"),
                                            dcc.Input(
                                                id="ss-r-query",
                                                type="search",
                                                value="",
                                                maxLength=200,
                                                debounce=True,
                                                className="ss-form-input",
                                            ),
                                        ]
                                    ),
                                ],
                                className="ss-review-queue-filters",
                            ),
                            _button("New statement", "ss-r-new"),
                            html.Div(id="ss-r-queue-status", className="ss-muted"),
                            dcc.RadioItems(
                                id="ss-r-review-select",
                                options=[],
                                value=None,
                                className="ss-review-options",
                                labelClassName="ss-choice",
                                inputClassName="ss-radio",
                            ),
                            _pager("queue"),
                        ],
                        className="ss-panel",
                    ),
                    html.Section(
                        [
                            section_heading("REVIEW EDITOR", "Observation and interpretation"),
                            html.Div(id="ss-r-editor-caption", className="ss-muted"),
                            html.P(
                                "Selecting a saved statement or Reload saved replaces this form. "
                                "Failed saves retain your text.",
                                className="ss-muted",
                            ),
                            _field("Title (required)", "title", multiline=False, limit=300),
                            _field("What is visible", "observation"),
                            _field("Interpretation", "interpretation"),
                            _field("Uncertainty and alternatives", "uncertainty", limit=6000),
                            _field("Rationale / reason for this revision", "rationale"),
                            html.Div(
                                [
                                    html.Label("Review state", htmlFor="ss-r-status"),
                                    dcc.Dropdown(
                                        id="ss-r-status",
                                        options=_status_options(),
                                        value="draft",
                                        clearable=False,
                                        className="ss-dropdown",
                                    ),
                                ],
                                className="ss-form-field",
                            ),
                            html.Div(
                                [
                                    html.Label(
                                        "Linked evidence — remove a reference with ×",
                                        htmlFor="ss-r-evidence",
                                    ),
                                    dcc.Dropdown(
                                        id="ss-r-evidence",
                                        options=[],
                                        value=[],
                                        multi=True,
                                        className="ss-dropdown",
                                    ),
                                    html.P(
                                        "At most 48 release-qualified frame references. Attach a selected "
                                        "Explorer frame or add a known frame ID below.",
                                        className="ss-muted",
                                    ),
                                ],
                                className="ss-form-field",
                            ),
                            html.Div(
                                [
                                    _button("Attach selected Explorer frame", "ss-r-attach-frame"),
                                    dcc.Input(
                                        id="ss-r-evidence-id",
                                        type="text",
                                        value="",
                                        maxLength=500,
                                        placeholder="Frame ID or release:frame ID",
                                        className="ss-form-input",
                                    ),
                                    _button("Add evidence ID", "ss-r-attach-id"),
                                ],
                                className="ss-editor-actions",
                            ),
                            html.Div(
                                id="ss-r-evidence-feedback",
                                role="status",
                                className="ss-form-feedback",
                            ),
                            html.Div(
                                [
                                    _button("Save revision", "ss-r-save", primary=True),
                                    _button("Reload saved (discard edits)", "ss-r-reload"),
                                ],
                                className="ss-editor-actions",
                            ),
                            html.Div(
                                id="ss-r-save-status", role="status", className="ss-form-feedback"
                            ),
                            html.H3("Linked evidence inspector"),
                            dcc.RadioItems(
                                id="ss-r-linked-frame",
                                options=[],
                                value=None,
                                className="ss-evidence-ref-options",
                            ),
                            html.Div(id="ss-r-linked-inspector"),
                        ],
                        className="ss-panel",
                    ),
                ],
                className="ss-review-grid",
            ),
            html.Section(
                [
                    section_heading(
                        "IMMUTABLE HISTORY",
                        "Saved review revisions",
                        "Timestamps use Europe/Berlin. Stored times remain UTC.",
                    ),
                    html.Div(id="ss-r-history"),
                    _pager("history"),
                ],
                className="ss-panel",
            ),
            html.Section(
                [
                    section_heading(
                        "SCENARIO EXCLUSION",
                        "Challenge a source video",
                        "Only a fork can exclude a source. Original evidence is preserved.",
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Label(
                                        "Source video from linked or selected evidence",
                                        htmlFor="ss-r-source",
                                    ),
                                    dcc.Dropdown(
                                        id="ss-r-source",
                                        options=[],
                                        value=None,
                                        clearable=False,
                                        className="ss-dropdown",
                                    ),
                                ]
                            ),
                            dcc.RadioItems(
                                id="ss-r-exclusion-action",
                                options=[
                                    {"label": "Exclude source", "value": "exclude"},
                                    {"label": "Restore source", "value": "restore"},
                                ],
                                value="exclude",
                                className="ss-unit-control",
                            ),
                        ],
                        className="ss-review-queue-filters",
                    ),
                    _field("Reason for exclusion or restoration (required)", "exclusion-reason"),
                    html.Div(
                        [
                            _button("Preview impact", "ss-r-preview"),
                            _button("Apply previewed change", "ss-r-apply", disabled=True),
                        ],
                        className="ss-editor-actions",
                    ),
                    html.Div(id="ss-r-preview-view", role="status", className="ss-form-feedback"),
                    html.Div(
                        id="ss-r-exclusion-status", role="status", className="ss-form-feedback"
                    ),
                    html.P(
                        "Exclusion appends Needs review revisions to every dependent statement. "
                        "Restoration records an event and does not mark statements Reviewed.",
                        className="ss-muted",
                    ),
                    html.H3("Source exclusion history"),
                    html.Div(id="ss-r-events"),
                    _pager("events"),
                ],
                className="ss-panel",
            ),
        ],
        id="ss-review-panel",
        style={"display": "none"},
        className="ss-review-panel",
    )


def empty_chronology(message: str = "Choose a review scenario to inspect posting coverage."):
    figure = go.Figure()
    figure.add_annotation(text=message, x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False)
    figure.update_layout(
        height=420,
        template="plotly_white",
        margin=dict(l=35, r=20, t=35, b=40),
        paper_bgcolor="#FFFFFF",
        plot_bgcolor="#FFFFFF",
        font=dict(family="Manrope, system-ui, sans-serif", color="#142229", size=14),
    )
    figure.update_xaxes(visible=False)
    figure.update_yaxes(visible=False)
    return figure


def chronology_figure(result: dict[str, Any]):
    bins = result.get("bins") or []
    if not bins:
        return empty_chronology("No supplied evidence in this scenario's release.")
    labels = [
        str(
            row.get("label")
            or (f"Week {row['week']}" if row.get("week") is not None else "Not supplied")
        )
        for row in bins
    ]
    keys = [UNKNOWN_FILTER if row.get("week") is None else f"Week {row['week']}" for row in bins]
    unit = str(result.get("unit") or "videos")
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.17,
        subplot_titles=(f"Supplied {unit}", "Linked saved statements"),
    )
    for field, title, color, row in (
        ("count", f"Supplied {unit}", "#276B63", 1),
        ("excluded_count", f"Excluded {unit} (subset)", "#754B05", 1),
        ("review_count", "Saved statements", "#52636C", 2),
        ("needs_review_count", "Needs review (subset)", "#754B05", 2),
    ):
        figure.add_trace(
            go.Bar(
                x=labels,
                y=[item.get(field, 0) for item in bins],
                name=title,
                marker_color=color,
                customdata=keys,
                hovertemplate="%{x}<br>%{y}<extra>" + title + "</extra>",
            ),
            row=row,
            col=1,
        )
    figure.update_layout(
        template="plotly_white",
        height=560,
        barmode="group",
        margin=dict(l=40, r=20, t=40, b=155),
        paper_bgcolor="#FFFFFF",
        plot_bgcolor="#FFFFFF",
        font=dict(family="Manrope, system-ui, sans-serif", color="#142229", size=14),
        legend=dict(orientation="h", y=-0.5),
        clickmode="event+select",
    )
    figure.update_yaxes(
        rangemode="tozero", dtick=1 if max(x.get("count", 0) for x in bins) < 8 else None
    )
    figure.update_xaxes(title_text="Exercise week first posted", title_standoff=18, row=2, col=1)
    return figure


def _evidence_options(ids: list[str]):
    return [{"label": key, "value": key} for key in ids]


def _editor_values(
    review: dict[str, Any] | None,
    scenario_id: str | None,
    release_id: str | None,
    *,
    evidence: list[str] | None = None,
    reviewer: str = "",
):
    review = review or {}
    ids = review.get("evidence_ids", evidence or [])
    # The acting reviewer is input, not the historical author of the loaded
    # revision. Opening another person's note must never impersonate them.
    fields = [
        str(reviewer or "")
        if key == "reviewer"
        else review.get(key, "draft" if key == "status" else "")
        for key in FORM_FIELDS
    ]
    ref = {
        "review_id": review.get("review_id"),
        "revision": review.get("revision", 0),
        "scenario_id": scenario_id,
        "release_id": release_id,
    }
    return (*fields, _evidence_options(ids), ids, ref)


def _feedback(text: str, *, error: bool = False):
    return html.Div(text, className="ss-inline-error" if error else "ss-inline-success")


def history_card(item: dict[str, Any], *, show_title: bool = False):
    return html.Details(
        [
            html.Summary(
                [
                    html.Strong(
                        (f"{item['title']} · " if show_title else "")
                        + f"Revision {item['revision']} · "
                        f"{REVIEW_STATUS_LABELS.get(item.get('status'), item.get('status'))}"
                    ),
                    html.Span(
                        f" · {item.get('reviewer', '')} · {berlin_stamp(item.get('created_at'))}"
                    ),
                ]
            ),
            html.Dl(
                [
                    part
                    for key, label in (
                        ("title", "Title"),
                        ("observation", "What is visible"),
                        ("interpretation", "Interpretation"),
                        ("uncertainty", "Uncertainty"),
                        ("rationale", "Rationale"),
                        ("change_reason", "Change reason"),
                    )
                    for part in (html.Dt(label), html.Dd(supplied(item.get(key))))
                ]
            ),
            html.P("Evidence: " + ", ".join(item.get("evidence_ids") or []), className="ss-muted"),
        ],
        className="ss-history-card",
    )


def register_review_callbacks(
    app: Dash,
    store: ExplorerStore,
    review_service: ReviewServiceProtocol,
    delivery_service: Any = None,
) -> None:
    """Wire real service actions. UI forms use State for saves; GET layouts stay pure."""

    navigation_outputs = [
        Output("ss-evidence-panel", "style"),
        Output("ss-review-panel", "style"),
        Output("ss-page-title", "children"),
        Output("ss-page-subtitle", "children"),
    ]
    if delivery_service is not None:
        navigation_outputs.append(Output("ss-delivery-panel", "style"))

    @app.callback(*navigation_outputs, Input("ss-view", "value"))
    def show_view(view):
        reviewing = view == "reviews"
        delivering = delivery_service is not None and view == "delivery"
        result = (
            {"display": "none"} if reviewing or delivering else {},
            {} if reviewing else {"display": "none"},
            "Release diff & brief"
            if delivering
            else "Reviews & chronology"
            if reviewing
            else "Evidence Explorer",
            "Compare supplied records and freeze a cited assessment."
            if delivering
            else "Save supported wording and inspect posting coverage."
            if reviewing
            else "Find a clip group. Inspect the supplied record.",
        )
        return (
            (*result, {} if delivering else {"display": "none"})
            if delivery_service is not None
            else result
        )

    @app.callback(
        Output("ss-view", "value", allow_duplicate=True),
        Input("ss-review-from-frame", "n_clicks"),
        prevent_initial_call=True,
    )
    def start_review_view(clicks):
        return "reviews" if clicks else no_update

    @app.callback(
        Output("ss-r-scenario", "options"),
        Output("ss-r-scenario", "value"),
        Output("ss-r-scenario-status", "children"),
        Input("ss-release", "value"),
        Input("ss-view", "value"),
        Input("ss-r-fork", "n_clicks"),
        State("ss-r-scenario", "value"),
        State("ss-r-scenario-name", "value"),
        State("ss-r-reviewer", "value"),
    )
    def scenarios(release_id, view, fork_clicks, current, name, reviewer):
        if not release_id:
            return [], None, "Import a release before creating a review workspace."
        try:
            if view == "reviews" or ctx.triggered_id == "ss-r-fork":
                baseline = review_service.ensure_baseline(str(release_id))
            else:
                baseline = None
            message = None
            chosen = current
            if ctx.triggered_id == "ss-r-fork" and fork_clicks:
                fork = review_service.create_scenario(
                    str(release_id),
                    str(name or ""),
                    str(reviewer or ""),
                    source_scenario_id=current,
                )
                chosen = fork["scenario_id"]
                message = _feedback("Created a separate scenario. Its source remains unchanged.")
            items = review_service.list_scenarios(str(release_id))
            options = [
                {
                    "label": str(x["name"]) + (" · baseline" if x["is_baseline"] else " · fork"),
                    "value": x["scenario_id"],
                }
                for x in items
            ]
            values = {x["value"] for x in options}
            if chosen not in values:
                chosen = baseline["scenario_id"] if baseline else None
            return options, chosen, message
        except (ValueError, LookupError) as exc:
            return no_update, no_update, _feedback(str(exc), error=True)
        except Exception:
            LOGGER.exception("Could not open review scenarios")
            return (
                no_update,
                no_update,
                _feedback("Review workspace unavailable. Your form is retained.", error=True),
            )

    @app.callback(
        Output("ss-r-scenario-caption", "children"),
        Input("ss-r-scenario", "value"),
        Input("ss-r-refresh", "data"),
    )
    def scenario_caption(scenario_id, _tick):
        if not scenario_id:
            return "Open Reviews & chronology to initialize a baseline explicitly."
        try:
            item = review_service.get_scenario(scenario_id)
            return f"{'Baseline — source exclusions protected' if item['is_baseline'] else 'Forked scenario'} · version {item['version']}"
        except (ValueError, LookupError):
            return "Scenario unavailable."

    @app.callback(
        Output("ss-r-week", "options"), Output("ss-r-week", "value"), Input("ss-release", "value")
    )
    def review_weeks(release_id):
        if not release_id:
            return filter_options([], "All posting weeks"), ALL_FILTER
        try:
            weeks = store.overview(release_id).get("filter_options", {}).get("week", [])
            return filter_options(weeks, "All posting weeks"), ALL_FILTER
        except Exception:
            LOGGER.exception("Could not read posting-week options")
            return filter_options([], "All posting weeks"), ALL_FILTER

    @app.callback(
        Output("ss-r-review-select", "options"),
        Output("ss-r-queue-status", "children"),
        Output("ss-r-queue-pagination", "children"),
        Output("ss-r-queue-previous", "disabled"),
        Output("ss-r-queue-next", "disabled"),
        Output("ss-r-page", "data"),
        Input("ss-r-scenario", "value"),
        Input("ss-r-status-filter", "value"),
        Input("ss-r-query", "value"),
        Input("ss-r-week", "value"),
        Input("ss-r-refresh", "data"),
        Input("ss-r-queue-previous", "n_clicks"),
        Input("ss-r-queue-next", "n_clicks"),
        State("ss-r-page", "data"),
    )
    def queue(scenario_id, status, query, week, _tick, _previous, _next, saved_page):
        if not scenario_id:
            return [], "Choose a review scenario.", "No statements", True, True, 1
        page = (
            _page(saved_page)
            if ctx.triggered_id in {"ss-r-queue-previous", "ss-r-queue-next"}
            else 1
        )
        page += (
            1
            if ctx.triggered_id == "ss-r-queue-next"
            else -1
            if ctx.triggered_id == "ss-r-queue-previous"
            else 0
        )
        page = max(1, page)
        try:
            kwargs = dict(
                status=normalize_filter(status),
                query=str(query or ""),
                week=normalize_filter(week),
                page_size=DEFAULT_PAGE_SIZE,
            )
            result = review_service.list_reviews(scenario_id, page=page, **kwargs)
            total = result["total"]
            pages = max(1, math.ceil(total / DEFAULT_PAGE_SIZE))
            if page > pages:
                page = pages
                result = review_service.list_reviews(scenario_id, page=page, **kwargs)
            # Dash 4 can retain an empty rich-label subtree when option IDs
            # change after a scenario fork. Plain labels survive replacement
            # reliably and keep each native radio meaningfully selectable.
            options = [
                {
                    "value": item["review_id"],
                    "label": f"{REVIEW_STATUS_LABELS[item['status']]} · {item['title']} · "
                    f"Revision {item['revision']} · {item['reviewer']} · "
                    f"{berlin_stamp(item['created_at'])}",
                }
                for item in result["items"]
            ]
            return (
                options,
                (
                    f"{number(total)} saved statements"
                    if total
                    else "No saved statements match. Create a new statement or clear filters."
                ),
                f"Page {page} of {pages}",
                page <= 1,
                page >= pages,
                page,
            )
        except (ValueError, LookupError) as exc:
            return [], str(exc), "Queue unavailable", True, True, 1

    @app.callback(
        Output("ss-r-review-select", "value"),
        Input("ss-r-review-select", "options"),
        State("ss-r-review-select", "value"),
    )
    def retain_queue_selection(options, current):
        if current is None or current in {x["value"] for x in (options or [])}:
            return no_update
        return None

    editor_outputs = [Output(f"ss-r-{key}", "value") for key in FORM_FIELDS] + [
        Output("ss-r-evidence", "options"),
        Output("ss-r-evidence", "value"),
        Output("ss-r-editor-ref", "data"),
        Output("ss-r-save-status", "children"),
    ]

    @app.callback(
        *editor_outputs,
        Input("ss-r-review-select", "value"),
        Input("ss-r-new", "n_clicks"),
        Input("ss-review-from-frame", "n_clicks"),
        Input("ss-r-reload", "n_clicks"),
        Input("ss-r-scenario", "value"),
        Input("ss-release", "value"),
        State("ss-r-editor-ref", "data"),
        State("ss-r-reviewer", "value"),
        State("ss-selected-frame", "value"),
    )
    def load_editor(
        selected, _new, _from, _reload, scenario_id, release_id, ref, reviewer, frame_id
    ):
        trigger = ctx.triggered_id
        unchanged = [no_update] * 10
        try:
            if trigger == "ss-r-review-select" and not selected:
                return *unchanged, no_update
            if trigger in {"ss-r-scenario", "ss-release"} and ref:
                message = "Workspace changed. Your form is retained; choose New statement or a saved statement."
                return *unchanged, _feedback(message)
            if trigger in {"ss-r-new", "ss-review-from-frame"}:
                if not release_id:
                    return *unchanged, _feedback("Import a release first.", error=True)
                if not scenario_id:
                    scenario_id = review_service.ensure_baseline(release_id)["scenario_id"]
                evidence = []
                if trigger == "ss-review-from-frame":
                    if not frame_id or not store.get_frame(release_id, frame_id):
                        return *unchanged, _feedback("Select an Explorer frame first.", error=True)
                    evidence = [f"{release_id}:{frame_id}"]
                return (
                    *_editor_values(
                        None,
                        scenario_id,
                        release_id,
                        evidence=evidence,
                        reviewer=str(reviewer or ""),
                    ),
                    _feedback("New unsaved statement. Save a revision to persist it."),
                )
            review_id = (ref or {}).get("review_id") if trigger == "ss-r-reload" else selected
            if trigger == "ss-r-reload" and not review_id:
                review_id = selected
            if not review_id:
                return *unchanged, _feedback("No saved statement selected. The form is retained.")
            item = review_service.get_review(review_id)
            if item["scenario_id"] != scenario_id or item["release_id"] != release_id:
                return *unchanged, _feedback(
                    "That saved statement belongs to another workspace.", error=True
                )
            return (
                *_editor_values(item, scenario_id, release_id, reviewer=str(reviewer or "")),
                _feedback(f"Loaded saved revision {item['revision']}."),
            )
        except (ValueError, LookupError) as exc:
            return *unchanged, _feedback(str(exc), error=True)
        except Exception:
            LOGGER.exception("Could not load review editor")
            return *unchanged, _feedback(
                "Could not load this statement. Your form is retained.", error=True
            )

    @app.callback(
        *[
            Output(o.component_id, o.component_property, allow_duplicate=True)
            for o in editor_outputs
        ],
        Output("ss-r-refresh", "data"),
        Input("ss-r-save", "n_clicks"),
        State("ss-r-editor-ref", "data"),
        State("ss-r-scenario", "value"),
        State("ss-release", "value"),
        *[State(f"ss-r-{key}", "value") for key in FORM_FIELDS],
        State("ss-r-evidence", "value"),
        State("ss-r-refresh", "data"),
        prevent_initial_call=True,
    )
    def save_editor(
        clicks,
        ref,
        scenario_id,
        release_id,
        title,
        observation,
        interpretation,
        uncertainty,
        rationale,
        reviewer,
        status,
        evidence,
        tick,
    ):
        unchanged = [no_update] * 10
        if not clicks:
            return *unchanged, no_update, no_update
        try:
            ref = ref or {}
            if (
                not scenario_id
                or ref.get("scenario_id") != scenario_id
                or ref.get("release_id") != release_id
            ):
                raise ValueError(
                    "The form belongs to another workspace. Choose New statement or reload a saved statement."
                )
            item = review_service.save_review(
                scenario_id,
                title=str(title or ""),
                observation=str(observation or ""),
                interpretation=str(interpretation or ""),
                uncertainty=str(uncertainty or ""),
                rationale=str(rationale or ""),
                reviewer=str(reviewer or ""),
                status=status,
                evidence_ids=evidence or [],
                review_id=ref.get("review_id"),
                expected_revision=ref.get("revision", 0),
            )
            return (
                *_editor_values(item, scenario_id, release_id, reviewer=str(reviewer or "")),
                _feedback(
                    f"Saved revision {item['revision']} · {REVIEW_STATUS_LABELS[item['status']]}."
                ),
                int(tick or 0) + 1,
            )
        except ReviewConflictError as exc:
            return (
                *unchanged,
                _feedback(
                    str(exc)
                    + " Your draft is retained. Reload saved only when ready to discard edits.",
                    error=True,
                ),
                no_update,
            )
        except (ValueError, LookupError) as exc:
            return (
                *unchanged,
                _feedback(str(exc) + " Your draft is retained.", error=True),
                no_update,
            )
        except Exception:
            LOGGER.exception("Could not save review")
            return (
                *unchanged,
                _feedback(
                    "Save failed. Your draft is retained; retry after checking the local workspace.",
                    error=True,
                ),
                no_update,
            )

    @app.callback(
        Output("ss-r-evidence", "options", allow_duplicate=True),
        Output("ss-r-evidence", "value", allow_duplicate=True),
        Output("ss-r-evidence-feedback", "children"),
        Input("ss-r-attach-frame", "n_clicks"),
        Input("ss-r-attach-id", "n_clicks"),
        State("ss-release", "value"),
        State("ss-selected-frame", "value"),
        State("ss-r-evidence-id", "value"),
        State("ss-r-evidence", "value"),
        prevent_initial_call=True,
    )
    def attach_evidence(_frame_clicks, _id_clicks, release_id, selected_frame, entered_id, current):
        try:
            value = (
                selected_frame
                if ctx.triggered_id == "ss-r-attach-frame"
                else str(entered_id or "").strip()
            )
            if not release_id or not value:
                raise ValueError("Select an Explorer frame or enter a known frame ID.")
            prefix = f"{release_id}:"
            frame_id = str(value)[len(prefix) :] if str(value).startswith(prefix) else str(value)
            if not store.get_frame(release_id, frame_id):
                raise ValueError("Frame not found in this release. Existing evidence is retained.")
            ids = list(dict.fromkeys([*(current or []), f"{release_id}:{frame_id}"]))
            if len(ids) > MAX_PAGE_SIZE:
                raise ValueError("Link at most 48 frame references.")
            return _evidence_options(ids), ids, _feedback("Evidence attached to this unsaved form.")
        except (ValueError, LookupError) as exc:
            return no_update, no_update, _feedback(str(exc), error=True)

    @app.callback(
        Output("ss-r-editor-caption", "children"),
        Input("ss-r-editor-ref", "data"),
        Input("ss-r-refresh", "data"),
    )
    def editor_caption(ref, _tick):
        if not ref:
            return "Choose New statement or open a saved statement."
        label = (
            f"Editing revision {ref['revision']}"
            if ref.get("review_id")
            else "New unsaved statement"
        )
        if ref.get("review_id"):
            try:
                latest = review_service.get_review(ref["review_id"])
                if latest["revision"] != ref["revision"]:
                    label += f" · Newer saved revision {latest['revision']} exists. Your form has not been replaced."
                eligibility = latest.get("eligibility") or {}
                if not eligibility.get("eligible"):
                    label += " · " + " ".join(eligibility.get("reasons") or [])
            except (ValueError, LookupError):
                label += " · Saved statement unavailable."
        return label

    @app.callback(
        Output("ss-r-linked-frame", "options"),
        Output("ss-r-source", "options"),
        Output("ss-r-source", "value"),
        Input("ss-r-evidence", "value"),
        Input("ss-selected-frame", "value"),
        Input("ss-release", "value"),
        State("ss-r-source", "value"),
    )
    def linked_choices(evidence, explorer_frame, release_id, source):
        frames, videos = [], set()
        prefix = f"{release_id}:"
        for reference in (evidence or [])[:MAX_PAGE_SIZE]:
            if str(reference).startswith(prefix):
                frame = store.get_frame(release_id, str(reference)[len(prefix) :])
                if frame:
                    frames.append({"label": frame["frame_id"], "value": reference})
                    if frame.get("video_id"):
                        videos.add(str(frame["video_id"]))
        if release_id and explorer_frame:
            frame = store.get_frame(release_id, explorer_frame)
            if frame and frame.get("video_id"):
                videos.add(str(frame["video_id"]))
        options = [{"label": key, "value": key} for key in sorted(videos)]
        return (
            frames,
            options,
            source if source in videos else (options[0]["value"] if options else None),
        )

    @app.callback(
        Output("ss-r-linked-frame", "value"),
        Input("ss-r-linked-frame", "options"),
        State("ss-r-linked-frame", "value"),
    )
    def choose_linked_frame(options, current):
        values = [x["value"] for x in (options or [])]
        return current if current in values else values[0] if values else None

    @app.callback(
        Output("ss-r-linked-inspector", "children"),
        Input("ss-r-linked-frame", "value"),
        Input("ss-release", "value"),
        Input("ss-reveal-previews", "value"),
    )
    def inspect_linked(reference, release_id, reveal):
        prefix = f"{release_id}:"
        if not reference or not str(reference).startswith(prefix):
            return empty_state(
                "No linked frame selected", "Attach evidence to inspect its supplied record."
            )
        frame = store.get_frame(release_id, str(reference)[len(prefix) :])
        return (
            evidence_inspector(frame, release_id, "reveal" in (reveal or []))
            if frame
            else empty_state(
                "Linked frame unavailable", "The local reference could not be resolved."
            )
        )

    @app.callback(
        Output("ss-r-history", "children"),
        Output("ss-r-history-pagination", "children"),
        Output("ss-r-history-previous", "disabled"),
        Output("ss-r-history-next", "disabled"),
        Output("ss-r-history-page", "data"),
        Input("ss-r-editor-ref", "data"),
        Input("ss-r-refresh", "data"),
        Input("ss-r-history-previous", "n_clicks"),
        Input("ss-r-history-next", "n_clicks"),
        State("ss-r-history-page", "data"),
    )
    def history(ref, _tick, _previous, _next, saved_page):
        if not ref or not ref.get("review_id"):
            return "No saved revision history for this draft.", "No revisions", True, True, 1
        page = (
            _page(saved_page)
            if ctx.triggered_id in {"ss-r-history-previous", "ss-r-history-next"}
            else 1
        )
        page = max(
            1,
            page
            + (
                1
                if ctx.triggered_id == "ss-r-history-next"
                else -1
                if ctx.triggered_id == "ss-r-history-previous"
                else 0
            ),
        )
        result = review_service.review_history(
            ref["review_id"], page=page, page_size=DEFAULT_PAGE_SIZE
        )
        pages = max(1, math.ceil(result["total"] / DEFAULT_PAGE_SIZE))
        return (
            [history_card(item) for item in result["items"]],
            f"Page {page} of {pages}",
            page <= 1,
            page >= pages,
            page,
        )

    @app.callback(
        Output("ss-r-timeline", "figure"),
        Input("ss-r-scenario", "value"),
        Input("ss-r-unit", "value"),
        Input("ss-r-status-filter", "value"),
        Input("ss-r-refresh", "data"),
    )
    def timeline(scenario_id, unit, status, _tick):
        if not scenario_id:
            return empty_chronology()
        try:
            return chronology_figure(
                review_service.chronology(scenario_id, unit=unit, status=normalize_filter(status))
            )
        except (ValueError, LookupError) as exc:
            return empty_chronology(str(exc))

    @app.callback(
        Output("ss-r-week", "value", allow_duplicate=True),
        Input("ss-r-timeline", "clickData"),
        prevent_initial_call=True,
    )
    def select_week(click_data):
        points = (click_data or {}).get("points") or []
        return points[0].get("customdata", no_update) if points else no_update

    @app.callback(
        Output("ss-week", "value", allow_duplicate=True),
        Output("ss-view", "value", allow_duplicate=True),
        Input("ss-r-open-evidence", "n_clicks"),
        State("ss-r-week", "value"),
        prevent_initial_call=True,
    )
    def open_week_evidence(clicks, week):
        return (week or ALL_FILTER, "evidence") if clicks else (no_update, no_update)

    @app.callback(
        Output("ss-r-exclusion-preview", "data"),
        Output("ss-r-preview-view", "children"),
        Output("ss-r-apply", "disabled"),
        Input("ss-r-preview", "n_clicks"),
        Input("ss-r-scenario", "value"),
        Input("ss-r-source", "value"),
        Input("ss-r-exclusion-action", "value"),
        Input("ss-r-exclusion-reason", "value"),
        Input("ss-r-reviewer", "value"),
        Input("ss-r-refresh", "data"),
    )
    def preview_exclusion(clicks, scenario_id, video_id, action, reason, reviewer, _tick):
        if ctx.triggered_id != "ss-r-preview" or not clicks:
            return (
                None,
                "Preview again after any change to the source, actor, reason or scenario.",
                True,
            )
        try:
            if (
                not scenario_id
                or not video_id
                or not str(reason or "").strip()
                or not str(reviewer or "").strip()
            ):
                raise ValueError(
                    "Choose a source and enter a reason and reviewer before previewing."
                )
            result = review_service.exclusion_preview(
                scenario_id, video_id, excluded=action == "exclude"
            )
            preview = {
                key: result[key]
                for key in ("scenario_version", "video_id", "excluded", "affected_review_count")
            }
            preview["scenario_id"] = scenario_id
            can_apply = (
                result.get("can_apply", True) and result["currently_excluded"] != result["excluded"]
            )
            message = [
                html.Strong(f"{result['affected_review_count']} dependent statements"),
                html.P(
                    "These will receive Needs review revisions."
                    if result["excluded"]
                    else "Restoration does not mark existing statements Reviewed."
                ),
                html.P(
                    f"Previewed scenario version {result['scenario_version']} · Reviewer {reviewer}"
                ),
                html.P(str(reason)),
                html.Ul([html.Li(item["title"]) for item in result.get("affected_reviews", [])]),
            ]
            if not result.get("can_apply", True):
                message.append(
                    html.P("Baseline protected. Fork this scenario before changing a source.")
                )
            elif not can_apply:
                message.append(html.P("The source already has this state."))
            return preview if can_apply else None, html.Div(message), not can_apply
        except (ValueError, LookupError) as exc:
            return None, _feedback(str(exc), error=True), True

    @app.callback(
        Output("ss-r-refresh", "data", allow_duplicate=True),
        Output("ss-r-exclusion-status", "children"),
        Input("ss-r-apply", "n_clicks"),
        State("ss-r-exclusion-preview", "data"),
        State("ss-r-scenario", "value"),
        State("ss-r-source", "value"),
        State("ss-r-exclusion-action", "value"),
        State("ss-r-exclusion-reason", "value"),
        State("ss-r-reviewer", "value"),
        State("ss-r-refresh", "data"),
        prevent_initial_call=True,
    )
    def apply_exclusion(clicks, preview, scenario_id, video_id, action, reason, reviewer, tick):
        if not clicks:
            return no_update, no_update
        try:
            if (
                not preview
                or preview.get("scenario_id") != scenario_id
                or preview.get("video_id") != video_id
                or preview.get("excluded") != (action == "exclude")
            ):
                raise ValueError("Preview this exact source change before applying it.")
            result = review_service.set_source_exclusion(
                scenario_id,
                video_id,
                excluded=preview["excluded"],
                reason=str(reason or ""),
                reviewer=str(reviewer or ""),
                expected_version=preview["scenario_version"],
            )
            message = f"Source {'excluded' if result['excluded'] else 'restored'} · scenario version {result['scenario_version']}. "
            message += (
                f"{result['affected_review_count']} statements now need review."
                if result["excluded"]
                else "Existing review states were not automatically changed."
            )
            return int(tick or 0) + 1, _feedback(message)
        except (ValueError, LookupError) as exc:
            return no_update, _feedback(str(exc) + " No form text was discarded.", error=True)
        except Exception:
            LOGGER.exception("Could not apply source exclusion")
            return no_update, _feedback(
                "Source change failed. Refresh the preview before retrying.", error=True
            )

    @app.callback(
        Output("ss-r-events", "children"),
        Output("ss-r-events-pagination", "children"),
        Output("ss-r-events-previous", "disabled"),
        Output("ss-r-events-next", "disabled"),
        Output("ss-r-events-page", "data"),
        Input("ss-r-scenario", "value"),
        Input("ss-r-refresh", "data"),
        Input("ss-r-events-previous", "n_clicks"),
        Input("ss-r-events-next", "n_clicks"),
        State("ss-r-events-page", "data"),
    )
    def exclusion_events(scenario_id, _tick, _previous, _next, saved_page):
        if not scenario_id:
            return "No scenario selected.", "No events", True, True, 1
        page = (
            _page(saved_page)
            if ctx.triggered_id in {"ss-r-events-previous", "ss-r-events-next"}
            else 1
        )
        page = max(
            1,
            page
            + (
                1
                if ctx.triggered_id == "ss-r-events-next"
                else -1
                if ctx.triggered_id == "ss-r-events-previous"
                else 0
            ),
        )
        result = review_service.exclusion_history(
            scenario_id, page=page, page_size=DEFAULT_PAGE_SIZE
        )
        pages = max(1, math.ceil(result["total"] / DEFAULT_PAGE_SIZE))
        cards = [
            html.Div(
                [
                    html.Strong(f"{'Excluded' if x['excluded'] else 'Restored'} · {x['video_id']}"),
                    html.P(x["reason"]),
                    html.P(
                        f"{x['reviewer']} · {berlin_stamp(x['created_at'])} · scenario version {x['scenario_version']}",
                        className="ss-muted",
                    ),
                ],
                className="ss-history-card",
            )
            for x in result["items"]
        ]
        return (
            cards or "No source changes in this scenario.",
            f"Page {page} of {pages}",
            page <= 1,
            page >= pages,
            page,
        )
