"""Pure, case-scoped dashboard presentation.

Views accept records and selection values and return Dash components or figures.
They do not read application state, open databases, or run detection. The web
factory owns callback registration, interaction state, and service calls.
"""

from __future__ import annotations

import json

from dash import html

from .graph import graph_pair_rows, graph_summary, graph_view
from .ui import (
    baseline,
    card_label,
    case_channels,
    case_messages,
    empty_figure,
    episode_figure,
    kind_name,
    matrix,
    number,
    post_map,
    safe_source,
    short,
    stamp,
    table,
    timeline,
)


def case_view(detail: dict | None, overview: dict, selected_pair_key: str | None = None) -> tuple:
    """Render the case overview, charts, and provenance from supplied records."""
    detail = detail or {}
    case = detail.get("case") or {}
    if not case:
        message = detail.get("error") or "Select a candidate or comparison from the queue."
        return (
            html.Div(message, className="empty-state"),
            [],
            empty_figure(message),
            "",
            [],
            "",
            [],
            None,
            html.P("No case selected."),
            "",
            empty_figure(message),
            [],
            "",
        )
    comparison = "compar" in str(case.get("kind", ""))
    header = [
        html.Div(
            [
                html.Span(
                    kind_name(case).upper(),
                    className="eyebrow amber" if comparison else "eyebrow",
                ),
                html.H2(case.get("name", case.get("case_id", "Selected case"))),
                html.P(
                    case.get(
                        "description", "Review observed behavior and alternative explanations."
                    )
                ),
                html.P(pagination_summary(detail), className="coverage-note")
                if pagination_summary(detail)
                else None,
            ]
        ),
        html.Div(
            [
                html.Span("HUMAN REVIEW REQUIRED", className="review-required"),
                html.Code(case.get("case_id", "")),
            ],
            className="case-id",
        ),
    ]
    concentration = detail.get("concentration") or {}
    pairs = detail.get("pairs") or []
    retained = sum(bool(p.get("retained")) for p in pairs)
    metrics = [
        card_label(
            "ACCOUNTS IN VIEW",
            len(detail.get("accounts") or case.get("account_keys") or []),
            "Identity is not inferred",
        ),
        card_label(
            "REPEATED FAMILIES", case.get("family_count", 0), "Related posts count together"
        ),
        card_label("RETAINED PAIRS", retained, "Inspect each connection"),
        card_label(
            "THREAD SHARE",
            f"{float(concentration.get('share', 0)) * 100:.1f}%",
            "Observed comments in scope",
        ),
    ]
    posts = sorted(detail.get("posts") or [], key=lambda p: str(p.get("timestamp_utc", "")))
    options = [
        {
            "label": f"{i + 1:02d} · {stamp(p.get('timestamp_utc'))} · {short(p.get('post_key'), 24)}",
            "value": str(p.get("post_key")),
        }
        for i, p in enumerate(posts)
    ]
    counter = detail.get("counterevidence") or [
        "No counterevidence has been recorded. This is not proof that none exists."
    ]
    counter_children = html.Ul([html.Li(str(item)) for item in counter], className="counter-list")
    sensitivity = detail.get("sensitivity") or []
    if isinstance(sensitivity, list) and sensitivity:
        sensitivity_view = [
            html.Strong("Window sensitivity"),
            html.P(
                "Pairs with repeated support at each timing window; this is not a second retention decision."
            ),
            table(
                ["Timing window", "Repeated support", "Pairs tested"],
                [
                    [
                        f"{item.get('window_seconds', '—')} seconds",
                        item.get("pairs_with_repeated_support", "—"),
                        item.get("pair_count", "—"),
                    ]
                    for item in sensitivity
                ],
            ),
        ]
    else:
        sensitivity_view = html.P("Timing-window sensitivity is recorded in the case export.")
    channel_keys = {m.get("channel_key") for m in case_messages(detail)}
    channels = [c for c in case_channels(detail) if c.get("channel_key") in channel_keys]
    channel_table = table(
        ["Channel", "Documented category", "Classification context"],
        [
            [
                c.get("name", c.get("channel_key")),
                html.Span(c.get("category", "Unclassified"), className="category-tag"),
                c.get("rationale", "Classification rationale unavailable"),
            ]
            for c in channels
        ],
    )
    run = detail.get("run") or {}
    run_details = html.Details(
        [
            html.Summary("Detector settings & provenance"),
            html.Pre(
                json.dumps(
                    {
                        "run_id": run.get("run_id", overview.get("run_id")),
                        "config": run.get("config", overview.get("config")),
                        "diagnostics": run.get("diagnostics", overview.get("diagnostics")),
                        "coverage_note": "Results describe only the supplied dataset and observation period.",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                className="small-code",
            ),
        ]
    )
    matrix_note = "First-arrival timing is descriptive; the detector evaluates repeated pair evidence against shuffled controls."
    if len(detail.get("accounts", [])) > 24 or len(posts) > 24:
        matrix_note += " Matrix displays up to 24 accounts and 24 discussions."
    graph = graph_view(detail, selected_pair_key)
    graph_note = graph_details(detail, graph)
    return (
        header,
        metrics,
        matrix(detail),
        matrix_note,
        graph["elements"],
        graph_note,
        options,
        options[0]["value"] if options else None,
        counter_children,
        sensitivity_view,
        timeline(detail),
        channel_table,
        run_details,
    )


def graph_details(detail: dict, view: dict) -> list:
    """Keep full pair evidence readable beside an intentionally sparse graph."""
    selected = view.get("selected_pair")
    children = [html.P(graph_summary(view), className="chart-note")]
    if selected:
        children.append(
            html.Div(
                [
                    html.Strong("Selected pair"),
                    html.P(
                        f"{selected['account_a']} ↔ {selected['account_b']}",
                        style={"overflowWrap": "anywhere"},
                    ),
                    html.P(
                        f"{selected.get('observed', 0)} observed families · "
                        f"control mean {number(selected.get('null_mean'))} · "
                        f"control 95th percentile {number(selected.get('null_p95'))} · "
                        + ("retained by detector" if selected.get("retained") else "context only"),
                        className="chart-note",
                    ),
                ],
                style={"marginTop": "12px"},
            )
        )
        children.append(
            html.Details(
                [
                    html.Summary(f"Read the {view['pair_count']} visible pair links as a table"),
                    table(
                        [
                            "Full account IDs",
                            "Observed families",
                            "Control mean",
                            "Control 95th",
                            "Status",
                        ],
                        [
                            [
                                f"{row['account_a']} ↔ {row['account_b']}",
                                row["observed"],
                                number(row["null_mean"]),
                                number(row["null_p95"]),
                                "Retained" if row["retained"] else "Context only",
                            ]
                            for row in graph_pair_rows(detail, view)
                        ],
                    ),
                ],
                style={"marginTop": "12px"},
            )
        )
    return children


def evidence_view(detail, episode, reveal, edge, progress) -> tuple:
    """Render source evidence with the explicit text-reveal selection."""
    detail = detail or {}
    visible = "reveal" in (reveal or [])
    post = post_map(detail).get(str(episode), {})
    context = [
        html.Div(
            [
                html.Span("PARENT POST", className="eyebrow"),
                html.Code(str(episode or "—")),
                html.Span(stamp(post.get("timestamp_utc")), className="text-muted"),
            ],
            className="post-meta",
        ),
        html.P(
            post.get("text_original") or post.get("title") or "No parent-post text available.",
            className="source-quote",
        )
        if visible
        else html.P(
            "Parent-post text is hidden. Use ‘Reveal source text’ to inspect language after behavior.",
            className="text-hidden",
        ),
        safe_source(post.get("source_ref")),
    ]
    pairs = detail.get("pairs") or []
    pair_key = (edge or {}).get("pair_key")
    pair = next((p for p in pairs if str(p.get("pair_key")) == str(pair_key)), None)
    pair = pair or next((p for p in pairs if p.get("retained")), pairs[0] if pairs else None)
    title = (
        f"{short(pair.get('account_a'), 22)} ↔ {short(pair.get('account_b'), 22)}"
        if pair
        else "No retained pair in this discussion"
    )
    pair_records = []
    if pair:
        pair_records = [
            [
                event.get("post_key"),
                event.get("family_key"),
                f"{float(event.get('delta_seconds', 0)):.0f}s",
                stamp(event.get("timestamp_a")),
            ]
            for event in pair.get("events", [])
        ][:20]
    pair_detail = [
        html.P(
            f"Observed: {pair.get('observed', 0)} · shuffled 95th percentile: {number(pair.get('null_p95', 0))}. Not a confidence interval or probability of automation.",
            className="chart-note",
        )
        if pair
        else None,
        html.P(
            f"Showing first 20 of {len(pair.get('events') or [])} loaded events.",
            className="coverage-note",
        )
        if pair and len(pair.get("events") or []) > 20
        else None,
        table(["Post", "Family", "Arrival gap", "First timestamp"], pair_records),
    ]
    messages = [m for m in case_messages(detail) if str(m.get("post_key")) == str(episode)]
    messages.sort(key=lambda m: str(m.get("timestamp_utc", "")))
    source_rows = []
    for message in messages[:100]:
        source_rows.append(
            [
                html.Div(
                    [
                        html.Code(str(message.get("account_key", ""))),
                        html.Small(str(message.get("message_key", ""))),
                    ]
                ),
                stamp(message.get("timestamp_utc")),
                html.P(message.get("text_original", ""), className="comment-text")
                if visible
                else html.Span("Text hidden", className="text-hidden"),
                safe_source(message.get("source_ref")),
            ]
        )
    sources = table(["Account / message", "Timestamp", "Original comment", "Source"], source_rows)
    quotes = (
        [
            html.Div(
                [
                    html.Div(
                        [
                            html.Code(str(m.get("account_key"))),
                            html.Span(stamp(m.get("timestamp_utc"))),
                        ],
                        className="quote-meta",
                    ),
                    html.Blockquote(m.get("text_original", "")),
                    safe_source(m.get("source_ref")),
                ],
                className="context-quote",
            )
            for m in case_messages(detail)[:6]
        ]
        if visible
        else html.Div(
            [
                html.Span("TEXT IS HIDDEN", className="eyebrow"),
                html.P(
                    "Reveal source text to assess context, quotation and endorsement. Narrative interpretation remains a separate human judgment."
                ),
            ],
            className="text-hidden-block",
        )
    )
    matches = detail.get("content_matches") or {}
    if visible:
        links = matches.get("links") or []
        texts = matches.get("texts") or []
        content = (
            [
                html.Div(
                    [
                        html.Strong("Shared URLs", className="content-subheading"),
                        table(
                            ["URL", "Case accounts", "Dataset comments"],
                            [
                                [
                                    html.A(
                                        str(item.get("url", "")),
                                        href=str(item.get("url")),
                                        target="_blank",
                                        rel="noopener noreferrer",
                                    )
                                    if str(item.get("url", "")).startswith(("https://", "http://"))
                                    else html.Code(str(item.get("url", ""))),
                                    item.get("accounts", len(item.get("account_keys", []))),
                                    item.get("dataset_count", "—"),
                                ]
                                for item in links[:15]
                            ],
                        ),
                    ]
                ),
                html.Div(
                    [html.Strong("Repeated text", className="content-subheading")]
                    + [
                        html.Div(
                            [
                                html.Blockquote(item.get("text", "")),
                                html.Span(
                                    f"Observed from {item.get('accounts', '—')} accounts",
                                    className="text-muted",
                                ),
                            ],
                            className="context-quote",
                        )
                        for item in texts[:8]
                    ]
                ),
            ]
            if links or texts
            else html.Div(
                "No exact shared URLs or repeated text were observed in this case.",
                className="text-hidden-block",
            )
        )
    else:
        content = html.Div(
            [
                html.P("Shared text and URLs stay hidden until you reveal source text."),
                html.Span("Excluded from behavioral ranking", className="neutral-tag"),
            ],
            className="text-hidden-block",
        )
    return (
        episode_figure(detail, episode, progress or 0),
        context,
        f"{progress or 0}% of episode",
        sources,
        title,
        baseline(detail, pair.get("pair_key") if pair else None),
        pair_detail,
        quotes,
        content,
    )


def review_form_values(detail: dict | None) -> tuple:
    """Populate the review form and source choices from saved case records."""
    review = (detail or {}).get("review") or {}
    evidence_options = [
        {
            "label": f"{m.get('message_key')} · {m.get('account_key')} · {stamp(m.get('timestamp_utc'))}",
            "value": m.get("message_key"),
        }
        for m in (detail or {}).get("messages", [])
        if m.get("message_key")
    ]
    return (
        review.get("reviewer", ""),
        review.get("decision", "Insufficient evidence"),
        review.get("reason", ""),
        review.get("claim", ""),
        review.get("endorsement", "unclear"),
        review.get("alignment", "unclear"),
        review.get("narrative_notes", ""),
        "Saved review loaded. Changes are saved only when you select ‘Save review’."
        if review
        else "No review recorded yet.",
        evidence_options,
        review.get("evidence_refs") or [],
    )


def queue_cards(cases: list[dict], current: str | None, unreviewed: set) -> list:
    """Render case buttons while leaving selection and filtering to callbacks."""
    cards = []
    for index, case in enumerate(cases):
        comparison = "compar" in str(case.get("kind", ""))
        reviewed = case.get("review_status") not in unreviewed
        cards.append(
            html.Button(
                [
                    html.Div(
                        [
                            html.Span(
                                "COMPARISON" if comparison else f"CANDIDATE {index + 1:02d}",
                                className="queue-type",
                            ),
                            html.Span("Reviewed" if reviewed else "Open", className="queue-state"),
                        ],
                        className="queue-card-top",
                    ),
                    html.Strong(case.get("name", case["case_id"])),
                    html.P(
                        short(case.get("description", "Repeated joint activity for review."), 100)
                    ),
                    html.Div(
                        [
                            html.Span(f"{len(case.get('account_keys') or [])} accounts"),
                            html.Span(
                                f"{case.get('family_count', len(case.get('post_keys') or []))} families"
                            ),
                        ],
                        className="queue-card-meta",
                    ),
                ],
                id={"type": "case-card", "index": case["case_id"]},
                n_clicks=0,
                className="queue-card"
                + (" comparison" if comparison else "")
                + (" active" if case["case_id"] == current else ""),
            )
        )
    return cards


def stress_result(result: dict) -> html.Div | html.P:
    """Explain the strongest-family removal result without recomputing it."""
    if not result.get("available"):
        return html.P(result.get("note", "This case has no retained group evidence to remove."))
    return html.Div(
        [
            html.Strong("One-family removal check"),
            html.P(
                [
                    "Removed discussion family: ",
                    html.Code(str(result.get("excluded_family", "—"))),
                ]
            ),
            table(
                ["Evidence", "Original", "After removal"],
                [
                    [
                        "Retained pairs",
                        result.get("original_pairs", "—"),
                        result.get("remaining_pairs", "—"),
                    ],
                    [
                        "Core accounts",
                        result.get("original_accounts", "—"),
                        result.get("remaining_accounts", "—"),
                    ],
                ],
            ),
            html.P(result.get("note", "Controls were recomputed without the strongest family.")),
            html.P("Stability under removal supports robustness; it does not prove manipulation."),
        ]
    )


def pagination_summary(detail: dict) -> str:
    labels = {
        "messages": "case comments",
        "context_messages": "surrounding comments",
        "posts": "posts",
        "accounts": "accounts",
        "channels": "channels",
        "pairs": "pairs",
        "events": "pair events",
    }
    parts = []
    for section, page in (detail.get("pagination") or {}).items():
        if page.get("has_more") or page.get("offset", 0):
            returned = page.get("returned", len(detail.get(section) or []))
            parts.append(
                f"showing {returned} of {page.get('total', 'unknown')} {labels.get(section, section)}"
            )
    return (
        "Partial evidence view · "
        + "; ".join(parts)
        + ". Counts and content shown on this page do not establish complete coverage."
        if parts
        else ""
    )


def readiness_view(workspace: dict) -> tuple:
    """Show observed coverage without manufacturing an unknown denominator."""
    readiness, overview = workspace.get("readiness") or {}, workspace.get("overview") or {}
    checks = readiness.get("checks") or []
    status_names = {
        "verified": "Verified",
        "limited": "Limited",
        "unknown": "Unknown",
        "unavailable": "Unavailable",
        "invalid": "Unavailable",
    }
    cards = []
    for check in checks:
        status = str(check.get("status", "unknown")).lower()
        cards.append(
            html.Details(
                [
                    html.Summary(
                        [
                            html.Strong(check.get("label", check.get("key", "Input check"))),
                            html.Span(
                                status_names.get(status, "Unknown"),
                                className="readiness-status " + status,
                            ),
                        ]
                    ),
                    html.P(check.get("detail", "Details unavailable.")),
                ],
                className="readiness-check",
            )
        )
    if not cards:
        cards = html.Div(
            "Readiness is unavailable. Do not infer usable identities or timestamps from missing checks.",
            className="empty-state",
        )
    coverage = readiness.get("coverage") or {}
    imported = coverage.get("imported_messages", overview.get("message_count"))
    available = coverage.get("available_messages")
    count = (
        f"{number(imported, 0)} imported comments"
        if imported is not None
        else "Imported count unknown"
    )
    count += (
        f" of {number(available, 0)} available"
        if available is not None
        else " · total available observations unknown"
    )
    start, end = readiness.get("observation_start"), readiness.get("observation_end")
    interval = (
        f"{stamp(start)} → {stamp(end)}" if start and end else "Observation interval unavailable"
    )
    omitted = coverage.get("omitted_discovery_posts", 0)
    summary = [
        html.P(count),
        html.P(interval),
        html.P(
            f"{number(omitted, 0)} dense posts omitted from candidate discovery."
            if omitted
            else "No discovery omissions reported; inspect collection limitations separately."
        ),
    ]
    limitations = readiness.get("limitations") or [
        "No collection limitations supplied. This does not establish complete coverage."
    ]
    details = [
        html.Ul([html.Li(str(note)) for note in limitations]),
        html.P("Saved analysis: " + str(workspace.get("run_id") or "Awaiting analysis")),
        html.Pre(
            json.dumps(
                {
                    "capabilities": readiness.get("capabilities", {}),
                    "settings": overview.get("config", {}),
                    "diagnostics": overview.get("diagnostics", {}),
                },
                ensure_ascii=False,
                indent=2,
            ),
            className="small-code",
        ),
    ]
    return cards, summary, details


def case_preview(detail: dict) -> list:
    case = detail.get("case") or {}
    if not case:
        return [
            html.P(
                detail.get("error")
                or "Select a candidate or comparison to inspect its scope and limitations.",
                className="empty-state",
            )
        ]
    retained = sum(bool(p.get("retained")) for p in detail.get("pairs", []))
    limitation = (
        detail.get("counterevidence")
        or ["The observation period and available channels limit the conclusion."]
    )[0]
    return [
        html.Span(kind_name(case), className="category-tag"),
        html.H3(case.get("name", case["case_id"])),
        html.P(case.get("description", "Repeated joint activity for human review.")),
        html.Div(
            [
                card_label("ACCOUNTS", len(case.get("account_keys", []))),
                card_label("FAMILIES", case.get("family_count", 0)),
                card_label("RETAINED PAIRS", retained),
            ],
            className="preview-metrics",
        ),
        html.H4("Main limitation"),
        html.P(limitation, className="limitation-copy"),
        html.P(
            "Coordination does not establish automation, intent or an operator.",
            className="field-help",
        ),
    ]


def pair_options(detail: dict) -> list[dict]:
    pairs = sorted(
        detail.get("pairs") or [],
        key=lambda p: (
            not bool(p.get("retained")),
            -float(p.get("observed", 0)),
            str(p.get("pair_key")),
        ),
    )
    return [
        {
            "label": f"{p.get('account_a')} ↔ {p.get('account_b')} · {p.get('observed', 0)} families · {'retained' if p.get('retained') else 'context only'}",
            "value": p["pair_key"],
        }
        for p in pairs
    ]


def with_selected_pair(detail: dict, selection: dict | None) -> dict:
    """Use an independently paged pair without widening the case corpus page."""
    selection = selection or {}
    if selection.get("case_id") != (detail.get("case") or {}).get("case_id") or selection.get(
        "run_id"
    ) != (detail.get("run") or {}).get("run_id"):
        return detail
    selected = selection.get("pair")
    if not selected:
        return detail
    return {
        **detail,
        "pairs": [
            selected if p.get("pair_key") == selected.get("pair_key") else p
            for p in detail.get("pairs", [])
        ],
    }


def episode_options(detail: dict, pair_key: str | None = None) -> list[dict]:
    pair = next((p for p in detail.get("pairs", []) if p.get("pair_key") == pair_key), None)
    relevant = {event.get("post_key") for event in pair.get("events", [])} if pair else None
    posts = sorted(
        detail.get("posts") or [],
        key=lambda p: (str(p.get("timestamp_utc", "")), str(p.get("post_key"))),
    )
    if relevant:
        posts = [p for p in posts if p.get("post_key") in relevant]
    return [
        {
            "label": f"{p.get('post_key')} · {stamp(p.get('timestamp_utc'))}",
            "value": str(p.get("post_key")),
        }
        for p in posts
    ]


def contextual_sources(
    detail: dict,
    episode: str | None,
    reveal: list,
    references: list | None = None,
    pair_key: str | None = None,
) -> list:
    """Keep original excerpts with the exact parent and available reply context."""
    visible = "reveal" in reveal
    refs = set(references or [])
    context = detail.get("context_messages") or case_messages(detail)
    post_by_key = post_map(detail)
    by_message = {m.get("message_key"): m for m in context}
    selected = (
        [m for m in context if m.get("message_key") in refs]
        if refs
        else [m for m in context if str(m.get("post_key")) == str(episode)]
    )
    selected.sort(key=lambda m: (str(m.get("timestamp_utc", "")), str(m.get("message_key", ""))))
    core = set((detail.get("case") or {}).get("account_keys", []))
    pair = next((p for p in detail.get("pairs", []) if p.get("pair_key") == pair_key), {})
    supporting = {
        event.get(field) for event in pair.get("events", []) for field in ("message_a", "message_b")
    }
    if not selected:
        return [
            html.Div(
                "Source comments unavailable for this selection. This is not an empty quotation.",
                className="empty-state",
            )
        ]
    result = []
    parents = set()
    for message in selected[:60]:
        post_key = message.get("post_key")
        if post_key not in parents:
            parents.add(post_key)
            parent = post_by_key.get(str(post_key), {})
            parent_text = parent.get("text_original") or parent.get("title")
            result.append(
                html.Div(
                    [
                        html.Div(
                            [
                                html.Span("PARENT POST", className="eyebrow"),
                                html.Code(str(post_key)),
                                html.Span(stamp(parent.get("timestamp_utc"))),
                            ],
                            className="quote-meta",
                        ),
                        html.P(
                            parent_text or "Parent-post text unavailable.", className="source-quote"
                        )
                        if visible
                        else html.P(
                            "Original parent text hidden. Reveal source text to inspect context.",
                            className="text-hidden",
                        ),
                        safe_source(parent.get("source_ref")),
                    ],
                    className="source-parent",
                )
            )
        reply_key = message.get("reply_to_message_key") or message.get("reply_to")
        reply = by_message.get(reply_key) if isinstance(reply_key, str) else None
        reply_view = None
        if reply_key:
            reply_view = html.Div(
                [
                    html.Span("Reply context: ", className="field-help"),
                    html.Code(str(reply_key)),
                    html.P(reply.get("text_original") or "Reply text unavailable.")
                    if visible and reply
                    else html.P(
                        "Reply context unavailable in the supplied records."
                        if not reply
                        else "Reply text hidden.",
                        className="text-hidden",
                    ),
                ],
                className="reply-context",
            )
        tags = ["Case account" if message.get("account_key") in core else "Surrounding activity"]
        if message.get("message_key") in supporting:
            tags.append("Pair evidence")
        if message.get("message_key") in refs:
            tags.append("Selected for narrative review")
        result.append(
            html.Article(
                [
                    html.Div(
                        [
                            html.Code(str(message.get("account_key", "Unknown account"))),
                            html.Span(" · ".join(tags), className="source-role"),
                        ],
                        className="quote-meta",
                    ),
                    html.Div(
                        [
                            html.Code(str(message.get("message_key"))),
                            html.Time(stamp(message.get("timestamp_utc"))),
                        ],
                        className="source-identifiers",
                    ),
                    reply_view,
                    html.Blockquote(
                        message.get("text_original") or "Original comment text unavailable."
                    )
                    if visible
                    else html.P("Original comment text hidden.", className="text-hidden"),
                    safe_source(message.get("source_ref")),
                ],
                className="context-quote"
                + (" surrounding" if message.get("account_key") not in core else ""),
            )
        )
    if len(selected) > 60:
        result.append(
            html.P(
                f"Showing the first 60 of {len(selected)} source comments in this selection. Export includes the case evidence.",
                className="field-help",
            )
        )
    for key, value in detail.items():
        if ("truncat" in key or "pagination" in key) and value:
            result.append(
                html.P(
                    "The case response is limited. Consult the source pagination or export before concluding coverage is complete.",
                    className="field-help",
                )
            )
            break
    return result
