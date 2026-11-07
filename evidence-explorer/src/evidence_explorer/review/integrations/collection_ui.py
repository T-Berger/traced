"""Modular live-source controls; collectors and durable jobs stay in the service.

Rendering consumes source summaries. User actions only save configuration, queue
collection, or open an immutable evidence snapshot. Credentials stay on the server.
"""

from __future__ import annotations

from dash import ALL, Dash, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from ..presentation import badge, empty
from .sources import PLATFORM_LABELS

_SETUP_LINKS = {
    "x": ("X developer setup", "https://console.x.com"),
    "reddit": ("Reddit app setup", "https://www.reddit.com/prefs/apps/"),
    "supabase": ("Supabase connection setup", "https://supabase.com/dashboard"),
}
_STATUS_LABELS = {
    "idle": "Not collected yet",
    "queued": "Waiting for collection",
    "running": "Collecting",
    "succeeded": "Collected",
    "failed": "Collection failed",
    "cancelled": "Collection cancelled",
}
_ACTIVE = {"queued", "running"}


def _notice(message: str, *, error: bool = False):
    return html.Div(
        message,
        className="rv-notice rv-notice--error" if error else "rv-notice",
        role="alert" if error else "status",
    )


def _field(label: str, field):
    return html.Div(
        [html.Label(label, htmlFor=field.id), field],
        style={"minWidth": 0, "flex": "1 1 220px"},
    )


def source_picker():
    """A shared form for presets and custom sources, independent of each collector."""
    input_style = {
        "width": "100%",
        "minHeight": "var(--traced-layout-control-height)",
        "border": "1px solid var(--rv-input-border)",
        "borderRadius": "6px",
        "color": "var(--rv-ink)",
        "backgroundColor": "var(--rv-surface)",
        # Dash uses these roles on the wrapper focus ring and selected text.
        "--Dash-Fill-Interactive-Strong": "var(--rv-focus)",
        "--Dash-Fill-Inverse-Strong": "var(--rv-surface)",
    }
    number_style = {
        **input_style,
        "--Dash-Text-Primary": "var(--rv-ink)",
        "--Dash-Stroke-Weak": "var(--rv-border)",
        "--Dash-Fill-Primary-Hover": "var(--rv-selected)",
        "--Dash-Fill-Primary-Active": "var(--rv-selected)",
    }
    return html.Div(
        [
            _field(
                "Choose a source",
                dcc.Dropdown(
                    id="live-preset",
                    options=[],
                    value=None,
                    placeholder="Choose a channel or publisher",
                    clearable=True,
                ),
            ),
            html.Div(id="live-preset-status", role="status", **{"aria-live": "polite"}),
            html.Details(
                [
                    html.Summary("Source details or a custom source"),
                    html.Div(
                        [
                            _field(
                                "Platform",
                                dcc.Dropdown(
                                    id="live-platform",
                                    options=[
                                        {"label": label, "value": key}
                                        for key, label in PLATFORM_LABELS.items()
                                    ],
                                    value="news",
                                    clearable=False,
                                ),
                            ),
                            _field(
                                "Source name",
                                dcc.Input(
                                    id="live-label",
                                    type="text",
                                    value="",
                                    maxLength=150,
                                    style=input_style,
                                ),
                            ),
                            _field(
                                "Channel, account, feed URL, or table",
                                dcc.Input(
                                    id="live-locator",
                                    type="text",
                                    value="",
                                    maxLength=2000,
                                    style=input_style,
                                ),
                            ),
                        ],
                        style={"display": "flex", "flexWrap": "wrap", "gap": "16px"},
                    ),
                    html.P(
                        "Telegram: public channel name. X: account name. Reddit: community name. "
                        "YouTube: channel ID or channel URL. News and other: public RSS/Atom URL. "
                        "Supabase: schema.table, such as public.content_items. "
                        "Database credentials are configured on the server.",
                        className="rv-small rv-muted",
                    ),
                    _field(
                        "Feed content",
                        dcc.Dropdown(
                            id="live-content-mode",
                            options=[
                                {"label": "Text supplied by the feed", "value": "feed_content"},
                                {"label": "Read linked news articles", "value": "article_links"},
                            ],
                            value="feed_content",
                            clearable=False,
                        ),
                    ),
                ],
                className="rv-details",
            ),
            html.Div(
                [
                    _field(
                        "Items per collection",
                        dcc.Input(
                            id="live-item-limit",
                            type="number",
                            min=1,
                            max=20,
                            step=1,
                            value=5,
                            style=number_style,
                        ),
                    ),
                    _field(
                        "Refresh every (minutes)",
                        dcc.Input(
                            id="live-interval",
                            type="number",
                            min=5,
                            max=10080,
                            step=1,
                            value=15,
                            style=number_style,
                        ),
                    ),
                ],
                style={"display": "flex", "flexWrap": "wrap", "gap": "16px"},
            ),
            dcc.Checklist(
                id="live-scheduled",
                options=[{"label": "Keep collecting on this schedule", "value": "scheduled"}],
                value=[],
                labelStyle={"color": "var(--rv-ink)"},
                inputStyle={"accentColor": "var(--rv-focus)"},
                style={
                    "--Dash-Text-Strong": "var(--rv-ink)",
                    "--Dash-Fill-Interactive-Strong": "var(--rv-focus)",
                    "--Dash-Stroke-Strong": "var(--rv-input-border)",
                },
            ),
            html.Button(
                "Add & collect",
                id="live-add",
                n_clicks=0,
                className="rv-button rv-button--primary",
            ),
        ],
        style={"display": "grid", "gap": "12px"},
    )


