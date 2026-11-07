"""On-demand archive lookup and Aleph access controls for Live sources."""

from __future__ import annotations

from datetime import date

from dash import Dash, Input, Output, ctx, dcc, html

from ...collection.aleph import aleph_configuration, check_aleph_connection
from ...collection.models import CollectionFailure
from ...collection.wayback import lookup_wayback
from ..presentation import badge

_INPUT_STYLE = {
    "width": "100%",
    "minHeight": "var(--traced-layout-control-height)",
    "border": "1px solid var(--rv-input-border)",
    "borderRadius": "6px",
    "color": "var(--rv-ink)",
    "backgroundColor": "var(--rv-surface)",
    "--Dash-Fill-Interactive-Strong": "var(--rv-focus)",
    "--Dash-Fill-Inverse-Strong": "var(--rv-surface)",
}
_PANEL_STYLE = {"display": "grid", "gap": "12px", "minWidth": 0, "overflowWrap": "anywhere"}


def external_source_tools():
    """Compose the two tools without reading configuration or starting a request."""
    return html.Section(
        [
            html.H3("Archive and data access"),
            html.Details(
                [
                    html.Summary("Wayback Machine lookup"),
                    html.Div(
                        [
                            html.P(
                                "Find an archived capture of a public page. Add a date to look "
                                "for the closest capture, or leave it blank for the latest one.",
                                className="rv-small rv-muted",
                            ),
                            html.Div(
                                [
                                    html.Label("Public page URL", htmlFor="live-wayback-url"),
                                    dcc.Input(
                                        id="live-wayback-url",
                                        type="url",
                                        value="",
                                        placeholder="https://example.org/page",
                                        maxLength=2000,
                                        style=_INPUT_STYLE,
                                    ),
                                ]
                            ),
                            html.Div(
                                [
                                    html.Label(
                                        "Capture date (optional, YYYY-MM-DD)",
                                        htmlFor="live-wayback-date",
                                    ),
                                    dcc.Input(
                                        id="live-wayback-date",
                                        type="text",
                                        value="",
                                        placeholder="YYYY-MM-DD",
                                        maxLength=10,
                                        style=_INPUT_STYLE,
                                    ),
                                ]
                            ),
                            html.Button(
                                "Find archived capture",
                                id="live-wayback-lookup",
                                n_clicks=0,
                                className="rv-button rv-button--outline",
                            ),
                            html.Div(id="live-wayback-result", role="status"),
                        ],
                        style=_PANEL_STYLE,
                    ),
                ],
                className="rv-details",
            ),
            html.Details(
                [
                    html.Summary("Aleph access"),
                    html.Div(
                        [
                            html.P(
                                "Check whether Traced can read the collections listing on your "
                                "Aleph instance. Aleph uses the API key configured on this server.",
                                className="rv-small rv-muted",
                            ),
                            html.Div(id="live-aleph-config", role="status"),
                            html.Button(
                                "Check Aleph access",
                                id="live-aleph-check",
                                n_clicks=0,
                                className="rv-button rv-button--outline",
                            ),
                            html.Div(id="live-aleph-result", role="status"),
                        ],
                        style=_PANEL_STYLE,
                    ),
                ],
                className="rv-details",
            ),
        ],
        **{"aria-label": "Archive and data access"},
    )


def _notice(message: str, *, error: bool = False):
    return html.Div(message, className="rv-notice rv-notice--error" if error else "rv-notice")


def _capture_date(value: str | None) -> date | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        requested = date.fromisoformat(value.strip())
        if requested.isoformat() == value.strip():
            return requested
    except (AttributeError, ValueError):
        pass
    raise CollectionFailure("Enter a capture date in YYYY-MM-DD format.")


def register_external_source_callbacks(app: Dash) -> None:
    @app.callback(
        Output("live-wayback-result", "children"),
        Input("live-wayback-lookup", "n_clicks"),
        Input("live-wayback-url", "value"),
        Input("live-wayback-date", "value"),
        prevent_initial_call=True,
        running=[(Output("live-wayback-lookup", "disabled"), True, False)],
    )
    def wayback_result(clicks, url, requested_date):
        if ctx.triggered_id != "live-wayback-lookup" or not clicks:
            return ""
        if not isinstance(url, str) or not url.strip():
            return _notice("Enter a public page URL, then find an archived capture.", error=True)
        try:
            result = lookup_wayback(url.strip(), _capture_date(requested_date))
        except CollectionFailure as exc:
            return _notice(str(exc), error=True)
        except Exception:
            return _notice("The Wayback lookup could not be completed. Try again.", error=True)
        return html.Div(
            [
                html.P(
                    "Archived capture found." if result.found else "No archived capture was found."
                ),
                html.Dl(
                    [
                        html.Dt("Public page"),
                        html.Dd(result.original_url, className="rv-mono"),
                        html.Dt("Capture time"),
                        html.Dd(
                            html.Time(result.captured_at, dateTime=result.captured_at)
                            if result.captured_at
                            else "Unavailable",
                            className="rv-mono",
                        ),
                        html.Dt("Lookup checked at"),
                        html.Dd(
                            html.Time(result.checked_at, dateTime=result.checked_at),
                            className="rv-mono",
                        ),
                    ]
                ),
                *(
                    [
                        html.A(
                            "Open archived capture (new tab)",
                            href=result.archive_url,
                            target="_blank",
                            rel="noopener noreferrer",
                        )
                    ]
                    if result.found
                    else []
                ),
            ],
            className="rv-notice",
        )

    @app.callback(
        Output("live-aleph-config", "children"),
        Output("live-aleph-result", "children"),
        Input("live-aleph-check", "n_clicks"),
        running=[(Output("live-aleph-check", "disabled"), True, False)],
    )
    def aleph_result(clicks):
        try:
            configuration = aleph_configuration()
        except Exception:
            return _notice("Aleph setup could not be read. Try again.", error=True), ""
        setup = [
            html.P(["Aleph instance ", html.Span(configuration.instance_url, className="rv-mono")])
            if configuration.instance_url
            else html.P("Aleph instance unavailable."),
            badge(
                "Ready to check" if configuration.ready else "Setup required",
                "neutral" if configuration.ready else "amber",
            ),
            html.P(configuration.message, className="rv-small"),
        ]
        if not clicks or not configuration.ready:
            return setup, ""
        try:
            result = check_aleph_connection()
        except CollectionFailure as exc:
            return setup, _notice(str(exc), error=True)
        except Exception:
            return setup, _notice("Aleph access could not be checked. Try again.", error=True)
        return setup, _notice(result.message, error=result.status != "connected")
