"""Takedown readiness page: is there enough evidence to report a post to X?"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import plotly.graph_objects as go
from dash import Input, Output, State, ctx, dcc, html, no_update
from pydantic import ValidationError

from ..review.comparison_ui import safe_source_url
from . import analysis
from .analysis import NEEDS, READY, Assessment
from .pdf_report import build_pdf
from .repository import AnalyticsRepository, AnalyticsUnavailable
from .settings import CATEGORIES, CATEGORY_LABELS, AlertSettings, AlertSettingsStore

PAGE_ID = "rv-alerts-page"
INK = "#131212"
FONT = '"Trebuchet MS", "Lucida Grande", "Segoe UI", sans-serif'
COLORS = {
    "disinformation": "#038095",
    "racism": "#B4472C",
    "hate_speech": "#D9A21B",
    "harassment": "#5A4E9C",
}
FORM_KEYS = [(c, f) for c in CATEGORIES for f in ("label_score", "filing_score")]


def _snapshot_fingerprint(data, settings) -> str:
    """Bind a displayed selection to all inputs used to construct the draft report."""
    payload = {
        "case": data.case,
        "total_posts": data.total_posts,
        "labels": data.labels,
        "references": data.references,
        "last_run": data.last_run,
        "bursts": data.bursts,
        "settings": settings.model_dump(mode="json"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _field_id(category: str, field: str) -> str:
    return f"al-th-{category}-{field}".replace("_", "-")


# --------------------------------------------------------------------------- helpers


def _compact(n) -> str:
    if n is None:
        return "Unknown"
    n = int(n)
    for div, suffix in ((1_000_000, "M"), (1_000, "K")):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + suffix
    return str(n)


def _excerpt(text: str | None, n: int = 280) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _date(ts) -> str:
    return ts.strftime("%d %b %Y") if ts else "Undated"


def _link(url: str | None, label: str = "Open post") -> Any:
    safe = safe_source_url(url)
    if not safe:
        return html.Span("Source link unavailable", className="al-muted")
    return html.A(label, href=safe, target="_blank", rel="noopener noreferrer", className="al-link")


def _pill(text: str, kind: str) -> html.Span:
    return html.Span(text, className=f"al-pill al-pill--{kind}")


def _cat(category: str) -> html.Span:
    return html.Span(
        CATEGORY_LABELS[category], className="al-cat", style={"--al-accent": COLORS[category]}
    )


def _notice(message: str, kind: str = "info") -> html.Div:
    return html.Div(message, className=f"al-notice al-notice--{kind}", role="status")


def _section(title: str, subtitle: str, body, *, section_id: str, extra=None) -> html.Section:
    head = [html.Div([html.H2(title, className="al-h2"), html.P(subtitle, className="al-sub")])]
    if extra is not None:
        head.append(extra)
    return html.Section(
        [html.Div(head, className="al-section-head"), body], className="al-card", id=section_id
    )


# --------------------------------------------------------------------------- layout


def _settings_panel(settings: AlertSettings, *, read_only: bool = False) -> html.Details:
    rows = []
    for c in CATEGORIES:
        values = settings.categories[c]
        cells = [html.Th(CATEGORY_LABELS[c], scope="row")]
        for field, label in (("label_score", "candidate"), ("filing_score", "review")):
            cells.append(
                html.Td(
                    html.Label(
                        [
                            html.Span(f"{CATEGORY_LABELS[c]} {label} score", className="al-sr"),
                            dcc.Input(
                                id=_field_id(c, field),
                                type="number",
                                min=0,
                                max=1,
                                step=0.05,
                                value=getattr(values, field),
                                className="al-input al-input--num",
                            ),
                        ]
                    )
                )
            )
        rows.append(html.Tr(cells))

    def field(label, component, hint=None):
        return html.Label(
            [html.Span(label, className="al-label"), component]
            + ([html.Span(hint, className="al-hint")] if hint else []),
            className="al-field",
        )

    return html.Details(
        [
            html.Summary(
                [
                    html.Span("Evidence standard", className="al-h2"),
                    html.Span("Thresholds and report preparer · saved in this workspace",
                              className="al-sub"),
                ],
                className="al-settings-summary",
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.H3("Scores", className="al-h3"),
                            html.P(
                                "Score thresholds select candidates; they do not establish "
                                "a violation or a human decision. Scores run from 0 to 1.",
                                className="al-hint",
                            ),
                            html.Table(
                                [
                                    html.Thead(html.Tr([html.Th("Category"), html.Th("Candidate"),
                                                        html.Th("Evidence review")])),
                                    html.Tbody(rows),
                                ],
                                className="al-table al-table--compact",
                            ),
                        ],
                        className="al-settings-col",
                    ),
                    html.Div(
                        [
                            html.H3("Evidence rules", className="al-h3"),
                            field(
                                "Group accounts after",
                                dcc.Input(id="al-account-ready", type="number", min=1, step=1,
                                          value=settings.account_ready_posts,
                                          className="al-input al-input--num"),
                                "candidate posts",
                            ),
                            field(
                                "High recorded views from",
                                dcc.Input(id="al-high-reach", type="number", min=0, step=100,
                                          value=settings.high_reach_views,
                                          className="al-input al-input--num"),
                                "views",
                            ),
                            dcc.Checklist(
                                id="al-require-archive",
                                options=[{"label": " Require an independent archive copy",
                                          "value": "yes"}],
                                value=["yes"] if settings.require_archive_copy else [],
                                className="al-check",
                            ),
                        ],
                        className="al-settings-col",
                    ),
                    html.Div(
                        [
                            html.H3("Report preparer", className="al-h3"),
                            html.P("Printed on the evidence report as the person who prepared it.",
                                   className="al-hint"),
                            field("Name", dcc.Input(id="al-notifier-name", type="text",
                                                    value=settings.notifier_name,
                                                    className="al-input")),
                            field("Email", dcc.Input(id="al-notifier-email", type="email",
                                                     value=settings.notifier_email,
                                                     className="al-input")),
                            field("Organisation", dcc.Input(id="al-notifier-org", type="text",
                                                            value=settings.notifier_organisation,
                                                            className="al-input")),
                        ],
                        className="al-settings-col",
                    ),
                ],
                className="al-settings-grid",
            ),
            html.Div(
                [
                    html.Button("Save", id="al-save", n_clicks=0, disabled=read_only,
                                className="al-btn al-btn--primary"),
                    html.Button("Restore defaults", id="al-reset", n_clicks=0, disabled=read_only,
                                className="al-btn al-btn--ghost"),
                    html.P("Settings cannot be changed in this read-only workspace.",
                           className="al-hint") if read_only else None,
                    html.Div(id="al-settings-message", **{"aria-live": "polite"}),
                ],
                className="al-actions",
            ),
        ],
        className="al-card al-settings",
        id="al-settings",
    )  # fmt: skip


def alerts_page(store: AlertSettingsStore, *, read_only: bool = False) -> html.Section:
    settings, problem = store.load()
    return html.Section(
        html.Div(
            [
                dcc.Store(id="al-settings-rev", data=0),
                dcc.Store(id="al-selection-snapshot", data=None),
                dcc.Download(id="al-download"),
                html.Header(
                    [
                        html.Div(
                            [
                                html.Span("Review candidates", className="al-eyebrow"),
                                html.H1("Evidence for human review", className="al-title"),
                                html.P(
                                    "Inspect model-labeled posts and their available evidence. "
                                    "A human must decide whether any report is appropriate.",
                                    className="al-lede",
                                ),
                            ],
                            className="al-top-copy",
                        ),
                        html.Div(
                            [
                                html.Label("Subject", htmlFor="al-case", className="al-label"),
                                html.Div(
                                    [
                                        dcc.Dropdown(id="al-case", options=[], value=None,
                                                     clearable=False, searchable=False,
                                                     placeholder="Select an analytics subject",
                                                     className="al-dropdown"),
                                        html.Button("Refresh", id="al-refresh", n_clicks=0,
                                                    className="al-btn al-btn--ghost-light"),
                                    ],
                                    className="al-top-row",
                                ),
                            ],
                            className="al-top-controls",
                        ),
                    ],
                    className="al-top",
                ),
                _notice(problem, "warning") if problem else None,
                dcc.Loading(html.Div(id="al-summary"), type="dot", color="#038095"),
                _section(
                    "Candidates for review",
                    "Selected candidates have the listed material. They are model proposals, "
                    "not confirmed violations or review decisions.",
                    html.Div(
                        [
                            dcc.Checklist(id="al-ev-select", options=[], value=[],
                                          className="al-ready-list",
                                          labelClassName="al-ready-item",
                                          inputClassName="al-ready-check"),
                            html.Div(id="al-ready-empty"),
                        ]
                    ),
                    section_id="al-ready",
                    extra=html.Div(
                        [
                            html.Button("Download draft evidence PDF", id="al-download-selected",
                                        n_clicks=0, className="al-btn al-btn--primary"),
                            html.Span(id="al-download-message", className="al-sub",
                                      **{"aria-live": "polite"}),
                        ],
                        className="al-head-actions",
                    ),
                ),
                _section(
                    "Other model candidates",
                    "Model candidates below the review threshold or missing evidence.",
                    html.Div(id="al-needs"),
                    section_id="al-needs-section",
                ),
                _section(
                    "Accounts",
                    "Account grouping of model candidates; inspect evidence before deciding "
                    "whether any action is appropriate.",
                    html.Div(id="al-accounts"),
                    section_id="al-accounts-section",
                ),
                _section(
                    "Model candidates over time",
                    "Model-labeled posts by UTC day in the saved sample. Markers show candidates "
                    "with the listed material available.",
                    dcc.Graph(id="al-timeline", config={"displayModeBar": False},
                              responsive=True, className="al-chart"),
                    section_id="al-timeline-section",
                ),
                _settings_panel(settings, read_only=read_only),
                html.P(
                    "Labels and scores are model proposals. No human decision is recorded here. "
                    "Check saved sources, context, and platform rules independently.",
                    className="al-footnote",
                ),
            ],
            className="al-shell",
        ),
        id=PAGE_ID,
        className="rv-page-surface al-page",
        style={"display": "none"},
    )  # fmt: skip


# --------------------------------------------------------------------------- page parts


def _summary(data, assessments, accounts, settings) -> html.Div:
    verdict = analysis.verdict(assessments)
    ready = [a for a in assessments if a.status == READY]
    needs = [a for a in assessments if a.status == NEEDS]
    ready_accounts = [x for x in accounts if x["status"] == READY]
    lower = [a for a in assessments if a.status == analysis.MONITOR]
    labeled = sum(1 for r in data.labels if analysis.is_classified(r))
    icon = {"red": "!", "amber": "…", "clear": "✓"}[verdict["level"]]
    known_views = [a.row["view_count"] for a in ready if a.row.get("view_count") is not None]
    unknown_views = sum(a.row.get("view_count") is None for a in ready)
    stats = [
        ("Candidates for review", len(ready), "posts", "amber"),
        ("Other model candidates", len(needs) + len(lower), "posts", "amber"),
        ("Accounts with candidates", len(ready_accounts), "accounts", "amber"),
        ("Known recorded views", _compact(sum(known_views)) if known_views else "Unknown",
         f"{unknown_views} posts unknown", ""),
    ]  # fmt: skip
    return html.Div(
        [
            html.Div(
                [
                    html.Span(icon, className="al-verdict-icon", **{"aria-hidden": "true"}),
                    html.Div(
                        [
                            html.Span(
                                {
                                    "red": "Review",
                                    "amber": "Review required",
                                    "clear": "No candidates",
                                }[verdict["level"]],
                                className="al-verdict-kicker",
                            ),
                            html.H2(verdict["headline"], className="al-verdict-title"),
                            html.P(verdict["detail"], className="al-verdict-detail"),
                        ]
                    ),
                ],
                className=f"al-verdict al-verdict--{verdict['level']}",
                role="status",
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.Span(label, className="al-stat-label"),
                            html.Strong(str(value), className="al-stat-value"),
                            html.Span(unit, className="al-stat-unit"),
                        ],
                        className=f"al-stat {('al-stat--' + kind) if kind else ''}".strip(),
                    )
                    for label, value, unit, kind in stats
                ],
                className="al-stats",
            ),
            html.P(
                [
                    html.Strong(f"{data.case['subject_name']}"),
                    f" · {data.total_posts:,} saved items; {labeled:,} have classified labels. "
                    "Labeled items do not establish collection completeness.",
                ],
                className="al-coverage",
            ),
        ],
        className="al-summary",
    )


def _checks_row(a: Assessment) -> html.Div:
    chips = []
    for c in a.checks[1:]:  # the violation itself is shown as the score
        kind = "ok" if c.ok else ("miss" if c.required else "optional")
        mark = "✓" if c.ok else ("✗" if c.required else "–")
        chips.append(
            html.Span(
                f"{mark} {c.label}",
                className=f"al-check-chip al-check-chip--{kind}",
                title=c.detail,
            )
        )
    return html.Div(chips, className="al-checks")


def _proof(a: Assessment, references: dict) -> Any:
    row = a.row
    if a.category == "disinformation":
        refs = [references[r] for r in row.get("reference_ids") or [] if r in references]
        record = (
            html.Blockquote(
                [_excerpt(refs[0].get("text"), 220), html.Br(),
                 html.Span(_date(refs[0].get("posted_at")), className="al-muted"), " · ",
                 _link(refs[0].get("url"), "Source")],
                className="al-quote al-quote--record",
            )
            if refs
            else html.P("No case-scoped linked context is available.", className="al-muted")
        )  # fmt: skip
        return html.Div(
            [
                html.Div([html.Span("Claim", className="al-mini"),
                          html.P(row.get("claim_summary") or "—", className="al-claim")]),
                html.Div([html.Span("Linked context (verify)", className="al-mini"), record]),
            ],
            className="al-proof al-proof--split",
        )  # fmt: skip
    if a.category in ("racism", "hate_speech"):
        basis = ", ".join(b.replace("_", " ") for b in row.get("hate_speech_basis") or [])
        target = row.get("racism_target") or basis
        return html.P([html.Span("Targets ", className="al-mini"), target or "—"],
                      className="al-proof")  # fmt: skip
    return html.P([html.Span("Target ", className="al-mini"), row.get("harassment_target") or "—"],
                  className="al-proof")  # fmt: skip


def _ready_option(a: Assessment, references: dict, settings: AlertSettings) -> dict:
    row = a.row
    high = (row.get("view_count") or 0) >= settings.high_reach_views
    return {
        "value": a.id,
        "label": html.Div(
            [
                html.Div(
                    [
                        _pill("Candidate", "amber"),
                        _cat(a.category),
                        html.Span(a.route["rule"], className="al-rule"),
                        html.Span(f"score {a.score:.2f}", className="al-score"),
                        html.Span(
                            [
                                _date(row.get("posted_at")),
                                " · ",
                                html.Strong(_compact(row.get("view_count"))) if high else
                                _compact(row.get("view_count")),
                                " views",
                            ],
                            className="al-meta-right",
                        ),
                    ],
                    className="al-item-head",
                ),
                html.Div(
                    [
                        html.Strong(f"@{row.get('handle') or 'unknown'}", className="al-handle"),
                        html.Span(f"{_compact(row.get('followers_count'))} followers",
                                  className="al-muted"),
                        _link(row.get("url")),
                    ],
                    className="al-item-author",
                ),
                html.Blockquote(_excerpt(row.get("text")), className="al-quote"),
                _proof(a, references),
                html.P(row.get("rationale"), className="al-why"),
                _checks_row(a),
            ],
            className="al-item",
        ),
    }  # fmt: skip


def _needs_table(needs: list[Assessment]) -> Any:
    if not needs:
        return html.P("Nothing waiting on evidence.", className="al-empty")
    rows = [
        html.Tr(
            [
                html.Td([html.Strong(f"@{a.row.get('handle') or 'unknown'}"), html.Br(),
                         html.Span(_excerpt(a.row.get("text"), 120), className="al-muted")]),
                html.Td(_cat(a.category)),
                html.Td(analysis.STATUS_LABELS[a.status]),
                html.Td(f"{a.score:.2f}", className="al-num"),
                html.Td([html.Span(m.label, className="al-check-chip al-check-chip--miss")
                         for m in a.missing], className="al-missing"),
                html.Td(_link(a.row.get("url"), "Open")),
            ]
        )
        for a in needs[:20]
    ]  # fmt: skip
    return html.Div(
        [html.P(f"Showing first 20 of {len(needs)} candidates; refine the case to see the rest.")
         if len(needs) > 20 else None,
        html.Table(
            [html.Thead(html.Tr([html.Th("Post"), html.Th("Category"), html.Th("Status"),
                                 html.Th("Score"),
                                 html.Th("Missing"), html.Th("")])),
             html.Tbody(rows)],
            className="al-table",
        )],
        className="al-table-wrap",
    )  # fmt: skip


def _accounts_table(accounts: list[dict], settings: AlertSettings) -> Any:
    eligible = [x for x in accounts if x["status"] in (READY, NEEDS)]
    shown = eligible[:20]
    if not shown:
        return html.P("No account has a candidate with the listed material.", className="al-empty")
    rows = []
    for x in shown:
        report = x["status"] == READY
        rows.append(html.Tr([
            html.Td([html.Strong(f"@{x['handle'] or 'unknown'}"), html.Br(),
                     html.Span(f"user id {x['platform_user_id'] or '—'}", className="al-muted")]),
            html.Td(_compact(x["followers"]), className="al-num"),
            html.Td(str(x["ready"]), className="al-num"),
            html.Td(str(x["violations"]), className="al-num"),
            html.Td([_cat(c) for c in x["categories"]], className="al-cats"),
            html.Td(_pill("Review account", "amber") if report else
                    [_pill("Review posts", "amber"), html.Br(),
                     html.Span(f"{x['ready']} of {settings.account_ready_posts} candidates "
                               "needed for this grouping", className="al-muted")]),
        ]))  # fmt: skip
    return html.Div(
        [html.P(f"Showing first 20 of {len(eligible)} account groups.")
         if len(eligible) > 20 else None,
        html.Table(
            [html.Thead(html.Tr([html.Th(h) for h in ("Account", "Followers", "Candidates",
                                                      "Labels", "Categories", "Review")])),
             html.Tbody(rows)],
            className="al-table",
        )],
        className="al-table-wrap",
    )  # fmt: skip


def timeline_figure(days: list[dict]) -> go.Figure:
    fig = go.Figure()
    # Categorical day labels: a date axis with one or two days zooms into milliseconds.
    x = [d["day"].strftime("%d %b %Y") for d in days]
    for c in CATEGORIES:
        if any(d[c] for d in days):
            fig.add_bar(
                x=x,
                y=[d[c] for d in days],
                name=CATEGORY_LABELS[c],
                marker_color=COLORS[c],
                hovertemplate="%{y}<extra>" + CATEGORY_LABELS[c] + "</extra>",
            )
    ready_x = [d["day"].strftime("%d %b %Y") for d in days if d[READY]]
    if ready_x:
        fig.add_scatter(
            x=ready_x,
            y=[d[READY] for d in days if d[READY]],
            mode="markers",
            name="Candidates for review",
            marker=dict(
                color="#C81E1E", size=11, symbol="diamond", line=dict(color="#FDFCFD", width=1.5)
            ),
            hovertemplate="%{y} candidates<extra></extra>",
        )
    fig.update_layout(
        height=300,
        barmode="stack",
        bargap=0.35 if len(days) > 6 else 0.8,
        margin=dict(l=40, r=16, t=8, b=32),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, color=INK, size=13),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title=None),
    )
    fig.update_xaxes(
        type="category", showgrid=False, linecolor="#D8D4D0", ticks="outside", tickcolor="#D8D4D0"
    )
    peak = max((sum(d[c] for c in CATEGORIES) for d in days), default=1)
    fig.update_yaxes(
        gridcolor="rgba(19,18,18,0.07)",
        zeroline=False,
        rangemode="tozero",
        tickformat="d",
        dtick=max(1, -(-peak // 5)),
    )  # whole posts only
    return fig  # fmt: skip


# --------------------------------------------------------------------------- callbacks


def _settings_from_form(values: list, account_ready, high_reach, archive, name, email, org):
    cats: dict[str, dict] = {c: {} for c in CATEGORIES}
    for (cat, field), value in zip(FORM_KEYS, values, strict=True):
        if value is None:
            raise ValueError(f"{CATEGORY_LABELS[cat]}: every score needs a value")
        cats[cat][field] = value
    return AlertSettings(
        categories=cats,
        account_ready_posts=int(account_ready or 1),
        high_reach_views=int(high_reach or 0),
        require_archive_copy=bool(archive),
        notifier_name=(name or "").strip(),
        notifier_email=(email or "").strip(),
        notifier_organisation=(org or "").strip(),
    )


_SETTINGS_INPUTS = ["al-account-ready", "al-high-reach", "al-require-archive", "al-notifier-name",
                    "al-notifier-email", "al-notifier-org"]  # fmt: skip


def _settings_values(s: AlertSettings) -> list:
    return [
        s.account_ready_posts,
        s.high_reach_views,
        ["yes"] if s.require_archive_copy else [],
        s.notifier_name,
        s.notifier_email,
        s.notifier_organisation,
    ]


def register_alert_callbacks(app, service) -> None:
    store = AlertSettingsStore(service.settings.data_dir)
    repo = AnalyticsRepository()

    @app.callback(
        Output("al-case", "options"),
        Output("al-case", "value"),
        Input("rv-page", "value"),
        Input("al-refresh", "n_clicks"),
        State("al-case", "value"),
    )
    def load_cases(page, _refresh, current):
        if page != "alerts":
            return no_update, no_update
        try:
            cases = repo.list_cases()
        except AnalyticsUnavailable:
            return [], None
        options = [{"label": c["subject_name"], "value": c["id"]} for c in cases]
        values = {o["value"] for o in options}
        return options, current if current in values else (options[0]["value"] if options else None)

    def _load(case_id, refresh=False):
        data = repo.case_data(case_id, refresh=refresh)
        settings, _ = store.load()
        assessments = analysis.assess_all(data.labels, settings, data.references)
        accounts = analysis.accounts(assessments, settings, data.bursts)
        return data, settings, assessments, accounts

    @app.callback(
        Output("al-selection-snapshot", "data"),
        Output("al-summary", "children"),
        Output("al-ev-select", "options"),
        Output("al-ev-select", "value"),
        Output("al-ready-empty", "children"),
        Output("al-needs", "children"),
        Output("al-accounts", "children"),
        Output("al-timeline", "figure"),
        Input("al-case", "value"),
        Input("al-settings-rev", "data"),
        Input("al-refresh", "n_clicks"),
        Input("rv-page", "value"),
        State("al-ev-select", "value"),
    )
    def render(case_id, _rev, _refresh, page, selected):
        if page != "alerts":
            return (no_update,) * 8
        empty_fig = timeline_figure([])
        if not case_id:
            try:
                repo.list_cases()
                message = "No analytics subjects found in the database."
            except AnalyticsUnavailable as exc:
                message = str(exc)
            return None, _notice(message, "warning"), [], [], "", "", "", empty_fig
        try:
            data, settings, assessments, accounts = _load(case_id, ctx.triggered_id == "al-refresh")
        except AnalyticsUnavailable as exc:
            return None, _notice(str(exc), "warning"), [], [], "", "", "", empty_fig
        ready = [a for a in assessments if a.status == READY]
        needs = [a for a in assessments if a.status in (NEEDS, analysis.MONITOR)]
        options = [_ready_option(a, data.references, settings) for a in ready]
        ids = [o["value"] for o in options]
        kept = [v for v in (selected or []) if v in ids]
        value = [] if ctx.triggered_id == "al-case" else kept
        empty = (
            ""
            if ready
            else html.P(
                "No candidate has the listed material available. See other candidates below.",
                className="al-empty",
            )
        )
        return (
            _snapshot_fingerprint(data, settings),
            _summary(data, assessments, accounts, settings),
            options,
            value,
            empty,
            _needs_table(needs),
            _accounts_table(accounts, settings),
            timeline_figure(analysis.timeline(assessments)),
        )

    field_ids = [_field_id(c, f) for c, f in FORM_KEYS]
    all_ids = field_ids + _SETTINGS_INPUTS

    @app.callback(
        Output("al-settings-rev", "data"),
        Output("al-settings-message", "children"),
        *[Output(i, "value") for i in all_ids],
        Input("al-save", "n_clicks"),
        Input("al-reset", "n_clicks"),
        *[State(i, "value") for i in all_ids],
        State("al-settings-rev", "data"),
        prevent_initial_call=True,
    )
    def save(_save, _reset, *args):
        values, extra, rev = list(args[: len(field_ids)]), args[len(field_ids) : -1], args[-1]
        keep = [no_update] * len(all_ids)
        if service.settings.read_only:
            return (no_update, _notice("This workspace is read-only.", "warning"), *keep)
        if ctx.triggered_id == "al-reset":
            settings = store.reset()
            message = _notice("Defaults restored.", "success")
        else:
            try:
                settings = _settings_from_form(values, *extra)
            except (ValidationError, ValueError) as exc:
                text = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
                text = text.removeprefix("Value error, ")
                return (no_update, _notice(f"Not saved: {text}", "error"), *keep)
            try:
                store.save(settings)
            except OSError:
                return (no_update, _notice("Could not write the settings file.", "error"), *keep)
            message = _notice("Saved. The page now uses this standard.", "success")
        out = [getattr(settings.categories[c], f) for c, f in FORM_KEYS] + _settings_values(
            settings
        )
        return ((rev or 0) + 1, message, *out)

    @app.callback(
        Output("al-download", "data"),
        Output("al-download-message", "children"),
        Input("al-download-selected", "n_clicks"),
        State("al-ev-select", "value"),
        State("al-case", "value"),
        State("al-selection-snapshot", "data"),
        prevent_initial_call=True,
    )
    def download(_clicks, selected, case_id, displayed_fingerprint):
        if not case_id:
            return no_update, "Choose a subject first."
        if not selected:
            return no_update, "Select at least one post."
        try:
            data = repo.case_snapshot(case_id, list(selected))
            settings, _ = store.load()
            if not displayed_fingerprint or displayed_fingerprint != _snapshot_fingerprint(
                data, settings
            ):
                return (
                    no_update,
                    "The saved case or settings changed; refresh and review your selection.",
                )
            assessments = analysis.assess_all(data.labels, settings, data.references)
            accounts = analysis.accounts(assessments, settings, data.bursts)
            chosen = [a for a in assessments if a.id in set(selected) and a.status == READY]
            if len(set(selected)) != len(selected) or {a.id for a in chosen} != set(selected):
                return (
                    no_update,
                    "The selection changed or is no longer available; refresh and review it.",
                )
            provenance = data.provenance
            content, filename, digest = build_pdf(
                data.case, chosen, data.references, provenance, accounts, settings
            )
        except (AnalyticsUnavailable, ValueError) as exc:
            return no_update, str(exc)
        # The file hash identifies this exact document when it is shared later.
        return dcc.send_bytes(
            content, filename
        ), f"{len(chosen)} evidence items · PDF SHA-256 {digest}"