def collection_tools():
    """Stable form and projection slots that can be composed into any workspace."""
    from .collection_scope_ui import scope_tools
    from .external_sources_ui import external_source_tools

    return html.Section(
        [
            html.H2("Live sources"),
            html.P(
                "Collect public posts, video titles and descriptions, and news into one "
                "workspace. Choose a source below or add your own.",
                className="rv-small rv-muted",
            ),
            html.P(
                "Collection and automatic refresh run in the background worker. "
                "The app saves source text and dates for review.",
                className="rv-small rv-muted",
            ),
            source_picker(),
            html.Div(id="live-action", role="status", **{"aria-live": "polite"}),
            html.Div(id="live-summary", role="status", **{"aria-live": "polite"}),
            html.Div(
                [
                    html.Button(
                        "Collect all ready sources",
                        id="live-collect-all",
                        n_clicks=0,
                        disabled=True,
                        className="rv-button rv-button--outline",
                    ),
                    html.Button(
                        "Choose sources for a case",
                        id="live-open",
                        n_clicks=0,
                        disabled=True,
                        className="rv-button rv-button--primary",
                    ),
                ],
                className="rv-toolbar",
            ),
            html.Div(id="live-open-message", role="status", **{"aria-live": "polite"}),
            scope_tools(),
            external_source_tools(),
            html.Div(id="live-source-list"),
            dcc.Store(id="live-revision", storage_type="memory", data=0),
            dcc.Interval(id="live-poll", interval=2500, disabled=True),
        ],
        className="rv-panel",
        style={"display": "grid", "gap": "12px", "marginBottom": "24px"},
        **{"aria-label": "Live multi-platform collection"},
    )


