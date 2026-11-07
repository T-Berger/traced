"""Reusable screen sections shared by every evidence case type.

These components know only UI IDs and supplied state. Specialist integrations fill
explicit slots instead of creating their own review forms or source inspectors.
"""

from __future__ import annotations

from typing import Any

import dash_cytoscape as cyto
from dash import dcc, get_asset_url, html

from .comparison_ui import source_comparison_panel
from .import_ui import import_panel
from .provider_ui import provider_controls

PRIMARY_BUTTON_CLASS = "rv-button rv-button--primary"


def page_heading(title: str, description: str) -> html.Div:
    return html.Div(
        [html.H1(title), html.P(description, className="rv-subtitle")], className="rv-page-heading"
    )


def case_header(cases: list[dict[str, Any]], session: dict[str, Any]) -> html.Header:
    return html.Header(
        [
            html.Div(
                [
                    html.Div(
                        [
                            html.Img(
                                src=get_asset_url("traced/logos/traced-primary.svg"),
                                alt="Traced",
                                className="traced-logo traced-logo--light",
                            ),
                            html.Img(
                                src=get_asset_url("traced/logos/traced-reversed.svg"),
                                alt="Traced",
                                className="traced-logo traced-logo--dark",
                            ),
                        ],
                        className="traced-lockup",
                    ),
                    html.Div(
                        [
                            html.Span("Evidence review workspace", className="rv-brand-caption"),
                            html.Span("Every claim. A clear trail.", className="rv-brand-tagline"),
                        ],
                        className="rv-brand-description",
                    ),
                ],
                className="rv-brand",
            ),
            html.Div(
                [
                    html.Label("Current case", htmlFor="rv-case", className="rv-small rv-muted"),
                    dcc.Dropdown(
                        id="rv-case",
                        options=[{"label": c["title"], "value": str(c["id"])} for c in cases],
                        value=session.get("case_id"),
                        persistence=True,
                        persistence_type="session",
                        clearable=False,
                        searchable=len(cases) > 5,
                    ),
                ],
                className="rv-case-picker",
            ),
        ],
        className="rv-header",
    )


def page_navigation(value: str = "review") -> dcc.Tabs:
    return dcc.Tabs(
        id="rv-page",
        value=value,
        className="rv-navigation",
        parent_className="rv-navigation-shell",
        children=[
            dcc.Tab(
                label=label,
                value=value,
                className="rv-navigation-tab",
                selected_className=(
                    f"rv-navigation-tab--selected rv-navigation-tab--selected-{value}"
                ),
            )
            for label, value in [
                ("Cases", "cases"),
                ("Review", "review"),
                ("Compare", "compare"),
                ("Briefing", "brief"),
            ]
        ],
    )


def assessment_header() -> html.Section:
    return html.Section(
        [
            html.Div(
                [html.Div(id="rv-context"), html.Div(id="rv-case-meta", className="rv-case-meta")],
                className="rv-context-copy",
            ),
            html.Div(
                [
                    html.Label("Assessment", htmlFor="rv-scenario", className="rv-small rv-muted"),
                    dcc.Dropdown(
                        id="rv-scenario",
                        options=[],
                        value=None,
                        clearable=False,
                        searchable=False,
                        placeholder="Baseline",
                    ),
                ],
                className="rv-scenario-picker",
            ),
            html.Button(
                "Return to baseline",
                id="rv-back-baseline",
                n_clicks=0,
                className="rv-button rv-button--quiet",
            ),
        ],
        className="rv-assessment-header",
        **{"aria-label": "Active assessment context"},
    )


def decision_panel() -> html.Section:
    return html.Section(
        [
            html.Div(
                [
                    html.H2("Record a decision", id="rv-decision-title"),
                    html.P(
                        "Choose how this statement should appear in the brief.",
                        className="rv-small rv-muted",
                    ),
                ],
                className="rv-section-heading",
            ),
            html.Div(
                [
                    html.Label("Reason or qualified wording", htmlFor="rv-review-reason"),
                    dcc.Textarea(
                        id="rv-review-reason",
                        value="",
                        placeholder="Explain what the supplied evidence supports…",
                        className="rv-textarea",
                    ),
                    html.Div(
                        [
                            html.Button(
                                "Use as written",
                                id="rv-use",
                                n_clicks=0,
                                className="rv-button rv-button--outline",
                            ),
                            html.Button(
                                "Use with qualification",
                                id="rv-qualify",
                                n_clicks=0,
                                className="rv-button rv-button--outline",
                            ),
                            html.Button(
                                "Withhold",
                                id="rv-withhold",
                                n_clicks=0,
                                className="rv-button rv-button--outline",
                            ),
                        ],
                        className="rv-review-buttons",
                    ),
                ],
                className="rv-review-input",
            ),
            html.Details(
                [html.Summary("Decision history"), html.Div(id="rv-review-history")],
                className="rv-details rv-history-details",
            ),
        ],
        id="rv-decision",
        className="rv-review-bar",
        tabIndex=-1,
        **{"aria-labelledby": "rv-decision-title"},
    )


