"""Policies page: how to report disinformation and abuse on each platform, case by case."""

from __future__ import annotations

from typing import Any

from dash import Input, Output, dcc, html, no_update

from ..review.comparison_ui import safe_source_url
from .catalog import Catalog, load_catalog

PAGE_ID = "rv-policies-page"
GENERAL = "__general__"


# --------------------------------------------------------------------------- helpers


def _a(title: str, url: str | None, cls: str = "al-link") -> Any:
    safe = safe_source_url(url)
    if not safe:
        return html.Span(f"{title} (link unavailable)")
    return html.A(title, href=safe, target="_blank", rel="noopener noreferrer", className=cls)


def _section(title: str, subtitle: str | None, body, *, section_id: str | None = None):
    head = [html.H2(title, className="al-h2")]
    if subtitle:
        head.append(html.P(subtitle, className="al-sub"))
    kwargs = {"id": section_id} if section_id else {}
    return html.Section(
        [html.Div(html.Div(head), className="al-section-head"), body],
        className="al-card",
        **kwargs,
    )


def _verified(item: dict) -> Any:
    if not item.get("verified"):
        return None
    # The check method (e.g. search index when a site blocks automated access) is a tooltip.
    return html.Span(f"Checked {item['verified']}", className="al-muted", title=item.get("method"))


# --------------------------------------------------------------------------- layout