def source_card(source: dict):
    """Render supplied state without deciding collection policy or reading credentials."""
    ready = bool(source.get("ready", False))
    status = source.get("last_status", "idle")
    active = status in _ACTIVE
    enabled = bool(source.get("enabled"))
    managed = source.get("managed_by") == "saved_feed"
    label = _STATUS_LABELS.get(status, "Status unavailable")
    tone = (
        "amber" if active or status == "failed" else "green" if status == "succeeded" else "neutral"
    )
    platform = source.get("platform", "other")
    setup = _SETUP_LINKS.get(platform)
    return html.Article(
        [
            html.Div(
                [badge(PLATFORM_LABELS.get(platform, "Other")), badge(label, tone)],
                className="rv-badges",
            ),
            html.H3(source.get("label") or "Saved source"),
            html.P(
                f"{source.get('item_count', 0)} saved records · "
                f"Last collected: {source.get('last_collected_at') or 'Not yet'}",
                className="rv-small rv-muted",
            ),
            html.P(
                (
                    "Refresh is managed in Saved feeds."
                    if managed
                    else (
                        f"Automatic refresh every {source.get('interval_minutes', 15)} minutes. "
                        f"Next due: {source.get('next_due_at') or 'Waiting for the worker'}."
                        if enabled
                        else (
                            "Automatic refresh paused. Collect now remains available."
                            if ready
                            else "Automatic refresh is paused until this source is connected."
                        )
                    )
                ),
                className="rv-small rv-muted",
            ),
            *(
                [
                    _notice(source.get("connection_status") or "Connection setup is required."),
                    *(
                        [
                            html.A(
                                setup[0], href=setup[1], target="_blank", rel="noopener noreferrer"
                            )
                        ]
                        if setup
                        else []
                    ),
                ]
                if not ready
                else []
            ),
            *(
                [_notice(str(source["last_error"]), error=status == "failed")]
                if source.get("last_error")
                else []
            ),
            html.Div(
                [
                    html.Button(
                        "Collecting…" if active else "Collect now",
                        id={"type": "live-collect", "sid": source["id"]},
                        n_clicks=0,
                        disabled=not ready or active,
                        className="rv-button rv-button--outline",
                    ),
                    html.Button(
                        "Pause refresh" if enabled else "Enable refresh",
                        id={"type": "live-toggle", "sid": source["id"]},
                        n_clicks=0,
                        disabled=managed or (not ready and not enabled),
                        className="rv-button rv-button--quiet",
                    ),
                ],
                className="rv-toolbar",
            ),
            html.Details(
                [
                    html.Summary("Source details"),
                    html.P(str(source.get("locator") or "Unknown source")),
                    html.P(f"Up to {source.get('item_limit', 5)} items per collection."),
                ],
                className="rv-details",
            ),
        ],
        className="rv-case-card",
        style={"marginTop": "12px"},
    )


