"""Declarative page layout, kept separate from application callbacks."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import dash_cytoscape as cyto
from dash import dcc, get_asset_url, html

from . import ui
from .feeds_ui import feeds_panel
from .graphs import GRAPH_LAYOUT, graph_outline, graph_style, source_graph
from .ingestion_ui import import_panel

if TYPE_CHECKING:
    from .service import ExplorerService

logger = logging.getLogger(__name__)


def build_layout(service: ExplorerService):
    collection = service.collection()
    articles = [record.model_dump(mode="json") for record in collection.articles]
    claims = [record.model_dump(mode="json") for record in collection.claims]
    try:
        matches = service.search_keyword("bridge", limit=10)
    except Exception:
        logger.exception("Could not load initial dashboard search")
        matches = []
    first_id = matches[0]["article_id"] if matches else articles[0]["id"] if articles else None
    selected = service.repo.get_article(first_id) if first_id is not None else None
    selected_claims = (
        service.repo.list_active_claims(article_id=first_id) if first_id is not None else []
    )
    graph = source_graph([selected] if selected else [], selected_claims, first_id)
    read_only = service.settings.read_only
    return html.Div(
        [
            dcc.Store(
                id="search-store", data={"query": "bridge", "mode": "keyword", "matches": matches}
            ),
            dcc.Store(id="selection-store", data=first_id),
            dcc.Store(id="job-store", data=None),
            dcc.Store(
                id="collection-signature",
                data=ui.content_signature(collection.model_dump(mode="json")),
            ),
            dcc.Store(
                id="article-signature",
                data=ui.article_signature(selected, selected_claims),
            ),
            dcc.Interval(id="poll", interval=1800, n_intervals=0),
            html.A("Skip to workspace", href="#workspace", className="skip-link"),
            html.Aside(
                [
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Img(
                                        src=get_asset_url("traced/logos/traced-primary.svg"),
                                        alt="",
                                        className="traced-logo traced-logo--light",
                                    ),
                                    html.Img(
                                        src=get_asset_url("traced/logos/traced-reversed.svg"),
                                        alt="",
                                        className="traced-logo traced-logo--dark",
                                    ),
                                ],
                                className="traced-lockup traced-lockup--inverse",
                                role="img",
                                **{"aria-label": "Traced"},
                            ),
                            html.P("Evidence Explorer", className="traced-specialist"),
                        ],
                        className="brand traced-brand",
                    ),
                    html.Div("INVESTIGATION WORKSPACE", className="sidebar-eyebrow"),
                    html.Nav(
                        [
                            html.A(
                                [html.Span("▤"), "Explore evidence"],
                                href="#workspace",
                                className="nav-link nav-link--active",
                            ),
                            html.A(
                                [html.Span("＋"), "Add news"],
                                href="#add-news",
                                className="nav-link",
                            ),
                            html.A(
                                [html.Span("↻"), "Saved feeds"],
                                href="#saved-feeds",
                                className="nav-link",
                            ),
                            html.A(
                                [html.Span("⌘"), "Source connections"],
                                href="#connections",
                                className="nav-link",
                            ),
                            html.A(
                                [html.Span("◷"), "Processing activity"],
                                href="#activity",
                                className="nav-link",
                            ),
                        ],
                        **{"aria-label": "Workspace navigation"},
                    ),
                    html.Div(
                        [
                            html.Div("COLLECTION ANALYSIS", className="sidebar-eyebrow"),
                            html.P(
                                "Extract claims with quotations from the saved collection.",
                                className="sidebar-note",
                            ),
                            dcc.Dropdown(
                                id="analysis-mode",
                                options=[
                                    {"label": "Demo · deterministic", "value": "demo"},
                                    {"label": "Ollama · local models", "value": "ollama"},
                                ],
                                value="demo",
                                clearable=False,
                                searchable=False,
                                disabled=read_only,
                            ),
                            html.Button(
                                "Run analysis ↗",
                                id="analyze-button",
                                n_clicks=0,
                                disabled=read_only,
                                className="button button--sidebar",
                            ),
                            *(
                                [
                                    html.P(
                                        "Analysis is disabled in this read-only presentation.",
                                        className="sidebar-note",
                                    )
                                ]
                                if read_only
                                else []
                            ),
                        ],
                        className="sidebar-analysis",
                    ),
                    html.Div(
                        [
                            html.Div("WORKSPACE STATUS", className="sidebar-eyebrow"),
                            html.Div(id="system-status", children=ui.status_content({}, read_only)),
                            html.Div("LOCAL FIRST · SOURCE LINKED", className="sidebar-footer"),
                        ],
                        className="sidebar-bottom",
                    ),
                ],
                className="sidebar",
            ),
            html.Main(
                [
                    html.Header(
                        [
                            html.Div(
                                [
                                    html.Span("WORKSPACE", className="eyebrow"),
                                    html.Span(" / ", className="breadcrumb-divider"),
                                    html.Span("News & events", className="breadcrumb-current"),
                                ]
                            ),
                            html.Div(
                                [
                                    ui.pill(
                                        "Read-only" if read_only else "Local workspace", "green"
                                    ),
                                ],
                                className="header-right",
                            ),
                        ],
                        className="topbar",
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Div(
                                        [
                                            html.Span(
                                                "OPEN SOURCE INTELLIGENCE", className="eyebrow"
                                            ),
                                            html.H1("Follow the evidence."),
                                            html.P(
                                                "Connect reporting, inspect claims, "
                                                "and keep the source in view.",
                                                className="page-description",
                                            ),
                                        ]
                                    ),
                                    html.Div(
                                        [
                                            html.Span("◌", className="collection-icon"),
                                            html.Div(
                                                [
                                                    html.Strong("News & events"),
                                                    html.Span("Evidence collection"),
                                                ]
                                            ),
                                        ],
                                        className="collection-tag",
                                    ),
                                ],
                                className="page-intro",
                            ),
                            html.Div(
                                [
                                    html.Span("◈", className="sample-banner__icon"),
                                    html.Div(
                                        [
                                            html.Strong("Synthetic samples in this collection"),
                                            html.Span(
                                                "Sample reporting and cached demo claims "
                                                "are fictional. Demo analysis uses deterministic "
                                                "fixtures; Ollama runs real local models."
                                            ),
                                        ]
                                    ),
                                ],
                                className="sample-banner",
                                id="sample-banner",
                                style={} if service.repo.has_samples() else {"display": "none"},
                            ),
                            html.Div(
                                ui.stat_cards(articles, claims, totals=collection.totals),
                                id="stats-row",
                                className="stats-row",
                            ),
                            import_panel(read_only),
                            html.Section(
                                [
                                    html.Div(
                                        [
                                            html.H2("Search the collection"),
                                            html.Span(
                                                "Passages with context", className="muted small"
                                            ),
                                        ],
                                        className="section-heading",
                                    ),
                                    html.Div(
                                        [
                                            html.Label(
                                                "Search saved articles",
                                                htmlFor="search-input",
                                                className="visually-hidden",
                                            ),
                                            dcc.Input(
                                                id="search-input",
                                                type="search",
                                                value="bridge",
                                                debounce=False,
                                                placeholder="Search an event or phrase…",
                                                className="search-input",
                                                n_submit=0,
                                            ),
                                            dcc.Dropdown(
                                                id="search-mode",
                                                options=[
                                                    {
                                                        "label": "Keyword · instant",
                                                        "value": "keyword",
                                                    },
                                                    {
                                                        "label": "Demo embeddings · queued",
                                                        "value": "demo",
                                                    },
                                                    {
                                                        "label": "Ollama embeddings · queued",
                                                        "value": "ollama",
                                                    },
                                                ],
                                                value="keyword",
                                                clearable=False,
                                                searchable=False,
                                                className="search-mode",
                                            ),
                                            html.Button(
                                                "Search →",
                                                id="search-button",
                                                n_clicks=0,
                                                className="button button--primary",
                                            ),
                                        ],
                                        className="search-controls",
                                    ),
                                    html.Div(
                                        id="action-message",
                                        role="status",
                                        **{"aria-live": "polite"},
                                    ),
                                ],
                                className="panel search-panel",
                                id="workspace",
                            ),
                            html.Div(
                                [
                                    html.Section(
                                        [
                                            html.Div(
                                                [
                                                    html.H2("Matching evidence"),
                                                    html.Span(
                                                        f"{len(matches)} passages · keyword",
                                                        id="results-meta",
                                                        className="muted small",
                                                    ),
                                                ],
                                                className="section-heading",
                                            ),
                                            html.Div(ui.result_cards(matches), id="search-results"),
                                        ],
                                        className="results-column",
                                    ),
                                    html.Section(
                                        [
                                            html.Div(
                                                [
                                                    html.Div(
                                                        [
                                                            html.Span(
                                                                "YOUR EVIDENCE NOTEBOOK",
                                                                className="eyebrow",
                                                            ),
                                                            html.H2("Source inspection"),
                                                        ]
                                                    ),
                                                    html.Span("↗", className="section-icon"),
                                                ],
                                                className="section-heading",
                                            ),
                                            html.Div(
                                                ui.article_panel(selected, selected_claims),
                                                id="article-detail",
                                            ),
                                        ],
                                        className="panel evidence-panel",
                                    ),
                                ],
                                className="evidence-grid",
                            ),
                            html.Div(
                                [
                                    html.Section(
                                        [
                                            html.Div(
                                                [
                                                    html.Div(
                                                        [
                                                            html.H2("Source connections"),
                                                            html.P(
                                                                "Publisher → selected article "
                                                                "→ saved quotations",
                                                                className="source-graph-caption",
                                                            ),
                                                        ]
                                                    ),
                                                    ui.pill("Select an article or claim"),
                                                ],
                                                className="section-heading",
                                            ),
                                            html.H3(
                                                selected.get("title", "Choose an article")
                                                if selected
                                                else "Choose an article",
                                                id="source-graph-title",
                                                className="source-graph-title",
                                            ),
                                            html.P(
                                                graph.summary,
                                                id="source-graph-summary",
                                                className="source-graph-caption",
                                                role="status",
                                                **{"aria-live": "polite"},
                                            ),
                                            cyto.Cytoscape(
                                                id="source-graph",
                                                elements=graph.elements,
                                                stylesheet=ui.GRAPH_STYLESHEET,
                                                layout=GRAPH_LAYOUT,
                                                minZoom=0.75,
                                                maxZoom=1.5,
                                                userZoomingEnabled=False,
                                                userPanningEnabled=False,
                                                autoungrabify=True,
                                                className="source-graph-canvas",
                                                style=graph_style(graph),
                                            ),
                                            html.Div(
                                                [
                                                    html.Span(
                                                        "● Publisher", className="legend-publisher"
                                                    ),
                                                    html.Span(
                                                        "● Article", className="legend-article"
                                                    ),
                                                    html.Span(
                                                        "■ Quoted claim", className="legend-claim"
                                                    ),
                                                ],
                                                className="graph-legend",
                                            ),
                                            html.P(
                                                "Links describe publication and matching saved "
                                                "quotations. They do not establish independent "
                                                "corroboration, common control, or truth.",
                                                className="source-graph-meaning",
                                            ),
                                            html.Details(
                                                [
                                                    html.Summary(
                                                        "Read publication path and exact quotations"
                                                    ),
                                                    html.Div(
                                                        graph_outline(graph),
                                                        id="source-graph-outline",
                                                    ),
                                                ],
                                                className="source-graph-transcript",
                                            ),
                                            html.Div(
                                                graph_outline(graph, "mobile"),
                                                id="source-graph-mobile",
                                                className="source-graph-mobile",
                                            ),
                                        ],
                                        className="panel graph-panel",
                                        id="connections",
                                    ),
                                    html.Section(
                                        [
                                            html.Div(
                                                [
                                                    html.H2("Publication timeline"),
                                                    html.Span(
                                                        "Saved collection", className="muted small"
                                                    ),
                                                ],
                                                className="section-heading",
                                            ),
                                            dcc.Graph(
                                                id="timeline",
                                                figure=ui.timeline_figure(articles),
                                                style={"height": "300px"},
                                                config={
                                                    "displayModeBar": False,
                                                    "responsive": True,
                                                },
                                            ),
                                            html.P(
                                                "Publication dates as reported by each source. "
                                                "The earliest saved article may not be "
                                                "the original report.",
                                                className="muted small timeline-note",
                                            ),
                                        ],
                                        className="panel timeline-panel",
                                    ),
                                ],
                                className="overview-grid",
                            ),
                            feeds_panel(service),
                            html.Section(
                                [
                                    html.Div(
                                        [
                                            html.H2("Processing activity"),
                                            html.Span("Collection jobs", className="muted small"),
                                        ],
                                        className="section-heading",
                                    ),
                                    html.Div(
                                        ui.job_cards(service.repo.list_jobs(limit=5)), id="job-list"
                                    ),
                                ],
                                className="panel activity-panel",
                                id="activity",
                            ),
                            html.Footer(
                                [
                                    html.Span("Traced · Evidence Explorer"),
                                    html.Span("Every claim. A clear trail."),
                                ],
                                className="page-footer",
                            ),
                        ],
                        className="page-content",
                    ),
                ],
                className="main-content",
            ),
        ],
        className="app-shell traced-app traced-explorer",
    )
