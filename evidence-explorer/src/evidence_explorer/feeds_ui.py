"""Saved-feed schedules and controls; callbacks only access local storage/queues."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from dash import ALL, Input, Output, State, ctx, dcc, html, no_update

from .feed_platform_ui import (
    PLATFORMS,
    card_source_details,
    locator_field,
    platform_save_button,
    platform_selector,
    register_platform_callbacks,
    source_options,
)
from .ingestion_ui import import_counts, source_error_list
from .ui import pill, source_link

if TYPE_CHECKING:
    from dash import Dash

    from .service import ExplorerService

logger = logging.getLogger(__name__)
DISPLAY_LIMIT = 30
ACTION_ID = {"type": "saved-feed-action", "feed_id": ALL, "action": ALL}


def _signature(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def _timestamp(value: str | None, empty: str) -> str:
    if not value:
        return empty
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return stamp.strftime("%d %b %Y · %H:%M")
        return stamp.astimezone(UTC).strftime("%d %b %Y · %H:%M UTC")
    except (ValueError, TypeError):
        return "Time unavailable"


def _input_field(label: str, component, *, class_name: str = "", element_id: str | None = None):
    return html.Div(
        [html.Label(label, htmlFor=component.id), component],
        className=class_name,
        **({"id": element_id} if element_id else {}),
    )


def _feed_form(read_only: bool, options: list[dict]):
    return html.Div(
        [
            html.Div(
                [
                    platform_selector(options, read_only),
                    _input_field(
                        "Feed name",
                        dcc.Input(
                            id="saved-feed-name",
                            type="text",
                            value="",
                            maxLength=150,
                            placeholder="e.g. Public news updates",
                            disabled=read_only,
                            className="search-input",
                        ),
                    ),
                    _input_field(
                        "RSS / Atom URL",
                        dcc.Input(
                            id="saved-feed-url",
                            type="url",
                            value="",
                            placeholder="https://…",
                            disabled=read_only,
                            className="search-input",
                        ),
                        class_name="saved-feed-url-field",
                        element_id="saved-feed-url-field",
                    ),
                    locator_field(),
                    _input_field(
                        "Topic (optional)",
                        dcc.Input(
                            id="saved-feed-topic",
                            type="text",
                            value="",
                            maxLength=150,
                            placeholder="General",
                            disabled=read_only,
                            className="search-input",
                        ),
                    ),
                    _input_field(
                        "Refresh every (minutes)",
                        dcc.Input(
                            id="saved-feed-interval",
                            type="number",
                            value=60,
                            min=5,
                            max=10080,
                            step=1,
                            disabled=read_only,
                            className="search-input",
                        ),
                    ),
                    _input_field(
                        "Items per refresh",
                        dcc.Input(
                            id="saved-feed-limit",
                            type="number",
                            value=5,
                            min=1,
                            max=20,
                            step=1,
                            disabled=read_only,
                            className="search-input",
                        ),
                    ),
                    _input_field(
                        "Feed content",
                        dcc.Dropdown(
                            id="saved-feed-content-mode",
                            options=[
                                {"label": "News pages linked by feed", "value": "article_links"},
                                {
                                    "label": "Full posts from feed (social RSS)",
                                    "value": "feed_content",
                                },
                            ],
                            value="article_links",
                            clearable=False,
                            searchable=False,
                            disabled=read_only,
                        ),
                        class_name="saved-feed-mode-field",
                        element_id="saved-feed-mode-field",
                    ),
                ],
                className="saved-feed-fields",
            ),
            html.P(id="saved-feed-mode-help", className="muted small saved-feed-mode-help"),
            html.P(id="saved-feed-source-help", className="muted small saved-feed-source-help"),
            html.Div(id="saved-feed-connection-status"),
            html.Div(
                [
                    dcc.Checklist(
                        id="saved-feed-analyze",
                        options=[
                            {
                                "label": "Analyze imported items with local Ollama",
                                "value": "analyze",
                                "disabled": read_only,
                            }
                        ],
                        value=["analyze"],
                        className="import-analysis-toggle",
                    ),
                    html.Div(
                        [
                            html.Button(
                                "Save feed →",
                                id="saved-feed-save",
                                n_clicks=0,
                                disabled=read_only,
                                className="button button--primary",
                            ),
                            platform_save_button(read_only),
                        ],
                        className="saved-feed-submit-buttons",
                    ),
                ],
                className="import-actions",
            ),
            html.P(
                "Saving a new feed schedules its first refresh now. The worker runs saved-feed "
                "schedules and any selected local analysis.",
                className="muted small saved-feed-note",
            ),
        ]
    )


def feeds_panel(service: ExplorerService):
    read_only = service.settings.read_only
    try:
        rows = service.list_feeds(limit=DISPLAY_LIMIT)
        listing = feed_cards(rows, read_only)
    except Exception as exc:
        logger.exception("Could not load saved feeds")
        rows = []
        listing = _feedback(f"Saved feeds are unavailable: {exc}", "error")
    return html.Section(
        [
            dcc.Store(id="feeds-action-store", data=None),
            dcc.Store(id="feeds-list-signature", data=_signature(rows)),
            html.Div(
                [
                    html.Div(
                        [
                            html.H2("Saved feeds"),
                            html.P(
                                "Refresh public news, RSS feeds, "
                                "and platform sources on a schedule.",
                                className="muted small",
                            ),
                        ]
                    ),
                    pill("Worker schedules"),
                ],
                className="section-heading",
            ),
            *(
                [
                    html.P(
                        "This snapshot is read-only. "
                        "Saved-feed schedules and actions are disabled.",
                        className="muted small saved-feed-note",
                    )
                ]
                if read_only
                else []
            ),
            html.Details(
                [html.Summary("Add a saved feed"), _feed_form(read_only, source_options(service))],
                open=not rows and not read_only,
                className="saved-feed-form",
            ),
            html.Div(id="saved-feeds-feedback", role="status", **{"aria-live": "polite"}),
            html.Div(
                _scheduler_alert(service),
                id="saved-feed-scheduler-alert",
                role="status",
                **{"aria-live": "polite"},
            ),
            html.Div(listing, id="feeds-list"),
        ],
        id="saved-feeds",
        className="panel saved-feeds-panel",
    )


def feed_cards(rows: list[dict], read_only: bool):
    if not rows:
        return html.Div(
            [
                html.Strong(
                    "No saved feeds in this snapshot" if read_only else "No saved feeds yet"
                ),
                html.P(
                    "Save a public feed or platform source to collect future updates automatically."
                    if not read_only
                    else "Saved articles remain available to explore."
                ),
            ],
            className="empty-state saved-feeds-empty",
        )
    cards = []
    for row in rows[:DISPLAY_LIMIT]:
        enabled = bool(row.get("enabled"))
        feed_id = row["id"]
        action = "pause" if enabled else "resume"
        result = row.get("last_result")
        errors = result.get("errors") or [] if isinstance(result, dict) else []
        current_analysis_id = result.get("analysis_job_id") if isinstance(result, dict) else None
        analysis_id = row.get("last_analysis_job_id")
        if analysis_id is None:
            analysis_id = current_analysis_id
        content_mode = row.get("content_mode", "article_links")
        platform = row.get("platform", "rss")
        analysis_status = row.get("last_analysis_status")
        analysis_label = (
            "Last local analysis"
            if row.get("last_error") and analysis_id != current_analysis_id
            else "Local analysis"
        )
        cards.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.H3(row.get("name") or "Saved feed"),
                                    source_link(row.get("url", ""), row.get("url", "Feed URL")),
                                ]
                            ),
                            pill(
                                "Enabled" if enabled else "Paused",
                                "green" if enabled else "neutral",
                            ),
                        ],
                        className="saved-feed-header",
                    ),
                    html.P(
                        f"Every {row.get('interval_minutes', 60)} minutes · "
                        f"up to {row.get('item_limit', 5)} items · {row.get('topic') or 'General'}",
                        className="muted small",
                    ),
                    html.Div(
                        [
                            pill(
                                PLATFORMS.get(platform, platform)
                                if platform != "rss"
                                else "Feed post content"
                                if content_mode == "feed_content"
                                else "Linked news pages"
                            ),
                            pill("Local analysis on" if row.get("analyze") else "Import only"),
                        ],
                        className="badge-row saved-feed-badges",
                    ),
                    *card_source_details(row),
                    html.Dl(
                        [
                            html.Dt("Next refresh"),
                            html.Dd(
                                _timestamp(row.get("next_refresh_at"), "Waiting to schedule")
                                if enabled
                                else "Paused"
                            ),
                            html.Dt("Last successful refresh"),
                            html.Dd(_timestamp(row.get("last_refreshed_at"), "Not refreshed yet")),
                            *(
                                [
                                    html.Dt("Last attempt"),
                                    html.Dd(
                                        _timestamp(row["last_finished_at"], "Time unavailable")
                                    ),
                                ]
                                if row.get("last_finished_at")
                                else []
                            ),
                        ],
                        className="saved-feed-times",
                    ),
                    *(
                        [
                            html.P(
                                "Refresh skipped: "
                                + str(result.get("skip_reason") or "This refresh was skipped.")
                                if result.get("skipped") is True
                                else "Last import: " + import_counts(result),
                                className="small saved-feed-result",
                            )
                        ]
                        if isinstance(result, dict)
                        else []
                    ),
                    *(
                        [_feedback(str(row["last_error"]), "error")]
                        if row.get("last_error")
                        else []
                    ),
                    *(
                        [
                            html.Div(
                                [
                                    html.Strong(f"{len(errors)} source(s) were not imported"),
                                    source_error_list(errors),
                                ],
                                className="job-import-errors",
                            )
                        ]
                        if errors
                        else []
                    ),
                    *(
                        [
                            html.A(
                                f"{analysis_label} job #{analysis_id} →",
                                href=f"#job-{analysis_id}",
                                className="small source-link",
                            )
                        ]
                        if analysis_id is not None
                        else []
                    ),
                    *(
                        [
                            html.P(
                                [
                                    f"{analysis_label}: ",
                                    pill(
                                        str(analysis_status).title(),
                                        "warning"
                                        if analysis_status == "failed"
                                        else "green"
                                        if analysis_status == "succeeded"
                                        else "neutral",
                                    ),
                                ],
                                className="small saved-feed-analysis-status",
                            )
                        ]
                        if analysis_status
                        else []
                    ),
                    *(
                        [
                            _feedback(
                                f"{analysis_label} failed: {row['last_analysis_error']}", "error"
                            )
                        ]
                        if analysis_status == "failed" and row.get("last_analysis_error")
                        else []
                    ),
                    html.Div(
                        [
                            html.Button(
                                "Refresh now",
                                id={
                                    "type": "saved-feed-action",
                                    "feed_id": feed_id,
                                    "action": "refresh",
                                },
                                n_clicks=0,
                                disabled=read_only or row.get("ready") is False,
                                className="button saved-feed-button",
                            ),
                            html.Button(
                                "Pause" if enabled else "Resume",
                                id={
                                    "type": "saved-feed-action",
                                    "feed_id": feed_id,
                                    "action": action,
                                },
                                n_clicks=0,
                                disabled=read_only,
                                className="text-button",
                            ),
                        ],
                        className="saved-feed-actions",
                    ),
                ],
                className="saved-feed-card",
            )
        )
    return [
        html.P(
            f"{len(cards)} saved feed(s) shown · latest {DISPLAY_LIMIT} maximum",
            className="muted small saved-feed-list-note",
        ),
        *cards,
    ]


def _clicked_action(clicks, identifiers) -> dict | None:
    trigger = ctx.triggered_id
    if not isinstance(trigger, dict):
        return None
    for identifier, count in zip(identifiers or [], clicks or [], strict=False):
        if identifier == trigger and count:
            return trigger
    return None


def register_callbacks(app: Dash, service: ExplorerService) -> None:
    @app.callback(
        Output("feeds-action-store", "data"),
        Output("saved-feeds-feedback", "children"),
        Input("saved-feed-save", "n_clicks"),
        Input(ACTION_ID, "n_clicks"),
        State("saved-feed-name", "value"),
        State("saved-feed-url", "value"),
        State("saved-feed-topic", "value"),
        State("saved-feed-interval", "value"),
        State("saved-feed-limit", "value"),
        State("saved-feed-content-mode", "value"),
        State("saved-feed-analyze", "value"),
        State(ACTION_ID, "id"),
        prevent_initial_call=True,
    )
    def mutate(_save, clicks, name, url, topic, interval, limit, mode, analysis, identifiers):
        try:
            trigger = ctx.triggered_id
            action = _clicked_action(clicks, identifiers)
            if trigger != "saved-feed-save" and action is None:
                return no_update, no_update
            if service.settings.read_only:
                return no_update, _feedback(
                    "Saved-feed actions are disabled in this read-only snapshot."
                )
            if trigger == "saved-feed-save":
                row = service.save_feed(
                    name=(name or "").strip(),
                    url=(url or "").strip(),
                    topic=(topic or "").strip() or "General",
                    interval_minutes=60 if interval is None else interval,
                    item_limit=5 if limit is None else limit,
                    content_mode=mode,
                    analyze="analyze" in (analysis or []),
                )
                return {"revision": uuid4().hex, "feed_id": row["id"]}, _feedback(
                    "Feed saved. The worker follows its refresh schedule."
                    if row.get("enabled", True)
                    else "Feed settings saved. This feed remains paused.",
                    "success",
                )
            feed_id = action["feed_id"]
            if action["action"] == "refresh":
                job_id = service.enqueue_feed_refresh(feed_id)
                message = html.Div(
                    [
                        "Refresh queued. ",
                        html.A(f"Follow import job #{job_id} →", href=f"#job-{job_id}"),
                    ],
                    className="notice notice--success",
                )
            elif action["action"] in {"pause", "resume"}:
                enabled = action["action"] == "resume"
                service.set_feed_enabled(feed_id, enabled)
                message = _feedback("Feed resumed." if enabled else "Feed paused.", "success")
            else:
                raise ValueError("Unknown saved-feed action.")
            return {"revision": uuid4().hex, "feed_id": feed_id}, message
        except Exception as exc:
            logger.exception("Could not update saved feed")
            return no_update, _feedback(f"Could not update this feed: {exc}", "error")

    @app.callback(
        Output("feeds-list", "children"),
        Output("feeds-list-signature", "data"),
        Output("saved-feed-scheduler-alert", "children"),
        Input("poll", "n_intervals"),
        Input("feeds-action-store", "data"),
        State("feeds-list-signature", "data"),
    )
    def refresh_list(_poll, _action, previous):
        try:
            rows = service.list_feeds(limit=DISPLAY_LIMIT)
            signature = _signature(rows)
            alert = _scheduler_alert(service)
            if signature == previous:
                return no_update, no_update, alert
            return feed_cards(rows, service.settings.read_only), signature, alert
        except Exception as exc:
            logger.exception("Could not refresh saved feeds")
            return (
                _feedback(f"Saved feeds are unavailable: {exc}", "error"),
                no_update,
                _scheduler_alert(service),
            )

    @app.callback(
        Output("saved-feed-mode-help", "children"), Input("saved-feed-content-mode", "value")
    )
    def mode_help(mode):
        return (
            "Imports each post's full content or summary directly from the public RSS/Atom feed."
            if mode == "feed_content"
            else "Fetches and extracts news pages linked by the RSS/Atom feed."
        )

    register_platform_callbacks(app, service)


def _feedback(text: str, tone: str = "neutral"):
    return html.Div(text, className=f"notice notice--{tone}")


def _scheduler_alert(service: ExplorerService):
    try:
        error = service.feed_scheduler_status().get("error")
        if not error:
            return ""
        prefix = (
            "Saved scheduler message: "
            if service.settings.read_only
            else ("Saved-feed scheduler needs attention: ")
        )
        return _feedback(prefix + str(error), "error")
    except Exception as exc:
        logger.exception("Could not read saved-feed scheduler status")
        return _feedback(f"Scheduler status is unavailable: {exc}", "error")
