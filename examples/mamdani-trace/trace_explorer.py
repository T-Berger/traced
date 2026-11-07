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
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import plotly.graph_objects as go
from plotly.offline import get_plotlyjs
from plotly.subplots import make_subplots

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
}
POST_LABELS = {
    "1985692613010502114": "Ballot post",
    "1985336258193662449": "Related ballot post",
    "1980606111838355647": "Diwali parent post",
    "1980606660528804219": "Diwali quote post",
}
GROUP_LABELS = {
    "archive_locator_family": "Archive pointers",
    "native_draft_evidence_chain": "Draft evidence chains",
    "parent_relationship_comparison": "Diwali quote",
    "same_post_captures": "Same-post captures",
    "saved_search_results": "Web leads",
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

    def family(label, kinds, reverse=False):
        selected = [edge for kind in kinds for edge in by_kind[kind]]
        if not selected:
            return
        category = tree.add("root", label, f"{len(selected)} saved links")
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

    family("Posts and captures", ("capture_of_same_post", "excerpt_of_post"), reverse=True)
    family("Quote and parent links", ("declared_parent", "provider_returned_parent"))
    claims = tree.add("root", "Draft claims and evidence")
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
    family("Archive pointers", ("archive_points_to",), reverse=True)
    family("Web search leads", ("discovered_web_result",))
    return tree.figure("tree")


def cluster_plot(nodes, clusters):
    branches = Branches("Evidence groups")
    families = {}
    for group in clusters:
        kind = group["kind"]
        if kind not in families:
            families[kind] = branches.add("root", GROUP_LABELS.get(kind, kind.replace("_", " ")))
        branch = branches.add(
            families[kind], group["label"], f"{len(group['member_ids'])} evidence items"
        )
        for member in group["member_ids"]:
            branches.item(branch, nodes[member])
    return branches.figure("cluster")


def timeline_plot(data, nodes):
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

    events = {"Saved posts": [], "Archive pointers": []}
    for node in nodes.values():
        timestamp = node.get("published_at")
        lane = "Saved posts"
        if node["kind"] == "archive_pointer":
            timestamp = node.get("archive_timestamp")
            lane = "Archive pointers"
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
        height=465,
        hovermode="closest",
        dragmode="zoom",
        margin={"l": 54, "r": 18, "t": 62, "b": 50},
        legend={"orientation": "h", "y": -0.19, "font": {"size": 11}},
        updatemenus=[
            {
                "type": "buttons",
                "direction": "right",
                "x": 0,
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
    figure.update_yaxes(showgrid=False, showticklabels=False, row=2, col=1)
    figure.update_xaxes(range=["2025-09-04", "2026-02-04"], tickformat="%b %d", nticks=7)
    return figure


def record_index(nodes, edges, archive):
    links = defaultdict(list)
    for edge in edges:
        links[edge["source"]].append(edge)
        links[edge["target"]].append(edge)
    anchors = {node_id: f"evidence-{i}" for i, node_id in enumerate(nodes, 1)}
    with zipfile.ZipFile(archive) as package:
        native = json.loads(package.read("bundle.json"))
    native_records = {record["id"]: record for record in native["records"]}
    native_anchors = {r["id"]: f"record-{i}" for i, r in enumerate(native["records"], 1)}

    def index_link(node_id):
        return f'<a href="#{anchors[node_id]}">{esc(node_label(nodes[node_id]))}</a>'

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
            f'<a href="preview.html#{native_anchor}">Inspect saved record in dashboard ↗</a>'
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
            f'<div id="{anchors[node_id]}"><p>{esc(dates)}</p><p>{esc(replay or scope)}</p>{block}'
            f"<p>{safe_link(node.get('source_url'), 'Open source')} {jump}</p><ul>{rows}</ul>"
            f"<details><summary>Saved source references</summary><ul>{''.join(provenance)}</ul></details></div></details>"
        )

    return "".join(row(node_id, node) for node_id, node in nodes.items()), index_link


STYLE = """
*{box-sizing:border-box}html{scroll-padding-top:84px}body{margin:0;background:#f4f3ee;color:#142229;font:16px/1.55 Manrope,sans-serif;overflow-wrap:anywhere}a{color:#276b63;text-underline-offset:3px}a:focus-visible,summary:focus-visible{outline:3px solid #142229;outline-offset:4px}header{position:sticky;top:0;z-index:3;background:#fff;border-bottom:1px solid #d9e0de}.bar{max-width:1180px;margin:auto;padding:12px 24px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}.bar img{width:106px;height:32px;object-fit:contain}.bar nav{display:flex;gap:16px;flex-wrap:wrap;font-size:14px}main{max-width:1180px;margin:auto;padding:30px 24px 62px}h1,h2,h3,p{margin:0}h1{font-size:clamp(29px,4.3vw,48px);line-height:1.1;letter-spacing:-1.4px}h2{font-size:25px;line-height:1.2}h3{font-size:16px}.eyebrow{font-size:12px;text-transform:uppercase;letter-spacing:1.3px;color:#52636c;font-weight:700}.lead{margin:12px 0 20px;max-width:75ch}.jump{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}.jump a{border:1px solid #b7c7c3;border-radius:5px;background:#fff;padding:7px 12px;font-size:14px}.section{margin-top:26px;padding:21px;border:1px solid #d9e0de;border-radius:8px;background:white;min-width:0}.section>p{max-width:88ch;margin:8px 0;font-size:14px}.plot{min-width:0}.muted,summary span{color:#52636c}.counts{display:flex;gap:13px;flex-wrap:wrap;font-size:13px;color:#52636c}.notice{margin-top:17px;padding:11px 14px;border-left:4px solid #f36643;background:#f8e8da}.record{border-top:1px solid #d9e0de;padding:9px 0;font-size:14px}.record summary{font-weight:700;color:#276b63;cursor:pointer}.record summary span{font-weight:400;margin-left:6px}.record p{margin:4px 0}.record blockquote{margin:8px 0;padding-left:12px;border-left:3px solid #276b63;white-space:pre-wrap}.record ul{margin:6px 0;padding-left:20px}.record li{margin:4px 0}.groups{display:grid;grid-template-columns:1fr 1fr;gap:8px 14px;padding:0;list-style:none}.groups li{padding:8px 0;border-top:1px solid #d9e0de}.groups a{font-size:13px;margin-right:8px}footer{font-size:13px;color:#52636c;margin-top:20px}@media(max-width:660px){.bar{gap:6px;padding:9px 14px}.bar nav{gap:12px;font-size:12px}main{padding:22px 13px 45px}.section{padding:13px}.groups{grid-template-columns:1fr}.lead{font-size:15px}}
"""


def render(graph_file, dashboard_file, archive, out):
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
    index, index_link = record_index(nodes, edges, archive)
    focuses = "".join(
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
    logo = base64.b64encode((BRAND / "logos/traced-primary.svg").read_bytes()).decode()
    font = base64.b64encode((BRAND / "fonts/Manrope-Variable.ttf").read_bytes()).decode()
    tree = chart(tree_plot(nodes, edges), "trace-tree-plot")
    cluster = chart(cluster_plot(nodes, clusters), "trace-cluster-plot")
    timeline = chart(timeline_plot(dashboard, nodes), "trace-timeline-plot")
    unknown = sum(
        node["kind"] == "post" and not node.get("published_at") for node in nodes.values()
    )
    content = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; base-uri 'none'; form-action 'none'">
<title>Mamdani trace explorer · Traced</title><style>@font-face{{font-family:Manrope;src:url(data:font/ttf;base64,{font})}}{STYLE}</style>
<script>{get_plotlyjs()}</script></head>
<body><header><div class="bar"><a href="preview.html#case-dashboard" aria-label="Back to the case dashboard"><img src="data:image/svg+xml;base64,{logo}" alt="Traced"></a>
<nav aria-label="Trace views"><a href="#tree">Tree</a><a href="#clusters">Clusters</a><a href="#timeline">Timeline</a><a href="#evidence">Evidence index</a><a href="preview.html#maltego-data-mcp">Maltego results</a></nav></div></header>
<main><span class="eyebrow">Mamdani · November 4, 2025 NYC mayoral election</span><h1>Explore the evidence trace</h1>
<p class="lead">Follow the ballot claim from the X post through its saved captures, archive pointers and draft evidence. Compare it with the Diwali quote link and the other cited findings.</p>
<div class="counts"><span>{len(nodes)} trace items</span><span>{len(edges)} typed links</span><span>{len(clusters)} evidence groups</span></div>
<div class="jump"><a href="#tree">Open tree ↓</a><a href="#clusters">Explore clusters ↓</a><a href="#timeline">Zoom the timeline ↓</a></div>
<section class="section" id="tree" aria-labelledby="tree-title"><h2 id="tree-title">Evidence tree</h2>
<p>Choose a branch or select a block to drill down. Use the chart’s path bar to return. The tree separates captures, parent links, cited drafts, public accounts, archives and search leads.</p>
<div class="plot" role="region" aria-label="Interactive evidence tree">{tree}</div><p><a href="#evidence">Open the text relationship index ↓</a></p></section>
<section class="section" id="clusters" aria-labelledby="cluster-title"><h2 id="cluster-title">Evidence clusters</h2>
<p>Select a group to see its members. Tile area reflects entries in the group. A record may appear in multiple groups when it serves different evidence roles.</p>
<div class="plot" role="region" aria-label="Interactive evidence cluster map">{cluster}</div>
<details><summary>Evidence groups and their exact members</summary><ul class="groups">{groups}</ul></details></section>
<section class="section" id="timeline" aria-labelledby="timeline-title"><h2 id="timeline-title">Election timeline</h2>
<p>Choose a window or drag to zoom. The upper plot counts saved collector posts per New York publication day. The lower plot shows case posts and dated archive pointers on separate rows.</p>
<div class="plot" role="region" aria-label="Interactive publication and archive timeline">{timeline}</div>
<p class="muted">Shaded intervals mark early coverage and the late mentions gap. Archive timestamps describe capture time. Posts without a precise publication time stay in the evidence index ({unknown}).</p></section>
<section class="section" id="evidence" aria-labelledby="evidence-title"><h2 id="evidence-title">Evidence index</h2>
<p>Open an item for its source, saved wording where available, and the basis for each adjacent relationship.</p><div class="jump">{focuses}</div>{index}</section>
<footer>Bound to the saved collector case and separately captured Maltego results. Evidence groups describe saved relationships. Bot and sentiment analysis remain unmeasured.</footer>
</main></body></html>"""
    temporary = out.with_suffix(".html.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(out)
    print(
        f"Trace explorer: {len(nodes)} items, {len(edges)} links, {len(clusters)} evidence groups"
    )


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
