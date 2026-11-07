"""Saved-source imports and a filtered, read-only cross-platform evidence browser."""

from __future__ import annotations

import json

from dash import Dash, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from ..controller import session_for_case
from ..import_ui import preview_card
from ..importing import MAX_IMPORT_BYTES
from ..presentation import badge, empty, record_button
from .sources import source_browser_data


def source_import_tools() -> html.Details:
    """Keep source content in the form and validation identity in a memory store."""
    return html.Details(
        [
            html.Summary("Import saved sources"),
            html.P(
                "Combine saved Telegram, X, Reddit, YouTube, and news records in one case. "
                "Use the source example to prepare your file. Collect public articles and "
                "RSS feeds in Explorer, then import its collection here.",
                className="rv-small rv-muted",
            ),
            html.P(
                "This imports saved text and source details. It does not connect to social "
                "accounts or fetch videos.",
                className="rv-small rv-muted",
            ),
            html.Div(
                [
                    html.Div(
                        [
                            dcc.Upload(
                                id="trail-source-upload",
                                accept=".json,application/json",
                                max_size=MAX_IMPORT_BYTES,
                                multiple=False,
                                children=html.Div(
                                    [
                                        html.Strong("Choose a source file"),
                                        html.Span(" or drop it here · JSON, up to 5 MB"),
                                    ]
                                ),
                                className="rv-import-dropzone",
                            ),
                            html.Details(
                                [
                                    html.Summary("Paste source JSON instead"),
                                    html.Label("Saved sources", htmlFor="trail-source-paste"),
                                    dcc.Textarea(
                                        id="trail-source-paste",
                                        value="",
                                        placeholder='{"source_schema": "1.0", "case": …}',
                                        className="rv-textarea rv-import-json",
                                        persistence=False,
                                    ),
                                    html.Button(
                                        "Validate pasted sources",
                                        id="trail-source-validate",
                                        n_clicks=0,
                                        className="rv-button rv-button--outline",
                                    ),
                                ],
                                className="rv-details rv-import-paste-section",
                            ),
                            html.Button(
                                "Download source example",
                                id="trail-source-example",
                                n_clicks=0,
                                className="rv-text-button",
                            ),
                            dcc.Download(id="trail-source-download"),
                        ],
                        className="rv-import-input",
                    ),
                    html.Div(
                        [
                            html.Div(id="trail-source-preview"),
                            html.Div(
                                id="trail-source-message", role="status", **{"aria-live": "polite"}
                            ),
                            html.Button(
                                "Import sources",
                                id="trail-source-commit",
                                n_clicks=0,
                                disabled=True,
                                className="rv-button rv-button--primary",
                            ),
                        ],
                        className="rv-import-result",
                    ),
                ],
                className="rv-import-grid",
            ),
            dcc.Store(id="trail-source-token", storage_type="memory"),
        ],
        id="trail-source-panel",
        className="rv-import-panel",
    )


def source_browser() -> html.Section:
    return html.Section(
        [
            html.H2("Sources & timeline"),
            html.P(
                "Compare saved records across platforms. Filters change this view only; "
                "the assessment continues to use its saved evidence.",
                className="rv-small rv-muted",
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.Label("Platforms", htmlFor="trail-source-platform"),
                            dcc.Dropdown(
                                id="trail-source-platform",
                                options=[],
                                value=[],
                                multi=True,
                                placeholder="All platforms",
                            ),
                        ],
                        style={"flex": "1 1 220px", "minWidth": 0},
                    ),
                    html.Div(
                        [
                            html.Label("Channels and publishers", htmlFor="trail-source-channel"),
                            dcc.Dropdown(
                                id="trail-source-channel",
                                options=[],
                                value=[],
                                multi=True,
                                placeholder="All channels and publishers",
                            ),
                        ],
                        style={"flex": "1 1 280px", "minWidth": 0},
                    ),
                ],
                style={"display": "flex", "flexWrap": "wrap", "gap": "16px"},
            ),
            html.Div(id="trail-source-summary", role="status", **{"aria-live": "polite"}),
            html.Div(id="trail-source-timeline"),
            html.Div(id="trail-source-groups"),
            dcc.Store(id="trail-source-filter-case", storage_type="memory"),
        ],
        className="rv-panel",
        **{"aria-label": "Compare sources and publication times"},
    )


