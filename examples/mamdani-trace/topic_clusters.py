"""Show case topics and exact saved post relationships in the trace workspace."""

from collections import defaultdict
from html import escape
from urllib.parse import urlsplit

from investigation_icons import icon
from topic_clusters_data import TOPICS

TOPIC_CSS = """
.topic-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;align-items:start;margin-top:12px}
.topic-card{margin:0;border:1px solid var(--traced-border-subtle,#d9e0de);border-radius:6px;background:var(--traced-surface,#fff);min-width:0}
.topic-card>summary{padding:12px;color:var(--traced-foreground,#142229);font-size:14px;cursor:pointer}
.topic-card[open]>summary{margin:0;border-bottom:1px solid var(--traced-border-subtle,#d9e0de);background:var(--traced-surface-subtle,#f4f3ee)}
.topic-card[open]{grid-column:1/-1}
.topic-card>summary .case-icon{margin:0 4px 0 3px;color:var(--traced-color-moss,#276b63)}
.topic-card>summary .topic-count{display:inline-block;margin-left:8px;color:var(--traced-foreground-secondary,#52636c);font-size:12px;font-weight:600}
.topic-body{padding:10px 12px 12px}
.topic-body>p{font-size:13px}
.topic-window-counts{display:flex;flex-wrap:wrap;gap:4px 12px;margin:5px 0 10px;color:var(--traced-foreground-secondary,#52636c);font-size:11px}
.topic-posts{padding:0;margin:0;list-style:none}
.topic-posts li{margin:0}
.topic-post{padding:8px 0;border-top:1px solid var(--traced-border-subtle,#d9e0de);font-size:13px}
.topic-post-head{display:flex;gap:3px 10px;flex-wrap:wrap;align-items:baseline}
.topic-post-head>time,.topic-post-head>.muted{font-size:11px}
.topic-role{font-weight:750;color:var(--traced-foreground,#142229)}
.topic-role span{margin-right:4px;color:var(--traced-foreground-secondary,#52636c);font-size:11px;font-weight:600}
.topic-post blockquote{margin:4px 0 2px;padding-left:8px;border-left:2px solid var(--traced-color-moss,#276b63);overflow-wrap:anywhere}
.topic-post>a{font-size:12px}
.topic-children{margin:0 0 3px 7px;padding-left:13px;border-left:2px solid var(--traced-border-subtle,#d9e0de)}
.topic-relationship{display:inline-flex;align-items:center;gap:5px;color:var(--traced-color-moss,#276b63);font-size:11px;font-weight:750}
.topic-relationship .case-icon{width:13px;height:13px}
.topic-jump{font-size:13px}
@media(max-width:700px){.topic-grid{grid-template-columns:1fr}.topic-children{margin-left:5px;padding-left:10px}}
"""


def _flag(edge, name):
    flags = edge.get("collector_flags", {})
    return flags.get(name) is True or flags.get(f"collector_{name}") is True


def _post_heading(post):
    parts = urlsplit(post.get("source_url") or "").path.strip("/").split("/")
    return f"@{parts[0]}" if len(parts) == 3 and parts[1] == "status" else "Saved X post"


def _topic_rows(notes, nodes):
    rows = {}
    for native_id, role, span in notes:
        post = nodes.get(f"post:x:{native_id}")
        if not post:
            continue
        for capture_id in post.get("capture_node_ids", ()):
            capture = nodes[capture_id]
            cue = span.extract(capture.get("text_preview") or "")
            if cue is not None:
                rows[post["id"]] = (post, role, cue, capture_id)
                break
    return rows


def _topic_threads(rows, edges, index_link, local_date):
    parents = {}
    children = defaultdict(list)
    conversation_only = set()
    for edge in edges:
        source, target = edge["source"], edge["target"]
        if source not in rows:
            continue
        if (
            edge["kind"] == "declared_conversation_member"
            and _flag(edge, "is_reply")
            and not edge.get("direct_parent_native_id")
        ):
            conversation_only.add(source)
        if edge["kind"] == "declared_parent" and target in rows:
            if source in parents or source == target:
                raise ValueError("A topic post has conflicting recorded parents.")
            parents[source] = (
                "Quotes the post above"
                if _flag(edge, "is_quote")
                else "Replies to the post above"
                if _flag(edge, "is_reply")
                else "Recorded parent above"
            )
            children[target].append(source)

    rendered = set()
    published = {post_id: local_date(row[0]) for post_id, row in rows.items()}

    def publication_order(post_id):
        date = published[post_id]
        return (date is None, date)

    def render_row(post_id):
        rendered.add(post_id)
        post, role, cue, capture_id = rows[post_id]
        date = published[post_id]
        time = (
            f'<time datetime="{date.isoformat()}">{date:%b %d, %Y} · {date:%H:%M %Z}</time>'
            if date
            else '<span class="muted">Publication time unknown</span>'
        )
        relationship = parents.get(post_id)
        if not relationship and post_id in conversation_only:
            relationship = "Same conversation · direct parent unknown"
        relation_line = (
            f'<p class="topic-relationship">{icon("link")}{escape(relationship)}</p>'
            if relationship
            else ""
        )
        nested = "".join(
            render_row(child) for child in sorted(children[post_id], key=publication_order)
        )
        branch = f'<ul class="topic-posts topic-children">{nested}</ul>' if nested else ""
        return (
            '<li><div class="topic-post"><div class="topic-post-head">'
            f"<strong>{index_link(post_id, _post_heading(post))}</strong>{time}</div>"
            f'{relation_line}<p class="topic-role"><span>Post role ·</span> {escape(role)}</p>'
            f"<blockquote>{escape(cue)}</blockquote>"
            f"{index_link(capture_id, 'Open saved wording')}</div>{branch}</li>"
        )

    result = "".join(
        render_row(post_id)
        for post_id in sorted(rows, key=publication_order)
        if post_id not in parents
    )
    if len(rendered) != len(rows):
        raise ValueError("Topic post relationships contain a cycle.")
    return result


def render_topics(nodes, edges, index_link, local_date, election):
    cards = []
    for slug, title, symbol, summary, notes in TOPICS:
        rows = _topic_rows(notes, nodes)
        if not rows:
            continue
        times = [local_date(post) for post, _, _, _ in rows.values()]
        before = sum(moment is not None and moment < election for moment in times)
        after = sum(moment is not None and moment >= election for moment in times)
        unknown = len(times) - before - after
        unknown_label = f"<span>Publication time unknown · {unknown}</span>" if unknown else ""
        omitted = len(notes) - len(rows)
        omitted_note = (
            f'<p class="muted">{omitted} planned excerpts unavailable in this saved graph.</p>'
            if omitted
            else ""
        )
        threads = _topic_threads(rows, edges, index_link, local_date)
        cards.append(
            f'<details class="topic-card" id="topic-{escape(slug)}">'
            f'<summary>{icon(symbol)}{escape(title)} <span class="topic-count">{len(rows)} posts</span></summary>'
            f'<div class="topic-body"><p>{escape(summary)}</p>'
            f'<div class="topic-window-counts"><span>Before Nov 4 · {before}</span>'
            f"<span>From Nov 4 · {after}</span>{unknown_label}</div>"
            f'<ul class="topic-posts">{threads}</ul>{omitted_note}</div></details>'
        )
    return f'<div class="topic-grid">{"".join(cards)}</div>' if cards else ""