def evidence_details(graph_style: list[dict[str, Any]]) -> html.Details:
    return html.Details(
        [
            html.Summary("Explore evidence connections"),
            html.P(
                "Evidence and premises point toward the selected finding. "
                "Connections record the stated basis for a claim.",
                className="rv-small rv-muted",
            ),
            html.Div(id="rv-graph-summary", role="status", **{"aria-live": "polite"}),
            dcc.Tabs(
                id="rv-tab",
                value="path",
                className="rv-tabs",
                children=[
                    dcc.Tab(
                        label="Evidence path",
                        value="path",
                        className="rv-tab",
                        selected_className="rv-tab--selected",
                    ),
                    dcc.Tab(
                        label="Source relationships",
                        value="groups",
                        className="rv-tab",
                        selected_className="rv-tab--selected",
                    ),
                ],
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.Button(
                                "Fit graph",
                                id="rv-graph-fit",
                                n_clicks=0,
                                className="rv-button rv-button--outline",
                            ),
                            html.Span(
                                "Select a node or connection for full labels and evidence.",
                                className="rv-small rv-muted",
                            ),
                        ],
                        className="rv-toolbar rv-graph-tools",
                    ),
                    cyto.Cytoscape(
                        id="rv-graph",
                        elements=[],
                        stylesheet=graph_style,
                        layout={"name": "preset", "padding": 25, "animate": False},
                        responsive=True,
                        autoRefreshLayout=False,
                        minZoom=0.5,
                        maxZoom=2.2,
                        style={"width": "100%", "height": "490px"},
                    ),
                    html.Div(
                        [
                            html.Span("Evidence / premise → selected finding"),
                            html.Span("Dashed records: excluded · Dashed red links: contradicts"),
                        ],
                        className="rv-legend",
                    ),
                ],
                id="rv-path-panel",
                className="rv-graph-canvas",
            ),
            html.P(
                "Use the connection list below on a narrow screen.",
                className="rv-small rv-muted rv-graph-mobile-note",
            ),
            html.Div(id="rv-graph-detail", **{"aria-live": "polite"}),
            html.Div(id="rv-graph-outline"),
            html.Div(id="rv-groups-panel", style={"display": "none"}),
        ],
        className="rv-details rv-evidence-connections",
        id="rv-evidence-connections",
    )


def challenge_details() -> html.Details:
    return html.Details(
        [
            html.Summary("Request an evidence check"),
            html.P(
                "Optional assistance. Your decision remains your own.",
                className="rv-small rv-muted",
            ),
            html.Div(
                [
                    html.Label("Check method", htmlFor="rv-challenge-mode"),
                    dcc.Dropdown(
                        id="rv-challenge-mode",
                        options=[
                            {"label": "Ollama · local model", "value": "ollama"},
                            {"label": "Demo · deterministic fixture", "value": "demo"},
                            {"label": "OpenRouter · live model", "value": "openrouter"},
                        ],
                        value="demo",
                        clearable=False,
                        searchable=False,
                        className="rv-mode-picker",
                    ),
                    provider_controls(),
                    html.Button(
                        "Run evidence check",
                        id="rv-challenge",
                        n_clicks=0,
                        className="rv-button rv-button--outline",
                    ),
                ],
                className="rv-check-controls",
            ),
            html.Div(id="rv-job", **{"aria-live": "polite"}),
        ],
        className="rv-details rv-challenge",
    )


