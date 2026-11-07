"""News import controls and a separate job watcher over the durable queue."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import plotly.graph_objects as go
from dash import Input, Output, State, ctx, dcc, html, no_update

if TYPE_CHECKING:
    from dash import Dash

    from .service import ExplorerService

logger = logging.getLogger(__name__)


def import_panel(read_only: bool):
    """Render a form that queues work; collection happens in the worker."""
    return html.Section(
        [
            dcc.Store(id="ingestion-store", data=None),
            html.Div(
                [
                    html.Div(
                        [
                            html.H2("Add news"),
                            html.P(
                                "Save a public article or collect recent items from a news feed.",
                                className="muted small",
                            ),
                        ]
                    ),
                    html.Span("Background import", className="pill"),
                ],
                className="section-heading",
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.Label("Article or feed URL", htmlFor="ingestion-url"),
                            dcc.Input(
                                id="ingestion-url",
                                type="url",
                                value="",
                                placeholder="https://…",
                                disabled=read_only,
                                className="search-input",
                            ),
                        ],
                        className="import-url-field",
                    ),
                    html.Div(
                        [
                            html.Label("Source type", htmlFor="ingestion-source-type"),
                            dcc.Dropdown(
                                id="ingestion-source-type",
                                options=[
                                    {"label": "Article", "value": "article"},
                                    {"label": "RSS / Atom feed", "value": "feed"},
                                ],
                                value="article",
                                clearable=False,
                                searchable=False,
                                disabled=read_only,
                            ),
                        ]
                    ),
                    html.Div(
                        [
                            html.Label("Feed article limit", htmlFor="ingestion-limit"),
                            dcc.Input(
                                id="ingestion-limit",
                                type="number",
                                min=1,
                                max=20,
                                step=1,
                                value=5,
                                disabled=True,
                                className="search-input",
                            ),
                        ]
                    ),
                    html.Div(
                        [
                            html.Label("Topic (optional)", htmlFor="ingestion-topic"),
                            dcc.Input(
                                id="ingestion-topic",
                                type="text",
                                value="",
                                placeholder="General",
                                maxLength=150,
                                disabled=read_only,
                                className="search-input",
                            ),
                        ],
                        className="import-topic-field",
                    ),
                ],
                className="import-fields",
            ),
            html.Div(
                [
                    html.Div(
                        [
                            dcc.Checklist(
                                id="ingestion-analyze",
                                options=[
                                    {
                                        "label": "Analyze imported articles with local Ollama",
                                        "value": "analyze",
                                        "disabled": read_only,
                                    }
                                ],
                                value=["analyze"],
                                className="import-analysis-toggle",
                            ),
                            html.P(
                                "Articles are saved first. Local analysis runs after import.",
                                className="muted small",
                            ),
                        ]
                    ),
                    html.Button(
                        "Queue import →",
                        id="ingestion-button",
                        n_clicks=0,
                        disabled=read_only,
                        className="button button--primary",
                    ),
                ],
                className="import-actions",
            ),
            *(
                [
                    html.P(
                        "Imports are disabled in this read-only presentation.",
                        className="muted small import-readonly",
                    )
                ]
                if read_only
                else []
            ),
            html.Div(id="ingestion-feedback", role="status", **{"aria-live": "polite"}),
        ],
        id="add-news",
        className="panel import-panel",
    )


def import_completion(result: dict, repo):
    """Show partial failures alongside saved articles and any follow-up analysis."""
    counts = import_counts(result)
    errors = result.get("errors") or []
    article_links = []
    for article_id in dict.fromkeys(result.get("article_ids") or []):
        article = repo.get_article(article_id)
        if article:
            article_links.append(
                html.Li(
                    html.Button(
                        article.get("title") or "Imported article",
                        id={"type": "open-article", "index": f"{article_id}-import"},
                        n_clicks=0,
                        className="text-button",
                    )
                )
            )
    analysis_job_id = result.get("analysis_job_id")
    follow_up = (
        html.P(
            [
                "Local analysis job created. ",
                html.A(f"Follow analysis job #{analysis_job_id} →", href=f"#job-{analysis_job_id}"),
            ],
            className="small",
        )
        if analysis_job_id is not None
        else html.P(
            "Saved articles are ready for keyword search."
            if article_links
            else "No articles were added by this import.",
            className="small",
        )
    )
    return html.Div(
        [
            html.Strong(f"Import finished: {counts}."),
            follow_up,
            *collection_notes(result),
            *([html.Ul(article_links, className="imported-articles")] if article_links else []),
            *(
                [
                    html.Details(
                        [
                            html.Summary(f"{len(errors)} source(s) could not be imported"),
                            source_error_list(errors),
                        ],
                        className="import-error-details",
                    )
                ]
                if errors
                else []
            ),
        ],
        className=f"notice notice--{'warning' if errors else 'success'} import-result",
    )


def import_counts(result: dict) -> str:
    return ", ".join(
        f"{int(result.get(key) or 0)} {label}"
        for key, label in [("imported", "new"), ("updated", "updated"), ("unchanged", "unchanged")]
    )


def collection_notes(result: dict) -> list:
    warnings = result.get("warnings") or []
    return (
        [
            html.Div(
                [
                    html.Strong("Collection notes"),
                    html.Ul([html.Li(str(note)) for note in warnings]),
                ],
                className="small import-collection-notes",
            )
        ]
        if warnings
        else []
    )


def source_error_list(errors: list[dict]):
    return html.Ul(
        [
            html.Li(
                [
                    html.Strong(str(error.get("url") or "Source")),
                    html.Span(str(error.get("error") or "Import failed")),
                ]
            )
            for error in errors
        ],
        className="import-errors",
    )


def job_result_details(result: dict):
    """Compact, always-visible outcome details for a processing activity card."""
    errors = result.get("errors") or []
    analysis_job_id = result.get("analysis_job_id")
    return html.Div(
        [
            html.P(import_counts(result), className="small"),
            *collection_notes(result),
            *(
                [
                    html.A(
                        f"Local analysis job #{analysis_job_id} →",
                        href=f"#job-{analysis_job_id}",
                        className="small source-link",
                    )
                ]
                if analysis_job_id is not None
                else []
            ),
            *(
                [
                    html.Div(
                        [
                            html.Strong(f"{len(errors)} source(s) could not be imported"),
                            source_error_list(errors),
                        ],
                        className="job-import-errors",
                    )
                ]
                if errors
                else []
            ),
        ],
        className="job-import-details",
    )


def _parse_date(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def publication_date_label(value: str | None) -> str:
    parsed = _parse_date(value)
    return parsed.strftime("%d %b %Y") if parsed is not None else "Publication date unavailable"


def provenance_panel(article: dict):
    """Keep collection times and source metadata distinct from publication dates."""
    rows = []
    if article.get("content_source"):
        origin = {
            "article": "Article page",
            "feed_content": "Full post content from feed",
            "feed_summary": "Post description from feed",
            "platform_post": "Collected platform text",
        }.get(article["content_source"], article["content_source"])
        rows.extend([html.Dt("Saved text source"), html.Dd(origin)])
    if article.get("source_platform"):
        platform = {"telegram": "Telegram", "youtube": "YouTube", "reddit": "Reddit", "x": "X"}.get(
            article["source_platform"], article["source_platform"]
        )
        rows.extend([html.Dt("Platform"), html.Dd(platform)])
    for key, label in (
        ("source_channel_id", "Channel / account ID"),
        ("source_native_id", "Post / video ID"),
    ):
        if article.get(key):
            rows.extend([html.Dt(label), html.Dd(article[key])])
    if article.get("content_scope"):
        scope = {
            "title_description": "Video title and description; no transcript collected",
            "post_text": "Public post text",
        }.get(article["content_scope"], article["content_scope"])
        rows.extend([html.Dt("Collected coverage"), html.Dd(scope)])
    if article.get("author"):
        rows.extend([html.Dt("Author"), html.Dd(article["author"])])
    if article.get("retrieved_at"):
        retrieved = _parse_date(article["retrieved_at"])
        label = retrieved.strftime("%d %b %Y · %H:%M %Z") if retrieved else "Time unavailable"
        rows.extend([html.Dt("Retrieved"), html.Dd(label)])
    if article.get("publication_source"):
        source = {
            "article": "Article metadata",
            "feed": "Feed metadata",
            "unknown": "Not provided",
            "platform": "Platform metadata",
        }.get(article["publication_source"], article["publication_source"])
        rows.extend([html.Dt("Publication date source"), html.Dd(source)])
    for key, label in [
        ("requested_url", "Requested URL"),
        ("final_url", "Retrieved URL"),
        ("feed_url", "Source feed"),
    ]:
        url = article.get(key)
        if url:
            value = (
                html.A(
                    url,
                    href=url,
                    target="_blank",
                    rel="noopener noreferrer",
                    className="source-link",
                )
                if urlparse(url).scheme.lower() in {"http", "https"}
                else html.Span(url)
            )
            rows.extend([html.Dt(label), html.Dd(value)])
    return html.Details(
        [html.Summary("Collection details"), html.Dl(rows)], className="provenance-panel"
    )


def publication_timeline(articles: list[dict]) -> go.Figure:
    """Only plot supplied publication dates; never substitute collection time."""
    dated = [
        (article, parsed)
        for article in articles
        if (parsed := _parse_date(article.get("published_at"))) is not None
    ]
    publishers = list(dict.fromkeys(a.get("publisher") or "Unknown source" for a, _ in dated))
    omitted = len(articles) - len(dated)
    figure = go.Figure()
    for index, publisher in enumerate(publishers):
        selected = [
            (article, date)
            for article, date in dated
            if (article.get("publisher") or "Unknown source") == publisher
        ]
        figure.add_trace(
            go.Scatter(
                x=[date.isoformat() for _, date in selected],
                y=[publisher] * len(selected),
                mode="markers",
                marker={
                    "size": 10,
                    "color": "#255e49" if index % 2 else "#88aa96",
                    "line": {"width": 2, "color": "white"},
                },
                customdata=[[article.get("id"), article.get("title")] for article, _ in selected],
                hovertemplate="%{customdata[1]}<br>%{x}<extra></extra>",
                showlegend=False,
            )
        )
    figure.update_layout(
        height=max(205, min(300, len(publishers) * 30 + 70)),
        margin={"l": 8, "r": 12, "t": 32 if omitted else 12, "b": 28},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": "Arial, sans-serif", "color": "#69756e", "size": 10},
        xaxis={"type": "date", "showgrid": True, "gridcolor": "#edf0e9", "zeroline": False},
        yaxis={"showgrid": False, "zeroline": False, "automargin": True},
    )
    if not dated:
        figure.add_annotation(
            text="No publication dates available"
            if articles
            else "Add articles to see a publication timeline",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
        )
    elif omitted:
        figure.add_annotation(
            text=f"{omitted} article(s) without publication dates are omitted",
            x=0,
            y=1.08,
            xref="paper",
            yref="paper",
            xanchor="left",
            showarrow=False,
            font={"size": 10, "color": "#8b9687"},
        )
    return figure


def register_callbacks(app: Dash, service: ExplorerService) -> None:
    @app.callback(
        Output("ingestion-store", "data"),
        Output("ingestion-feedback", "children"),
        Input("ingestion-button", "n_clicks"),
        Input("poll", "n_intervals"),
        State("ingestion-url", "value"),
        State("ingestion-source-type", "value"),
        State("ingestion-limit", "value"),
        State("ingestion-topic", "value"),
        State("ingestion-analyze", "value"),
        State("ingestion-store", "data"),
        prevent_initial_call=True,
    )
    def import_news(_clicks, _poll, url, source_type, limit, topic, analyze_values, active):
        try:
            if ctx.triggered_id == "poll":
                if not active or active.get("finished"):
                    return no_update, no_update
                job = service.repo.get_job(active["id"])
                if not job:
                    return {**active, "finished": True}, _feedback(
                        "Import job unavailable.", "error"
                    )
                if job["status"] == "succeeded":
                    return {**active, "finished": True}, import_completion(
                        job.get("result") or {}, service.repo
                    )
                if job["status"] == "failed":
                    details = [
                        _feedback(
                            "Import failed: " + str(job.get("error") or "See processing activity."),
                            "error",
                        )
                    ]
                    if job.get("result"):
                        details.append(import_completion(job["result"], service.repo))
                    return {**active, "finished": True}, html.Div(details)
                text = (
                    "Import queued. Waiting for the worker."
                    if job["status"] == "queued"
                    else (job.get("stage") or "Importing news…")
                )
                return no_update, _feedback(text)
            if ctx.triggered_id != "ingestion-button":
                return no_update, no_update
            if service.settings.read_only:
                return no_update, _feedback("Imports are disabled in this read-only presentation.")
            job_id = service.enqueue_ingestion(
                (url or "").strip(),
                source_type=source_type,
                limit=5 if source_type != "feed" or limit is None else limit,
                topic=(topic or "").strip() or "General",
                analyze="analyze" in (analyze_values or []),
            )
            return {"id": job_id, "finished": False}, _feedback(
                "Import queued. Articles will appear as the worker saves them."
            )
        except Exception as exc:
            logger.exception("Could not process news import action")
            return no_update, _feedback(f"Could not queue or update this import: {exc}", "error")

    @app.callback(Output("ingestion-limit", "disabled"), Input("ingestion-source-type", "value"))
    def limit_for_feed(source_type):
        return service.settings.read_only or source_type != "feed"

    @app.callback(Output("sample-banner", "style"), Input("collection-signature", "data"))
    def update_sample_banner(_signature):
        return {} if service.repo.has_samples() else {"display": "none"}


def _feedback(text: str, tone: str = "neutral"):
    return html.Div(text, className=f"notice notice--{tone}")
