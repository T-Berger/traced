"""Bounded, deterministic evidence diagrams with complete connection provenance.

Edges point from a supplied record or premise finding to the finding under review.
Grouping repeated citations is a display operation, never an independence claim.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from .selectors import Bundle, finding_id, reference


@dataclass(frozen=True)
class GraphView:
    elements: list[dict[str, Any]]
    shown_premises: int
    total_premises: int
    total_links: int
    shown_links: int
    relationship_count: int
    hidden_premises: int
    summary: str


def compact_label(kind: str, text: str) -> str:
    lines = textwrap.wrap(" ".join(text.split()), width=26, max_lines=2, placeholder="…")
    return kind + "\n" + "\n".join(lines)


def _connection_label(kind: str, relation: str, citations: list) -> str:
    if relation != "depends_on":
        return relation.replace("_", " ")
    assessment = any(link.get("assessment_input") is True for link, _ in citations)
    challenge = any(link.get("challenge_input") is True for link, _ in citations)
    if assessment and challenge:
        return "assessment / challenge input"
    if assessment:
        return "assessment input"
    if challenge:
        return "challenge input"
    return "premise for" if kind == "finding" else "input dependency"


def direct_connections(bundle: Bundle, selected: str | None) -> list[dict[str, Any]]:
    """Group identical typed endpoints and relation, retaining every link and span."""
    findings = {finding_id(f): f for f in bundle.get("findings", [])}
    if selected not in findings:
        return []
    records = {str(r["id"]): r for r in bundle.get("records", [])}
    fragments = {str(f["id"]): f for f in bundle.get("fragments", [])}
    grouped = defaultdict(list)
    for link in bundle.get("links", []):
        target_kind, target = reference(link.get("target"))
        if target_kind != "finding" or target != selected:
            continue
        kind, source = reference(link.get("source"))
        fragment_ids = list(link.get("fragment_ids", []))
        if kind == "fragment":
            if source not in fragments:
                continue
            fragment_ids.append(source)
            source, kind = str(fragments[source]["record_id"]), "record"
        if (
            (kind == "record" and source not in records)
            or (kind == "finding" and source not in findings)
            or kind not in {"record", "finding"}
        ):
            continue
        grouped[(kind, source, str(link.get("relation", "supports")), target)].append(
            (link, fragment_ids)
        )
    connections = []
    for key, citations in sorted(grouped.items()):
        kind, source, relation, target = key
        identity = hashlib.sha256(json.dumps(key).encode()).hexdigest()[:20]
        connections.append(
            {
                "id": "evidence-edge:" + identity,
                "kind": "evidence_link",
                "source": f"{kind}:{source}",
                "target": f"finding:{target}",
                "source_kind": kind,
                "source_ref": source,
                "target_ref": target,
                "relation": relation,
                "label": _connection_label(kind, relation, citations),
                "source_label": (
                    records[source].get("source_label", source)
                    if kind == "record"
                    else findings[source].get("statement", source)
                ),
                "link_ids": sorted({str(link["id"]) for link, _ in citations}),
                "fragment_ids": sorted({str(fid) for _, ids in citations for fid in ids}),
                "bases": sorted({str(link["basis"]) for link, _ in citations if link.get("basis")}),
            }
        )
    return connections


def build_evidence_graph(
    bundle: Bundle,
    selected: str | None,
    affected: Collection[str] = (),
    excluded: Collection[str] = (),
    *,
    max_premises: int = 4,
    active_record_id: str | None = None,
) -> GraphView:
    findings = {finding_id(f): f for f in bundle.get("findings", [])}
    if selected not in findings:
        return GraphView([], 0, 0, 0, 0, 0, 0, "Choose a finding to inspect its connections.")
    if max_premises < 1:
        raise ValueError("The graph must show at least one available premise.")
    connections = direct_connections(bundle, selected)
    candidates = sorted({(edge["source_kind"], edge["source_ref"]) for edge in connections})
    shown = candidates[:max_premises]
    active = ("record", active_record_id)
    if active in candidates and active not in shown:
        shown = [*shown[:-1], active]
    chosen = set(shown)
    visible_edges = [e for e in connections if (e["source_kind"], e["source_ref"]) in chosen]
    selected_class = "finding active" + (" affected" if selected in affected else "")
    nodes = [
        {
            "data": {
                "id": f"finding:{selected}",
                "kind": "finding",
                "ref_id": selected,
                "label": compact_label(
                    "Finding · needs review" if selected in affected else "Selected finding",
                    findings[selected]["statement"],
                ),
                "full_label": findings[selected]["statement"],
                "status_label": "Needs review" if selected in affected else "Selected finding",
            },
            "classes": selected_class,
            "position": {"x": 570, "y": 70 + max(0, len(shown) - 1) * 60},
        }
    ]
    for index, (kind, source) in enumerate(shown):
        edge = next(
            e for e in visible_edges if e["source_kind"] == kind and e["source_ref"] == source
        )
        is_excluded = kind == "record" and source in excluded
        prefix = (
            "Excluded record"
            if is_excluded
            else ("Saved record" if kind == "record" else "Premise finding")
        )
        if kind == "finding" and source in affected:
            prefix = "Premise · needs review"
        nodes.append(
            {
                "data": {
                    "id": f"{kind}:{source}",
                    "kind": kind,
                    "ref_id": source,
                    "label": compact_label(prefix, edge["source_label"]),
                    "full_label": edge["source_label"],
                    "status_label": prefix,
                },
                "classes": kind
                + (" excluded" if is_excluded else "")
                + (" affected" if kind == "finding" and source in affected else "")
                + (" focused" if kind == "record" and source == active_record_id else ""),
                "position": {"x": 150, "y": 70 + index * 120},
            }
        )
    edges = [
        {
            "data": e,
            "classes": (
                "excluded " if e["source_kind"] == "record" and e["source_ref"] in excluded else ""
            )
            + ("contradicts" if e["relation"] == "contradicts" else ""),
        }
        for e in visible_edges
    ]
    total_links = sum(len(e["link_ids"]) for e in connections)
    shown_links = sum(len(e["link_ids"]) for e in visible_edges)
    count = len(candidates)
    summary = (
        f"{len(shown)} of {count} direct premises shown · "
        f"{len(visible_edges)} relationship(s) · {shown_links} of {total_links} citations"
        if count
        else "No direct evidence linked to this finding. Inspect its stated assumptions."
    )
    if len(shown) < count:
        summary += ". All connections remain available in the list below."
    return GraphView(
        nodes + edges,
        len(shown),
        count,
        total_links,
        shown_links,
        len(visible_edges),
        count - len(shown),
        summary,
    )


GRAPH_STYLE = [
    {
        "selector": "node",
        "style": {
            "label": "data(label)",
            "font-family": "Manrope, Arial, sans-serif",
            "font-size": 14,
            "text-wrap": "wrap",
            "text-max-width": 200,
            "text-valign": "center",
            "text-halign": "center",
            "color": "#142229",
            "background-color": "#ECEEE9",
            "shape": "round-rectangle",
            "width": 220,
            "height": 78,
            "padding": 10,
            "border-color": "#52636C",
            "border-width": 1.5,
        },
    },
    {"selector": ".finding", "style": {"background-color": "#D9E9E4"}},
    {"selector": ".active", "style": {"border-width": 3, "border-color": "#276B63"}},
    {"selector": ".focused", "style": {"border-width": 3, "border-color": "#276B63"}},
    {"selector": ".affected", "style": {"background-color": "#FFF0CF", "border-color": "#754B05"}},
    {"selector": ".excluded", "style": {"border-style": "dashed"}},
    {
        "selector": "edge",
        "style": {
            "label": "data(label)",
            "font-family": "Manrope, Arial, sans-serif",
            "font-size": 12,
            "color": "#52636C",
            "text-background-color": "#F4F3EE",
            "text-background-opacity": 1,
            "text-background-padding": 4,
            "width": 2,
            "line-color": "#52636C",
            "target-arrow-color": "#52636C",
            "target-arrow-shape": "triangle",
            "curve-style": "bezier",
            "arrow-scale": 1.2,
        },
    },
    {"selector": "edge.excluded", "style": {"line-style": "dashed"}},
    {
        "selector": "edge.contradicts",
        "style": {
            "line-color": "#8F2F33",
            "target-arrow-color": "#8F2F33",
            "line-style": "dashed",
        },
    },
    {
        "selector": "edge:selected",
        "style": {"width": 4, "line-color": "#276B63", "target-arrow-color": "#276B63"},
    },
    {"selector": "node:selected", "style": {"border-color": "#276B63", "border-width": 4}},
]