def example_source_import() -> dict:
    """A visibly synthetic example, including a duplicate and a declared repost."""
    text = "Fictional Harbor bridge inspections will begin on Monday."
    shared = "https://harbor-news.example/bridge-inspections"
    entries = [
        ("telegram", "harbor-updates", "Harbor updates", "1", text),
        ("x", "harbor-residents", "Harbor residents", "2", text),
        ("reddit", "harbor", "Harbor forum", "3", "Residents discuss the inspection notice."),
        (
            "youtube",
            "harbor-video",
            "Harbor video",
            "4",
            "Saved description: an overview of the planned bridge inspection.",
        ),
        ("news", "harbor-news", "Harbor News", "5", "The city announced bridge inspections."),
    ]
    items = [
        {
            "platform": platform,
            "channel_id": channel,
            "channel_name": name,
            "native_id": native,
            "title": f"Fictional {name} record",
            "text": saved,
            "source_url": f"https://{platform}.example/{channel}/{native}",
            "author_id": f"fictional-{platform}-author",
            "published_at": None if platform == "youtube" else f"2026-11-02T10:0{index}:00Z",
            "acquired_at": "2026-11-03T12:00:00Z",
            "shared_urls": [shared],
            "repost_of": (
                {"platform": "telegram", "channel_id": "harbor-updates", "native_id": "1"}
                if platform == "x"
                else None
            ),
        }
        for index, (platform, channel, name, native, saved) in enumerate(entries)
    ]
    return {
        "source_schema": "1.0",
        "case": {
            "title": "Fictional Harbor cross-platform example",
            "question": "What do the supplied records show about this fictional announcement?",
            "synthetic": True,
        },
        "items": items,
    }


def _timeline(records: list[dict]):
    if not records:
        return empty("No matching records", "Clear a filter to see more saved evidence.")
    return html.Ol(
        [
            html.Li(
                [
                    html.Div(
                        [
                            badge(record.get("platform_label") or "Other"),
                            html.Span(record.get("channel_name") or record.get("source_label", "")),
                        ],
                        className="rv-badges",
                    ),
                    record_button(record, f"source-timeline-{index}"),
                    html.P(
                        f"Published: {record.get('published_label') or 'Unknown publication time'}"
                        " · Collected: "
                        f"{record.get('acquired_label') or 'Unknown collection time'}",
                        className="rv-small rv-muted",
                    ),
                    html.P(
                        (record.get("text") or "No saved text")[:280]
                        + ("…" if len(record.get("text") or "") > 280 else ""),
                        style={"whiteSpace": "pre-wrap", "overflowWrap": "anywhere"},
                    ),
                ],
                className="rv-details",
            )
            for index, record in enumerate(records)
        ],
        style={"paddingLeft": "24px"},
    )


def _groups(data: dict, bundle: dict):
    records = {record["id"]: record for record in bundle.get("records", [])}

    def references(ids: list[str], location: str):
        return html.Ul(
            [
                html.Li(record_button(records[rid], f"{location}-{index}"))
                for index, rid in enumerate(ids[:12])
                if rid in records
            ]
            + (
                [html.Li(f"{len(ids) - 12} additional records in this group.")]
                if len(ids) > 12
                else []
            )
        )

    groups = []
    for key, title, explanation in (
        ("exact_groups", "Exact saved text", "Identical saved text does not establish its origin."),
        (
            "url_groups",
            "Shared links",
            "A shared link connects the supplied records; it does not establish coordination.",
        ),
    ):
        entries = data.get(key, [])
        groups.append(
            html.Details(
                [
                    html.Summary(f"{title} ({len(entries)})"),
                    html.P(explanation, className="rv-small rv-muted"),
                    *(
                        [html.P("Showing the first 20 matching groups.", className="rv-small")]
                        if len(entries) > 20
                        else []
                    ),
                    *[
                        html.Div(
                            [
                                html.P(
                                    group.get("url") or group.get("basis") or "Matching records",
                                    style={"overflowWrap": "anywhere"},
                                ),
                                references(group.get("record_ids", []), f"source-{key}-{index}"),
                                *(
                                    [html.P("Additional records are omitted from this view.")]
                                    if group.get("truncated")
                                    else []
                                ),
                            ]
                        )
                        for index, group in enumerate(entries[:20])
                    ],
                ],
                className="rv-details",
            )
        )
    reposts = data.get("reposts", [])
    groups.append(
        html.Details(
            [
                html.Summary(f"Documented reposts ({len(reposts)})"),
                html.P(
                    "These relationships were declared in the imported evidence. "
                    "Their basis remains available for review.",
                    className="rv-small rv-muted",
                ),
                *(
                    [html.P("Showing the first 20 supplied relationships.", className="rv-small")]
                    if len(reposts) > 20
                    else []
                ),
                *[
                    html.Div(
                        [
                            html.P(repost.get("basis") or "Supplied repost relationship"),
                            references(
                                [repost["source_id"], repost["target_id"]],
                                f"source-repost-{index}",
                            ),
                        ]
                    )
                    for index, repost in enumerate(reposts[:20])
                ],
            ],
            className="rv-details",
        )
    )
    return groups


