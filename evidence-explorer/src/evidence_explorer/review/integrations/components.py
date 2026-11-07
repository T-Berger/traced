"""Page-local controls for case imports, portable exports, and specialist replay.

The shared review shell owns navigation and assessment state. These components
only provide stable control IDs for the integration callbacks.
"""

from __future__ import annotations

from dash import dcc, html

from .packages import MAX_PACKAGE_BYTES


def case_tools() -> html.Section:
    """Offer case import on Cases without adding actions to the review screen."""
    from ...investigation.intake_ui import manual_draft_panel, saved_investigation_tools
    from .activity_ui import activity_panel
    from .brief_editor_ui import planning_brief_panel
    from .collection_ui import collection_tools
    from .sources_ui import source_import_tools
    from .use_cases_ui import use_cases_panel

    return html.Section(
        [
            html.P(
                "Plan investigation → Gather evidence → Confirm scope → Investigate findings "
                "→ Review conclusions → Prepare and share briefing.",
                className="rv-small",
            ),
            html.Details(
                [html.Summary("Investigation plan"), planning_brief_panel()],
                id="trail-start-panel",
                className="rv-details rv-investigation-step",
            ),
            html.Details(
                [
                    html.Summary("Gather evidence"),
                    html.P(
                        "Collect selected public sources or import saved evidence. After saving "
                        "a case, open your saved plan, attach its evidence and confirm the scope.",
                        className="rv-small",
                    ),
                    collection_tools(),
                    html.Div(
                        [
                            dcc.Upload(
                                id="trail-upload",
                                accept=".zip,.json",
                                max_size=MAX_PACKAGE_BYTES,
                                multiple=False,
                                children=html.Button(
                                    "Import case package",
                                    className="rv-button rv-button--outline",
                                ),
                            ),
                            html.Button(
                                "Import Explorer collection",
                                id="trail-explorer",
                                n_clicks=0,
                                className="rv-button rv-button--outline",
                            ),
                        ],
                        className="rv-toolbar",
                    ),
                    html.P(
                        "Case packages: ZIP or JSON, up to 64 MB.", className="rv-small rv-muted"
                    ),
                    html.Div(id="trail-import-message", role="status", **{"aria-live": "polite"}),
                    source_import_tools(),
                    saved_investigation_tools(),
                ],
                id="trail-gather-evidence",
                className="rv-details rv-investigation-step",
            ),
            manual_draft_panel(),
            html.Details(
                [
                    html.Summary("Compare activity over time (optional)"),
                    html.P(
                        "Requires saved coverage for both time windows. Successful collection "
                        "alone is not enough. You can inspect evidence and prepare a briefing "
                        "without this comparison; it does not enable alerts or notifications.",
                        className="rv-small",
                    ),
                    activity_panel(),
                ],
                className="rv-details rv-investigation-step",
            ),
            html.Details(
                [html.Summary("Explore workflows (optional)"), use_cases_panel()],
                className="rv-details rv-investigation-step",
            ),
        ],
        **{"aria-label": "Start investigation and gather evidence"},
    )


def export_tools() -> html.Section:
    """Keep the readable brief and portable case package together on Brief."""
    from .handoff_ui import handoff_panel

    return html.Section(
        [
            handoff_panel(),
            html.Div(
                [
                    html.Button(
                        "Download case package",
                        id="trail-package",
                        n_clicks=0,
                        className="rv-button rv-button--primary",
                    ),
                    html.Button(
                        "Download brief",
                        id="trail-brief",
                        n_clicks=0,
                        className="rv-button rv-button--outline",
                    ),
                ],
                className="rv-toolbar",
            ),
            html.Div(id="trail-export-message", role="status", **{"aria-live": "polite"}),
            html.Details(
                [
                    html.Summary("Advanced: evidence JSON"),
                    html.Button(
                        "Download evidence JSON",
                        id="trail-json",
                        n_clicks=0,
                        className="rv-button rv-button--outline",
                    ),
                ],
                className="rv-details",
            ),
            dcc.Download(id="trail-download"),
            dcc.Store(id="trail-export-attempt", storage_type="memory"),
        ],
        **{"aria-label": "Export the reviewed assessment"},
    )


def comparison_tools() -> html.Section:
    """Reveal specialist replay on demand while keeping progress messages visible."""
    from ...social_dashboard.events_ui import event_clusters_panel
    from ...social_dashboard.ui import dashboard_panel
    from .sources_ui import source_browser

    return html.Section(
        [
            source_browser(),
            html.Details(
                [
                    html.Summary("Specialist analysis and replay"),
                    html.P(
                        "Behavioral cases can rerun analysis when the complete input context "
                        "is available. A source exclusion alone shows dependency impact.",
                        className="rv-small rv-muted",
                    ),
                    html.Div(
                        [
                            html.Button(
                                "Inspect strongest episode",
                                id="trail-episode",
                                n_clicks=0,
                                disabled=True,
                                className="rv-button rv-button--outline",
                            ),
                            html.Button(
                                "Recompute Swarm scenario",
                                id="trail-recompute",
                                n_clicks=0,
                                disabled=True,
                                className="rv-button rv-button--outline",
                            ),
                            html.Button(
                                "Return to baseline",
                                id="trail-original",
                                n_clicks=0,
                                className="rv-button rv-button--outline",
                            ),
                            html.Button(
                                "Review result",
                                id="trail-result",
                                n_clicks=0,
                                disabled=True,
                                className="rv-button rv-button--outline",
                            ),
                        ],
                        className="rv-toolbar",
                    ),
                ],
                className="rv-details",
            ),
            html.Div(id="trail-operation-message", role="status", **{"aria-live": "polite"}),
            html.Div(id="trail-comparison", **{"aria-live": "polite"}),
            dcc.Interval(id="trail-tick", interval=1800, disabled=True),
            dashboard_panel(),
            event_clusters_panel(),
        ],
        **{"aria-label": "Specialist scenario comparison"},
    )
