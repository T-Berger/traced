"""Three-screen analyst workflow, independent of callbacks and persistence."""

from __future__ import annotations

import dash_cytoscape as cyto
from dash import dcc, get_asset_url, html

from .service import DECISIONS
from .ui import GRAPH_STYLES, empty_figure, section_heading


def _plot(identifier: str, height: int):
    return dcc.Graph(
        id=identifier,
        figure=empty_figure("Select a case to inspect its evidence.", height),
        className="bounded-plot",
        responsive=True,
        style={"height": f"{height}px", "minHeight": 0, "width": "100%"},
        config={"displayModeBar": False, "responsive": True},
    )


def _dropdown(identifier, options=None, value=None, **kwargs):
    return dcc.Dropdown(
        id=identifier, options=options or [], value=value, clearable=False, **kwargs
    )


def create_layout(
    overview: dict,
    boot_error: str | None = None,
    readiness: dict | None = None,
    workbench_links: list[tuple[str, str]] | None = None,
) -> html.Div:
    readiness = readiness or overview.get("readiness") or {}
    synthetic = bool(overview.get("synthetic", readiness.get("synthetic", True)))
    run_id = readiness.get("run_id", overview.get("run_id"))
    workspace = {
        "overview": overview,
        "readiness": readiness,
        "run_id": run_id,
        "dataset_id": readiness.get("dataset_id", overview.get("dataset_id")),
    }
    label = readiness.get("label") or overview.get("dataset_label") or "No dataset loaded"
    return html.Div(
        [
            dcc.Store(id="workspace-store", data=workspace),
            dcc.Store(id="screen-select", data="find"),
            dcc.Store(id="detail-store"),
            dcc.Store(id="selected-pair-store", data={}),
            dcc.Store(id="review-tick", data=0),
            dcc.Store(id="form-state", data={}),
            dcc.Store(id="drafts-store", data={}),
            dcc.Store(id="preview-store", data={}),
            dcc.Download(id="download-case"),
            dcc.Interval(id="replay-clock", interval=650, disabled=True),
            html.A("Skip to main content", href="#main-content", className="skip-link"),
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
                            html.P("Swarm Detector", className="traced-specialist"),
                        ],
                        className="brand traced-brand",
                    ),
                    html.Nav(
                        [
                            html.A(label, href=href, className="traced-switcher-link")
                            for label, href in (workbench_links or [])
                        ],
                        className="traced-switcher traced-switcher--inverse",
                        **{"aria-label": "Traced workbench surfaces"},
                    )
                    if workbench_links
                    else None,
                    html.P(
                        "From coordinated activity to inspectable evidence.", className="brand-note"
                    ),
                    html.Nav(
                        [
                            html.Button(
                                [html.Span("01"), html.Strong("Find groups")],
                                id="nav-find",
                                n_clicks=0,
                                className="nav-step active",
                            ),
                            html.Button(
                                [html.Span("02"), html.Strong("Verify evidence")],
                                id="nav-verify",
                                n_clicks=0,
                                className="nav-step",
                            ),
                            html.Button(
                                [html.Span("03"), html.Strong("Review & brief")],
                                id="nav-review",
                                n_clicks=0,
                                className="nav-step",
                            ),
                        ],
                        **{"aria-label": "Investigation steps"},
                    ),
                    html.Div(
                        [
                            html.Span("DATASET", className="sidebar-label"),
                            html.Div(
                                "SAMPLE DATA" if synthetic else "IMPORTED DATA",
                                className="dataset-badge",
                            ),
                            html.P(label, className="dataset-name"),
                            html.P(
                                "Synthetic examples. These are not real findings."
                                if synthetic
                                else "Conclusions apply only to supplied observations.",
                                className="dataset-note",
                            ),
                            html.Div(
                                [
                                    html.Span("ANALYSIS RUN", className="sidebar-label"),
                                    html.Code(
                                        run_id or "Awaiting analysis",
                                        title=run_id or "No saved analysis",
                                    ),
                                ],
                                className="sidebar-run",
                            ),
                        ],
                        className="dataset-panel",
                    ),
                    html.Div(
                        [
                            html.Span(className="live-dot"),
                            html.Span("LOCAL WORKSPACE"),
                            html.P(
                                "A candidate is a request for review. Criticism alone is never a coordination signal."
                            ),
                        ],
                        className="sidebar-footer",
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
                                    html.P(
                                        "FIND GROUPS / VERIFY EVIDENCE / REVIEW & BRIEF",
                                        className="page-kicker",
                                    ),
                                    html.H1(
                                        id="screen-title", children="What deserves investigation?"
                                    ),
                                    html.P(
                                        id="screen-subtitle",
                                        children="Start with usable data and a shortlist based on behavior.",
                                        className="page-subtitle",
                                    ),
                                ]
                            ),
                            html.Span(
                                "SAMPLE DATA" if synthetic else "IMPORTED DATA",
                                className="sample-badge",
                            ),
                        ],
                        className="page-header",
                    ),
                    html.Div(boot_error, className="error-banner", role="alert")
                    if boot_error
                    else None,
                    html.Div(
                        [
                            html.Label("Selected case", htmlFor="case-select"),
                            _dropdown(
                                "case-select",
                                placeholder="Choose a candidate or comparison",
                                disabled=not bool(run_id),
                            ),
                            html.Span(
                                "Pinned to this dataset and saved analysis.", className="field-help"
                            ),
                        ],
                        id="compact-case-picker",
                        className="compact-case-picker",
                    ),
                    html.Div(id="case-header", className="case-header"),
                    html.Div(
                        [
                            html.Section(
                                [
                                    section_heading(
                                        "INPUT READINESS", "Know what the data can support"
                                    ),
                                    html.Div(id="readiness-checks", className="readiness-grid"),
                                    html.Div(id="coverage-summary", className="coverage-summary"),
                                    html.Details(
                                        [
                                            html.Summary(
                                                "Coverage, limitations & analysis settings"
                                            ),
                                            html.Div(id="readiness-limitations"),
                                        ]
                                    ),
                                ],
                                className="panel readiness-panel",
                            ),
                            html.Div(
                                [
                                    html.Section(
                                        [
                                            section_heading(
                                                "BEHAVIOR-RANKED SHORTLIST",
                                                "Candidates and comparisons",
                                            ),
                                            html.Div(
                                                [
                                                    html.Div(
                                                        [
                                                            html.Label(
                                                                "Search cases or accounts",
                                                                htmlFor="case-search",
                                                            ),
                                                            dcc.Input(
                                                                id="case-search",
                                                                type="search",
                                                                debounce=True,
                                                                placeholder="Case name or account ID",
                                                                className="field-input",
                                                            ),
                                                        ]
                                                    ),
                                                    html.Div(
                                                        [
                                                            html.Label("Type", htmlFor="case-kind"),
                                                            _dropdown(
                                                                "case-kind",
                                                                [
                                                                    {
                                                                        "label": "All cases",
                                                                        "value": "all",
                                                                    },
                                                                    {
                                                                        "label": "Candidates",
                                                                        "value": "candidate",
                                                                    },
                                                                    {
                                                                        "label": "Comparisons",
                                                                        "value": "comparison",
                                                                    },
                                                                ],
                                                                "all",
                                                                searchable=False,
                                                            ),
                                                        ]
                                                    ),
                                                    html.Div(
                                                        [
                                                            html.Label(
                                                                "Review status",
                                                                htmlFor="case-status",
                                                            ),
                                                            _dropdown(
                                                                "case-status",
                                                                [
                                                                    {
                                                                        "label": "All reviews",
                                                                        "value": "all",
                                                                    },
                                                                    {
                                                                        "label": "Unreviewed",
                                                                        "value": "unreviewed",
                                                                    },
                                                                    {
                                                                        "label": "Reviewed",
                                                                        "value": "reviewed",
                                                                    },
                                                                ],
                                                                "all",
                                                                searchable=False,
                                                            ),
                                                        ]
                                                    ),
                                                ],
                                                className="queue-filters",
                                            ),
                                            html.Div(
                                                [
                                                    html.Span(id="queue-count"),
                                                    html.Button(
                                                        "Clear filters",
                                                        id="clear-filters",
                                                        n_clicks=0,
                                                        className="text-button",
                                                    ),
                                                ],
                                                className="queue-heading",
                                            ),
                                            dcc.Loading(
                                                html.Div(id="queue-list", className="queue-list"),
                                                type="circle",
                                                color="#087f82",
                                            ),
                                            html.P(
                                                "No political stance, language or narrative label contributes to rank.",
                                                className="field-help",
                                            ),
                                        ],
                                        className="panel shortlist-panel",
                                    ),
                                    html.Section(
                                        [
                                            section_heading(
                                                "SELECTED CASE", "Choose where to look closer"
                                            ),
                                            html.Button(
                                                "Open evidence →",
                                                id="open-evidence",
                                                n_clicks=0,
                                                className="button primary",
                                                disabled=True,
                                            ),
                                            dcc.Loading(
                                                html.Div(id="case-preview"),
                                                type="circle",
                                                color="#087f82",
                                            ),
                                        ],
                                        className="panel case-preview-panel",
                                    ),
                                ],
                                className="find-grid",
                            ),
                        ],
                        id="find-view",
                        className="workflow-view",
                    ),
                    html.Div(
                        [
                            html.Div(id="case-metrics", className="case-metrics"),
                            html.Div(
                                [
                                    dcc.Checklist(
                                        id="reveal-text",
                                        options=[
                                            {
                                                "label": "Reveal original source text",
                                                "value": "reveal",
                                            }
                                        ],
                                        value=[],
                                    ),
                                    html.P(
                                        "Timing and repeated participation determine rank. Text is a separate investigation step."
                                    ),
                                ],
                                className="reveal-bar",
                            ),
                            html.Div(
                                [
                                    html.Section(
                                        [
                                            section_heading(
                                                "FIRST ARRIVALS",
                                                "Repeated accounts across discussions",
                                                "Click a matrix cell to open its discussion. Blank cells mean no observed comment.",
                                            ),
                                            _plot("timing-matrix", 360),
                                            html.P(id="matrix-footnote", className="chart-note"),
                                        ],
                                        className="panel matrix-panel",
                                    ),
                                    html.Details(
                                        [
                                            html.Summary("Account connections · focused graph"),
                                            html.Div(
                                                [
                                                    html.Div(
                                                        [
                                                            html.Button(
                                                                "Fit connections",
                                                                id="pair-graph-fit",
                                                                n_clicks=0,
                                                                className="button secondary",
                                                            ),
                                                            html.Span(
                                                                "Click a line to inspect that exact pair.",
                                                                className="chart-note",
                                                            ),
                                                        ],
                                                        className="replay-toolbar",
                                                    ),
                                                    cyto.Cytoscape(
                                                        id="pair-graph",
                                                        elements=[],
                                                        layout={
                                                            "name": "preset",
                                                            "fit": True,
                                                            "padding": 18,
                                                        },
                                                        style={"height": "360px", "width": "100%"},
                                                        stylesheet=GRAPH_STYLES,
                                                        responsive=True,
                                                        autoungrabify=True,
                                                        boxSelectionEnabled=False,
                                                        minZoom=0.55,
                                                        maxZoom=2.2,
                                                        wheelSensitivity=0.15,
                                                    ),
                                                    html.Div(id="account-summary"),
                                                    html.P(
                                                        "Green accounts: candidate members. Gray: context accounts. "
                                                        "Solid lines: retained by detector; dashed: context only. "
                                                        "Orange line: selected pair. A-labels identify the full IDs below.",
                                                        className="chart-note",
                                                    ),
                                                ],
                                                className="details-body",
                                            ),
                                        ],
                                        id="connections-disclosure",
                                        className="panel connection-disclosure",
                                    ),
                                ],
                                className="evidence-grid",
                            ),
                            html.Section(
                                [
                                    section_heading(
                                        "SELECT EVIDENCE", "One pair. Its exact episodes."
                                    ),
                                    html.Div(
                                        [
                                            html.Div(
                                                [
                                                    html.Label(
                                                        "Account pair", htmlFor="pair-select"
                                                    ),
                                                    _dropdown(
                                                        "pair-select",
                                                        placeholder="No pair evidence in this case",
                                                    ),
                                                ]
                                            ),
                                            html.Div(
                                                [
                                                    html.Label(
                                                        "Discussion", htmlFor="episode-select"
                                                    ),
                                                    _dropdown(
                                                        "episode-select",
                                                        placeholder="Select a discussion",
                                                    ),
                                                ]
                                            ),
                                        ],
                                        className="selection-grid",
                                    ),
                                    html.Div(
                                        [
                                            html.Button(
                                                "▶ Replay arrivals",
                                                id="play-replay",
                                                n_clicks=0,
                                                className="button secondary",
                                            ),
                                            html.Span(id="replay-label"),
                                        ],
                                        className="replay-toolbar",
                                    ),
                                    _plot("episode-chart", 420),
                                    dcc.Slider(
                                        id="replay-progress",
                                        min=0,
                                        max=100,
                                        step=1,
                                        value=100,
                                        marks={0: "Parent post", 100: "All observed comments"},
                                        tooltip={"placement": "bottom"},
                                    ),
                                    html.P(
                                        "Green circles: case accounts. Gray circles: surrounding activity. Manual replay remains available.",
                                        className="chart-note",
                                    ),
                                ],
                                className="panel replay-panel",
                            ),
                            html.Section(
                                [
                                    section_heading(
                                        "ORIGINAL RECORDS",
                                        "Parent, replies and surrounding comments",
                                    ),
                                    html.Div(id="episode-context"),
                                    html.Div(id="source-records"),
                                ],
                                className="panel source-panel",
                            ),
                            html.Div(
                                [
                                    html.Section(
                                        [
                                            section_heading(
                                                "SELECTED PAIR",
                                                "Observed versus shuffled",
                                                "Both counts use distinct discussion families; neither is a bot probability.",
                                            ),
                                            html.Div(id="pair-title", className="pair-title"),
                                            _plot("baseline-chart", 190),
                                            html.Div(id="pair-detail"),
                                        ],
                                        className="panel",
                                    ),
                                    html.Section(
                                        [
                                            section_heading(
                                                "CHALLENGE THE EXPLANATION",
                                                "What could explain this?",
                                            ),
                                            html.Div(id="counterevidence"),
                                            html.Div(
                                                id="sensitivity-summary",
                                                className="sensitivity-box",
                                            ),
                                            html.Button(
                                                "Remove the strongest episode family",
                                                id="stress-test",
                                                n_clicks=0,
                                                className="button secondary",
                                            ),
                                            dcc.Loading(
                                                html.Div(id="stress-result", role="status"),
                                                type="circle",
                                                color="#087f82",
                                            ),
                                        ],
                                        className="panel",
                                    ),
                                ],
                                className="two-column",
                            ),
                            html.Div(
                                [
                                    html.P(
                                        "Next: inspect where these accounts participated and read their words in context."
                                    ),
                                    html.Button(
                                        "Assess community context →",
                                        id="continue-context",
                                        n_clicks=0,
                                        className="button primary",
                                        disabled=True,
                                    ),
                                ],
                                className="screen-action",
                            ),
                        ],
                        id="verify-view",
                        className="workflow-view",
                        style={"display": "none"},
                    ),
                    html.Div(
                        [
                            html.Div(
                                id="review-draft-status",
                                className="review-draft-status",
                                role="status",
                            ),
                            html.Div(
                                [
                                    html.Div(
                                        [
                                            html.Section(
                                                [
                                                    section_heading(
                                                        "OBSERVED PARTICIPATION",
                                                        "Community activity in this dataset",
                                                        "Activity order does not establish influence, nationality or origin.",
                                                    ),
                                                    html.Div(
                                                        id="community-capability",
                                                        className="field-help",
                                                    ),
                                                    _plot("community-timeline", 360),
                                                    html.Div(id="channel-list"),
                                                ],
                                                className="panel",
                                            ),
                                            html.Section(
                                                [
                                                    section_heading(
                                                        "CONTEXT BESIDE JUDGMENT",
                                                        "Read the original evidence",
                                                    ),
                                                    dcc.Checklist(
                                                        id="review-reveal-text",
                                                        options=[
                                                            {
                                                                "label": "Reveal original source text",
                                                                "value": "reveal",
                                                            }
                                                        ],
                                                        value=[],
                                                    ),
                                                    html.Div(id="context-quotes"),
                                                    html.Details(
                                                        [
                                                            html.Summary(
                                                                "Shared content · excluded from behavioral rank"
                                                            ),
                                                            html.Div(id="content-observations"),
                                                        ]
                                                    ),
                                                ],
                                                className="panel source-panel",
                                            ),
                                            html.Section(
                                                [
                                                    section_heading(
                                                        "REMAINING GAPS",
                                                        "Keep alternative explanations visible",
                                                    ),
                                                    html.Div(id="review-counterevidence"),
                                                ],
                                                className="panel",
                                            ),
                                        ],
                                        className="review-context-column",
                                    ),
                                    html.Section(
                                        [
                                            section_heading(
                                                "ANALYST JUDGMENT", "Record a bounded conclusion"
                                            ),
                                            html.Div(
                                                id="existing-review", className="existing-review"
                                            ),
                                            html.Label("Reviewer", htmlFor="reviewer-name"),
                                            dcc.Input(
                                                id="reviewer-name",
                                                type="text",
                                                placeholder="Name or analyst ID",
                                                className="field-input",
                                            ),
                                            html.Label(
                                                "Behavioral decision", htmlFor="review-decision"
                                            ),
                                            _dropdown(
                                                "review-decision",
                                                [{"label": d, "value": d} for d in DECISIONS],
                                                "Insufficient evidence",
                                                searchable=False,
                                            ),
                                            html.Label(
                                                "Reason and alternative explanations",
                                                htmlFor="review-reason",
                                            ),
                                            dcc.Textarea(
                                                id="review-reason",
                                                placeholder="Explain what supports or weakens coordination and what remains unknown…",
                                                className="field-textarea",
                                            ),
                                            html.H3(
                                                "Separate narrative annotation",
                                                className="form-subheading",
                                            ),
                                            html.P(
                                                "Criticism, quotation and endorsement are different. These labels never change rank.",
                                                className="field-help",
                                            ),
                                            html.Label(
                                                "Claim or narrative observed",
                                                htmlFor="narrative-claim",
                                            ),
                                            dcc.Input(
                                                id="narrative-claim",
                                                type="text",
                                                placeholder="Describe a claim without attributing an operator",
                                                className="field-input",
                                            ),
                                            html.Label(
                                                "Use of the claim in context",
                                                htmlFor="narrative-endorsement",
                                            ),
                                            _dropdown(
                                                "narrative-endorsement",
                                                [
                                                    {"label": a, "value": b}
                                                    for a, b in [
                                                        ("Endorses the claim", "endorses"),
                                                        ("Rebuts the claim", "rebuts"),
                                                        ("Quotes or reports it", "quotes"),
                                                        ("Unclear", "unclear"),
                                                    ]
                                                ],
                                                "unclear",
                                                searchable=False,
                                            ),
                                            html.Label(
                                                "Narrative alignment", htmlFor="narrative-alignment"
                                            ),
                                            _dropdown(
                                                "narrative-alignment",
                                                [
                                                    {"label": a, "value": b}
                                                    for a, b in [
                                                        ("Unclear / not assessed", "unclear"),
                                                        (
                                                            "Pro-Kremlin alignment supported by context",
                                                            "supported",
                                                        ),
                                                        ("Not established", "not_established"),
                                                        ("Mixed", "mixed"),
                                                    ]
                                                ],
                                                "unclear",
                                                searchable=False,
                                            ),
                                            html.Label(
                                                "Supporting original comments",
                                                htmlFor="supporting-evidence",
                                            ),
                                            _dropdown(
                                                "supporting-evidence",
                                                value=[],
                                                multi=True,
                                                placeholder="Choose source records",
                                            ),
                                            html.Label(
                                                "Contextual reasoning and uncertainty",
                                                htmlFor="narrative-notes",
                                            ),
                                            dcc.Textarea(
                                                id="narrative-notes",
                                                placeholder="Explain endorsement, rebuttal, quotation or uncertainty, with the source context…",
                                                className="field-textarea",
                                            ),
                                            html.Div(
                                                [
                                                    html.Button(
                                                        "Save review",
                                                        id="save-review",
                                                        n_clicks=0,
                                                        className="button primary",
                                                    ),
                                                    html.Button(
                                                        "Discard this case's draft",
                                                        id="discard-review",
                                                        n_clicks=0,
                                                        className="button secondary",
                                                    ),
                                                ],
                                                className="review-actions",
                                            ),
                                            html.Div(
                                                id="save-status",
                                                className="save-status",
                                                role="status",
                                            ),
                                        ],
                                        className="panel review-form",
                                    ),
                                ],
                                className="review-grid",
                            ),
                            html.Section(
                                [
                                    section_heading(
                                        "SAVED SNAPSHOT",
                                        "Preview the brief before exporting",
                                        "Exports use the exact saved revision shown below. Unsaved drafts are excluded.",
                                    ),
                                    html.Div(
                                        [
                                            html.Button(
                                                "Preview saved brief",
                                                id="render-brief",
                                                n_clicks=0,
                                                className="button secondary",
                                            ),
                                            html.Span(
                                                id="preview-revision", className="field-help"
                                            ),
                                        ],
                                        className="brief-toolbar",
                                    ),
                                    dcc.Loading(
                                        dcc.Markdown(
                                            id="brief-preview",
                                            children="Create a preview to inspect the saved case file.",
                                            className="brief-preview",
                                            link_target="_blank",
                                        ),
                                        type="circle",
                                        color="#087f82",
                                    ),
                                    html.Div(
                                        [
                                            _dropdown(
                                                "export-format",
                                                [
                                                    {
                                                        "label": "Markdown brief",
                                                        "value": "markdown",
                                                    },
                                                    {"label": "JSON evidence", "value": "json"},
                                                    {"label": "CSV records", "value": "csv"},
                                                ],
                                                "markdown",
                                                searchable=False,
                                            ),
                                            html.Button(
                                                "↓ Export previewed revision",
                                                id="export-case",
                                                n_clicks=0,
                                                className="button secondary",
                                                disabled=True,
                                            ),
                                        ],
                                        className="export-actions",
                                    ),
                                    html.Div(
                                        id="export-status", role="status", className="field-help"
                                    ),
                                ],
                                className="panel brief-panel",
                            ),
                            html.Div(id="run-details", className="run-details"),
                        ],
                        id="review-view",
                        className="workflow-view",
                        style={"display": "none"},
                    ),
                    html.Footer(
                        [
                            html.Span("Traced · Swarm Detector"),
                            html.Span(
                                "Describe behavior. Preserve uncertainty. Attribute no operator."
                            ),
                        ],
                        className="page-footer",
                    ),
                ],
                id="main-content",
                className="main-canvas",
            ),
        ],
        className="app-shell traced-app traced-swarm",
    )
