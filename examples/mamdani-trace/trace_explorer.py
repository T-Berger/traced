"""Build the interactive Mamdani trace page from verified, saved inputs."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from functools import partial
from pathlib import Path
from textwrap import wrap
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import plotly.graph_objects as go
from investigation_icons import ICON_CSS, icon, signal_chip
from plotly.offline import get_plotlyjs
from plotly.subplots import make_subplots
from topic_clusters import TOPIC_CSS, render_topics

BRAND = Path(__file__).resolve().parents[2] / "brand" / "traced"
NY = ZoneInfo("America/New_York")
PALETTE = {
    "ink": "#142229",
    "slate": "#52636C",
    "line": "#D9E0DE",
    "moss": "#276B63",
    "trace": "#F36643",
    "blue": "#376780",
    "paper": "#F4F3EE",
}
COLORS = {
    "post": "#a8d0c8",
    "capture": "#d7e9e5",
    "account": "#ccdbe2",
    "draft_claim": "#f6d1a7",
    "draft_step": "#fae3c9",
    "archive_pointer": "#d7dae3",
    "web_result": "#dbe8ef",
    "conversation": "#dce5fa",
}
POST_LABELS = {
    "1985692613010502114": "Ballot post",
    "1985336258193662449": "Related ballot post",
    "1980606111838355647": "Diwali parent post",
    "1980606660528804219": "Diwali quote post",
    "1985642138177044502": "Election Day quote",
    "1984725922528866654": "Forbes original",
    "1994890085477601610": "Small business post",
    "1994890243095314530": "Same conversation",
    "1994906376922173580": "Quote by @Heminator",
    "1995082155417092188": "Quote by @BehizyTweets",
}
GROUP_LABELS = {
    "archive_locator_family": "Archive pointers",
    "native_draft_evidence_chain": "Draft chains",
    "parent_relationship_comparison": "Quote links",
    "declared_conversation_and_quotes": "Conversations",
    "same_post_captures": "Post captures",
    "saved_search_results": "Web leads",
}
GROUP_COLORS = {
    "archive_locator_family": "#dce2f0",
    "native_draft_evidence_chain": "#f5d3b2",
    "parent_relationship_comparison": "#e8d9ee",
    "declared_conversation_and_quotes": "#cce2f4",
    "same_post_captures": "#c4e3dc",
    "saved_search_results": "#dde5e9",
}
FOCUSES = {
    "small-business": "1994890085477601610",
    "election-quote": "1985642138177044502",
    "diwali": "1980606660528804219",
    "ballot": "1985692613010502114",
}


def esc(value):
    return html.escape(str(value), quote=True)


def node_label(node):
    return (
        POST_LABELS.get(node.get("native_id"), node["label"])
        if node["kind"] == "post"
        else node["label"]
    )


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_link(url, label):
    if not isinstance(url, str):
        return ""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return ""
    return f'<a href="{esc(url)}" rel="noopener noreferrer" target="_blank">{esc(label)} ↗</a>'


def chart(figure, chart_id):
    figure.update_layout(
        template="plotly_white",
        paper_bgcolor="white",
        plot_bgcolor="white",
        font={"family": "Manrope, sans-serif", "color": PALETTE["ink"], "size": 13},
        autosize=True,
    )
    return figure.to_html(
        full_html=False,
        include_plotlyjs=False,
        div_id=chart_id,
        config={"responsive": True, "displaylogo": False, "scrollZoom": False},
    )


def focus_plot(nodes, edges, anchors, focus_ids, *, same_page=False):
    """Display immediate neighbors with the directions supplied by the evidence."""
    fig = go.Figure()
    views = []
    for native_id in focus_ids:
        root = "post:x:" + native_id
        if root not in nodes:
            continue
        neighbors = defaultdict(list)
        for edge in edges:
            if edge["kind"] in {"authored_by", "archive_points_to"}:
                continue
            source, target = edge["source"], edge["target"]
            if root in (source, target):
                other = target if source == root else source
                neighbors[other].append(edge)
        if not neighbors:
            continue
        positions = {root: (0, 0.78)}
        for index, node_id in enumerate(neighbors):
            x = 0 if len(neighbors) == 1 else -0.94 + index * 1.88 / (len(neighbors) - 1)
            positions[node_id] = (x, -0.57)
        arrows = []
        for links in neighbors.values():
            edge = links[0]
            sx, sy = positions[edge["source"]]
            tx, ty = positions[edge["target"]]
            membership = "conversation" in edge["kind"]
            arrows.append(
                {
                    "x": sx + (tx - sx) * 0.88,
                    "y": sy + (ty - sy) * 0.88,
                    "ax": sx + (tx - sx) * 0.10,
                    "ay": sy + (ty - sy) * 0.10,
                    "xref": "x",
                    "yref": "y",
                    "axref": "x",
                    "ayref": "y",
                    "text": "",
                    "showarrow": True,
                    "arrowhead": 0 if membership else 2,
                    "arrowcolor": PALETTE["slate"] if membership else PALETTE["moss"],
                    "arrowwidth": 1.5,
                }
            )
            edge_label = {
                "capture_of_same_post": "Capture",
                "excerpt_of_post": "Excerpt",
                "declared_conversation_member": "Member",
                "provider_returned_parent": "Vetric original",
            }.get(edge["kind"], "Quote" if "quote" in edge["label"] else "Parent")
            if len(links) > 1 and any(item["kind"] == "provider_returned_parent" for item in links):
                edge_label = (
                    "Quote + Vetric"
                    if any("quote" in item["label"] for item in links)
                    else "Parent + Vetric"
                )
            fraction = 0.63
            nx, ny = (tx, ty) if edge["source"] == root else (sx, sy)
            rx, ry = positions[root]
            arrows.append(
                {
                    "x": rx + (nx - rx) * fraction,
                    "y": ry + (ny - ry) * fraction,
                    "text": edge_label,
                    "showarrow": False,
                    "bgcolor": "white",
                    "borderpad": 2,
                    "font": {"color": PALETTE["slate"], "size": 11},
                }
            )
        labels = []
        details = []
        for node_id in positions:
            node = nodes[node_id]
            short = {
                "1994906376922173580": "Heminator quote",
                "1995082155417092188": "Behizy quote",
            }.get(node.get("native_id"))
            if node["kind"] in {"capture", "conversation"}:
                short = {"capture": "Saved capture", "conversation": "Conversation"}[node["kind"]]
            lines = wrap(
                short or node_label(node),
                width=13,
                max_lines=3,
                placeholder="…",
                break_long_words=False,
            )
            if node["kind"] == "capture":
                lines = ["Saved", "capture"]
            label = "<br>".join(esc(line) for line in lines)
            target = ' target="_self"' if same_page else ""
            labels.append(f'<a href="#{anchors[node_id]}"{target}>{label}</a>')
            detail = [esc(edge["label"]) for edge in neighbors.get(node_id, ())]
            if node.get("native_id"):
                detail.append(esc(node["native_id"]))
            details.append("<br>".join(detail))
        fig.add_trace(
            go.Scatter(
                x=[point[0] for point in positions.values()],
                y=[point[1] for point in positions.values()],
                mode="markers+text",
                marker={
                    "size": [28 if node_id == root else 20 for node_id in positions],
                    "color": [
                        PALETTE["moss"]
                        if node_id == root
                        else COLORS.get(nodes[node_id]["kind"], "#ccdbe2")
                        for node_id in positions
                    ],
                    "line": {"color": PALETTE["moss"], "width": 1.4},
                },
                text=labels,
                textfont={"size": 12},
                textposition=["top center", *(["bottom center"] * len(neighbors))],
                customdata=details,
                hovertemplate="%{text}<br>%{customdata}<extra></extra>",
                visible=not views,
            )
        )
        views.append((POST_LABELS[native_id], arrows))
    fig.update_layout(
        height=245,
        margin={"l": 2, "r": 2, "t": 10, "b": 4},
        xaxis={"range": [-1.24, 1.24], "visible": False, "fixedrange": True},
        yaxis={"range": [-1.15, 1.48], "visible": False, "fixedrange": True},
        showlegend=False,
    )
    if views:
        fig.update_layout(annotations=views[0][1])
    if len(views) > 1:
        fig.update_layout(
            updatemenus=[
                {
                    "x": 0.02,
                    "xanchor": "left",
                    "y": 1.17,
                    "direction": "down",
                    "buttons": [
                        {
                            "label": label,
                            "method": "update",
                            "args": [
                                {"visible": [index == other for other in range(len(views))]},
                                {"annotations": arrows},
                            ],
                        }
                        for index, (label, arrows) in enumerate(views)
                    ],
                }
            ],
        )
    return fig


class Branches:
    def __init__(self, root):
        self.labels = [esc(root)]
        self.ids = ["root"]
        self.parents = [""]
        self.detail = [""]
        self.colors = [PALETTE["paper"]]

    def add(self, parent, label, detail="", color="#e8edeb"):
        identity = f"b{len(self.ids)}"
        self.ids.append(identity)
        self.parents.append(parent)
        self.labels.append(esc(label))
        self.detail.append(esc(detail))
        self.colors.append(color)
        return identity

    def item(self, parent, node, detail="", label=None):
        summary = " · ".join(
            str(value)
            for value in (node["kind"].replace("_", " "), node.get("native_id"), detail)
            if value
        )
        return self.add(
            parent,
            label or node_label(node),
            summary,
            COLORS.get(node["kind"], "#e8edeb"),
        )

    def figure(self, kind):
        plot = go.Icicle if kind == "tree" else go.Treemap
        options = {}
        if kind == "tree":
            child_counts = Counter(self.parents)
            weights = {"root": 1.0}
            for node_id, parent in zip(self.ids[1:], self.parents[1:], strict=True):
                weights[node_id] = weights[parent] * 0.999999 / child_counts[parent]
            options = {
                "tiling": {"orientation": "v"},
                "values": [weights[node_id] for node_id in self.ids],
                "branchvalues": "total",
                "sort": False,
            }
        fig = go.Figure(
            plot(
                ids=self.ids,
                labels=self.labels,
                parents=self.parents,
                customdata=self.detail,
                marker={"colors": self.colors, "line": {"color": "white", "width": 2}},
                textfont={"color": PALETTE["ink"], "size": 14},
                maxdepth=2,
                textinfo="label",
                hovertemplate="%{label}<br>%{customdata}<extra></extra>",
                **options,
            )
        )
        fig.update_layout(
            height=390,
            margin={"l": 5, "r": 5, "t": 26, "b": 5},
            uniformtext={"minsize": 11, "mode": "hide"},
        )
        if kind == "tree":
            buttons = [
                {
                    "label": "All branches",
                    "method": "restyle",
                    "args": [{"level": "root"}],
                }
            ]
            buttons.extend(
                {
                    "label": self.labels[i],
                    "method": "restyle",
                    "args": [{"level": node_id}],
                }
                for i, (node_id, parent) in enumerate(zip(self.ids, self.parents, strict=True))
                if parent == "root"
            )
            fig.update_layout(
                margin={"l": 5, "r": 5, "t": 48, "b": 5},
                updatemenus=[{"buttons": buttons, "direction": "down", "x": 0, "y": 1.14}],
            )
        return fig


def tree_plot(nodes, edges):
    by_kind = defaultdict(list)
    for edge in edges:
        by_kind[edge["kind"]].append(edge)
    tree = Branches("Mamdani evidence trace")

    def family(label, kinds, reverse=False, color="#e8edeb"):
        selected = [edge for kind in kinds for edge in by_kind[kind]]
        if not selected:
            return
        category = tree.add("root", label, f"{len(selected)} saved links", color)
        grouped = defaultdict(list)
        root_key, leaf_key = ("target", "source") if reverse else ("source", "target")
        for edge in selected:
            grouped[edge[root_key]].append(edge)
        for root, links in grouped.items():
            branch = tree.item(category, nodes[root])
            for edge in links:
                leaf = nodes[edge[leaf_key]]
                qualifier = {
                    "declared_parent": "Collector",
                    "provider_returned_parent": "Vetric",
                }.get(edge["kind"])
                label = f"{node_label(leaf)} · {qualifier}" if qualifier else None
                tree.item(branch, leaf, edge["label"], label)

    family(
        "Posts and captures",
        ("capture_of_same_post", "excerpt_of_post"),
        reverse=True,
        color=GROUP_COLORS["same_post_captures"],
    )
    family(
        "Quote and parent links",
        ("declared_parent", "provider_returned_parent"),
        color=GROUP_COLORS["parent_relationship_comparison"],
    )
    family(
        "Conversations",
        ("declared_conversation_member",),
        reverse=True,
        color=GROUP_COLORS["declared_conversation_and_quotes"],
    )
    claims = tree.add(
        "root",
        "Draft claims and evidence",
        color=GROUP_COLORS["native_draft_evidence_chain"],
    )
    evidence = defaultdict(list)
    for edge in by_kind["declared_draft_evidence"]:
        evidence[edge["target"]].append(edge)
    premises = defaultdict(list)
    for edge in by_kind["draft_dependency"]:
        premises[edge["target"]].append(edge)
    for claim, links in premises.items():
        claim_branch = tree.item(claims, nodes[claim])
        for link in links:
            premise = tree.item(claim_branch, nodes[link["source"]], link["label"])
            for item in evidence[link["source"]]:
                tree.item(premise, nodes[item["source"]], item["label"])
    family("Public accounts and posts", ("authored_by",), reverse=True)
    family(
        "Archive pointers",
        ("archive_points_to",),
        reverse=True,
        color=GROUP_COLORS["archive_locator_family"],
    )
    family("Web search leads", ("discovered_web_result",))
    return tree.figure("tree")


def cluster_plot(nodes, clusters):
    branches = Branches("Evidence groups")
    families = {}
    for group in clusters:
        kind = group["kind"]
        color = GROUP_COLORS.get(kind, "#e8edeb")
        if kind not in families:
            families[kind] = branches.add(
                "root", GROUP_LABELS.get(kind, kind.replace("_", " ")), color=color
            )
        branch = branches.add(
            families[kind],
            group["label"],
            f"{len(group['member_ids'])} evidence items",
            color=color,
        )
        for member in group["member_ids"]:
            branches.item(branch, nodes[member])
    return branches.figure("cluster")


def timeline_plot(data, nodes, root, related):
    rows = data["timeline"]["bins"]
    days = [row["date"] for row in rows]
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.73, 0.27],
        vertical_spacing=0.08,
    )
    groups = {
        "Third-party": ("third_party",),
        "Subject / affiliated": ("subject_own", "subject_affiliated"),
    }
    for (label, values), color in zip(
        groups.items(), (PALETTE["slate"], PALETTE["moss"]), strict=True
    ):
        figure.add_trace(
            go.Bar(
                x=days,
                y=[
                    sum(row["counts_by_attribution"].get(value, 0) for value in values)
                    for row in rows
                ],
                name=label,
                marker_color=color,
                customdata=[row["selected_case_count"] for row in rows],
                hovertemplate="%{x|%b %d, %Y}<br>%{y:,} saved X posts<br>%{customdata} posts selected into the case<extra>%{fullData.name}</extra>",
            ),
            row=1,
            col=1,
        )

    events = {"Post dates": [], "Archive dates": []}
    for node in nodes.values():
        timestamp = node.get("published_at")
        lane = "Post dates"
        if node["kind"] == "archive_pointer":
            timestamp = node.get("archive_timestamp")
            lane = "Archive dates"
        elif node["kind"] != "post":
            continue
        if timestamp:
            moment = datetime.fromisoformat(timestamp)
            if moment.tzinfo is None:
                continue
            events[lane].append((moment.astimezone(NY), node))
    for (label, points), color, symbol in zip(
        events.items(),
        (PALETTE["moss"], PALETTE["blue"]),
        ("circle", "diamond"),
        strict=True,
    ):
        figure.add_trace(
            go.Scatter(
                x=[moment.replace(tzinfo=None).isoformat() for moment, _ in points],
                y=[label] * len(points),
                name=label,
                mode="markers",
                marker={"size": 10, "color": color, "symbol": symbol},
                customdata=[esc(node_label(node)) for _, node in points],
                hovertemplate="%{customdata}<br>%{x|%b %d, %H:%M}<extra>%{fullData.name}</extra>",
            ),
            row=2,
            col=1,
        )
        selected = [(moment, node) for moment, node in points if node["id"] in related]
        figure.add_trace(
            go.Scatter(
                x=[moment.replace(tzinfo=None).isoformat() for moment, _ in selected],
                y=[label] * len(selected),
                name="Selected trace",
                mode="markers",
                showlegend=label == "Post dates",
                marker={
                    "size": [15 if node["id"] == root else 12 for _, node in selected],
                    "symbol": [
                        symbol if node["id"] == root else f"{symbol}-open" for _, node in selected
                    ],
                    "color": PALETTE["trace"],
                    "line": {"width": 2},
                },
                customdata=[esc(node_label(node)) for _, node in selected],
                hovertemplate="%{customdata}<br>%{x|%b %d, %H:%M}<extra>Selected trace</extra>",
            ),
            row=2,
            col=1,
        )
    for gap in data["scope"]["coverage_gaps"]["mentions"]:
        start, end = [
            datetime.fromisoformat(gap[key]).astimezone(NY).replace(tzinfo=None).isoformat()
            for key in ("start", "end")
        ]
        figure.add_vrect(
            x0=start,
            x1=end,
            line_width=0,
            fillcolor="rgba(243,102,67,0.10)",
            layer="below",
            row=1,
            col=1,
        )
    figure.add_vline(
        x="2025-11-04",
        line_width=2,
        line_dash="dot",
        line_color=PALETTE["trace"],
        row="all",
        col=1,
    )
    figure.add_annotation(
        x="2025-11-04",
        y=1.02,
        yref="paper",
        text="Nov 4 · Election Day",
        showarrow=False,
        xanchor="left",
    )
    figure.update_layout(
        barmode="stack",
        height=390,
        hovermode="closest",
        dragmode="zoom",
        margin={"l": 54, "r": 18, "t": 62, "b": 50},
        legend={"orientation": "h", "y": -0.19, "font": {"size": 11}},
        updatemenus=[
            {
                "type": "dropdown",
                "direction": "down",
                "x": 0,
                "xanchor": "left",
                "y": 1.23,
                "buttons": [
                    {
                        "label": label,
                        "method": "relayout",
                        "args": [{"xaxis.range": [start, end], "xaxis2.range": [start, end]}],
                    }
                    for label, start, end in (
                        ("Full window", "2025-09-04", "2026-02-04"),
                        ("Before Nov 4", "2025-09-04", "2025-11-04"),
                        ("From Nov 4", "2025-11-04", "2026-02-04"),
                    )
                ],
            }
        ],
    )
    figure.update_yaxes(title_text="Saved posts / day", gridcolor=PALETTE["line"], row=1, col=1)
    figure.update_yaxes(
        showgrid=False,
        showticklabels=True,
        automargin=True,
        tickfont={"size": 10},
        row=2,
        col=1,
    )
    figure.update_xaxes(range=["2025-09-04", "2026-02-04"], tickformat="%b %d", nticks=7)
    return figure


def record_index(nodes, edges, archive, focuses, *, preview_prefix):
    links = defaultdict(list)
    for edge in edges:
        links[edge["source"]].append(edge)
        links[edge["target"]].append(edge)
    anchors = {node_id: f"evidence-{i}" for i, node_id in enumerate(nodes, 1)}
    with zipfile.ZipFile(archive) as package:
        native = json.loads(package.read("bundle.json"))
    native_records = {record["id"]: record for record in native["records"]}
    native_anchors = {r["id"]: f"record-{i}" for i, r in enumerate(native["records"], 1)}

    def index_link(node_id, label=None, focus=None):
        anchor = anchors[node_id]
        if focus is not None:
            anchor = f"{anchor}-{focus}"
        return f'<a href="#{anchor}">{esc(label or node_label(nodes[node_id]))}</a>'

    def relationship(edge):
        quotes = []
        for ref in edge.get("exact_source_quotes", []):
            text = native_records[ref["record_id"]]["text"]
            start, end = ref["start"], ref["end"]
            if (
                not 0 <= start < end <= len(text)
                or text[start:end] != ref["quote"]
                or digest(text.encode()) != ref["text_sha256"]
            ):
                raise ValueError("A displayed evidence fragment does not match the case.")
            quotes.append(
                f"<blockquote>{esc(ref['quote'])}<br><small>Saved text · characters {start}–{end}</small></blockquote>"
            )
        return (
            f"<li>{index_link(edge['source'])} → {index_link(edge['target'])}"
            f"<br><strong>{esc(edge['label'])}</strong> · {esc(edge['basis'])}{''.join(quotes)}</li>"
        )

    def row(node_id, node):
        rows = "".join(map(relationship, links[node_id]))
        native_anchor = native_anchors.get(node.get("record_id"))
        jump = (
            f'<a href="{preview_prefix}#{native_anchor}">Inspect native source record</a>'
            if native_anchor
            else ""
        )
        quote = node.get("text_preview") or node.get("statement")
        block = f"<blockquote>{esc(quote)}</blockquote>" if quote else ""
        dates = " · ".join(
            f"{label} {node[key]}"
            for key, label in (
                ("published_at", "Published"),
                ("acquired_at", "Captured"),
                ("archive_timestamp", "Archive capture"),
            )
            if node.get(key)
        )
        replay = {
            "checked_error_shell": "Replay checked. X returned an error page.",
            "not_checked": "Archive pointer. Replay remains unchecked.",
        }.get(node.get("replay_status"), "")
        scope = {
            "outside_requested_dates_by_saved_locator_and_snippet": "Search lead outside the requested election windows.",
            "publication_date_not_independently_verified": "Search lead with publication date unverified in this graph.",
        }.get(node.get("scope_status"), "")
        provenance = []
        for ref in node.get("evidence_refs", [])[:5]:
            values = [ref["artifact"], ref.get("entity_ref"), ref.get("fragment_id")]
            provenance.append(
                "<li>" + " · ".join(esc(value) for value in values if value) + "</li>"
            )
        return (
            f'<details class="record"><summary>{esc(node_label(node))} <span>{esc(node["kind"].replace("_", " "))}</span></summary>'
            f'<div id="{anchors[node_id]}">{focus_targets(focuses, anchors[node_id])}'
            f"<p>{esc(dates)}</p><p>{esc(replay or scope)}</p>{block}"
            f"<p>{safe_link(node.get('source_url'), 'Open source')} {jump}</p><ul>{rows}</ul>"
            f"<details><summary>Saved source references</summary><ul>{''.join(provenance)}</ul></details></div></details>"
        )

    return (
        "".join(row(node_id, node) for node_id, node in nodes.items()),
        index_link,
        anchors,
    )


def trace_context(root, edges, clusters):
    memberships = [group for group in clusters if root in group["member_ids"]]
    related = {root}
    for group in memberships:
        related.update(group["member_ids"])
    for edge in edges:
        if root in (edge["source"], edge["target"]) and edge["kind"] != "authored_by":
            related.update((edge["source"], edge["target"]))
    return memberships, related


def local_date(node):
    key = "archive_timestamp" if node["kind"] == "archive_pointer" else "published_at"
    value = node.get(key)
    if node["kind"] not in {"post", "archive_pointer"} or not value:
        return None
    moment = datetime.fromisoformat(value)
    return moment.astimezone(NY) if moment.tzinfo is not None else None


def source_reader(root, nodes, edges, index_link):
    selected = nodes[root]
    by_post = defaultdict(list)
    for edge in edges:
        if edge["kind"] == "capture_of_same_post":
            by_post[edge["target"]].append(nodes[edge["source"]])
    captures = by_post[root]
    methods = {
        "pinned_collector_dump": "Saved X collector dump",
        "pinned_collector_dump_supplement": "Supplement from the saved X collector dump",
        "official_x_oembed_public_fallback": "X oEmbed capture",
        "vetric_via_maltego_datamcp": "Vetric via Maltego Data MCP",
    }
    moment = local_date(selected)
    date = moment.strftime("%b %d, %Y · %H:%M %Z") if moment else "Publication time unavailable"
    relationship_list = []
    originals = {}
    for edge in edges:
        if root not in (edge["source"], edge["target"]) or edge["kind"] in {
            "capture_of_same_post",
            "authored_by",
        }:
            continue
        other = edge["target"] if root == edge["source"] else edge["source"]
        if (
            root == edge["source"]
            and edge["kind"] in {"declared_parent", "provider_returned_parent"}
            and nodes[other]["kind"] == "post"
        ):
            originals[other] = nodes[other]
        relationship_list.append(f"<li>{index_link(other)}<span>{esc(edge['label'])}</span></li>")

    def capture_markup(capture):
        body = capture.get("text_preview") or ""
        truncated = capture.get("text_length", len(body)) > len(body)
        quote = f"<blockquote>{esc(body)}</blockquote>" if body else "<p>Wording unavailable.</p>"
        return (
            f'<p class="reader-meta">{esc(methods.get(capture.get("capture_method"), "Saved capture"))}'
            f"{' · excerpt' if truncated else ''}</p>{quote}"
            f'<p class="reader-links">{index_link(capture["id"], "Saved capture details")}</p>'
        )

    first = capture_markup(captures[0]) if captures else "<p>No saved wording.</p>"
    others = "".join(
        f"<details><summary>{esc(capture['label'])}</summary>{capture_markup(capture)}</details>"
        for capture in captures[1:]
    )
    linked_sources = "".join(
        f"<details><summary>Read linked source · {esc(node_label(original))}</summary>"
        + ("".join(map(capture_markup, by_post[post_id])) or "<p>No saved wording.</p>")
        + f'<p class="reader-links">{index_link(post_id, "Linked evidence record")}</p></details>'
        for post_id, original in originals.items()
    )
    direct = "".join(relationship_list)
    return (
        '<aside class="reader"><span class="eyebrow">Source inspector</span>'
        f'<h3>{esc(node_label(selected))}</h3><p class="reader-meta">Published · {esc(date)}</p>'
        f'<p class="reader-id">X · {esc(selected["native_id"])}</p>{first}{others}'
        f'<p class="reader-links">{safe_link(selected.get("source_url"), "Open original on X")} '
        f"{index_link(root, 'Full evidence record')}</p>"
        + linked_sources
        + (
            f'<details class="relationships"><summary>Exact relationship records ({len(relationship_list)})</summary>'
            f"<ul>{direct}</ul></details>"
            if direct
            else ""
        )
        + "</aside>"
    )


def focus_controls(focuses, nodes):
    choices = []
    for key, root in focuses.items():
        choices.append(
            f'<a data-choice="{key}" href="#focus-{key}">{esc(node_label(nodes[root]))}</a>'
        )
    return "".join(choices)


def selection_css(focuses):
    first = next(iter(focuses), "")
    if not first:
        return ""
    active = {key: [f'body:has([data-target="{key}"]:target)'] for key in focuses}
    active[first].append("body:not(:has(.focus-anchor:target))")
    return "\n".join(
        f'{prefix} .context-panel[data-focus="{key}"]'
        f"{{position:relative;visibility:visible;pointer-events:auto;z-index:1}}"
        f'{prefix} [data-choice="{key}"]'
        f"{{background:#276b63;color:white;border-color:#276b63;text-decoration:none}}"
        f'{prefix} .workspace-nav[data-focus="{key}"]{{display:contents}}'
        for key, prefixes in active.items()
        for prefix in prefixes
    )


def focus_targets(focuses, section):
    return "".join(
        f'<span class="focus-anchor" data-target="{key}" id="{section}-{key}"></span>'
        for key in focuses
    )


def workspace_navigation(focuses):
    return "".join(
        f'<span class="workspace-nav" data-focus="{key}">'
        f'<a href="#focus-{key}">Workspace</a><a href="#timeline-{key}">Timeline</a>'
        f'<a href="#clusters-{key}">Clusters</a><a href="#evidence-{key}">Evidence index</a></span>'
        for key in focuses
    )


def trace_views(focuses, nodes, edges, clusters, dashboard, index_link, anchors, *, same_page):
    panels, timelines = [], []
    for key, root in focuses.items():
        memberships, related = trace_context(root, edges, clusters)
        focused_link = partial(index_link, focus=key)
        focused = chart(
            focus_plot(
                nodes,
                edges,
                {item: f"{anchor}-{key}" for item, anchor in anchors.items()},
                (nodes[root]["native_id"],),
                same_page=same_page,
            ),
            f"trace-focus-{key}",
        )

        def group_title(group, selected_root):
            if group["kind"] == "parent_relationship_comparison" and len(group["member_ids"]) == 2:
                labels = [
                    node_label(nodes[item]) for item in group["member_ids"] if item != selected_root
                ]
                if labels:
                    return f"{labels[0]} · saved relationship"
            return group["label"]

        group_links = (
            "".join(
                f'<details class="focus-group"><summary>{esc(group_title(group, root))}'
                f" <span>· {len(group['member_ids'])}</span></summary>"
                f'<p>{esc(group["basis"])}</p><p class="muted">{esc(" ".join(group["limits"]))}</p>'
                f'<p class="group-members">{" ".join(focused_link(member) for member in group["member_ids"])}</p></details>'
                for group in memberships
            )
            or "<p>No saved group for this post.</p>"
        )
        panels.append(
            f'<article class="context-panel focus-grid" data-focus="{key}">'
            f'<div class="focus-graph"><span class="eyebrow">{icon("graph")} Relationship tree</span>'
            f"<h3>{esc(node_label(nodes[root]))}</h3>"
            f'<div class="plot" role="region" aria-label="Selected post relationships">{focused}</div>'
            '<p class="muted">Arrows follow the saved link. A plain line shows conversation membership.</p>'
            f'<div class="focus-groups"><h4>Related clusters</h4>{group_links}</div></div>'
            f"{source_reader(root, nodes, edges, focused_link)}</article>"
        )
        date_rows = sorted(
            (moment, item) for item in related if (moment := local_date(nodes[item])) is not None
        )
        dates = "".join(
            f'<li>{focused_link(item)}<time datetime="{moment.isoformat()}">'
            f"{moment.strftime('%b %d · %H:%M %Z')} · "
            f"{'archive capture' if nodes[item]['kind'] == 'archive_pointer' else 'published'}"
            "</time></li>"
            for moment, item in date_rows
        )
        timeline = chart(timeline_plot(dashboard, nodes, root, related), f"trace-timeline-{key}")
        timelines.append(
            f'<div class="context-panel" data-focus="{key}"><p class="trace-key">'
            f"{icon('target')} Highlighted on the timeline · {esc(node_label(nodes[root]))} and linked records</p>"
            f'<div class="plot" role="region" aria-label="Publication and archive timeline">{timeline}</div>'
            f"<details><summary>Exact dated records ({len(date_rows)})</summary>"
            f'<ul class="dated-records">{dates}</ul></details></div>'
        )
    return "".join(panels), "".join(timelines)


STYLE = """
*{box-sizing:border-box}html{scroll-padding-top:84px}body{margin:0;background:#f4f3ee;color:#142229;font:16px/1.55 Manrope,sans-serif;overflow-wrap:anywhere}a{color:#276b63;text-underline-offset:3px}a:focus-visible,summary:focus-visible{outline:3px solid #142229;outline-offset:4px}header{position:sticky;top:0;z-index:3;background:#fff;border-bottom:1px solid #d9e0de}.bar{max-width:1180px;margin:auto;padding:12px 24px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}.bar img{width:106px;height:32px;object-fit:contain}.bar nav{display:flex;gap:16px;flex-wrap:wrap;font-size:14px}main{max-width:1180px;margin:auto;padding:30px 24px 62px}h1,h2,h3,p{margin:0}h1{font-size:clamp(29px,4.3vw,48px);line-height:1.1;letter-spacing:-1.4px}h2{font-size:25px;line-height:1.2}h3{font-size:16px}.eyebrow{font-size:12px;text-transform:uppercase;letter-spacing:1.3px;color:#52636c;font-weight:700}.lead{margin:12px 0 20px;max-width:75ch}.jump{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}.jump a{border:1px solid #b7c7c3;border-radius:5px;background:#fff;padding:7px 12px;font-size:14px}.section{margin-top:26px;padding:21px;border:1px solid #d9e0de;border-radius:8px;background:white;min-width:0}.section>p{max-width:88ch;margin:8px 0;font-size:14px}.plot{min-width:0}.muted,summary span{color:#52636c}.counts{display:flex;gap:13px;flex-wrap:wrap;font-size:13px;color:#52636c}.notice{margin-top:17px;padding:11px 14px;border-left:4px solid #f36643;background:#f8e8da}.record{border-top:1px solid #d9e0de;padding:9px 0;font-size:14px}.record summary{font-weight:700;color:#276b63;cursor:pointer}.record summary span{font-weight:400;margin-left:6px}.record p{margin:4px 0}.record blockquote{margin:8px 0;padding-left:12px;border-left:3px solid #276b63;white-space:pre-wrap}.record ul{margin:6px 0;padding-left:20px}.record li{margin:4px 0}.groups{display:grid;grid-template-columns:1fr 1fr;gap:8px 14px;padding:0;list-style:none}.groups li{padding:8px 0;border-top:1px solid #d9e0de}.groups a{font-size:13px;margin-right:8px}footer{font-size:13px;color:#52636c;margin-top:20px}@media(max-width:660px){.bar{gap:6px;padding:9px 14px}.bar nav{gap:12px;font-size:12px}main{padding:22px 13px 45px}.section{padding:13px}.groups{grid-template-columns:1fr}.lead{font-size:15px}}
"""

EMBEDDED_STYLE = """
.trace-workspace{margin-top:38px}
.trace-workspace .section{margin-top:14px;padding:20px;border:1px solid var(--traced-border-subtle);border-radius:8px;background:var(--traced-surface);min-width:0}
.trace-workspace .section>p{margin:7px 0;font-size:14px;max-width:84ch}
.trace-workspace .plot{min-width:0}
.trace-workspace .jump{display:flex;gap:6px 14px;flex-wrap:wrap;margin:12px 0;font-size:13px}
.trace-workspace .record{border-top:1px solid var(--traced-border-subtle);margin-top:0;padding:9px 0;font-size:14px}
.trace-workspace .record summary{margin-bottom:0}
.trace-workspace .record summary span{color:var(--traced-foreground-secondary);font-weight:400;margin-left:6px}
.trace-workspace .record p{margin:4px 0}
.trace-workspace .record blockquote{margin:8px 0;padding-left:12px;border-left:3px solid var(--traced-color-moss);white-space:pre-wrap}
.trace-workspace .record ul{margin:6px 0;padding-left:20px}
.trace-workspace .groups{display:grid;grid-template-columns:1fr;gap:8px 14px;padding:0;list-style:none}
.trace-workspace .groups li{padding:8px 0;border-top:1px solid var(--traced-border-subtle)}
.trace-workspace .groups a{font-size:13px;margin-right:8px}
.trace-footnote{margin:10px 0 0;color:var(--traced-foreground-secondary);font-size:13px}
@media(max-width:660px){.trace-workspace .section{padding:13px}}
"""

WORKSPACE_STYLE = """
.workspace-nav{display:none}
h4{margin:0 0 6px;font-size:14px}.workspace{padding:22px}.workspace-head{display:flex;align-items:start;justify-content:space-between;gap:16px}.workspace-head p{margin:6px 0;max-width:58ch;font-size:14px;color:#52636c}.focus-anchor{display:block;height:0;scroll-margin-top:10px}.focus-options{display:flex;flex-wrap:wrap;gap:8px;margin:15px 0 18px}.focus-options a{background:#fff;color:#276b63;border:1px solid #82958f;border-radius:5px;padding:7px 11px;font-size:13px;font-weight:700}.context-stack{position:relative;min-width:0}.context-panel{position:absolute;inset:0 0 auto;visibility:hidden;pointer-events:none;min-width:0}.focus-grid{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr);gap:16px;align-items:start}.focus-graph,.reader{padding:15px;border:1px solid #d9e0de;border-radius:7px;min-width:0}.focus-graph>h3,.reader>h3{font-size:20px;line-height:1.2;margin:5px 0 7px}.focus-graph>p{font-size:12px}.reader{background:#f4f3ee}.reader-meta,.reader-id,.reader-links{font-size:12px;color:#52636c}.reader-id{margin:3px 0 11px;font-variant-numeric:tabular-nums}.reader blockquote{border-left:3px solid #276b63;margin:6px 0 9px;padding-left:11px;white-space:pre-wrap;font-size:14px}.reader-links{display:flex;flex-wrap:wrap;gap:4px 11px;margin:6px 0}.reader details,.focus-groups{border-top:1px solid #d9e0de;margin-top:12px;padding-top:9px}.focus-group{padding:5px 0}.focus-group p{font-size:13px;margin:5px 0}.group-members{display:flex;flex-wrap:wrap;gap:5px 10px}.reader summary,.focus-group summary,.dated-records time,.relationships li span{font-size:12px;color:#52636c}.reader ul,.dated-records{margin:8px 0 0;padding-left:18px;font-size:13px}.dated-records time,.relationships li span{display:block}.reader li,.dated-records li{margin-top:5px}.overview-grid{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);gap:14px;align-items:start}.overview-grid .section{margin-top:14px}.trace-key{font-size:12px;color:#52636c;margin:8px 0 2px}.trace-key span{display:inline-block;background:#f36643;width:8px;height:8px;border-radius:50%;margin-right:4px}.group-section .groups{grid-template-columns:1fr}.workspace-library .all-branches{margin:12px 0 8px}.record:has(:target){outline:2px solid #276b63;outline-offset:3px}.status-row{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}.status-row span{background:#f4f3ee;color:#52636c;padding:4px 8px;border-radius:4px;font-size:12px}
@media(max-width:830px){.overview-grid{grid-template-columns:1fr;gap:0}}
@media(max-width:660px){html{scroll-padding-top:122px}.workspace{padding:12px}.workspace-head{display:block}.focus-grid{grid-template-columns:1fr;gap:10px}.focus-graph,.reader{padding:10px}.focus-options{gap:5px}.focus-options a{font-size:12px;padding:6px 8px}}
"""


def render_workspace(graph_file, dashboard_file, archive, *, same_page=False):
    graph = json.loads(graph_file.read_bytes())
    dashboard = json.loads(dashboard_file.read_bytes())
    nodes = {node["id"]: node for node in graph["nodes"]}
    edges, clusters = graph["edges"], graph["clusters"]
    if (
        graph["schema"] != "traced.evidence-trace-graph/1"
        or len(nodes) != len(graph["nodes"])
        or len(nodes) > 250
        or len(edges) > 500
    ):
        raise ValueError("Trace exceeds the bounded explorer limits or repeats a node.")
    if any(edge["source"] not in nodes or edge["target"] not in nodes for edge in edges):
        raise ValueError("A trace edge has an unresolved endpoint.")
    binding = graph["binding"]
    if binding["package_sha256"] != digest(archive.read_bytes()):
        raise ValueError("The trace graph is for a different case package.")
    if any(
        dashboard["binding"][key] != binding[key]
        for key in ("case_id", "package_sha256", "snapshot_sha256")
    ):
        raise ValueError("The trace graph and timeline are for different cases.")
    timeline_pin = next(
        item for item in graph["inputs"] if Path(item["path"]).name == "dashboard-data.json"
    )
    if timeline_pin["sha256"] != digest(dashboard_file.read_bytes()):
        raise ValueError("The trace graph references a different timeline snapshot.")
    focuses = {
        key: root for key, native_id in FOCUSES.items() if (root := f"post:x:{native_id}") in nodes
    }
    if not focuses:
        raise ValueError("No saved trace presets resolve to evidence.")
    preview_prefix = "" if same_page else "preview.html"
    index, index_link, anchors = record_index(
        nodes, edges, archive, focuses, preview_prefix=preview_prefix
    )
    index_shortcuts = "".join(
        index_link(node_id)
        for node_id, node in nodes.items()
        if node["kind"] == "post" and node.get("native_id") in POST_LABELS
    )
    groups = "".join(
        f"<li><details><summary>{esc(group['label'])}</summary><p>{esc(group['basis'])}</p>"
        f'<p class="muted">{esc(" ".join(group["limits"]))}</p>'
        f"<p>{' '.join(index_link(node_id) for node_id in group['member_ids'])}</p></details></li>"
        for group in clusters
    )
    tree = chart(tree_plot(nodes, edges), "trace-tree-plot")
    cluster = chart(cluster_plot(nodes, clusters), "trace-cluster-plot")
    controls = focus_controls(focuses, nodes)
    navigation = workspace_navigation(focuses)
    targets = focus_targets(focuses, "focus")
    focus_panels, timelines = trace_views(
        focuses,
        nodes,
        edges,
        clusters,
        dashboard,
        index_link,
        anchors,
        same_page=same_page,
    )
    active_css = selection_css(focuses)
    topic_cards = render_topics(
        nodes,
        edges,
        index_link,
        local_date,
        datetime.fromisoformat(dashboard["scope"]["event_at"]),
    )
    topic_section = (
        f'<section class="section" id="clusters" aria-labelledby="topic-title">'
        f'{focus_targets(focuses, "clusters")}<h2 id="topic-title">{icon("stack")} Topic clusters</h2>'
        "<p>Open a topic to see the saved posts and what each post does. Indented posts have a recorded link to the post above.</p>"
        f'{topic_cards}<p class="muted">These are selected examples. The role labels describe the saved wording. Motive and coordination remain unverified.</p>'
        f'<p class="topic-jump"><a href="#evidence-groups">Explore the evidence group map {icon("arrow")}</a></p></section>'
        if topic_cards
        else ""
    )
    evidence_group_id = "evidence-groups" if topic_cards else "clusters"
    group_targets = "" if topic_cards else focus_targets(focuses, "clusters")
    unknown = sum(
        node["kind"] == "post" and not node.get("published_at") for node in nodes.values()
    )
    index_content = f'<div class="jump">{index_shortcuts}</div>{index}'
    if same_page:
        index_content = (
            f'<details class="index-records"><summary>Browse {len(nodes)} trace records</summary>'
            f"{index_content}</details>"
        )
    workspace = f"""<div class="trace-workspace">
<section class="section workspace" id="tree" aria-labelledby="tree-title">{targets}
<div class="workspace-head"><div><h2 id="tree-title">{icon("graph")} Evidence workspace</h2>
<p>Pick a trace to update the tree, related clusters, timeline highlights, and source inspector together.</p></div>
<a href="{preview_prefix}#case-dashboard">Case dashboard</a></div>
<nav class="focus-options" aria-label="Choose a saved trace">{controls}</nav>
<div class="context-stack">{focus_panels}</div>
<div class="status-row">{signal_chip("Bot activity · unmeasured", "question")}{signal_chip("Public sentiment · unresolved", "split")}
<a href="{preview_prefix}#review-scenarios">Review the investigation scenarios {icon("arrow")}</a></div>
</section>
{topic_section}
<div class="overview-grid">
<section class="section" id="timeline" aria-labelledby="timeline-title">{focus_targets(focuses, "timeline")}<h2 id="timeline-title">Election timeline</h2>
<p>Choose a window or drag to zoom. Bars count saved posts per New York publication day. Dots show publication times and diamonds show archive capture times.
<a href="{preview_prefix}#sentiment-shift">Compare the sentiment sample {icon("arrow")}</a></p>
<div class="context-stack">{timelines}</div>
<p class="muted">Shading marks known coverage gaps. Posts without a precise publication time stay in the index ({unknown}).</p></section>
<section class="section group-section" id="{evidence_group_id}" aria-labelledby="cluster-title">{group_targets}<h2 id="cluster-title">Evidence group map</h2>
<p>Drill into a tile to see its members. A record can appear in more than one group.</p>
<div class="plot" role="region" aria-label="Interactive evidence cluster map">{cluster}</div>
<details><summary>Evidence groups and their exact members</summary><ul class="groups">{groups}</ul></details></section>
</div>
<section class="section workspace-library" id="evidence" aria-labelledby="evidence-title">{focus_targets(focuses, "evidence")}<h2 id="evidence-title">Evidence index</h2>
<p>Open any record for its source, saved wording where available, and the basis for its links.</p>
<details class="all-branches"><summary>Browse all evidence branches</summary><p>Select a branch or block to drill down.</p>
<div class="plot" role="region" aria-label="Interactive case tree">{tree}</div></details>
{index_content}</section>
<p class="trace-footnote">Bound to the saved collector case and separately captured Maltego results. Evidence groups show recorded relationships.</p>
</div>"""
    counts = f'<div class="counts"><span>{len(nodes)} trace items</span><span>{len(edges)} typed links</span><span>{len(clusters)} evidence groups</span></div>'
    styles = (
        (EMBEDDED_STYLE if same_page else STYLE + ICON_CSS)
        + WORKSPACE_STYLE
        + TOPIC_CSS
        + active_css
    )
    return workspace, styles, navigation, counts


def render(graph_file, dashboard_file, archive, out):
    workspace, styles, navigation, counts = render_workspace(graph_file, dashboard_file, archive)
    logo = base64.b64encode((BRAND / "logos/traced-primary.svg").read_bytes()).decode()
    font = base64.b64encode((BRAND / "fonts/Manrope-Variable.ttf").read_bytes()).decode()
    content = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; base-uri 'none'; form-action 'none'">
<title>Mamdani evidence workspace · Traced</title><style>@font-face{{font-family:Manrope;src:url(data:font/ttf;base64,{font})}}{styles}</style>
<script>{get_plotlyjs()}</script></head>
<body><header><div class="bar"><a href="preview.html#case-dashboard" aria-label="Back to the case dashboard"><img src="data:image/svg+xml;base64,{logo}" alt="Traced"></a>
<nav aria-label="Case workspace">{navigation}<a href="preview.html#case-dashboard">Case overview</a></nav></div></header>
<main><span class="eyebrow">Mamdani · November 4, 2025 NYC mayoral election</span><h1>Investigate the evidence</h1>
<p class="lead">Choose a saved trace. Follow its links, read the source wording, and see where the linked records fall around Election Day.</p>
{counts}{workspace}</main></body></html>"""
    temporary = out.with_suffix(".html.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(out)
    print(f"Trace explorer written to {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_dir", type=Path)
    parser.add_argument("--graph", type=Path)
    args = parser.parse_args()
    case_dir = args.case_dir
    render(
        args.graph or case_dir / "trace-graph.json",
        case_dir / "dashboard-data.json",
        case_dir / "mamdani-election-demo.zip",
        case_dir / "trace-explorer.html",
    )
