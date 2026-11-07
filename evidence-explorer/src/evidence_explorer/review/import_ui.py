"""Investigation upload and preview UI, independent of the review screen callbacks."""

from __future__ import annotations

from importlib.resources import files
from typing import Any

from dash import Dash, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from .controller import session_for_case
from .presentation import badge
from .service import ReviewService


def import_panel() -> html.Details:
    """Uploaded data stays in the current form; only its digest enters a memory store."""
    return html.Details(
        [
            html.Summary("Import investigation"),
            html.Div(
                [
                    html.Div(
                        [
                            html.H2("Bring your evidence into review"),
                            html.P(
                                "Upload a saved investigation JSON bundle, inspect the summary, "
                                "then import it. Importing saves data locally; it does not send "
                                "evidence to a model.",
                                className="rv-small rv-muted",
                            ),
                            dcc.Upload(
                                id="rv-import-upload",
                                accept=".json,application/json",
                                multiple=False,
                                children=html.Div(
                                    [
                                        html.Strong("Choose a JSON file"),
                                        html.Span(" or drop it here · up to 5 MB"),
                                    ]
                                ),
                                className="rv-import-dropzone",
                            ),
                            html.Details(
                                [
                                    html.Summary("Paste JSON instead"),
                                    html.Label("Investigation JSON", htmlFor="rv-import-paste"),
                                    dcc.Textarea(
                                        id="rv-import-paste",
                                        value="",
                                        placeholder='{"schema_version": "1.0", "case": …}',
                                        className="rv-textarea rv-import-json",
                                        persistence=False,
                                    ),
                                    html.Button(
                                        "Validate pasted JSON",
                                        id="rv-import-validate",
                                        n_clicks=0,
                                        className="rv-button rv-button--outline",
                                    ),
                                ],
                                className="rv-details rv-import-paste-section",
                            ),
                            html.Button(
                                "Download example JSON",
                                id="rv-import-template",
                                n_clicks=0,
                                className="rv-text-button",
                            ),
                            dcc.Download(id="rv-import-download"),
                        ],
                        className="rv-import-input",
                    ),
                    html.Div(
                        [
                            html.Div(id="rv-import-preview"),
                            html.Div(
                                id="rv-import-message", role="status", **{"aria-live": "polite"}
                            ),
                            html.Button(
                                "Import and open",
                                id="rv-import-commit",
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
            dcc.Store(id="rv-import-token", storage_type="memory"),
        ],
        id="rv-import-panel",
        className="rv-import-panel",
    )


def preview_card(summary: dict[str, Any]) -> html.Div:
    statuses = {
        "new": "New investigation",
        "existing": "Already saved · safe to reopen",
        "update": "Adds to an existing investigation",
    }
    counts = summary.get("counts", {})
    return html.Div(
        [
            html.Div(
                [
                    badge("Validated JSON", "green"),
                    badge("Synthetic" if summary.get("synthetic") else "Imported investigation"),
                ],
                className="rv-badges",
            ),
            html.H3(summary.get("title") or "Untitled investigation"),
            html.P(summary.get("question") or "No investigation question supplied."),
            html.P(
                statuses.get(summary.get("status"), "Ready to import"),
                className="rv-small rv-muted",
            ),
            html.Dl(
                [
                    child
                    for label in ("records", "fragments", "findings", "runs", "links")
                    for child in (html.Dt(label.capitalize()), html.Dd(str(counts.get(label, 0))))
                ],
                className="rv-import-counts",
            ),
            *(
                [
                    html.Strong("Declared limitations", className="rv-small"),
                    html.Ul([html.Li(str(item)) for item in summary["limitations"]]),
                ]
                if summary.get("limitations")
                else []
            ),
            *[html.P(str(item), className="rv-notice") for item in summary.get("warnings", [])],
        ],
        className="rv-import-summary",
    )


def register_import_callbacks(app: Dash, service: ReviewService) -> None:
    @app.callback(
        Output("rv-import-preview", "children"),
        Output("rv-import-token", "data"),
        Output("rv-import-commit", "disabled"),
        Output("rv-import-message", "children"),
        Input("rv-import-upload", "contents"),
        Input("rv-import-validate", "n_clicks"),
        Input("rv-import-paste", "value"),
        State("rv-import-upload", "filename"),
        prevent_initial_call=True,
    )
    def preview(contents, _validate, pasted, filename):
        trigger = ctx.triggered_id
        if trigger == "rv-import-paste":
            message = "Validate the edited JSON before importing." if pasted else ""
            return "", None, True, message if pasted else no_update
        source = "paste" if trigger == "rv-import-validate" else "upload"
        content = pasted if source == "paste" else contents
        if not content:
            return "", None, True, no_update
        try:
            summary = service.preview_import(content, filename if source == "upload" else None)
            return (
                preview_card(summary),
                {"fingerprint": summary["fingerprint"], "source": source},
                False,
                "Preview only. Nothing has been imported yet.",
            )
        except ValueError as exc:
            return (
                "",
                None,
                True,
                html.Div(str(exc), className="rv-notice rv-notice--error", role="alert"),
            )
        except Exception:
            return (
                "",
                None,
                True,
                html.Div(
                    "The investigation could not be validated. Check the file and try again.",
                    className="rv-notice rv-notice--error",
                    role="alert",
                ),
            )

    @app.callback(
        Output("rv-case", "options", allow_duplicate=True),
        Output("rv-case", "value", allow_duplicate=True),
        Output("rv-session", "data", allow_duplicate=True),
        Output("rv-import-message", "children", allow_duplicate=True),
        Output("rv-import-upload", "contents"),
        Output("rv-import-paste", "value"),
        Output("rv-page", "value", allow_duplicate=True),
        Output("rv-import-panel", "open"),
        Output("rv-import-success", "children"),
        Input("rv-import-commit", "n_clicks"),
        State("rv-import-token", "data"),
        State("rv-import-upload", "contents"),
        State("rv-import-upload", "filename"),
        State("rv-import-paste", "value"),
        prevent_initial_call=True,
    )
    def commit(clicks, token, contents, filename, pasted):
        if not clicks:
            raise PreventUpdate
        try:
            if not token or not token.get("fingerprint"):
                raise ValueError("Validate the investigation before importing.")
            from_upload = token.get("source") == "upload"
            content = contents if from_upload else pasted
            case_id = service.commit_import(
                content or "",
                filename if from_upload else None,
                expected_fingerprint=token["fingerprint"],
            )
            state = session_for_case(service, case_id)
            options = [
                {"label": case["title"], "value": str(case["id"])} for case in service.list_cases()
            ]
            return (
                options,
                case_id,
                state,
                html.Div(
                    "Investigation imported. Its saved evidence is ready to review.",
                    className="rv-notice rv-notice--success",
                ),
                None,
                "",
                "review",
                False,
                html.Div(
                    "Investigation imported. Its saved evidence is ready to review.",
                    className="rv-notice rv-notice--success",
                ),
            )
        except ValueError as exc:
            message = str(exc)
        except Exception:
            message = "The investigation could not be imported. No new case was opened. Try again."
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
        Output("rv-import-download", "data"),
        Input("rv-import-template", "n_clicks"),
        prevent_initial_call=True,
    )
    def download_template(clicks):
        if not clicks:
            raise PreventUpdate
        example = files("evidence_explorer.review").joinpath("data/import-example.json")
        return {
            "content": example.read_text(encoding="utf-8"),
            "filename": "investigation-example.json",
            "type": "application/json",
        }