def policies_page(catalog: Catalog | None = None) -> html.Section:
    catalog = catalog or load_catalog()
    platforms = [{"label": p["name"], "value": k} for k, p in catalog.platforms.items()]
    cases = [{"label": f"Case: {c['subject']}", "value": k} for k, c in catalog.cases.items()]
    cases.append({"label": "General guidance (no case)", "value": GENERAL})
    platform = "twitter" if "twitter" in catalog.platforms else (platforms or [{}])[0].get("value")
    case = next(iter(catalog.cases), GENERAL)
    return html.Section(
        html.Div(
            [
                html.Header(
                    [
                        html.Div(
                            [
                                html.Span("Platform policy guide", className="al-eyebrow"),
                                html.H1("How to report disinformation and abuse",
                                        className="al-title"),
                                html.P(
                                    "Which platform rule applies, how to report a post or account, "
                                    "and what to keep as evidence. Compiled from official sources.",
                                    className="al-lede",
                                ),
                            ],
                            className="al-top-copy",
                        ),
                        html.Div(
                            [
                                html.Label("Platform", htmlFor="pg-platform", className="al-label"),
                                dcc.Dropdown(id="pg-platform", options=platforms, value=platform,
                                             clearable=False, searchable=False,
                                             className="al-dropdown"),
                                html.Label("Case", htmlFor="pg-case", className="al-label"),
                                html.Div(
                                    [
                                        dcc.Dropdown(id="pg-case", options=cases, value=case,
                                                     clearable=False, searchable=False,
                                                     className="al-dropdown"),
                                        html.Button("Reload", id="pg-reload", n_clicks=0,
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
                html.Div(id="pg-body", children=render_body(catalog, platform, case)),
                html.P(
                    "Platforms change their rules and menus. Each entry shows when it was last "
                    "checked against the official page; follow the links for the current text.",
                    className="al-footnote",
                ),
            ],
            className="al-shell",
        ),
        id=PAGE_ID,
        className="rv-page-surface al-page",
        style={"display": "none"},
    )  # fmt: skip


# --------------------------------------------------------------------------- body


def _report_steps(platform: dict) -> html.Section:
    flows = []
    for flow in platform.get("reporting", []):
        flows.append(
            html.Div(
                [
                    html.H3(flow["title"], className="al-h3"),
                    html.Ol([html.Li(s) for s in flow["steps"]], className="pg-steps"),
                    _a("Official help page", flow.get("source")),
                ],
                className="pg-flow",
            )
        )
    after = platform.get("after_reporting") or []
    body = [html.Div(flows, className="pg-flows")]
    if after:
        body.append(
            html.Div(
                [html.Span("After you report", className="al-mini"),
                 html.Ul([html.Li(a) for a in after], className="pg-list")],
                className="pg-after",
            )
        )  # fmt: skip
    return _section(
        f"Report on {platform['name']}",
        "Document the post first (see below), then report it.",
        html.Div(body),
        section_id="pg-report",
    )


def _rules_table(platform: dict, case: dict | None) -> html.Section:
    policies = {p["id"]: p for p in platform.get("policies", [])}
    rows = []
    key = f"{platform['id']}_rules" if platform["id"] != "twitter" else "x_rules"
    if case:
        for cat in case["categories"]:
            ids = cat.get(key) or [p["id"] for p in policies.values() if cat["id"] in p["covers"]]
            for i, pid in enumerate(i for i in ids if i in policies):
                p = policies[pid]
                rows.append(
                    html.Tr(
                        [
                            html.Td(html.Strong(cat["name"]) if i == 0 else ""),
                            html.Td([_a(p["title"], p.get("url"), "al-link pg-rule"), html.Br(),
                                     _verified(p)]),
                            html.Td(p["summary"]),
                            html.Td(p.get("enforcement", "")),
                        ],
                        className="pg-row-first" if i == 0 else None,
                    )
                )  # fmt: skip
    else:
        for p in policies.values():
            rows.append(
                html.Tr(
                    [
                        html.Td(", ".join(c.replace("_", " ") for c in p.get("covers", []))),
                        html.Td([_a(p["title"], p.get("url"), "al-link pg-rule"), html.Br(),
                                 _verified(p)]),
                        html.Td(p["summary"]),
                        html.Td(p.get("enforcement", "")),
                    ]
                )
            )  # fmt: skip
    table = html.Div(
        html.Table(
            [html.Thead(html.Tr([html.Th("Category"), html.Th("Platform rule"),
                                 html.Th("What it covers"), html.Th("Possible outcome")])),
             html.Tbody(rows)],
            className="al-table pg-rules",
        ),
        className="al-table-wrap",
    )  # fmt: skip
    return _section(
        "Which rule applies",
        "Choose the rule that matches the post when you report it, and name it in your evidence.",
        table,
        section_id="pg-rules",
    )


def _enforcement(platform: dict) -> html.Section | None:
    model = platform.get("enforcement_model")
    if not model:
        return None
    src = model.get("source") or {}
    return _section(
        f"How {platform['name']} acts on reports: {model['title']}",
        model.get("summary"),
        html.Div(
            [
                html.Div(
                    [
                        html.Div(
                            [html.Span(str(i), className="pg-level-n"),
                             html.H3(level["name"], className="al-h3")],
                            className="pg-level-head",
                        ),
                        html.P(level["detail"], className="pg-level-text"),
                    ],
                    className=f"pg-level pg-level--{i}",
                )
                for i, level in enumerate(model["levels"], 1)
            ]
            + [html.P(["Source: ", _a(src.get("title", "official source"), src.get("url")), " ",
                       _verified(src)], className="pg-source")],
            className="pg-levels",
        ),
        section_id="pg-enforcement",
    )  # fmt: skip


def _documentation(doc: dict) -> html.Section | None:
    if not doc:
        return None
    steps = html.Ol(
        [
            html.Li([html.Strong(s["title"]), html.Span(s["detail"], className="pg-step-detail")])
            for s in doc.get("steps", [])
        ],
        className="pg-doc-steps",
    )
    types = html.Div(
        [
            html.Div([html.Strong(t["type"]), html.Span(t["what"], className="al-muted")],
                     className="pg-type")
            for t in doc.get("evidence_types", [])
        ],
        className="pg-types",
    )  # fmt: skip
    tools = html.P(
        ["Tools: "]
        + [x for t in doc.get("tools", []) for x in (_a(t["title"], t["url"]), " · ")][:-1],
        className="pg-source",
    )
    return _section(
        doc.get("title", "How to document"),
        doc.get("summary"),
        html.Div(
            [
                steps,
                html.Div(
                    [
                        html.Span("Evidence types to collect", className="al-mini"),
                        types,
                        html.P(
                            "Alerts shows model candidates and available records. Human review "
                            "of evidence and current policy is required before any decision.",
                            className="pg-callout",
                        ),
                    ]
                ),
                tools,
            ],
            className="pg-doc",
        ),
        section_id="pg-document",
    )


def _case_guide(case: dict, platform: dict) -> html.Section:
    accounts = [a for a in case.get("official_accounts", []) if a["platform"] == platform["id"]]
    cats = []
    for cat in case["categories"]:
        examples = [
            html.Div(
                [html.Strong("Unsupported, unreviewed example: source unavailable"),
                 html.Blockquote(e["quote"], className="al-quote"),
                 html.P([html.Strong("Supplied interpretation (unverified): "), e["analysis"]],
                        className="al-why")],
                className="pg-example",
            )
            for e in cat.get("examples", [])
        ]  # fmt: skip
        cats.append(
            html.Details(
                [
                    html.Summary([html.Span(cat["name"], className="pg-cat-name"),
                                  html.Span(f"{len(examples)} examples", className="al-muted")]),
                    html.Div(
                        [
                            html.P([html.Strong("Supplied working definition (not a finding): "),
                                    cat["definition"]]),
                            html.P([html.Span("Evidence needed ", className="al-mini"),
                                    cat.get("evidence_needed", "")]),
                            *examples,
                        ],
                        className="pg-cat-body",
                    ),
                ],
                className="pg-cat",
            )
        )  # fmt: skip
    body = [
        html.Div(
            [
                html.Div(
                    [
                        html.Span("Review guidance", className="al-mini"),
                        html.P("The examples below have no supplied exact source references. "
                               "Check original records, context and current rules; a human "
                               "decides whether a report is appropriate."),
                    ],
                    className="pg-strategy",
                ),
                html.Div(
                    [
                        html.Span("Declared account references (verify independently)",
                                  className="al-mini"),
                        html.Ul([html.Li([_a(a["handle"], a.get("url")), f" · {a['note']}"])
                                 for a in accounts] or [html.Li("None listed for this platform.")],
                                className="pg-list"),
                    ],
                    className="pg-accounts",
                ),
            ],
            className="pg-case-top",
        ),
        html.Div(cats, className="pg-cats"),
        html.P("Case examples and interpretations are unsupported planning material, "
               "not verified findings or review decisions.", className="al-muted"),
    ]  # fmt: skip
    return _section(
        f"Case guide: {case['subject']}",
        case.get("role"),
        html.Div(body),
        section_id="pg-case-overview",
    )


def _regulations(catalog: Catalog, case: dict | None, platform: dict) -> html.Section | None:
    ids = case.get("regulations") if case else list(catalog.regulations)
    regs = [catalog.regulations[i] for i in ids or [] if i in catalog.regulations]
    if not regs:
        return None
    blocks = []
    for r in regs:
        filed = platform.get("regulatory_reports") or []
        blocks.append(
            html.Div(
                [
                    html.H3(r["title"], className="al-h3"),
                    html.P([html.Span(r.get("citation", ""), className="al-muted"), " ",
                            _verified(r)]),
                    html.P(r["summary"]),
                    html.P([html.Strong("Applies to: "), r.get("applies_to", "")]),
                    html.Div(
                        [
                            html.Div([html.Span("What you can do", className="al-mini"),
                                      html.Ul([html.Li(x) for x in r.get("what_you_can_do", [])],
                                              className="pg-list")]),
                            html.Div([html.Span(f"{platform['name']} filings", className="al-mini"),
                                      html.Ul([html.Li([_a(f["title"], f["url"]),
                                                        html.Span(f" · filed {f['date']}",
                                                                  className="al-muted")])
                                               for f in filed] or [html.Li("None listed.")],
                                              className="pg-list")]),
                        ],
                        className="pg-two",
                    ),
                    html.P(r["status_note"], className="pg-callout") if r.get("status_note")
                    else None,
                    html.P(["Official pages: "]
                           + [x for link in r.get("links", [])
                              for x in (_a(link["title"], link["url"]), " · ")][:-1],
                           className="pg-source"),
                ],
                className="pg-reg",
            )
        )  # fmt: skip
    return _section(
        "Regulation",
        "Government rules on how platforms publish and enforce their policies.",
        html.Div(blocks),
        section_id="pg-regulation",
    )


def _sources(platform: dict, case: dict | None) -> html.Section:
    groups = [
        {"group": f"{platform['name']}: official pages", "items": platform.get("sources", [])}
    ]
    if case:
        groups += case.get("resources", [])
    return _section(
        "Trusted sources",
        "Official platform pages, plus fact-checking, news and research sources for verifying "
        "claims.",
        html.Div(
            [
                html.Div(
                    [html.Span(g["group"], className="al-mini"),
                     html.Ul([html.Li(_a(i["title"], i["url"])) for i in g["items"]],
                             className="pg-list")],
                    className="pg-source-group",
                )
                for g in groups
            ],
            className="pg-sources",
        ),
        section_id="pg-sources",
    )  # fmt: skip


def outcomes_section(rows, analytics_case: dict | None, message: str = "") -> html.Section:
    """Observed outcome per category and rule, from the analytics database, under the rules."""
    from .outcomes import LOW_PERCENTILE, outcome_figure

    how = html.Dl(
        [item for label, text in (
            ("Shows", "For each category and the X rule it falls under, what happened to the "
             "flagged posts by the time they were captured."),
            ("Remain", "Still public with ordinary reach for the account's audience."),
            ("Restrict?", "Still public, but views per follower in the bottom 10% of unflagged "
             "posts: a sign of possible restricted reach."),
            ("Remove", "Cannot be seen in a single capture (removed posts are never collected), "
             "so it is always 0 here."),
            ("Read it", "Compare each bar with the baseline row. Unflagged posts are 10% low "
             "reach by definition (dotted line). A rule far above 10% suggests restriction; "
             "about 10% or less means flagged posts spread like any other post."),
        ) for item in (html.Dt(label), html.Dd(text))],
        className="pg-how",
    )  # fmt: skip
    if not rows:
        body = html.Div([how, html.P(message or "No analytics data for this case.",
                                     className="al-muted")])  # fmt: skip
    else:
        fig = outcome_figure(rows)
        body = html.Div(
            [
                how,
                dcc.Graph(figure=fig, config={"displayModeBar": False}, responsive=True,
                          className="al-chart pg-chart",
                          style={"height": f"{fig.layout.height}px", "width": "100%"}),
                html.P([html.Strong("In this data: "), _outcome_takeaway(rows, LOW_PERCENTILE)],
                       className="pg-takeaway"),
            ]
        )  # fmt: skip
    subject = f" · {analytics_case['subject_name']}" if analytics_case else ""
    return _section(
        "What happened to flagged posts, by rule",
        f"Observed at capture from the analytics database{subject}. Each bar is 100% of the "
        "posts in that row.",
        body,
        section_id="pg-outcomes",
    )


def _outcome_takeaway(rows, low_share: float) -> str:
    measured = [r for r in rows[1:] if r.total]
    if not measured:
        return "No flagged posts are mapped to these rules yet."
    above = [r for r in measured if r.counts["low_reach"] / r.total > low_share * 1.5]
    at_or_below = [r for r in measured if r not in above]

    def name(r):
        return f"{r.label} ({r.counts['low_reach'] / r.total:.0%} of {r.total})"

    text = "Every post in these rows was still live at capture; none can be shown as removed. "
    if at_or_below:
        text += (
            "At or below the 10% baseline, so no sign of restricted reach: "
            + ", ".join(name(r) for r in at_or_below)
            + ". "
        )
    if above:
        small = any(r.total < 30 for r in above)
        text += (
            "Above the baseline, consistent with some restriction: "
            + ", ".join(name(r) for r in above)
            + (" (small samples; treat with caution)." if small else ".")
        )
    return text


def render_body(
    catalog: Catalog, platform_id: str | None, case_id: str | None, outcomes=None
) -> list:
    out: list = []
    if catalog.problems:
        out.append(
            html.Div("Some guideline files could not be loaded: " + " ".join(catalog.problems),
                     className="al-notice al-notice--warning", role="status")
        )  # fmt: skip
    platform = catalog.platforms.get(platform_id or "")
    if not platform:
        out.append(html.Div("No platform guides found in the policy-guidelines folder.",
                            className="al-notice al-notice--warning"))  # fmt: skip
        return out
    case = catalog.cases.get(case_id or "")
    if case and platform["id"] not in case.get("platforms", [platform["id"]]):
        case = None
    out.append(
        html.Div(
            [
                html.Strong(platform["name"]),
                f" · reviewed {platform.get('last_reviewed', 'unknown')}",
                f" · {len(platform.get('policies', []))} rules · ",
                html.Span(platform.get("summary", "")),
            ],
            className="al-coverage",
        )
    )
    out.append(_report_steps(platform))
    out.append(_rules_table(platform, case))
    if outcomes is not None:
        out.append(outcomes)
    if enforcement := _enforcement(platform):
        out.append(enforcement)
    if documentation := _documentation(catalog.documentation):
        out.append(documentation)
    if case:
        out.append(_case_guide(case, platform))
    if regulations := _regulations(catalog, case, platform):
        out.append(regulations)
    out.append(_sources(platform, case))
    return out


# --------------------------------------------------------------------------- callbacks


def _outcomes_for(catalog: Catalog, platform_id, case_id, repo, store):
    """Outcome section for an X case that has analytics data; None for other views."""
    from ..alerts.repository import AnalyticsUnavailable
    from ..alerts.settings import AlertSettings
    from .outcomes import outcome_rows

    platform = catalog.platforms.get(platform_id or "")
    case = catalog.cases.get(case_id or "")
    if not platform or platform["id"] != "twitter" or not case:
        return None
    try:
        match = next((c for c in repo.list_cases()
                      if (c.get("subject_name") or "").casefold()
                      == (case.get("subject") or "").casefold()), None)  # fmt: skip
        if not match:
            return outcomes_section([], None, "No analytics case matches this subject yet.")
        data = repo.case_data(match["id"])
    except AnalyticsUnavailable as exc:
        return outcomes_section([], None, str(exc))
    settings = store.load()[0] if store else AlertSettings.defaults()
    policies = {p["id"]: p for p in platform.get("policies", [])}
    rows, _cutoff = outcome_rows(data.labels, data.bursts, settings, case, policies)
    return outcomes_section(rows, data.case)


def register_policy_callbacks(app, service=None) -> None:
    from ..alerts.repository import AnalyticsRepository
    from ..alerts.settings import AlertSettingsStore

    repo = AnalyticsRepository()
    store = AlertSettingsStore(service.settings.data_dir) if service else None

    @app.callback(
        Output("pg-body", "children"),
        Output("pg-platform", "options"),
        Output("pg-case", "options"),
        Input("pg-platform", "value"),
        Input("pg-case", "value"),
        Input("pg-reload", "n_clicks"),
        Input("rv-page", "value"),
    )
    def render(platform_id, case_id, _reload, page):
        # Only build the page (and query analytics) while the Policies tab is open.
        if page != "policies":
            return no_update, no_update, no_update
        catalog = load_catalog()  # files are small; re-read so edits show up on change
        platforms = [{"label": p["name"], "value": k} for k, p in catalog.platforms.items()]
        cases = [{"label": f"Case: {c['subject']}", "value": k} for k, c in catalog.cases.items()]
        cases.append({"label": "General guidance (no case)", "value": GENERAL})
        outcomes = _outcomes_for(catalog, platform_id, case_id, repo, store)
        return render_body(catalog, platform_id, case_id, outcomes), platforms, cases