def register_source_callbacks(app: Dash, service) -> None:
    @app.callback(
        Output("trail-source-preview", "children"),
        Output("trail-source-token", "data"),
        Output("trail-source-commit", "disabled"),
        Output("trail-source-message", "children"),
        Input("trail-source-upload", "contents"),
        Input("trail-source-validate", "n_clicks"),
        Input("trail-source-paste", "value"),
        State("trail-source-upload", "filename"),
        prevent_initial_call=True,
    )
    def preview(contents, _validate, pasted, filename):
        if ctx.triggered_id == "trail-source-paste":
            return "", None, True, "Validate the edited sources before importing." if pasted else ""
        source = "paste" if ctx.triggered_id == "trail-source-validate" else "upload"
        content = pasted if source == "paste" else contents
        if not content:
            return "", None, True, no_update
        try:
            summary = service.preview_source_import(
                content, filename if source == "upload" else None
            )
            return (
                preview_card(summary),
                {"fingerprint": summary["fingerprint"], "source": source},
                False,
                "Preview only. No sources have been saved yet.",
            )
        except ValueError as exc:
            message = str(exc)
        except Exception:
            message = "The source file could not be validated. Check it and try again."
        return (
            "",
            None,
            True,
            html.Div(message, className="rv-notice rv-notice--error", role="alert"),
        )

    @app.callback(
        Output("rv-case", "options", allow_duplicate=True),
        Output("rv-case", "value", allow_duplicate=True),
        Output("rv-session", "data", allow_duplicate=True),
        Output("trail-source-message", "children", allow_duplicate=True),
        Output("trail-source-upload", "contents"),
        Output("trail-source-paste", "value"),
        Output("rv-page", "value", allow_duplicate=True),
        Output("trail-source-panel", "open"),
        Output("rv-import-success", "children", allow_duplicate=True),
        Input("trail-source-commit", "n_clicks"),
        State("trail-source-token", "data"),
        State("trail-source-upload", "contents"),
        State("trail-source-upload", "filename"),
        State("trail-source-paste", "value"),
        prevent_initial_call=True,
    )
    def commit(clicks, token, contents, filename, pasted):
        if not clicks:
            raise PreventUpdate
        try:
            if not token or not token.get("fingerprint"):
                raise ValueError("Validate the sources before importing.")
            upload = token.get("source") == "upload"
            case_id = service.commit_source_import(
                (contents if upload else pasted) or "",
                filename if upload else None,
                expected_fingerprint=token["fingerprint"],
            )
            options = [
                {"label": case["title"], "value": str(case["id"])} for case in service.list_cases()
            ]
            message = html.Div(
                "Sources imported. Review the saved evidence or open Compare for the timeline.",
                className="rv-notice rv-notice--success",
            )
            return (
                options,
                case_id,
                session_for_case(service, case_id),
                message,
                None,
                "",
                "review",
                False,
                message,
            )
        except ValueError as exc:
            message = str(exc)
        except Exception:
            message = "The sources could not be imported. Check the file and try again."
        return (
            no_update,
            no_update,
            no_update,
            html.Div(message, className="rv-notice rv-notice--error", role="alert"),
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
        )

    @app.callback(
        Output("trail-source-download", "data"),
        Input("trail-source-example", "n_clicks"),
        prevent_initial_call=True,
    )
    def download_example(clicks):
        if not clicks:
            raise PreventUpdate
        return dcc.send_string(
            json.dumps(example_source_import(), ensure_ascii=False, indent=2),
            "saved-sources-example.json",
        )

    @app.callback(
        Output("trail-source-platform", "value"),
        Output("trail-source-channel", "value"),
        Output("trail-source-filter-case", "data"),
        Input("rv-case", "value"),
        State("trail-source-filter-case", "data"),
    )
    def reset_filters(case_id, previous_case_id):
        if case_id == previous_case_id:
            return no_update, no_update, no_update
        return [], [], case_id

    @app.callback(
        Output("trail-source-platform", "options"),
        Output("trail-source-channel", "options"),
        Output("trail-source-summary", "children"),
        Output("trail-source-timeline", "children"),
        Output("trail-source-groups", "children"),
        Input("rv-session", "data"),
        Input("trail-source-platform", "value"),
        Input("trail-source-channel", "value"),
    )
    def browse(state, platforms, channels):
        if not state or not state.get("case_id"):
            return [], [], "Choose a case to compare its sources.", "", ""
        try:
            bundle = service.get_case(state["case_id"])
            data = source_browser_data(
                bundle, platforms=platforms or None, channels=channels or None
            )
        except ValueError:
            return [], [], "This case is unavailable. Choose another case.", "", ""
        count = data["filtered_count"]
        summary = f"{count} of {data['total']} saved records match the filters."
        if data.get("truncated"):
            summary += " Showing the first 100 records in publication order."
        summary += " Unknown publication times appear last; collection time is shown separately."
        return (
            data["platforms"],
            data["channels"],
            html.P(summary, className="rv-small rv-muted"),
            _timeline(data["records"]),
            _groups(data, bundle),
        )