def register_collection_callbacks(app: Dash, service) -> None:
    @app.callback(
        Output("live-preset", "options"),
        Input("live-revision", "data"),
    )
    def presets(_revision):
        return [
            {
                "label": (
                    f"{PLATFORM_LABELS.get(row['platform'], 'Other')} · {row['label']}"
                    + (" · setup needed" if not row.get("ready") else "")
                ),
                "value": row["id"],
            }
            for row in service.collection_catalog()
        ]

    @app.callback(
        Output("live-platform", "value"),
        Output("live-label", "value"),
        Output("live-locator", "value"),
        Output("live-content-mode", "value"),
        Output("live-preset-status", "children"),
        Input("live-preset", "value"),
        prevent_initial_call=True,
    )
    def choose_preset(selected):
        row = next((item for item in service.collection_catalog() if item["id"] == selected), None)
        if row is None:
            return (
                no_update,
                no_update,
                no_update,
                no_update,
                "Enter a custom source in Source details.",
            )
        return (
            row["platform"],
            row["label"],
            row["locator"],
            row.get("content_mode", "feed_content"),
            html.P(
                row.get("status") or "Source readiness has not been checked.",
                className="rv-small rv-muted",
            ),
        )

    @app.callback(
        Output("live-revision", "data"),
        Output("live-action", "children"),
        Input("live-add", "n_clicks"),
        Input("live-collect-all", "n_clicks"),
        Input({"type": "live-collect", "sid": ALL}, "n_clicks"),
        Input({"type": "live-toggle", "sid": ALL}, "n_clicks"),
        State("live-platform", "value"),
        State("live-label", "value"),
        State("live-locator", "value"),
        State("live-interval", "value"),
        State("live-item-limit", "value"),
        State("live-content-mode", "value"),
        State("live-scheduled", "value"),
        State("live-revision", "data"),
        prevent_initial_call=True,
    )
    def act(
        add,
        collect_all,
        collects,
        toggles,
        platform,
        label,
        locator,
        interval,
        limit,
        mode,
        scheduled,
        revision,
    ):
        trigger = ctx.triggered_id
        try:
            if trigger == "live-add":
                if not add:
                    raise PreventUpdate
                source_id = service.add_collection_source(
                    {
                        "platform": platform,
                        "label": label or "",
                        "locator": locator or "",
                        "interval_minutes": interval,
                        "item_limit": limit,
                        "content_mode": mode,
                        "enabled": "scheduled" in (scheduled or []),
                    }
                )
                saved = next(
                    row for row in service.list_collection_sources() if row["id"] == source_id
                )
                if saved.get("ready"):
                    service.collect_source(source_id)
                    message = "Source added. Collection is queued in the background."
                else:
                    message = "Source saved. Complete connection setup before collecting."
            elif trigger == "live-collect-all":
                if not collect_all:
                    raise PreventUpdate
                ready = [
                    row
                    for row in service.list_collection_sources()
                    if row.get("ready") and row.get("last_status") not in _ACTIVE
                ]
                for row in ready:
                    service.collect_source(row["id"])
                message = f"Collection queued for {len(ready)} ready source(s)."
            elif isinstance(trigger, dict) and trigger.get("type") == "live-collect":
                if not any(collects or []):
                    raise PreventUpdate
                service.collect_source(trigger["sid"])
                message = "Collection queued in the background."
            elif isinstance(trigger, dict) and trigger.get("type") == "live-toggle":
                if not any(toggles or []):
                    raise PreventUpdate
                saved = next(
                    row for row in service.list_collection_sources() if row["id"] == trigger["sid"]
                )
                if saved.get("managed_by") == "saved_feed":
                    raise ValueError(
                        "This source is managed in Saved feeds. Change its schedule there."
                    )
                enabled = not saved["enabled"]
                service.set_collection_enabled(trigger["sid"], enabled)
                message = "Automatic refresh enabled." if enabled else "Automatic refresh paused."
            else:
                raise PreventUpdate
            return int(revision or 0) + 1, _notice(message)
        except PreventUpdate:
            raise
        except ValueError as exc:
            return no_update, _notice(str(exc), error=True)
        except Exception:
            return no_update, _notice(
                "The source action could not be completed. Try again.", error=True
            )

    @app.callback(
        Output("live-summary", "children"),
        Output("live-source-list", "children"),
        Output("live-poll", "disabled"),
        Output("live-poll", "interval"),
        Output("live-collect-all", "disabled"),
        Output("live-open", "disabled"),
        Input("live-revision", "data"),
        Input("live-poll", "n_intervals"),
    )
    def render(_revision, _poll):
        try:
            snapshot = service.collection_status()
            sources, jobs = snapshot.get("sources", []), snapshot.get("jobs", [])
            active = any(job.get("status") in _ACTIVE for job in jobs)
            scheduled = any(source.get("enabled") and source.get("ready") for source in sources)
            ready = any(
                source.get("ready") and source.get("last_status") not in _ACTIVE
                for source in sources
            )
            summary = f"{len(sources)} saved sources."
            if active:
                summary += " Collection is in progress; results update here automatically."
            elif scheduled:
                summary += " Automatic refresh is enabled while the background worker runs."
            elif sources:
                summary += " Collect now when you want to refresh the saved evidence."
            return (
                html.P(summary, className="rv-small rv-muted"),
                [source_card(source) for source in sources]
                or empty("No live sources yet", "Choose a preset or add a public source above."),
                not (active or scheduled),
                2500 if active else 30000,
                not ready,
                not bool(sources),
            )
        except Exception:
            return (
                _notice(
                    "Collection status is unavailable. Try a source action to refresh it.",
                    error=True,
                ),
                "",
                True,
                30000,
                True,
                True,
            )

    @app.callback(
        Output("live-case-panel", "open"),
        Output("live-open-message", "children"),
        Input("live-open", "n_clicks"),
        prevent_initial_call=True,
    )
    def choose_case_sources(clicks):
        if not clicks:
            raise PreventUpdate
        return True, "Choose and preview the sources for your new case below."

    from .collection_scope_ui import register_scope_callbacks
    from .external_sources_ui import register_external_source_callbacks

    register_scope_callbacks(app, service)
    register_external_source_callbacks(app)
