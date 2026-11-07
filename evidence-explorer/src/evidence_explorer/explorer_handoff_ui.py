"""Prepare a portable case from the articles selected in the Explorer inspector."""

from collections import Counter
from sqlite3 import Error as SQLiteError

from dash import Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate
from pydantic import ValidationError

from .ingestion_ui import provenance_panel, publication_date_label
from .review.integrations.explorer import MAX_SELECTED_ARTICLES, bundle_for_selected_articles
from .review.integrations.packages import export_case_package


def handoff_panel():
    return html.Section(
        [
            html.H3("Use selected articles in Traced"),
            html.P(
                "Add articles as you inspect them, then preview the saved versions.",
                className="muted small",
            ),
            html.Label("Selected articles", htmlFor="explorer-handoff-articles"),
            dcc.Dropdown(
                id="explorer-handoff-articles",
                options=[],
                value=[],
                multi=True,
                placeholder="Add an article from the inspector",
            ),
            html.Div(id="explorer-handoff-add-status", role="status"),
            html.Div(
                [
                    html.Button(
                        "Preview selected articles",
                        id="explorer-handoff-preview",
                        className="button saved-feed-button",
                        n_clicks=0,
                        disabled=True,
                    ),
                    html.Button(
                        "Download case package",
                        id="explorer-handoff-export",
                        className="button button--primary",
                        n_clicks=0,
                        disabled=True,
                    ),
                ],
                className="badge-row",
            ),
            html.Div(id="explorer-handoff-summary"),
            html.Div(id="explorer-handoff-status", role="status"),
            dcc.Store(id="explorer-handoff-version"),
            dcc.Download(id="explorer-handoff-download"),
        ],
        id="explorer-handoff",
        className="article-disclosure explorer-handoff",
        **{"aria-label": "Use selected articles in Traced"},
    )


def _summary(bundle):
    records_by_fragment = {fragment.id: fragment.record_id for fragment in bundle.fragments}
    claim_counts = Counter(
        records_by_fragment[fragment_id]
        for link in bundle.links
        if link.relation == "supports"
        for fragment_id in link.fragment_ids
    )
    return html.Div(
        [
            html.P(
                f"{len(bundle.records)} saved articles · {len(bundle.findings)} quoted claims",
                className="muted small",
            ),
            html.Ul(
                [
                    html.Li(
                        [
                            html.Strong(record.source_label),
                            html.Div(
                                " · ".join(
                                    [
                                        publication_date_label(record.published_at),
                                        f"Saved text {record.content_hash[:12]}",
                                        "Synthetic sample"
                                        if record.metadata["synthetic"]
                                        else "Saved source",
                                        f"{claim_counts[record.id]} saved quoted claims"
                                        if claim_counts[record.id]
                                        else "No saved quoted claims",
                                    ]
                                ),
                                className="muted small",
                            ),
                            provenance_panel(record.metadata),
                        ]
                    )
                    for record in bundle.records
                ]
            ),
            html.P(
                "Download the ZIP, then choose Cases → Gather evidence → Import case package "
                "in Traced. Review the imported evidence before attaching it to your plan.",
                className="muted small",
            ),
        ]
    )


def _error(message):
    return html.Div(message, className="notice notice--error")


def register_callbacks(app, service):
    @app.callback(
        Output("explorer-handoff-articles", "options"),
        Output("explorer-handoff-articles", "value"),
        Output("explorer-handoff-add-status", "children"),
        Input("explorer-handoff-add", "n_clicks", allow_optional=True),
        State("explorer-handoff-add", "value", allow_optional=True),
        State("explorer-handoff-articles", "options"),
        State("explorer-handoff-articles", "value"),
        prevent_initial_call=True,
    )
    def add_article(clicks, article_id, options, selected):
        if not clicks or not isinstance(article_id, str) or not article_id.isascii():
            raise PreventUpdate
        if not article_id.isdecimal() or len(article_id) > 19 or int(article_id) >= 2**63:
            raise PreventUpdate
        article_id = int(article_id)
        selected = selected or []
        if article_id in selected:
            return no_update, no_update, "This article is already selected."
        if len(selected) >= MAX_SELECTED_ARTICLES:
            return (
                no_update,
                no_update,
                _error(f"Select up to {MAX_SELECTED_ARTICLES} articles per case."),
            )
        try:
            article = service.repo.articles.get_article(article_id)
        except (ValidationError, SQLiteError):
            article = None
        if article is None:
            return no_update, no_update, _error("This article is unavailable. Inspect it again.")
        retained = [option for option in options or [] if option["value"] in selected]
        label = f"{article.title} · {article.publisher}"
        if article.is_sample:
            label += " · Synthetic sample"
        return [*retained, {"label": label, "value": article_id}], [*selected, article_id], ""

    @app.callback(
        Output("explorer-handoff-version", "data"),
        Output("explorer-handoff-summary", "children"),
        Output("explorer-handoff-status", "children"),
        Output("explorer-handoff-preview", "disabled"),
        Output("explorer-handoff-export", "disabled"),
        Output("explorer-handoff-download", "data"),
        Input("explorer-handoff-articles", "value"),
        Input("explorer-handoff-preview", "n_clicks"),
        Input("explorer-handoff-export", "n_clicks"),
        State("explorer-handoff-version", "data"),
        prevent_initial_call=True,
    )
    def prepare(selected, preview_clicks, export_clicks, version):
        trigger = ctx.triggered_id
        if trigger == "explorer-handoff-articles":
            return None, "", "", not selected, True, no_update
        downloading = trigger == "explorer-handoff-export" and bool(export_clicks)
        if not downloading and not (trigger == "explorer-handoff-preview" and preview_clicks):
            raise PreventUpdate
        try:
            bundle = bundle_for_selected_articles(service.repo, selected)
            current_version = bundle.fingerprint()
            if not downloading:
                return current_version, _summary(bundle), "Preview ready.", False, False, no_update
            if current_version != version:
                raise ValueError(
                    "The selected evidence changed. Preview it again before downloading."
                )
            data = dcc.send_bytes(
                export_case_package(bundle),
                f"explorer-selected-articles-{bundle.case.id.rsplit(':', 1)[-1][:12]}.zip",
            )
            return no_update, no_update, "Case package ready to import.", False, False, data
        except (ValidationError, SQLiteError):
            message = "Saved evidence could not be read. Check the selected articles and try again."
        except ValueError as exc:
            message = str(exc)
        return None, "", _error(message), not selected, True, no_update
