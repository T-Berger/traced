"""Platform-specific saved-feed controls, with server-side configuration checks."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from dash import Input, Output, State, dcc, html, no_update

from .ui import pill

if TYPE_CHECKING:
    from dash import Dash

    from .service import ExplorerService

logger = logging.getLogger(__name__)
PLATFORMS = {
    "rss": "RSS / Atom",
    "telegram": "Telegram",
    "youtube": "YouTube",
    "reddit": "Reddit",
    "x": "X",
}
LOCATOR_FIELDS = {
    "telegram": ("Public channel", "@channel or https://t.me/channel"),
    "youtube": ("Channel ID or channel URL", "UC… or https://www.youtube.com/channel/UC…"),
    "reddit": ("Community", "r/community or https://www.reddit.com/r/community/"),
    "x": ("Account", "@account or https://x.com/account"),
}
RSS_FALLBACK = [
    {
        "platform": "rss",
        "label": "RSS / Atom",
        "help": "Use a public RSS or Atom URL.",
        "ready": True,
        "reason": "Public feeds require no platform credentials.",
    }
]


def source_options(service: ExplorerService) -> list[dict]:
    try:
        return service.feed_source_options()
    except Exception:
        logger.exception("Could not read source configuration")
        return RSS_FALLBACK


def platform_selector(options: list[dict], read_only: bool):
    return html.Div(
        [
            html.Label("Source platform", htmlFor="saved-feed-platform"),
            dcc.Dropdown(
                id="saved-feed-platform",
                options=[
                    {"label": option["label"], "value": option["platform"]} for option in options
                ],
                value="rss",
                clearable=False,
                searchable=False,
                disabled=read_only,
            ),
        ],
        className="saved-feed-platform-field",
    )


def locator_field():
    return html.Div(
        [
            html.Label(
                "Public source", id="saved-feed-locator-label", htmlFor="saved-feed-locator"
            ),
            dcc.Input(
                id="saved-feed-locator",
                type="text",
                value="",
                maxLength=2000,
                placeholder="Enter a public source",
                disabled=True,
                className="search-input",
            ),
        ],
        id="saved-feed-locator-field",
        style={"display": "none"},
        className="saved-feed-locator-field",
    )


def platform_save_button(read_only: bool):
    return html.Button(
        "Save source →",
        id="saved-platform-save",
        n_clicks=0,
        disabled=read_only,
        style={"display": "none"},
        className="button button--primary",
    )


def setup_links(platform: str):
    links = {
        "reddit": [
            (
                "Official Reddit access policy",
                "https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy",
            ),
            (
                "Reddit OAuth requirements",
                "https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki",
            ),
        ],
        "x": [("Official X API setup", "https://docs.x.com/x-api/getting-started/about-x-api")],
    }
    return [
        html.A(label, href=url, target="_blank", rel="noopener noreferrer", className="source-link")
        for label, url in links.get(platform, [])
    ]


def connection_status(platform: str, ready: bool, reason: str | None):
    return html.Div(
        [
            pill(
                "Configuration ready" if ready else "Needs server setup",
                "green" if ready else "warning",
            ),
            html.Span(reason or "Configuration is checked on the server."),
            *(
                [
                    html.Span(
                        "Credentials stay on the server. Configuration does not confirm API access."
                    ),
                    *setup_links(platform),
                ]
                if platform in {"reddit", "x"}
                else []
            ),
        ],
        className="saved-feed-connection",
    )


def card_source_details(row: dict):
    platform = row.get("platform", "rss")
    if platform == "rss":
        return []
    return [connection_status(platform, row.get("ready") is not False, row.get("readiness_reason"))]


def _view(options: list[dict], platform: str, read_only: bool):
    option = next((item for item in options if item["platform"] == platform), None)
    rss = platform == "rss"
    valid = option is not None
    option = option or {"ready": False, "reason": "Choose a supported source platform.", "help": ""}
    label, placeholder = LOCATOR_FIELDS.get(platform, ("Public source", "Enter a public source"))
    hidden = {"display": "none"}
    return (
        {} if rss else hidden,
        read_only or not rss,
        hidden if rss else {},
        read_only or rss or not valid,
        label,
        placeholder,
        {} if rss else hidden,
        read_only or not rss,
        {} if rss else hidden,
        {} if rss else hidden,
        hidden if rss else {},
        read_only or not valid or not option.get("ready", False),
        option.get("help", ""),
        connection_status(platform, bool(option.get("ready")), option.get("reason")),
    )


def register_platform_callbacks(app: Dash, service: ExplorerService) -> None:
    @app.callback(
        Output("saved-feed-url-field", "style"),
        Output("saved-feed-url", "disabled"),
        Output("saved-feed-locator-field", "style"),
        Output("saved-feed-locator", "disabled"),
        Output("saved-feed-locator-label", "children"),
        Output("saved-feed-locator", "placeholder"),
        Output("saved-feed-mode-field", "style"),
        Output("saved-feed-content-mode", "disabled"),
        Output("saved-feed-mode-help", "style"),
        Output("saved-feed-save", "style"),
        Output("saved-platform-save", "style"),
        Output("saved-platform-save", "disabled"),
        Output("saved-feed-source-help", "children"),
        Output("saved-feed-connection-status", "children"),
        Input("saved-feed-platform", "value"),
    )
    def choose_source(platform):
        return _view(source_options(service), platform, service.settings.read_only)

    @app.callback(
        Output("feeds-action-store", "data", allow_duplicate=True),
        Output("saved-feeds-feedback", "children", allow_duplicate=True),
        Input("saved-platform-save", "n_clicks"),
        State("saved-feed-platform", "value"),
        State("saved-feed-locator", "value"),
        State("saved-feed-name", "value"),
        State("saved-feed-topic", "value"),
        State("saved-feed-interval", "value"),
        State("saved-feed-limit", "value"),
        State("saved-feed-analyze", "value"),
        prevent_initial_call=True,
    )
    def save_source(clicks, platform, locator, name, topic, interval, limit, analysis):
        if not clicks:
            return no_update, no_update
        if service.settings.read_only:
            return no_update, _notice(
                "Saved-source actions are disabled in this read-only snapshot."
            )
        try:
            if platform not in LOCATOR_FIELDS:
                raise ValueError("Choose a supported source platform.")
            option = next(
                (item for item in source_options(service) if item["platform"] == platform), None
            )
            if not option or not option.get("ready"):
                return no_update, _notice(
                    (option or {}).get("reason") or "Configure this source on the server first.",
                    "error",
                )
            row = service.save_feed(
                name=(name or "").strip(),
                url="",
                platform=platform,
                locator=(locator or "").strip(),
                topic=(topic or "").strip() or "General",
                interval_minutes=60 if interval is None else interval,
                item_limit=5 if limit is None else limit,
                content_mode="feed_content",
                analyze="analyze" in (analysis or []),
            )
            return {"revision": uuid4().hex, "feed_id": row["id"]}, _notice(
                "Source saved. The worker follows its refresh schedule."
                if row.get("enabled", True)
                else "Source settings saved. This source remains paused.",
                "success",
            )
        except Exception as exc:
            logger.exception("Could not save platform source")
            return no_update, _notice(f"Could not save this source: {exc}", "error")


def _notice(text: str, tone: str = "neutral"):
    return html.Div(text, className=f"notice notice--{tone}")
