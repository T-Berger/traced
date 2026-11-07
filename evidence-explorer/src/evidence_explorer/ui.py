"""Small, stateless view helpers for the evidence dashboard."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from urllib.parse import urlparse

import plotly.graph_objects as go
from dash import html

from .graphs import GRAPH_STYLESHEET as GRAPH_STYLESHEET
from .graphs import source_graph
from .ingestion_ui import (
    job_result_details,
    provenance_panel,
    publication_date_label,
    publication_timeline,
)
from .records import CollectionTotals


def content_signature(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def article_signature(article, claims) -> str:
    return content_signature([article, claims])


def pill(text: str, tone: str = "neutral") -> html.Span:
    return html.Span(text, className=f"pill pill--{tone}")


def format_date(value: str | None, *, full: bool = False) -> str:
    if not value:
        return "Date unavailable"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%d %b %Y · %H:%M %Z" if full else "%d %b %Y")
    except (ValueError, TypeError):
        return str(value)[:40]


def source_link(url: str, label: str = "Open source ↗"):
    if urlparse(url).scheme.lower() not in {"https", "http"}:
        return html.Span("Source link unavailable", className="muted")
    return html.A(
        label, href=url, target="_blank", rel="noopener noreferrer", className="source-link"
    )


def empty_state(title: str, description: str):
    return html.Div(
        [
            html.Div("◇", className="empty-state__icon"),
            html.Strong(title),
            html.P(description),
        ],
        className="empty-state",
    )


def stat_cards(articles: list[dict], claims: list[dict], *, totals: CollectionTotals | None = None):
    totals = totals or CollectionTotals(
        articles=len(articles),
        claims=len(claims),
        sources=len({a.get("publisher") for a in articles}),
        languages=len({a.get("language", "en") for a in articles}),
    )
    counts = [
        ("Articles", totals.articles, f"Latest {len(articles)} shown"),
        ("Sources", totals.sources, "Named publishers"),
        ("Evidence claims", totals.claims, f"{len(claims)} shown with quotations"),
        ("Languages", totals.languages, "In this collection"),
    ]
    return [
        html.Div(
            [
                html.Span(label, className="stat__label"),
                html.Strong(f"{value:,}"),
                html.Span(note, className="stat__note"),
            ],
            className="stat",
        )
        for label, value, note in counts
    ]


def result_cards(matches: list[dict], mode: str = "keyword"):
    if not matches:
        return empty_state("No matching passages", "Try a shorter phrase or another keyword.")
    cards = []
    for index, match in enumerate(matches):
        article_id = match.get("article_id", match.get("id"))
        if article_id is None:
            continue
        badges = [pill(str(match.get("language", "")).upper())] if match.get("language") else []
        if match.get("is_sample"):
            badges.append(pill("Synthetic sample", "sample"))
        cards.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Span(f"{index + 1:02}", className="result-card__rank"),
                            html.Span(
                                match.get("publisher", "Unknown publisher"),
                                className="result-card__publisher",
                            ),
                            html.Span(
                                publication_date_label(match.get("published_at")),
                                className="muted small",
                            ),
                        ],
                        className="result-card__meta",
                    ),
                    html.H3(match.get("title", "Untitled article")),
                    html.P(str(match.get("text", ""))[:420], className="result-card__passage"),
                    html.Div(
                        [
                            html.Div(badges, className="badge-row"),
                            html.Button(
                                "Inspect evidence →",
                                id={"type": "open-article", "index": f"{article_id}-{index}"},
                                n_clicks=0,
                                className="text-button",
                                **{"data-article-id": str(article_id)},
                            ),
                        ],
                        className="result-card__footer",
                    ),
                ],
                className="result-card",
            )
        )
    return cards


def article_panel(article: dict | None, claims: list[dict]):
    if not article:
        return empty_state("Choose an article", "Select a search result or a node in the graph.")
    sample = bool(article.get("is_sample"))
    badges = [pill(str(article.get("language", "en")).upper())]
    if sample:
        badges.append(pill("Synthetic sample", "sample"))
    cards = []
    for claim in claims:
        model_key = str(claim.get("model_key", claim.get("model", "")))
        is_demo = model_key.startswith("demo")
        label = "Cached demo claim" if is_demo else "Local model claim"
        if not model_key:
            label = "Saved claim"
        quote = str(claim.get("quote", ""))
        verified = bool(quote) and quote in article.get("text", "")
        details = []
        if claim.get("subject"):
            details.append(html.Span(f"Subject: {claim['subject']}"))
        if claim.get("attribution"):
            details.append(html.Span(f"Attributed to: {claim['attribution']}"))
        if claim.get("event_time"):
            details.append(html.Span(f"Event time: {claim['event_time']}"))
        if claim.get("location"):
            details.append(html.Span(f"Location: {claim['location']}"))
        cards.append(
            html.Div(
                [
                    html.Div(
                        [
                            pill(label, "sample" if is_demo else "green"),
                            pill(
                                "Quote found in source" if verified else "Check source quote",
                                "green" if verified else "warning",
                            ),
                        ],
                        className="badge-row",
                    ),
                    html.H4(claim.get("claim_text", "Extracted claim")),
                    html.Blockquote(quote),
                    html.Div(details, className="claim-details"),
                    html.Span(model_key, className="model-reference"),
                ],
                className="claim-card",
            )
        )
    return html.Div(
        [
            html.Div(badges, className="badge-row"),
            html.H3(article.get("title", "Untitled article"), className="article-title"),
            html.P(
                [
                    html.Strong(article.get("publisher", "Unknown publisher")),
                    " · ",
                    publication_date_label(article.get("published_at")),
                ],
                className="article-byline",
            ),
            html.Div(
                [
                    source_link(
                        article.get("url", ""),
                        "Fictional source URL ↗" if sample else "Open source ↗",
                    ),
                    html.Button(
                        "Add to Traced selection",
                        id="explorer-handoff-add",
                        value=str(article["id"]),
                        className="button saved-feed-button",
                        n_clicks=0,
                    ),
                    html.A("View selection", href="#explorer-handoff", className="source-link"),
                ],
                className="badge-row",
            ),
            *(
                [provenance_panel(article)]
                if any(
                    article.get(key)
                    for key in [
                        "retrieved_at",
                        "author",
                        "requested_url",
                        "final_url",
                        "feed_url",
                        "publication_source",
                    ]
                )
                else []
            ),
            html.Div(className="rule"),
            html.Div(
                [
                    html.H4("Claims & source quotations"),
                    html.Span(str(len(claims)), className="count-badge"),
                ],
                className="section-heading section-heading--small",
            ),
            html.P(
                "A source quotation supports attribution. Review the reporting and its context.",
                className="muted small",
            ),
            *(
                cards
                if cards
                else [
                    empty_state(
                        "No claims saved yet", "Run collection analysis to extract quoted claims."
                    )
                ]
            ),
            html.Details(
                [
                    html.Summary("Read saved article text"),
                    html.Div(
                        [html.P(p) for p in article.get("text", "").split("\n") if p.strip()],
                        className="saved-article-text",
                    ),
                ],
                className="article-disclosure",
            ),
        ]
    )


def graph_elements(
    articles: list[dict],
    claims: list[dict],
    limit: int = 50,
    *,
    selected_article_id: int | None = None,
) -> list[dict]:
    """Compatibility adapter for the focused publication-path renderer."""
    return source_graph(articles, claims, selected_article_id, limit).elements


def timeline_figure(articles: list[dict]) -> go.Figure:
    return publication_timeline(articles)


def status_content(status: dict, read_only: bool):
    worker = status.get("worker", {})
    ollama = status.get("ollama", {})
    worker_online = worker.get("online", False)
    local_ready = ollama.get("available", False) and not ollama.get("missing")
    missing = ollama.get("missing", [])
    return [
        html.Div(
            [
                html.Span(className="status-dot status-dot--green"),
                "Presentation snapshot" if read_only else "Local workspace",
            ],
            className="status-line",
        ),
        html.Div(
            [
                html.Span(className=f"status-dot {'status-dot--green' if worker_online else ''}"),
                "Worker online" if worker_online else "Worker offline",
            ],
            className="status-line",
        ),
        html.Div(
            [
                html.Span(className=f"status-dot {'status-dot--green' if local_ready else ''}"),
                "Models ready" if local_ready else "Models unavailable",
            ],
            className="status-line",
        ),
        html.P(
            "Read-only presentation"
            if read_only
            else "Keyword search works offline. Queued analysis needs the worker.",
            className="sidebar-note",
        ),
        *([html.P("Missing: " + ", ".join(missing), className="sidebar-note")] if missing else []),
    ]


def job_cards(jobs: list[dict]):
    if not jobs:
        return html.P(
            "No processing jobs yet. Your saved collection is ready to explore.",
            className="muted small activity-empty",
        )
    cards = []
    for job in jobs[:5]:
        state = job.get("status", "queued")
        tone = "green" if state == "succeeded" else "warning" if state == "failed" else "neutral"
        progress = job.get("progress", 0) or 0
        try:
            progress = float(progress)
            progress = progress * 100 if 0 < progress <= 1 else progress
        except (ValueError, TypeError):
            progress = 0
        kind = {
            "ingest": "News import",
            "analyze": "Collection analysis",
            "search": "Collection search",
        }.get(job.get("kind"), str(job.get("kind", "Processing")).replace("_", " ").title())
        cards.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Strong(kind),
                            pill(state.replace("_", " ").title(), tone),
                        ],
                        className="job-card__header",
                    ),
                    html.P(
                        job.get("error") or job.get("stage") or "Waiting to start",
                        className="small muted",
                    ),
                    *(
                        [job_result_details(job["result"])]
                        if job.get("kind") == "ingest" and job.get("result")
                        else []
                    ),
                    *(
                        [
                            html.P(
                                f"{job['result']['claims_rejected']} proposed claim(s) excluded: "
                                "quotations did not match their source passages.",
                                className="notice notice--neutral small",
                            )
                        ]
                        if job.get("kind") == "analyze"
                        and (job.get("result") or {}).get("claims_rejected", 0)
                        else []
                    ),
                    html.Div(
                        html.Div(style={"width": f"{max(0, min(100, progress))}%"}),
                        className="progress-track",
                    ),
                ],
                className="job-card",
                id=f"job-{job['id']}",
            )
        )
    return cards
