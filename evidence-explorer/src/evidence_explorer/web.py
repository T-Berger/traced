"""Dash application and thin callbacks over the shared explorer service."""

from __future__ import annotations

import logging
from pathlib import Path

from dash import ALL, Dash, Input, Output, State, ctx, html, no_update

from . import ui
from .config import Settings
from .explorer_handoff_ui import register_callbacks as register_handoff_callbacks
from .feeds_ui import register_callbacks as register_feed_callbacks
from .graphs import MAX_QUOTATIONS, graph_outline, graph_style, source_graph
from .ingestion_ui import register_callbacks as register_ingestion_callbacks
from .layout import build_layout
from .service import ExplorerService

logger = logging.getLogger(__name__)


def _notice(text: str, tone: str = "neutral"):
    return html.Div(text, className=f"notice notice--{tone}")


def _refresh_keyword_results(service: ExplorerService, search: dict | None):
    """Make imported passages searchable while preserving an independent watched job."""
    if not search or search.get("mode") != "keyword" or not search.get("query"):
        return no_update
    matches = service.search_keyword(search["query"], limit=10)
    if ui.content_signature(matches) == ui.content_signature(search.get("matches", [])):
        return no_update
    return {**search, "matches": matches}


def create_app(settings: Settings | None = None) -> Dash:
    service = ExplorerService(settings or Settings.from_env())
    if not service.settings.read_only:
        from .demo import seed_demo

        seed_demo(service.repo)
    app = Dash(
        __name__,
        assets_folder=str(Path(__file__).parent / "assets"),
        title="Traced · Evidence Explorer",
        suppress_callback_exceptions=True,
        update_title="Working…",
    )
    app.layout = lambda: build_layout(service)
    app.explorer_service = service

    @app.server.get("/health")
    def health():
        return {"status": "ok", "read_only": service.settings.read_only}

    @app.callback(
        Output("search-store", "data"),
        Output("job-store", "data"),
        Output("action-message", "children"),
        Input("search-button", "n_clicks"),
        Input("search-input", "n_submit"),
        Input("analyze-button", "n_clicks"),
        Input("poll", "n_intervals"),
        State("search-input", "value"),
        State("search-mode", "value"),
        State("analysis-mode", "value"),
        State("search-store", "data"),
        State("job-store", "data"),
        prevent_initial_call=True,
    )
    def act(_search, _submit, _analyze, _poll, query, mode, analysis_mode, search, active_job):
        trigger = ctx.triggered_id
        try:
            if trigger == "poll":
                refreshed_search = _refresh_keyword_results(service, search)
                if not active_job or active_job.get("finished"):
                    return refreshed_search, no_update, no_update
                if active_job.get("kind") == "search" and (
                    active_job.get("query") != (query or "").strip()
                    or active_job.get("mode") != mode
                ):
                    return (
                        refreshed_search,
                        {**active_job, "finished": True},
                        _notice("Search settings changed. Run Search to use the new query."),
                    )
                job = service.repo.get_job(active_job["id"])
                if not job:
                    return (
                        refreshed_search,
                        {**active_job, "finished": True},
                        _notice("Job unavailable."),
                    )
                state = job.get("status")
                if state == "succeeded":
                    finished = {**active_job, "finished": True}
                    if active_job["kind"] == "search":
                        result = job.get("result") or {}
                        matches = result.get("matches", [])
                        return (
                            {
                                "query": active_job["query"],
                                "mode": active_job["mode"],
                                "matches": matches,
                            },
                            finished,
                            _notice("Search complete.", "success"),
                        )
                    return (
                        refreshed_search,
                        finished,
                        _notice("Analysis complete. Saved claims are ready.", "success"),
                    )
                if state == "failed":
                    return (
                        refreshed_search,
                        {**active_job, "finished": True},
                        _notice(
                            "Processing failed: "
                            + str(job.get("error") or "Check processing activity."),
                            "error",
                        ),
                    )
                online = service.status().get("worker", {}).get("online", False)
                message = job.get("stage") or "Waiting to start"
                if state == "queued" and not online:
                    message = "Queued · waiting for the worker. Keyword search remains available."
                return refreshed_search, no_update, _notice(message)
            if trigger in {"search-button", "search-input"}:
                query = (query or "").strip()
                if not query:
                    return no_update, no_update, _notice("Enter a word or phrase to search.")
                if mode == "keyword":
                    matches = service.search_keyword(query, limit=10)
                    job_state = no_update
                    if active_job and active_job.get("kind") == "search":
                        job_state = {**active_job, "finished": True}
                    return {"query": query, "mode": mode, "matches": matches}, job_state, ""
                if service.settings.read_only:
                    return (
                        no_update,
                        no_update,
                        _notice(
                            "This presentation is read-only. Choose Keyword for instant search."
                        ),
                    )
                job_id = service.enqueue_search(query, mode=mode)
                job = {"id": job_id, "kind": "search", "query": query, "mode": mode}
            elif trigger == "analyze-button":
                if service.settings.read_only:
                    return no_update, no_update, _notice("Analysis is disabled in read-only mode.")
                job_id = service.enqueue_analysis(mode=analysis_mode)
                job = {"id": job_id, "kind": "analysis", "mode": analysis_mode}
            else:
                return no_update, no_update, no_update
            online = service.status().get("worker", {}).get("online", False)
            message = "Queued for processing." if online else "Queued · waiting for the worker."
            return no_update, job, _notice(message)
        except Exception as exc:
            logger.exception("Could not complete dashboard action")
            return no_update, no_update, _notice(f"Could not complete this action: {exc}", "error")

    @app.callback(
        Output("search-results", "children"),
        Output("results-meta", "children"),
        Input("search-store", "data"),
    )
    def render_results(search):
        search = search or {}
        matches = search.get("matches", [])
        mode = search.get("mode", "keyword")
        label = {"keyword": "keyword", "demo": "demo embeddings", "ollama": "local embeddings"}
        return ui.result_cards(matches, mode), f"{len(matches)} passages · {label.get(mode, mode)}"

    @app.callback(
        Output("selection-store", "data"),
        Input({"type": "open-article", "index": ALL}, "n_clicks"),
        Input("source-graph", "tapNodeData"),
        Input("timeline", "clickData"),
        Input("search-store", "data"),
        State("selection-store", "data"),
        prevent_initial_call=True,
    )
    def select_article(_clicks, node, point, search, selected):
        trigger = ctx.triggered_id
        if isinstance(trigger, dict) and isinstance(trigger.get("index"), str):
            value = ctx.triggered[0].get("value")
            clicked = any(_clicks or []) if value is None else bool(value)
            if clicked:
                return int(trigger["index"].split("-", 1)[0])
        if trigger == "source-graph" and node and node.get("article_id") is not None:
            return node["article_id"]
        if trigger == "timeline" and point:
            points = point.get("points", [])
            if points and points[0].get("customdata"):
                return points[0]["customdata"][0]
        if trigger == "search-store":
            matches = (search or {}).get("matches", [])
            if matches and selected not in {m.get("article_id") for m in matches}:
                return matches[0]["article_id"]
        return no_update

    @app.callback(
        Output("article-detail", "children"),
        Output("article-signature", "data"),
        Input("selection-store", "data"),
        Input("poll", "n_intervals"),
        State("article-signature", "data"),
    )
    def render_article(article_id, _poll, previous):
        try:
            article = service.repo.get_article(article_id) if article_id is not None else None
            claims = (
                service.repo.list_active_claims(article_id=article_id)
                if article is not None
                else []
            )
            signature = ui.article_signature(article, claims)
            if signature == previous:
                return no_update, no_update
            return ui.article_panel(article, claims), signature
        except Exception as exc:
            logger.exception("Could not load article %s", article_id)
            return _notice(f"Could not load this article: {exc}", "error"), no_update

    @app.callback(
        Output("stats-row", "children"),
        Output("timeline", "figure"),
        Output("collection-signature", "data"),
        Output("job-list", "children"),
        Output("system-status", "children"),
        Input("poll", "n_intervals"),
        State("collection-signature", "data"),
    )
    def refresh_collection(_poll, previous):
        try:
            collection = service.collection()
            articles = [record.model_dump(mode="json") for record in collection.articles]
            claims = [record.model_dump(mode="json") for record in collection.claims]
            signature = ui.content_signature(collection.model_dump(mode="json"))
            jobs = ui.job_cards(service.repo.list_jobs(limit=5))
            status = ui.status_content(service.status(), service.settings.read_only)
            if signature == previous:
                return no_update, no_update, no_update, jobs, status
            return (
                ui.stat_cards(articles, claims, totals=collection.totals),
                ui.timeline_figure(articles),
                signature,
                jobs,
                status,
            )
        except Exception as exc:
            logger.exception("Could not refresh dashboard collection")
            return (
                no_update,
                no_update,
                no_update,
                _notice(f"Status unavailable: {exc}", "error"),
                no_update,
            )

    @app.callback(
        Output("source-graph", "elements"),
        Output("source-graph", "style"),
        Output("source-graph-summary", "children"),
        Output("source-graph-title", "children"),
        Output("source-graph-outline", "children"),
        Output("source-graph-mobile", "children"),
        Input("selection-store", "data"),
        Input("collection-signature", "data"),
    )
    def focus_source_graph(article_id, _signature):
        """Read one indexed article and a bounded quotation preview after selection."""
        try:
            article = (
                service.repo.articles.get_article(article_id) if article_id is not None else None
            )
            claims = (
                service.repo.claims.list_active_claims(
                    article_id=article_id, limit=MAX_QUOTATIONS + 1
                )
                if article
                else []
            )
            graph = source_graph(
                [article.model_dump(mode="json")] if article else [],
                [claim.model_dump(mode="json") for claim in claims],
                article_id,
            )
            return (
                graph.elements,
                graph_style(graph),
                graph.summary,
                article.title if article else "Choose an article",
                graph_outline(graph),
                graph_outline(graph, "mobile"),
            )
        except Exception:
            logger.exception("Could not load the selected article publication path")
            return (
                [],
                {"width": "100%", "height": "250px"},
                "Publication path unavailable. Choose an article to try again.",
                "",
                "",
                "",
            )

    register_ingestion_callbacks(app, service)
    register_feed_callbacks(app, service)
    register_handoff_callbacks(app, service)
    return app
