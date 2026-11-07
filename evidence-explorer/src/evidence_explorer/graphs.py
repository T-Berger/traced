"""Readable publication paths for one selected article and its saved quotations.

The graph describes recorded publication and extraction relationships. It never
infers corroboration, copying, ownership, or truth from the displayed connections.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from dash import html

MAX_QUOTATIONS = 3
GRAPH_LAYOUT = {"name": "preset", "animate": False, "fit": True, "padding": 28}
GRAPH_STYLESHEET = [
    {
        "selector": "node",
        "style": {
            "label": "data(label)",
            "font-family": "Manrope, sans-serif",
            "font-size": 15,
            "color": "#142229",
            "text-wrap": "wrap",
            "text-max-width": 180,
            "text-valign": "center",
            "text-halign": "center",
            "shape": "round-rectangle",
            "width": 195,
            "height": 116,
            "padding": 10,
            "border-width": 1,
            "border-color": "#52636C",
            "background-color": "#F4F3EE",
        },
    },
    {"selector": ".publisher", "style": {"background-color": "#ECEEE9"}},
    {
        "selector": ".article",
        "style": {
            "background-color": "#D9E9E4",
            "border-color": "#276B63",
            "border-width": 2,
        },
    },
    {"selector": ".claim", "style": {"background-color": "#FFFFFF", "border-color": "#52636C"}},
    {
        "selector": "edge",
        "style": {
            "label": "data(label)",
            "font-size": 13,
            "font-family": "Manrope, sans-serif",
            "color": "#52636C",
            "text-background-color": "#ffffff",
            "text-background-opacity": 1,
            "text-background-padding": 4,
            "width": 1.5,
            "line-color": "#276B63",
            "target-arrow-color": "#276B63",
            "target-arrow-shape": "triangle",
            "curve-style": "bezier",
            "arrow-scale": 1,
        },
    },
    {"selector": "node:selected", "style": {"border-color": "#F36643", "border-width": 3}},
]


@dataclass(frozen=True)
class SourceGraph:
    elements: list[dict]
    article: dict | None
    claims: tuple[dict, ...]
    summary: str
    omitted_claim_count: int


def _label(text: str, maximum: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= maximum else text[: maximum - 1].rstrip() + "…"


def source_graph(
    articles: list[dict],
    claims: list[dict],
    selected_article_id: int | None = None,
    limit: int = 5,
) -> SourceGraph:
    """Bound the visible path; browser nodes contain IDs and labels, not bodies."""
    article = next((row for row in articles if row["id"] == selected_article_id), None)
    if article is None and selected_article_id is None:
        article = articles[0] if articles else None
    if article is None or limit < 2:
        return SourceGraph([], article, (), "Choose an article to inspect its publication path.", 0)
    article_id = article["id"]
    verified = []
    for claim in sorted(claims, key=lambda row: str(row["id"])):
        quote = claim.get("quote")
        if (
            claim.get("article_id") != article_id
            or not isinstance(quote, str)
            or not quote.strip()
            or not claim.get("claim_text")
        ):
            continue
        # Summary records intentionally omit source bodies. Their verified flag
        # comes from the indexed repository query; a supplied body takes priority.
        if "text" in article:
            matches_source = isinstance(article["text"], str) and quote in article["text"]
        else:
            matches_source = claim.get("source_verified") is True
        if matches_source:
            verified.append(claim)
    shown = tuple(verified[: min(MAX_QUOTATIONS, max(limit - 2, 0))])
    count = len(shown)
    center_y = 80 + max(count - 1, 0) * 82
    publisher = str(article.get("publisher") or "Publisher not supplied")
    source_id = "source-" + hashlib.sha256(publisher.encode()).hexdigest()[:12]
    article_node_id = f"article-{article_id}"
    elements = [
        {
            "data": {
                "id": source_id,
                "kind": "publisher",
                "label": "Publisher\n" + _label(publisher, 80),
            },
            "classes": "publisher",
            "position": {"x": 110, "y": center_y},
            "grabbable": False,
        },
        {
            "data": {
                "id": article_node_id,
                "kind": "article",
                "article_id": article_id,
                "label": "Selected article\n"
                + _label(str(article.get("title", "Untitled article")), 90),
            },
            "classes": "article",
            "position": {"x": 410, "y": center_y},
            "grabbable": False,
        },
        {
            "data": {
                "id": f"published-{article_id}",
                "source": source_id,
                "target": article_node_id,
                "label": "published",
                "kind": "publication",
            }
        },
    ]
    for index, claim in enumerate(shown):
        claim_node_id = f"claim-{claim['id']}"
        elements += [
            {
                "data": {
                    "id": claim_node_id,
                    "kind": "claim",
                    "article_id": article_id,
                    "claim_id": claim["id"],
                    "label": "Quoted claim\n" + _label(str(claim["claim_text"]), 110),
                },
                "classes": "claim",
                "position": {"x": 710, "y": 80 + index * 164},
                "grabbable": False,
            },
            {
                "data": {
                    "id": f"quoted-{claim['id']}",
                    "source": article_node_id,
                    "target": claim_node_id,
                    "label": "saved quote",
                    "kind": "saved_quotation",
                }
            },
        ]
    omitted = len(verified) - count
    summary = (
        f"1 publisher · 1 article · {count} source-matched "
        f"quotation{'s' if count != 1 else ''} shown."
    )
    if omitted:
        summary = (
            f"1 publisher · 1 article · {count} of {len(verified)} "
            "source-matched quotations shown in this preview. "
            "More may be available in the article inspector."
        )
    if not count:
        summary += " No saved quotation in this preview matches the selected article."
    return SourceGraph(elements, article, shown, summary, omitted)


def graph_style(graph: SourceGraph) -> dict[str, str]:
    return {"width": "100%", "height": f"{max(250, len(graph.claims) * 164 + 44)}px"}


def graph_outline(graph: SourceGraph, location: str = "source"):
    """A full-text path that remains readable and keyboard usable on small screens."""
    if graph.article is None:
        return html.P(graph.summary)
    article = graph.article
    return html.Div(
        [
            html.P([html.Strong("Publisher: "), article.get("publisher") or "Not supplied"]),
            html.P([html.Strong("Article: "), article.get("title") or "Untitled article"]),
            html.Ol(
                [
                    html.Li(
                        [
                            html.Strong(claim["claim_text"]),
                            html.Blockquote(claim["quote"]),
                            html.Button(
                                "Inspect in article",
                                id={
                                    "type": "open-article",
                                    "index": f"{article['id']}-graph-{location}-{claim['id']}",
                                },
                                n_clicks=0,
                                className="text-button",
                            ),
                        ]
                    )
                    for claim in graph.claims
                ]
            ),
        ],
        className="source-graph-outline-content",
    )
