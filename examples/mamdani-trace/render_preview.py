#!/usr/bin/env python3
"""Render a self-contained, read-only Traced preview from a native case ZIP."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from dashboard_preview import CSS as DASHBOARD_CSS
from dashboard_preview import render_dashboard
from investigation_icons import icon as case_icon
from investigation_icons import signal_chip

BRAND = Path(__file__).resolve().parents[2] / "brand" / "traced"


def H(value):
    return html.escape(str(value if value is not None else "Not supplied"), quote=True)


def data_uri(path: Path, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def link(url, label):
    if not isinstance(url, str):
        return '<span class="muted">Original link unavailable</span>'
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        return '<span class="muted">Original link unavailable</span>'
    return f'<a href="{H(url)}" target="_blank" rel="noopener noreferrer">{H(label)} ↗</a>'


def badge(text, tone=""):
    return f'<span class="badge {H(tone)}">{H(text)}</span>'


def pairs(items):
    return "<dl>" + "".join(f"<dt>{H(k)}</dt><dd>{H(v)}</dd>" for k, v in items) + "</dl>"


def overview_flow(archive, raw, records, anchors):
    path = archive.with_name("overview-flow.json")
    if not path.is_file():
        return ""
    flow = json.loads(path.read_text())
    binding = flow["binding"]
    if binding["bundle_sha256"] != hashlib.sha256(raw).hexdigest():
        return ""
    with archive.open("rb") as package_file:
        if binding["package_sha256"] != hashlib.file_digest(package_file, "sha256").hexdigest():
            return ""
    companion = archive.with_name("pattern-review.json")
    if (
        not companion.is_file()
        or binding["pattern_review_sha256"] != hashlib.sha256(companion.read_bytes()).hexdigest()
    ):
        return ""
    steps = []
    for step in flow["steps"]:
        sources = []
        for item in step.get("evidence", []):
            record_id = item["record_id"]
            if records[record_id]["text"].count(item["quote"]) != 1:
                raise ValueError("A walkthrough quotation does not match its saved record.")
            sources.append(
                f'<li><a href="#{anchors[record_id]}">{H(item["label"])}</a>'
                f"<blockquote>{H(item['quote'])}</blockquote></li>"
            )
        links = []
        for item in step["links"]:
            href = item["href"]
            if href == "http://127.0.0.1:8053/":
                links.append(link(href, item["label"]))
            elif href.startswith("#") or (
                Path(href).name == href and path.with_name(href).is_file()
            ):
                links.append(f'<a href="{H(href)}">{H(item["label"])}</a>')
        evidence = f"<ul>{''.join(sources)}</ul>" if sources else ""
        steps.append(
            f'<article class="card"><span class="eyebrow">Step {H(step["number"])}</span>'
            f'<h3>{H(step["title"])}</h3><p class="small muted">{H(step["where"])}</p>'
            f"<p>{H(step['action'])}</p><details><summary>What to check</summary>"
            f"<p>{H(step['check'])}</p>{evidence}<p>{H(step['outcome'])}</p></details>"
            f'<p class="small">{" · ".join(links)}</p></article>'
        )
    return (
        '<section class="section" id="use-flow"><div class="section-heading"><div>'
        '<span class="eyebrow">Demo walkthrough</span>'
        f"<h2>{H(flow['title'])}</h2><p>{H(flow['lead'])}</p></div></div>"
        f'<div class="record-grid">{"".join(steps)}</div></section>'
    )


def pattern_review(archive, raw, records, anchors, labels):
    path = archive.with_name("pattern-review.json")
    if not path.is_file():
        return ""
    review = json.loads(path.read_text())
    if review["bundle_sha256"] != hashlib.sha256(raw).hexdigest():
        return ""
    cards = []
    for check in review["checks"]:
        passages = []
        for record_id, quote in check["evidence"]:
            record = records[record_id]
            if record["text"].count(quote) != 1:
                raise ValueError("A pattern review quote is missing or ambiguous.")
            passages.append(
                f'<div class="input-quote"><a href="#{anchors[record_id]}">'
                f"{labels[record_id]} · {H(record['source_label'])}</a>"
                f"<blockquote>{H(quote)}</blockquote></div>"
            )
        cards.append(
            f'<article class="comparison"><div><span class="eyebrow">Exact input</span>'
            f"<h3>{H(check['title'])}</h3>{''.join(passages)}</div>"
            f'<div><span class="eyebrow">Draft review</span>{badge(check["state"], "review")}'
            f'<p>{H(check["finding"])}</p><p class="muted">{H(check["next"])}</p></div></article>'
        )
    references = "".join(
        f"<li>{link(url, label)}<p>{H(note)}</p></li>" for label, url, note in review["policies"]
    )
    restriction_items = []
    for item in review.get("restriction_review", []):
        record_id = item["record_id"]
        record = records[record_id]
        restriction_items.append(
            f'<li><a href="#{anchors[record_id]}">{H(item["issue"])}</a>'
            f"<p>{badge(item['status'], 'review')} {H(record['source_label'])}</p>"
            f"<p>{H(item['basis'])}</p></li>"
        )
    restriction_panel = (
        '<details open id="restriction-readiness"><summary>Reach restriction readiness</summary>'
        f"<ul>{''.join(restriction_items)}</ul></details>"
        if restriction_items
        else ""
    )
    return (
        '<section class="section" id="patterns"><div class="section-heading"><div>'
        '<span class="eyebrow">Shared notepad</span><h2>Patterns and policy review</h2>'
        "<p>Draft checks on the saved case. Open each input to see its capture context.</p>"
        f"</div></div>{''.join(cards)}"
        "<details open><summary>Scoring readiness</summary>"
        f"{pairs(review['dimensions'])}</details>"
        f"{restriction_panel}"
        "<details><summary>Reporting duties and possible platform action</summary>"
        f"<p>{H(review['reporting'])}</p><p>{H(review['action'])}</p></details>"
        "<details><summary>Policy versions and official sources</summary>"
        f"<ul>{references}</ul></details></section>"
    )


CSS = """
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:100px}body{margin:0;background:var(--traced-background);color:var(--traced-foreground);font:16px/1.6 var(--traced-font-sans);overflow-wrap:anywhere}a{color:var(--traced-link);text-underline-offset:3px}a:focus-visible,summary:focus-visible{outline:3px solid var(--traced-focus-ring);outline-offset:4px}h1,h2,h3,p{margin:0}h1{font-size:clamp(32px,4vw,48px);line-height:1.15;letter-spacing:-1.5px;max-width:850px}h2{font-size:25px;line-height:1.3;letter-spacing:-.4px}h3{font-size:18px;line-height:1.4}.muted,dt{color:var(--traced-foreground-secondary)}.small{font-size:13px}.mono{font:12px/1.7 var(--traced-font-mono)}.eyebrow{font-size:12px;letter-spacing:1.4px;font-weight:700;text-transform:uppercase;color:var(--traced-foreground-secondary)}header{position:sticky;top:0;z-index:2;background:var(--traced-surface);border-bottom:1px solid var(--traced-border-subtle)}.bar{max-width:1280px;margin:auto;padding:14px 28px;display:flex;align-items:center;gap:28px}.logo{width:132px;height:40px;object-fit:contain}.nav{display:flex;gap:10px 18px;flex-wrap:wrap;font-size:13px;flex:1}.nav a{text-decoration:none}.primary{display:inline-flex;justify-content:center;align-items:center;background:var(--traced-action-primary-background);color:var(--traced-action-primary-foreground);border:1px solid transparent;border-radius:6px;padding:11px 17px;text-decoration:none;font-size:13px;font-weight:700;white-space:nowrap}.primary:hover{background:var(--traced-action-primary-hover-background)}main{max-width:1280px;margin:auto;padding:42px 28px 64px}.hero{display:grid;gap:18px}.lead{font-size:18px;max-width:850px}.chips{display:flex;gap:8px;flex-wrap:wrap}.badge{display:inline-block;vertical-align:middle;font-size:11px;line-height:1.5;font-weight:600;padding:4px 8px;border-radius:4px;background:var(--traced-surface-subtle);color:var(--traced-foreground-secondary)}.badge.review{background:var(--traced-status-review-background);color:var(--traced-status-review-foreground)}.badge.info{background:var(--traced-status-info-background);color:var(--traced-status-info-foreground)}.notice{display:grid;gap:6px;padding:18px 22px;border:1px solid var(--traced-status-review-border);border-left:4px solid var(--traced-status-review-foreground);background:var(--traced-status-review-background);border-radius:6px;font-size:14px;margin:28px 0}.notice p{max-width:1000px}.windows,.record-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.window{background:var(--traced-surface);border:1px solid var(--traced-border-subtle);border-radius:8px;padding:20px 22px;display:grid;gap:9px}.window h3{font-size:20px}.window .date{font-size:15px;font-weight:600}.window small{font-size:12px}.section{margin-top:42px}.section-heading{display:flex;align-items:end;justify-content:space-between;gap:20px;margin-bottom:20px}.section-heading p{max-width:720px;font-size:14px;color:var(--traced-foreground-secondary);margin-top:8px}.card{background:var(--traced-surface);border:1px solid var(--traced-border-subtle);border-radius:8px;padding:22px;min-width:0}.card.post{border-top:3px solid var(--traced-color-moss)}.card.context{border-top:3px solid var(--traced-color-slate)}.card-head{display:flex;justify-content:space-between;gap:12px;align-items:start;margin-bottom:15px}.card-head .identity{display:grid;gap:4px;min-width:0}.identity>.badge{justify-self:start}.card-head>.badge{flex:none}.handle{font-size:13px;color:var(--traced-foreground-secondary)}.record-text{font-size:18px;line-height:1.65;white-space:pre-wrap;border:0;padding:0;margin:20px 0}.record-text:before{content:'“';font-size:35px;color:var(--traced-color-moss);line-height:.3;display:block;margin-bottom:12px}.record-source{font-size:13px;border-top:1px solid var(--traced-border-subtle);padding-top:14px;margin-top:16px;display:grid;gap:5px}.links{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;margin-top:16px}details{margin-top:16px;font-size:13px}summary{color:var(--traced-link);cursor:pointer;font-weight:600}details[open] summary{margin-bottom:12px}dl{display:grid;grid-template-columns:108px minmax(0,1fr);gap:6px 12px;font-size:12px;margin:0}dd{margin:0}ul{margin:10px 0 0;padding-left:20px}li{margin:6px 0}.empty{padding:20px;border:1px dashed var(--traced-border-strong);border-radius:8px;color:var(--traced-foreground-secondary)}.comparison{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);border:1px solid var(--traced-border-subtle);border-radius:8px;overflow:hidden;background:var(--traced-surface);margin:18px 0}.comparison>div{padding:24px;min-width:0}.comparison .output{background:var(--traced-surface-subtle);border-left:1px solid var(--traced-border-subtle)}.comparison h3{margin:10px 0 16px}.input-quote{border-left:3px solid var(--traced-color-moss);padding-left:15px;margin:18px 0}.input-quote blockquote{margin:8px 0;font-size:15px;white-space:pre-wrap}.output p{font-size:17px;line-height:1.65;margin:10px 0}.step{font-size:13px;border-top:1px solid var(--traced-border-subtle);padding-top:13px;margin-top:18px}.step p{font-size:14px}.audit{margin-top:32px;padding-top:24px;border-top:1px solid var(--traced-border-subtle);font-size:12px;color:var(--traced-foreground-secondary)}footer{display:flex;justify-content:space-between;gap:20px;margin-top:36px;font-size:12px;color:var(--traced-foreground-secondary)}.post-groups>.section-heading:not(:first-child){margin-top:30px}.card:target{outline:3px solid var(--traced-color-moss);outline-offset:4px}@media(max-width:760px){.bar{padding:12px 18px;gap:12px;flex-wrap:wrap}.logo{width:110px;height:34px}.nav{display:none}.primary{margin-left:auto;font-size:11px;padding:9px 11px;white-space:normal;max-width:190px}main{padding:28px 18px 44px}.windows,.record-grid,.comparison{grid-template-columns:1fr}.comparison .output{border-left:0;border-top:1px solid var(--traced-border-subtle)}.section-heading{display:block}.card,.comparison>div{padding:20px}.record-text{font-size:17px}.lead{font-size:16px}h1{letter-spacing:-1px}.notice{padding:16px}.section{margin-top:32px}footer{flex-direction:column}.bar .primary{min-height:42px}dl{grid-template-columns:95px minmax(0,1fr)}}@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
"""


MISSION_CSS = """
.mission-hero{gap:11px;padding:25px 28px;border:1px solid #2d4a50;border-left:4px solid var(--traced-color-trace);border-radius:8px;background:linear-gradient(110deg,#142229,#203c42);color:#fff}
.mission-hero .eyebrow{display:flex;gap:8px;align-items:center;color:#cce5df}
.mission-hero .lead{color:#edf3f1}
.mission-status,.mission-nav{display:flex;align-items:center;flex-wrap:wrap;gap:7px 10px}
.mission-status{margin-top:4px}
.mission-nav a{display:inline-flex;align-items:center;gap:5px;color:#eff8f5;font-size:13px;font-weight:700;padding:5px 9px;border:1px solid #839c9b;border-radius:4px}
.mission-nav a:hover{background:#37565a}
.mission-nav a:focus-visible{outline-color:#fff}
.mission-nav a:first-child{background:#f6bda8;color:#142229;border-color:#f6bda8}
.mission-coverage{display:block;padding:11px 16px;margin:12px 0 22px}
.mission-coverage summary{color:var(--traced-foreground);font-size:13px}
.mission-coverage p{margin-top:5px}
.mission-coverage .case-icon{margin-right:4px}
@media(max-width:620px){.mission-hero{padding:18px 15px}.mission-nav a{min-height:36px}}
"""


def render(archive: Path, output: Path, *, trace_alias: bool = False):
    with zipfile.ZipFile(archive) as package:
        if package.getinfo("bundle.json").file_size > 16_000_000:
            raise ValueError("Bundle is too large for this bounded static preview.")
        raw = package.read("bundle.json")
    bundle = json.loads(raw)
    case, records = bundle["case"], bundle["records"]
    if (
        len(records) > 200
        or len(bundle["findings"]) > 500
        or any(len(r.get("text") or "") > 100_000 for r in records)
    ):
        raise ValueError("Case exceeds the bounded static preview limits.")
    by_record = {r["id"]: r for r in records}
    anchors = {r["id"]: f"record-{i}" for i, r in enumerate(records, 1)}
    labels = {r["id"]: f"R{i}" for i, r in enumerate(records, 1)}
    graph = archive.with_name("trace-graph.json")
    dashboard_data = archive.with_name("dashboard-data.json")
    unified = graph.is_file() and dashboard_data.is_file()
    workspace = trace_css = trace_script = ""
    if unified:
        from trace_explorer import get_plotlyjs, render_workspace

        workspace, trace_css, workspace_nav, _ = render_workspace(
            graph, dashboard_data, archive, same_page=True
        )
        trace_script = f"<script>{get_plotlyjs()}</script>"
    elif trace_alias:
        raise ValueError(
            "A verified trace graph and dashboard data are required for the trace alias."
        )
    dashboard = render_dashboard(archive, bundle, anchors, labels, unified=unified)
    dashboard_nav = (
        '<a href="#case-dashboard">Dashboard</a><a href="#case-timeline">Timeline</a>'
        if dashboard
        else ""
    )
    trace_nav = (
        workspace_nav
        if unified
        else (
            '<a href="trace-explorer.html">Interactive trace</a>'
            if dashboard and archive.with_name("trace-explorer.html").is_file()
            else ""
        )
    )
    flow = overview_flow(archive, raw, by_record, anchors)
    flow_nav = '<a href="#use-flow">Walkthrough</a>' if flow else ""
    patterns = pattern_review(archive, raw, by_record, anchors, labels)
    pattern_nav = '<a href="#patterns">Patterns</a>' if patterns else ""
    case_nav = (
        '<a href="#case-dashboard">Overview</a><a href="#review-scenarios">Scenarios</a>'
        f'{trace_nav}<a href="#saved-material">Sources</a>'
        if unified
        else f'{dashboard_nav}{trace_nav}{flow_nav}<a href="#scope">Scope</a>'
        f'<a href="#saved-material">Saved material</a><a href="#input-output">Draft review</a>{pattern_nav}'
    )
    fragments = {f["id"]: f for f in bundle["fragments"]}
    findings = {f["snapshot_id"]: f for f in bundle["findings"]}
    draft_runs = [r["id"] for r in bundle["runs"] if r["method"] == "traced_investigator"]
    claims = [
        f
        for f in bundle["findings"]
        if f.get("details", {}).get("role") == "claim"
        and (not draft_runs or f["run_id"] == draft_runs[-1])
    ]
    incoming = {}
    for edge in bundle["links"]:
        if edge["target"]["type"] == "finding" and edge["relation"] in {
            "supports",
            "depends_on",
            "contradicts",
        }:
            incoming.setdefault(edge["target"]["id"], []).append(edge)

    def citations(fid, seen=None):
        seen = set() if seen is None else seen
        if fid in seen:
            return []
        seen.add(fid)
        found = []
        for edge in incoming.get(fid, []):
            found.extend(edge.get("fragment_ids", []))
            source = edge["source"]
            if source["type"] == "fragment":
                found.append(source["id"])
            elif source["type"] == "finding":
                found.extend(citations(source["id"], seen))
        return list(dict.fromkeys(found))

    def post_data(record):
        meta = record.get("metadata", {})
        if meta.get("collector_post"):
            capture = meta["collector_post"]
            return {
                **capture,
                "author": meta.get("provenance", {}).get("author_label")
                or meta.get("channel_name"),
                "original_url": record.get("source_url"),
                "capture": "Saved X post from the collector’s pinned SQLite snapshot",
            }
        if meta.get("post"):
            return meta["post"]
        original = meta.get("original_url", "")
        if urlsplit(original).hostname in {"x.com", "twitter.com", "www.x.com", "www.twitter.com"}:
            return {
                "original_url": original,
                "capture": "Published reproduction. Original access: "
                + meta.get("original_access", "not independently verified"),
            }
        return {}

    def record_card(record):
        meta = record.get("metadata", {})
        post = post_data(record)
        from_dump = bool(meta.get("collector_post"))
        role = meta.get("sample_role", "context")
        post_type = (
            "X post · collector snapshot"
            if from_dump
            else ("X post · published reproduction" if post else "Context document")
        )
        author = post.get("author") or meta.get("author") or "Not supplied"
        saved_handle = post.get("handle") or post.get("author_handle")
        handle = "@" + saved_handle.lstrip("@") if saved_handle else "Account handle not supplied"
        title = (
            author
            if post and post.get("author")
            else record.get("source_label", "Saved context document")
        )
        capture = (
            post.get("capture")
            or meta.get("capture")
            or meta.get("source_origin")
            or "Saved public document excerpt"
        )
        published_at = record.get("published_at")
        date = meta.get("document_date") or post.get("date")
        timing = (
            f"Published {published_at}"
            if published_at
            else f"Source date {date or 'unknown'} · precise publication time unknown"
        )
        provenance = [
            ("Source type", post_type),
            ("Author", author),
            ("Date on source", date),
            ("Published at", published_at or "Unknown precise publication timestamp"),
            ("Captured at", record.get("acquired_at")),
            ("Capture", capture),
            ("Locator", meta.get("locator")),
            ("Record ID", record["id"]),
            ("Content SHA256", record.get("content_hash")),
        ]
        original = link(post.get("original_url"), "Original X post") if post else ""
        acquisition_url = record.get("source_url")
        counters = ""
        summary_metrics = ""
        if from_dump:
            acquisition_url = meta.get("provenance", {}).get("retrieved_url")
            selection = post.get("sampling_reason", [])
            if isinstance(selection, list):
                selection = ", ".join(role.replace("_", " ") for role in selection)
            provenance += [
                ("X post ID", meta.get("native_id")),
                ("Collector attribution", post.get("attribution")),
                ("Selection", selection or None),
            ]
            engagement = post.get("engagement") or post.get("snapshot_metrics", {})
            metrics = [
                (label, engagement.get(key))
                for key, label in (
                    ("view_count", "Views"),
                    ("like_count", "Likes"),
                    ("repost_count", "Reposts"),
                    ("reply_count", "Replies"),
                    ("quote_count", "Quotes"),
                    ("bookmark_count", "Bookmarks"),
                )
            ]
            summary_metrics = (
                '<div class="chips" aria-label="Selected engagement counts at capture">'
                + "".join(
                    badge(f"{value:,} {label.lower()}")
                    for label, value in metrics[:4]
                    if type(value) is int
                )
                + "</div>"
            )
            counters = (
                "<details><summary>Engagement at capture</summary>" + pairs(metrics) + "</details>"
            )
        return f'''<article class="card {"post" if post else "context"}" id="{anchors[record["id"]]}"><div class="card-head"><div class="identity">{badge(post_type, "info" if post else "")}<h3>{H(title)}</h3><span class="handle">{H(handle) if post else H(date or "Document date unknown")}</span></div>{badge(labels[record["id"]])}</div><div class="chips">{badge(role.title())}{badge("Saved post text" if from_dump else "Saved excerpt")}</div><blockquote class="record-text">{H(record.get("text") or "Saved text unavailable")}</blockquote>{summary_metrics}<p class="small muted">{H(meta.get("locator") or "Exact source locator not supplied")}</p><div class="record-source"><strong>{H(record.get("source_label"))}</strong><span>Author: {H(author)}</span><span>{H(capture)}</span><span class="muted">{H(timing)}</span><span class="mono">Captured at {H(record.get("acquired_at") or "unknown time")}</span></div><div class="links">{link(acquisition_url, "Acquisition source")}{original}</div>{counters}<details><summary>Source, capture and exact record</summary>{pairs(provenance)}</details></article>'''

    def claim_panel(claim, number):
        quoted = []
        for fid in citations(claim["snapshot_id"]):
            fragment = fragments[fid]
            record = by_record[fragment["record_id"]]
            selector, quote = fragment["selector"], fragment.get("quote")
            start, end = selector.get("start"), selector.get("end")
            if (
                selector["kind"] != "text"
                or type(start) is not int
                or type(end) is not int
                or record["text"][start:end] != quote
            ):
                raise ValueError("A cited quotation does not match its saved record.")
            quoted.append(
                f'<div class="input-quote"><a href="#{anchors[record["id"]]}">{labels[record["id"]]} · {H(record["source_label"])}</a><blockquote>{H(quote)}</blockquote><span class="mono">Exact saved text · characters {start}–{end}</span></div>'
            )
        steps = [
            findings[e["source"]["id"]]
            for e in incoming.get(claim["snapshot_id"], [])
            if e["source"]["type"] == "finding"
        ]
        reasoning = "".join(
            f'<div class="step">{badge(step.get("details", {}).get("role", step["kind"]).title())}<p>{H(step["statement"])}</p></div>'
            for step in steps
        )
        return f'<article class="comparison"><div><span class="eyebrow">Exact input</span><h3>Saved excerpts behind C{number}</h3>{"".join(quoted) or "<p class=empty>No exact saved quotation is linked to this draft.</p>"}</div><div class="output"><span class="eyebrow">Proposed output · C{number}</span><h3>Draft wording for review</h3>{badge("Unreviewed draft", "review")}<p>{H(claim["statement"])}</p>{reasoning}</div></article>'

    coverage = case.get("coverage", {})
    collector_count = coverage.get("collector_record_count")
    collector_windows = coverage.get("collector_windows", {})
    windows = coverage.get("intended_windows", [])
    window_cards = []
    for window in windows:
        role = window.get("id", "window")
        count = sum(r.get("metadata", {}).get("sample_role") == role for r in records)
        title = {"baseline": "Before the election", "observation": "After the election"}.get(
            role, role.title()
        )
        corpus_count = collector_windows.get(role, {}).get("matching_rows_in_snapshot")
        window_summary = f"{count} case records"
        if type(corpus_count) is int:
            selected = collector_windows[role]["selected_rows"]
            window_summary = (
                f"{selected} collector posts selected from {corpus_count:,} saved in this window"
            )
        window_cards.append(
            f'<div class="window"><span class="eyebrow">{H(role)} window</span><h3>{H(title)}</h3><div class="date">{H(window.get("start", "")[:10])} → {H(window.get("end", "")[:10])}</div><p class="small muted">{H(window_summary)} · {H(coverage.get("timezone", "Timezone not supplied"))}</p><small>Start included, end excluded. Historical coverage is bounded.</small></div>'
        )
    posts = [r for r in records if post_data(r)]
    context = [r for r in records if not post_data(r)]
    status = (
        coverage.get("supabase_status")
        or "Unavailable for this demo; no Supabase records are represented."
    )
    if isinstance(status, dict):
        status = status.get("message") or json.dumps(status, ensure_ascii=False)
    supabase_count = coverage.get("supabase_record_count")
    curated_count = coverage.get("curated_public_excerpt_count")
    if supabase_count:
        banner = "Saved Supabase input · collection scope remains bounded"
    elif any(word in str(status).lower() for word in ("pending", "failed", "unavailable")):
        banner = "Supabase input unavailable · public excerpts are a separate demonstration"
    else:
        banner = "Supabase input not included · inspect the saved collection scope"
    counts = f"{supabase_count if supabase_count is not None else 'Unknown number of'} Supabase records · {curated_count if curated_count is not None else 'Unknown number of'} curated public excerpts."
    material_description = "Post cards distinguish published reproductions from context documents and show where the text was saved."
    if collector_count:
        banner = "Pinned collector snapshot · bounded election sample"
        status = (
            coverage.get("collector_status")
            or "Saved posts from the collector’s public SQLite export."
        )
        counts = f"{collector_count} collector posts · {curated_count} curated public excerpts."
        material_description = "Collector cards show exact saved text, publication and capture times, and engagement at capture. Published reproductions are labeled separately."
    if collector_count:
        groups = []
        for role, heading in (
            ("baseline", "Before Election Day"),
            ("observation", "From Election Day"),
        ):
            role_posts = [r for r in posts if r.get("metadata", {}).get("sample_role") == role]
            cards = "".join(map(record_card, role_posts))
            groups.append(
                f'<div class="section-heading"><h3>{heading}</h3></div><div class="record-grid">{cards}</div>'
            )
        other_cards = "".join(
            record_card(r)
            for r in posts
            if r.get("metadata", {}).get("sample_role") not in {"baseline", "observation"}
        )
        if other_cards:
            groups.append(
                f'<div class="section-heading"><h3>Background posts</h3></div><div class="record-grid">{other_cards}</div>'
            )
        post_cards = '<div class="post-groups">' + "".join(groups) + "</div>"
    else:
        cards = (
            "".join(map(record_card, posts))
            or '<p class="empty">No X post text is included in this sample.</p>'
        )
        post_cards = f'<div class="record-grid">{cards}</div>'
    limits = "".join(f"<li>{H(item)}</li>" for item in case.get("limitations", []))
    questions = "".join(f"<li>{H(item)}</li>" for item in case.get("review_dimensions", []))
    tokens = (BRAND / "traced-tokens.css").read_text()
    for font in ("Manrope-Variable.ttf", "IBMPlexMono-Regular.ttf"):
        tokens = tokens.replace(f"./fonts/{font}", data_uri(BRAND / "fonts" / font, "font/ttf"))
    logo = data_uri(BRAND / "logos/traced-primary.svg", "image/svg+xml")
    icon = data_uri(BRAND / "logos/traced-favicon.svg", "image/svg+xml")
    policy = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; base-uri 'none'; form-action 'none'"
    if unified:
        policy = policy.replace("img-src data:", "script-src 'unsafe-inline'; img-src data: blob:")
    heading = "What changed around Election Day?" if unified else H(case["title"])
    eyebrow = (
        f"{case_icon('flag')} Case 01 · Mamdani · November 4, 2025"
        if unified
        else "Evidence Trail · First case demo"
    )
    overview_question = (
        "Follow the ballot claim to the official records, compare who the posts target, "
        "and see how the sample changes the story about sentiment."
        if unified
        else H(case["question"])
    )
    input_output = f"""
<section class="section" id="input-output">
  <div class="section-heading"><div>
    <span class="eyebrow">Review the draft against its sources</span>
    <h2>Check input vs output</h2>
    <p>Compare exact saved quotations with the proposed wording. Follow each input link to its source card.</p>
  </div></div>
  {"".join(claim_panel(c, i) for i, c in enumerate(claims, 1)) or '<p class="empty">No substantive draft claim is saved.</p>'}
</section>"""
    source_library = f"""
<section id="scope">
  <div class="windows">{"".join(window_cards)}</div>
  <details><summary>Scope and gaps before review</summary><ul>{limits}</ul>
    <p class="small muted">Publication timestamps stay unknown when only a source date was saved.</p></details>
  <details><summary>Questions still for a reviewer</summary><ul>{questions}</ul></details>
</section>
<section class="section" id="saved-material">
  <div class="section-heading"><div><span class="eyebrow">Read the inputs</span>
  <h2>Saved posts and their source trail</h2><p>{H(material_description)}</p></div></div>
  {post_cards}
</section>
<section class="section" id="context">
  <div class="section-heading"><div><h2>Context documents</h2>
  <p>Published reporting and official statements provide context and attributed wording.</p></div></div>
  <div class="record-grid">{"".join(map(record_card, context))}</div>
</section>"""
    if unified:
        content = f"""
{dashboard}{workspace}{input_output}
<details class="card section"><summary>Source library, scope, and walkthrough</summary>
{source_library}{flow}{patterns}
</details>"""
    else:
        content = f"{dashboard}{flow}{source_library}{input_output}{patterns}"

    if unified:
        briefing_actions = (
            f'<nav class="mission-nav" aria-label="Case shortcuts">'
            f'<a href="#case-dashboard">{case_icon("flag")} Objectives</a>'
            f'<a href="#tree">{case_icon("graph")} Evidence map</a>'
            f'<a href="#clusters">{case_icon("stack")} Topic clusters</a>'
            f'<a href="#input-output">{case_icon("document")} Review draft</a></nav>'
        )
        briefing_status = (
            '<div class="mission-status" aria-label="Case status">'
            f"{signal_chip('Human review pending', 'clock', 'review')}"
            f"{signal_chip(f'{len(records)} saved records', 'stack', 'context')}"
            f"{signal_chip(f'{len(claims)} cited draft claims', 'document', 'context')}"
            "</div>"
        )
        coverage_note = (
            f'<details class="notice mission-coverage"><summary>{case_icon("question")} '
            f"{H(banner)} · historical coverage incomplete</summary>"
            f"<p>{H(status)}</p><p>{H(counts)}</p></details>"
        )
    else:
        briefing_actions = ""
        briefing_status = (
            f'<div class="chips">{badge("Unreviewed draft", "review")}'
            f"{badge(f'{len(records)} saved records')}{badge(f'{len(claims)} cited draft claims')}"
            f"{badge('No human decision recorded')}</div>"
        )
        coverage_note = (
            f'<aside class="notice"><strong>{H(banner)}</strong><p>{H(status)}</p>'
            f"<p>{H(counts)} The selected sample has incomplete historical coverage.</p></aside>"
        )

    result = f"""
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{policy}">
<title>{H(case["title"])} · Traced</title>
<link rel="icon" href="{icon}">
<style>{tokens}
{CSS}{DASHBOARD_CSS if dashboard else ""}{MISSION_CSS if unified else ""}{trace_css}</style>
{trace_script}</head>
<body><header><div class="bar">
<a href="#overview" aria-label="Traced case overview"><img class="logo" src="{logo}" alt="Traced"></a>
<nav class="nav" aria-label="Case sections">{case_nav}</nav>
<a class="primary" href="#input-output">Check input vs output</a></div></header>
<main id="overview">
<section class="hero{" mission-hero" if unified else ""}">
<div class="eyebrow">{eyebrow}</div><h1>{heading}</h1>
<p class="lead">{overview_question}</p>
{briefing_status}{briefing_actions}
</section>
{coverage_note}
{content}
<details class="audit"><summary>Native package and traceability</summary>
{pairs([("Case ID", case["id"]), ("Bundle SHA256", hashlib.sha256(raw).hexdigest()), ("Draft run", draft_runs[-1] if draft_runs else None)])}
<p id="package-trace"><a href="{H(archive.name)}" download>Download native case package</a>
 · Import it into Traced to inspect the native evidence and planning history.</p></details>
<footer><span>Generated from the native case package</span><span>Local case preview · no live analysis</span></footer>
</main></body></html>""".lstrip()
    output.parent.mkdir(parents=True, exist_ok=True)
    outputs = [output]
    if trace_alias:
        alias = output.with_name("trace-explorer.html")
        if alias != output:
            outputs.append(alias)
    for path in outputs:
        temporary = path.with_suffix(".html.tmp")
        temporary.write_text(result, encoding="utf-8")
        temporary.replace(path)
    print(
        json.dumps(
            {
                "output": str(output),
                "records": len(records),
                "posts": len(posts),
                "context_documents": len(context),
                "claims": len(claims),
                "windows": len(windows),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--trace-alias",
        action="store_true",
        help="Keep the legacy trace-explorer.html URL as a copy of the combined page.",
    )
    args = parser.parse_args()
    render(
        args.archive,
        args.out or args.archive.with_name("preview.html"),
        trace_alias=args.trace_alias,
    )