def source_inspector_panel() -> html.Aside:
    return html.Aside(
        [
            html.H2("Exact evidence", className="rv-section-heading"),
            html.Div(id="rv-media-slot"),
            html.Div(id="rv-source"),
            html.Details(
                [
                    html.Summary("Test without this record"),
                    html.P(
                        "Create a reversible scenario. "
                        "The baseline and saved record are preserved.",
                        className="rv-small rv-muted",
                    ),
                    html.Label("Reason for this test", htmlFor="rv-exclude-reason"),
                    dcc.Textarea(
                        id="rv-exclude-reason",
                        value="",
                        placeholder="Why test without this evidence?",
                        className="rv-textarea",
                    ),
                    html.Button(
                        "Create exclusion scenario",
                        id="rv-toggle-source",
                        n_clicks=0,
                        className="rv-button rv-button--outline",
                    ),
                ],
                className="rv-details rv-source-actions",
            ),
        ],
        id="rv-inspector",
        className="rv-inspector",
        tabIndex=-1,
        **{"aria-label": "Exact evidence inspector"},
    )


def review_page(graph_style: list[dict[str, Any]]) -> html.Section:
    return html.Section(
        [
            html.Div(
                [
                    html.Section(
                        [
                            html.Div(
                                [
                                    html.Span("Current finding", className="rv-kicker"),
                                    html.Div(id="rv-finding-heading"),
                                    html.Div(
                                        id="rv-save-state",
                                        role="status",
                                        **{"aria-live": "polite"},
                                    ),
                                ],
                                className="rv-finding-copy",
                            ),
                            html.Div(
                                [
                                    html.A(
                                        "View evidence",
                                        href="#rv-inspector",
                                        className="rv-button rv-button--outline rv-evidence-link",
                                    ),
                                    html.A(
                                        "Record decision ↓",
                                        href="#rv-decision",
                                        className=PRIMARY_BUTTON_CLASS,
                                    ),
                                ],
                                className="rv-finding-actions",
                            ),
                        ],
                        className="rv-finding-intro",
                        **{"aria-label": "Current finding"},
                    ),
                    html.Aside(
                        [
                            html.Div(
                                [
                                    html.H2("Findings"),
                                    html.Span(id="rv-finding-count", className="rv-count"),
                                ],
                                className="rv-panel-heading",
                            ),
                            html.Div(id="rv-findings", className="rv-findings"),
                        ],
                        className="rv-queue",
                        **{"aria-label": "Finding queue"},
                    ),
                    html.Section(
                        [
                            html.Div(id="rv-finding-summary", className="rv-finding-summary"),
                            html.Section(
                                [
                                    html.H3("Evidence for this finding"),
                                    html.Div(id="rv-source-buttons", className="rv-linked-sources"),
                                ],
                                className="rv-evidence-section",
                            ),
                            html.Div(id="rv-domain-slot"),
                        ],
                        className="rv-center rv-finding-evidence",
                    ),
                    source_inspector_panel(),
                    html.Div(
                        [decision_panel(), challenge_details()],
                        className="rv-center rv-finding-decision",
                    ),
                ],
                className="rv-workspace",
                id="rv-workspace",
            ),
            evidence_details(graph_style),
        ],
        id="rv-review-page",
    )


def cases_page() -> html.Section:
    validated_import = import_panel()
    validated_import.children[0] = html.Summary("Advanced: validate investigation JSON")
    return html.Section(
        [
            page_heading(
                "Your cases",
                "Open an investigation, inspect its evidence, and prepare a qualified briefing.",
            ),
            html.Div(id="rv-case-tools"),
            validated_import,
            html.Div(id="rv-case-list", className="rv-case-list"),
        ],
        id="rv-cases-page",
        className="rv-page-surface",
        style={"display": "none"},
    )


def compare_page() -> html.Section:
    return html.Section(
        [
            page_heading(
                "Compare sources and assessments",
                "Inspect saved source wording and see what changes when evidence is excluded.",
            ),
            source_comparison_panel(),
            html.H2("Compare assessments"),
            html.Div(id="rv-compare-summary"),
            html.Div(id="rv-compare-tools"),
        ],
        id="rv-compare-page",
        className="rv-page-surface",
        style={"display": "none"},
    )


def brief_page() -> html.Section:
    return html.Section(
        [
            page_heading(
                "Prepare your brief",
                "Preview saved decisions, qualifications, and exact references before export.",
            ),
            html.Div(id="rv-brief-preview"),
            html.Div(id="rv-export-tools"),
        ],
        id="rv-brief-page",
        className="rv-page-surface",
        style={"display": "none"},
    )
