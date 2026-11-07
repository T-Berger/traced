"""Render the collector-backed case dashboard using saved evidence only."""

from __future__ import annotations

import csv
import hashlib
import html
import json
from collections import Counter
from datetime import UTC, date, datetime
from io import StringIO
from urllib.parse import urlsplit


def esc(value):
    return html.escape(str(value), quote=True)


CSS = """
.case-dashboard{margin-top:28px;display:grid;gap:18px}
.case-dashboard>div>p{max-width:82ch}
.case-dashboard .section-heading{margin:0}
.db-workflow{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px}
.db-workflow li{margin:0;min-width:0}
.db-workflow a{height:100%;display:flex;flex-direction:column;gap:4px;padding:11px 12px;background:var(--traced-surface);border:1px solid var(--traced-border-subtle);border-radius:6px;text-decoration:none;font-size:13px}
.db-workflow a strong{text-decoration:underline;text-underline-offset:3px}
.db-workflow a span{color:var(--traced-foreground-secondary);font-size:12px}
.db-workflow [data-stage=scope] a{background:var(--traced-status-review-background);border-color:var(--traced-status-review-border)}
.db-lanes{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.db-lane{padding:18px;display:flex;flex-direction:column;gap:9px}
.db-lane p,.db-lane a{font-size:14px}
.db-lane a{margin-top:auto;padding-top:5px}
.db-lane .badge{align-self:flex-start}
.db-timeline{padding:20px}
.db-timeline p{margin:7px 0}
.db-chart{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,3fr);gap:14px;margin-top:16px}
.db-chart>div{border:1px solid var(--traced-border-subtle);border-radius:6px;padding:11px;min-width:0}
.db-bars{height:114px;display:flex;align-items:flex-end;gap:3px;border-bottom:1px solid var(--traced-border-strong);margin:4px 0}
.db-week{position:relative;flex:1;min-width:0;height:100%;display:flex;flex-direction:column;justify-content:flex-end;border-radius:3px 3px 0 0}
.db-week:hover{background:var(--traced-surface-subtle)}
.db-week:focus-visible{z-index:1}
.db-week>span{display:block;width:100%;min-height:0}
.db-week .db-lead{position:absolute;inset:auto 0 0;height:4px;background:var(--traced-color-trace)}
.db-own{background:var(--traced-color-moss)}
.db-third{background:var(--traced-color-slate)}
.db-unknown{background:var(--traced-border-strong)}
.db-gap{background:repeating-linear-gradient(135deg,var(--traced-surface-subtle) 0 4px,var(--traced-border-subtle) 4px 6px)}
.db-mentions-gap{background:repeating-linear-gradient(135deg,var(--traced-status-review-background) 0 4px,var(--traced-status-review-border) 4px 6px)}
.db-gap .db-own,.db-gap .db-third{opacity:.65}
.db-ticks{display:flex;justify-content:space-between;font-size:11px;color:var(--traced-foreground-secondary)}
.db-key{display:flex;gap:14px;flex-wrap:wrap;margin-top:9px;font-size:12px}
.db-key i{display:inline-block;width:12px;height:10px;margin-right:4px;border-radius:2px}
.db-week-list{padding-left:0;list-style:none}
.db-week-list li{padding:8px 0;border-top:1px solid var(--traced-border-subtle)}
.db-week-list li:target{outline:2px solid var(--traced-focus-ring);outline-offset:4px}
.db-record-links{display:flex;gap:6px 13px;flex-wrap:wrap;font-size:13px}
.db-trace{padding:20px}
.db-trace h3{margin:0 0 9px}
.db-trace h4{margin:6px 0 9px}
.db-trace-item{border-top:1px solid var(--traced-border-subtle);padding-top:12px;margin-top:12px}
.db-trace-item blockquote{margin:4px 0 9px;padding-left:10px;border-left:3px solid var(--traced-color-moss);white-space:pre-wrap}
.db-trace-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.db-trace-grid>div{min-width:0}
.db-native{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
@media(max-width:900px){.db-workflow{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:620px){.db-lanes,.db-chart,.db-trace-grid{grid-template-columns:1fr}.db-workflow{grid-template-columns:repeat(2,minmax(0,1fr))}.db-timeline,.db-trace{padding:16px}.db-bars{height:92px}}
"""


def workflow(count, scope):
    scope_state = (
        "Scope confirmation saved" if scope["planning_confirmations"] else "Human confirmation due"
    )
    decision_state = (
        f"{scope['human_reviews']} saved decisions"
        if scope["human_reviews"]
        else "Drafts await review"
    )
    steps = [
        ("plan", "Plan", "Draft saved", "#use-flow"),
        ("gather", "Gather evidence", f"{count} records saved", "#saved-material"),
        ("scope", "Confirm scope", scope_state, "#scope"),
        ("investigate", "Investigate", "Timeline and source trace", "#case-timeline"),
        ("decide", "Record decisions", decision_state, "#input-output"),
        ("brief", "Prepare briefing", "Review before export", "#package-trace"),
    ]
    return (
        '<ol class="db-workflow" aria-label="Investigation workflow">'
        + "".join(
            f'<li data-stage="{stage}"><a href="{target}"><strong>{index}. {title}</strong>'
            f"<span>{state}</span></a></li>"
            for index, (stage, title, state, target) in enumerate(steps, 1)
        )
        + "</ol>"
    )


def lane(title, status, text, target, action):
    return (
        f'<article class="card db-lane"><span class="badge review">{esc(status)}</span>'
        f'<h3>{esc(title)}</h3><p>{esc(text)}</p><a href="{esc(target)}">{esc(action)} ↓</a></article>'
    )


def short_day(value):
    moment = date.fromisoformat(value)
    return f"{moment:%b} {moment.day}"


def render_timeline(payload, record_link):
    bins = payload["timeline"]["bins"]
    windows = {window["id"]: window for window in payload["scope"]["windows"]}
    mentions_gaps = [
        (datetime.fromisoformat(gap["start"]), datetime.fromisoformat(gap["end"]))
        for gap in payload["scope"]["coverage_gaps"].get("mentions", [])
    ]
    weeks = {}
    for day in bins:
        day_label, role = day["date"], day["window"]
        window_start = date.fromisoformat(windows[role]["start"][:10])
        week_index = (date.fromisoformat(day_label) - window_start).days // 7
        week = weeks.setdefault(
            (role, week_index),
            {
                "start": day_label,
                "end": day_label,
                "counts": Counter(),
                "refs": [],
                "gap": False,
                "mentions_gap": False,
            },
        )
        week["end"] = day_label
        week["counts"].update(day["counts_by_attribution"])
        week["refs"].extend(day["record_ids"])
        overlap = day["declared_scope_overlap"]
        week["gap"] |= (
            overlap is None or overlap["start"] != day["start"] or overlap["end"] != day["end"]
        )
        day_start, day_end = (datetime.fromisoformat(day[key]) for key in ("start", "end"))
        week["mentions_gap"] |= any(
            start < day_end and end > day_start for start, end in mentions_gaps
        )
        if day["saved_corpus_count"] != sum(day["counts_by_attribution"].values()):
            raise ValueError("Timeline counts do not match the attributed saved rows.")

    peak = max(1, *(sum(w["counts"].values()) for w in weeks.values()))
    chart, entries = [], []
    for role, label in (
        ("baseline", "Before Election Day"),
        ("observation", "From Election Day"),
    ):
        bars = []
        role_weeks = [week for (window, _), week in weeks.items() if window == role]
        for week in role_weeks:
            count = sum(week["counts"].values())
            week_id = "week-" + week["start"]
            dates = short_day(week["start"])
            if week["start"] != week["end"]:
                dates += "–" + short_day(week["end"])
            subject = week["counts"]["subject_own"] + week["counts"]["subject_affiliated"]
            third = week["counts"]["third_party"]
            tally = f"{count:,} saved posts, {len(week['refs'])} in case"
            if week["gap"]:
                tally += ", overlaps the early coverage gap"
            elif week["mentions_gap"]:
                tally += ", overlaps a gap in mentions collection"
            label_text = f"{dates}: {tally}"
            segments = "".join(
                f'<span class="{cls}" style="height:{amount / peak * 100:.4f}%" aria-hidden="true"></span>'
                for cls, amount in (
                    ("db-unknown", count - subject - third),
                    ("db-third", third),
                    ("db-own", subject),
                )
                if amount > 0
            )
            lead = '<span class="db-lead" aria-hidden="true"></span>' if week["refs"] else ""
            gap = " db-gap" if week["gap"] else " db-mentions-gap" if week["mentions_gap"] else ""
            bars.append(
                f'<a href="#{week_id}" class="db-week{gap}" title="{esc(label_text)}" aria-label="{esc(label_text)}">{segments}{lead}</a>'
            )
            refs = "".join(map(record_link, week["refs"]))
            entries.append(
                f'<li id="{week_id}"><strong>{esc(dates)}</strong> · {esc(tally)}'
                f'<div class="small muted">{subject:,} subject/affiliated · {third:,} third-party</div>'
                f'<div class="db-record-links">{refs or "<span>Open the pinned snapshot for any underlying posts.</span>"}</div></li>'
            )
        chart.append(
            f'<div><strong class="small">{label}</strong><div class="db-bars">{"".join(bars)}</div>'
            f'<div class="db-ticks"><span>{short_day(role_weeks[0]["start"])}</span>'
            f"<span>{short_day(role_weeks[-1]['end'])}</span></div></div>"
        )
    corpus_total = sum(day["saved_corpus_count"] for day in bins)
    selected_total = sum(day["selected_case_count"] for day in bins)
    return (
        '<article class="card db-timeline" id="case-timeline" aria-labelledby="case-timeline-title">'
        '<div class="eyebrow">November 4, 2025 · New York City mayoral election</div>'
        '<h3 id="case-timeline-title">Timeline of saved X posts</h3>'
        f"<p>{corpus_total:,} posts in the collector snapshot across the two windows. "
        f"{selected_total} are linked to this case. Select a week to open its saved evidence.</p>"
        f'<div class="db-chart">{"".join(chart)}</div>'
        '<div class="db-key"><span><i class="db-own"></i>Subject and affiliated accounts</span>'
        '<span><i class="db-third"></i>Third-party accounts</span>'
        '<span><i style="background:var(--traced-color-trace)"></i>Case records available</span>'
        '<span><i class="db-gap"></i>Outside all or part of declared coverage</span>'
        '<span><i class="db-mentions-gap"></i>Gap in successful mentions slices</span></div>'
        '<p class="small muted">Weeks use publication dates in America/New_York and share one count scale. '
        "The baseline starts September 4, before the collector’s declared October 1 UTC start. "
        "Third-party records end December 30. Account groups reflect collector attribution. "
        "The windows have different lengths and incomplete coverage.</p>"
        f'<details><summary>Weekly counts and exact case records</summary><ol class="db-week-list">{"".join(entries)}</ol></details>'
        "</article>"
    )


def public_supplement(archive, payload):
    directory = archive.parent.parent / "maltego-enrichment" / "public-cross-check"
    if not (directory / "manifest.json").is_file():
        return ""

    def checked_capture(name, host):
        receipt = json.loads((directory / f"{name}.receipt.json").read_bytes())
        raw = (directory / f"{name}.response").read_bytes()
        locator = urlsplit(receipt["requested_url"])
        if (
            receipt["http_status"] != 200
            or receipt["response_sha256"] != hashlib.sha256(raw).hexdigest()
            or locator.hostname != host
            or locator.scheme != "https"
            or receipt["credential_or_cookie_used"]
        ):
            raise ValueError("Public supplement receipt does not match its saved response.")
        return receipt, raw

    corpus_ids = {row["native_id"] for row in payload["timeline"]["corpus_rows"]}
    captures = []
    for name in (
        "musk-ballot",
        "related-seth-requested",
        "mamdani-diwali",
        "mamdani-diwali-followup",
    ):
        receipt, _ = checked_capture(name, "publish.x.com")
        post = json.loads((directory / f"{name}.extracted.json").read_bytes())
        if (
            post["response_record_id"] != receipt["record_id"]
            or post["response_sha256"] != receipt["response_sha256"]
            or post["text_sha256"] != hashlib.sha256(post["text"].encode()).hexdigest()
            or not post["native_id"].isdecimal()
        ):
            raise ValueError("Public post does not match its response receipt.")
        status = (
            "also in the collector snapshot"
            if post["native_id"] in corpus_ids
            else "new native ID for this case"
        )
        captures.append(
            f'<li><a href="{esc(receipt["requested_url"])}">{esc(post["author_name"])} · {post["native_id"]}</a>'
            f" · {status}</li>"
        )
    recap, raw = checked_capture("nyc-mayor-recap", "www.vote.nyc")
    labels = (
        "Zohran Kwame Mamdani (Democratic)",
        "Zohran Kwame Mamdani (Working Families)",
    )
    rows = csv.reader(StringIO(raw.decode("utf-8-sig"), newline=""))
    matches = [
        row
        for row in rows
        if len(row) > 5 and row[3] == "Total for Mayor (Citywide)" and row[4] in labels
    ]
    if len(matches) != 2 or {row[4] for row in matches} != set(labels):
        raise ValueError("The saved citywide recap is missing distinct candidate party lines.")
    votes = {row[4]: int(row[5].replace(",", "")) for row in matches}
    return (
        '<div class="db-trace-item" id="public-source-supplement"><strong>Public-source supplement</strong>'
        "<p>X’s public embed returned four exact post IDs. Two were absent from the collector snapshot. "
        "The embed supplies post wording and attribution. Exact publication times and thread context remain incomplete.</p>"
        f"<ul>{''.join(captures)}</ul>"
        f"<p>The official recap lists {votes[labels[0]]:,} votes on the Democratic line and "
        f"{votes[labels[1]]:,} on the Working Families line for Mamdani. "
        f"The two published line totals sum to {sum(votes.values()):,}. "
        f'<a href="{esc(recap["requested_url"])}">NYC Board of Elections recap CSV</a>.</p>'
        '<p class="small muted">Separate captures from X and the Board of Elections.</p>'
        "</div>"
    )


def maltego_supplement(archive):
    base = archive.parent.parent / "maltego-enrichment"
    archive_receipt = base / "live-archive" / "1-maltego_run.json"
    search_receipt = base / "live-web-search" / "1-maltego_run.json"
    if not archive_receipt.is_file() or not search_receipt.is_file():
        return ""

    def verified_result(path, capability, post_id=None):
        receipt = json.loads(path.read_bytes())
        result = receipt["result"]
        raw = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if (
            receipt["endpoint"] != "https://datamcp.maltego.com/mcp"
            or receipt["tool"] != "maltego_run"
            or receipt["arguments"]["capability_id"] != capability
            or receipt["arguments"].get("validate_only")
            or post_id is not None
            and receipt["arguments"].get("entity_properties", {}).get("id") != post_id
            or receipt["result_sha256"] != hashlib.sha256(raw).hexdigest()
            or result.get("isError")
        ):
            raise ValueError("The Maltego lookup receipt could not be verified.")
        output = result.get("structuredContent") or json.loads(result["content"][0]["text"])
        if output["run_status"] != "SUCCEEDED" or len(output["entities"]) != output["count"]:
            raise ValueError("The Maltego lookup did not return a complete saved result page.")
        return output

    result = verified_result(archive_receipt, "ci_Qxwz_uFRF52FsdHjK1q-L1tBvA")
    search = verified_result(search_receipt, "ci_L__1U2Yf1njZLdfDn_3hTy0ick")
    original = "https://x.com/elonmusk/status/1985692613010502114"
    snapshots = []
    replays_without_post = 0
    for item in sorted(result["entities"], key=lambda item: item["rawTimestamp"]):
        stamp = item["rawTimestamp"]
        expected = f"https://web.archive.org/web/{stamp}/{original}"
        if item["originalUrl"] != original or item["archiveUrl"] != expected:
            raise ValueError("The archived URL does not resolve to the investigated post ID.")
        captured = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        if int(captured.timestamp() * 1000) != int(item["dateTime"]):
            raise ValueError("The archive capture timestamps disagree.")
        replay = base / "archive-cross-check" / f"musk-ballot-{stamp}-receipt.json"
        suffix = ""
        if replay.is_file():
            receipt = json.loads(replay.read_bytes())
            raw = replay.with_name(f"musk-ballot-{stamp}-replay.html").read_bytes()
            if (
                receipt["source_url"] != expected
                or receipt["sha256"] != hashlib.sha256(raw).hexdigest()
            ):
                raise ValueError("The replay does not match its archive receipt.")
            if receipt["assessment"]["availability"] == "http_success_post_content_unavailable":
                suffix = " · checked replay returned an X error page"
                replays_without_post += 1
        snapshots.append(
            f'<li><a href="{esc(expected)}">{captured:%b %d, %Y · %H:%M:%S UTC}</a>{suffix}</li>'
        )
    links = {
        "https://www.pbs.org/newshour/politics/fact-focus-new-york-city-ballots-do-not-show-proof-of-election-fraud": "Associated Press reporting, via PBS",
        "https://www.cozen.com/news-resources/news/2025/listing-candidates-twice-on-nyc-ballot-is-part-of-fusion-voting-it-s-not-a-scam-as-musk-said": "Cozen’s reference to the existing PolitiFact article",
    }
    leads = "".join(
        f'<li><a href="{esc(url)}">{esc(label)}</a></li>'
        for url, label in links.items()
        if any(entity["value"] == url for entity in search["entities"])
    )
    outputs = [result, search]
    posts = ""
    ballot_path = base / "live-vetric-ballot" / "1-maltego_run.json"
    diwali_path = base / "live-vetric-diwali" / "1-maltego_run.json"
    if ballot_path.is_file() and diwali_path.is_file():
        ballot = verified_result(
            ballot_path, "ci_uqmVgJpK6q5RzVbK6DX_ah20tg", "1985692613010502114"
        )
        diwali = verified_result(
            diwali_path, "ci_3Ea4-YAxI1G0xD1TQpR6g93YCi", "1980606660528804219"
        )
        outputs.extend((ballot, diwali))

        def post(output, native_id):
            matches = [
                item
                for item in output["entities"]
                if item["type"] == "maltego.Twit" and item["id"] == native_id
            ]
            if len(matches) != 1:
                raise ValueError("The Data MCP result lacks the exact X post ID.")
            return matches[0]

        original_post = post(ballot, "1985692613010502114")
        parent = post(diwali, "1980606111838355647")
        published = datetime.strptime(
            original_post["pubdate"], "%a %b %d %H:%M:%S %z %Y"
        ).astimezone(UTC)
        ballot_quote = "Other mayoral candidates appear twice"
        if (
            original_post["content"].count(ballot_quote) != 1
            or "I visited four temples across Queens" not in parent["content"]
        ):
            raise ValueError("The Data MCP text does not match the exact case references.")
        posts = (
            "<p><strong>Original X post via Vetric</strong></p>"
            f"<blockquote>{esc(ballot_quote)}</blockquote>"
            f'<p><a href="{esc(original)}">Ballot post · 1985692613010502114</a> · published {published:%b %d, %Y at %H:%M:%S UTC}.</p>'
            f'<p class="small muted">At lookup: {original_post["view_count"]:,} views, '
            f"{original_post['retweet_count']:,} reposts and {original_post['reply_count']:,} replies. "
            "These are a single provider snapshot.</p>"
            "<p><strong>Diwali source trace:</strong> Vetric’s original-post lookup for "
            '<a href="https://x.com/ZohranKMamdani/status/1980606660528804219">the follow-up</a> returned '
            '<a href="https://x.com/ZohranKMamdani/status/1980606111838355647">the same parent ID</a> recorded by the collector.</p>'
        )
    usage = sum(output["credits"]["consumed"] for output in outputs)
    replay_note = (
        f"The {replays_without_post} checked Election Day replays displayed X error pages. "
        if replays_without_post
        else ""
    )
    return (
        '<div class="db-trace-item" id="maltego-data-mcp"><strong>Maltego Data MCP</strong>'
        f"{posts}"
        f"<p>Wayback returned {len(snapshots)} snapshots of the exact ballot-post URL. "
        f"The times below mark archive captures. {replay_note}</p>"
        f"<details><summary>Archive captures</summary><ul>{''.join(snapshots)}</ul></details>"
        f"<p>Relevant web-search leads</p><ul>{leads}</ul>"
        f'<p class="small muted">{usage} estimated team credits across {len(outputs)} lookups.</p></div>'
    )


def render_trace(payload, record_link, excerpt, supplement):
    items = payload["exact_source_observations"]
    comparisons = []
    language = []
    for item in items:
        refs = "".join(excerpt(ref) for ref in item["source_refs"])
        section = (
            f'<div class="db-trace-item"><strong>{esc(item["title"])}</strong>'
            f'<p>{esc(item["statement"])}</p>{refs}<p class="small muted">{esc(item["next_review"])}</p></div>'
        )
        if item["id"] == "source-observation-1":
            comparisons.append(section)
        else:
            language.append(section)
    links = []
    for relation in payload["relationships"]["declared_parent_links"]:
        label = "Collector-declared parent link"
        if relation["collector_is_quote"]:
            label += " · quote flag"
        links.append(
            f'<div class="db-trace-item"><strong>{label}</strong>'
            f'<div class="db-record-links">{record_link(relation["source_record_id"])} '
            f"<span>→ parent</span> {record_link(relation['target_record_id'])}</div>"
            f'<p class="small muted">{esc(relation["source_native_id"])} → {esc(relation["target_native_id"])}</p></div>'
        )
    structure = payload["coordination_and_automation"]["observed_structure"]
    return (
        '<article id="trace" class="card db-trace" aria-labelledby="case-trace-title">'
        '<span class="eyebrow">Open the inputs</span><h3 id="case-trace-title">Trace the evidence</h3>'
        '<div class="db-trace-grid"><div id="trace-claim"><h4>Claim and public-source check</h4>'
        f"{''.join(comparisons)}{supplement}</div>"
        '<div id="trace-coordination"><h4>Parent links and bot-analysis readiness</h4>'
        f"<p>The snapshot has {structure['records_with_parent_reference']:,} in-window posts with a parent reference. "
        f"{structure['parent_refs_unresolved_in_snapshot']:,} parent references are unresolved in the snapshot. "
        "Bot and coordination analysis needs complete discussion context, background activity, and a saved detector run.</p>"
        f"{''.join(links)}</div></div>"
        "<details><summary>Language examples for sentiment review</summary>"
        '<p id="trace-sentiment">Read each speaker’s wording alongside the source and target. The selected posts have no saved sentiment scores.</p>'
        f"{''.join(language)}</details></article>"
    )


def render_dashboard(archive, bundle, anchors, labels):
    path = archive.with_name("dashboard-data.json")
    if not path.is_file():
        return ""
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Dashboard data exceeds the preview limit.")
    payload = json.loads(path.read_bytes())
    binding = payload["binding"]
    with archive.open("rb") as case_file:
        package_hash = hashlib.file_digest(case_file, "sha256").hexdigest()
    if (
        payload["schema"] != "traced.mamdani-evidence-dashboard/1"
        or binding["package_sha256"] != package_hash
        or binding["case_id"] != bundle["case"]["id"]
        or binding["snapshot_sha256"] != bundle["case"]["coverage"]["collector_snapshot"]["sha256"]
        or binding["pattern_review_sha256"]
        != hashlib.sha256(archive.with_name("pattern-review.json").read_bytes()).hexdigest()
    ):
        raise ValueError("Dashboard source bindings do not match this native case package.")

    records = {record["id"]: record for record in bundle["records"]}

    def record_link(record_id):
        record = records[record_id]
        post = record.get("metadata", {}).get("collector_post", {})
        handle = post.get("handle")
        label = f"@{handle.lstrip('@')}" if handle else record["source_label"]
        return f'<a href="#{anchors[record_id]}">{labels[record_id]} · {esc(label)}</a>'

    def excerpt(ref):
        record_id = ref["record_id"]
        text = records[record_id]["text"]
        start, end, quote = ref["start"], ref["end"], ref["quote"]
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(text)
            or text[start:end] != quote
        ):
            raise ValueError("Dashboard excerpt does not match the saved case record.")
        return f"{record_link(record_id)}<blockquote>{esc(quote)}</blockquote>"

    timeline = render_timeline(payload, record_link)
    supplement = maltego_supplement(archive) + public_supplement(archive, payload)
    trace = render_trace(payload, record_link, excerpt, supplement)
    count = len(records)
    sentiment = payload["sentiment"]
    bot = payload["coordination_and_automation"]
    lanes = "".join(
        (
            lane(
                "Misinformation",
                "Draft claim check",
                "Compare the ballot allegation with the certified result and counting rule. The repeated listing is visible in the saved sources. Duplicate counting remains unsubstantiated.",
                "#trace-claim",
                "Open claim and exact sources",
            ),
            lane(
                "Bots and coordination",
                "Unmeasured" if bot["status"] == "unavailable" else "Saved result to inspect",
                "Inspect a saved parent link and the evidence required to assess coordination or automation.",
                "#trace-coordination",
                "Open trace and input gaps",
            ),
            lane(
                "Public sentiment",
                "Unmeasured" if sentiment["status"] == "unavailable" else "Saved annotations",
                f"{sentiment['classified_records']} of {sentiment['denominator']} selected X posts have a saved sentiment annotation. Review attributed language and its context.",
                "#trace-sentiment",
                "Open saved language examples",
            ),
        )
    )
    explorer = (
        '<div class="db-native"><a class="primary" href="trace-explorer.html">'
        "Explore interactive tree, clusters and timeline ↗</a></div>"
        if archive.with_name("trace-explorer.html").is_file()
        else ""
    )
    return (
        '<section id="case-dashboard" class="case-dashboard" aria-labelledby="case-dashboard-title">'
        '<div class="section-heading"><div><span class="eyebrow">Investigation overview</span>'
        '<h2 id="case-dashboard-title">Mamdani case dashboard</h2>'
        f"<p>Follow the workflow, compare saved activity around Election Day, and trace the draft observations back to {count} case records.</p>"
        "</div></div>"
        f'{workflow(count, payload["scope"])}{explorer}<div class="db-lanes">{lanes}</div>{timeline}{trace}'
        '<div class="db-native"><a class="primary" href="http://127.0.0.1:8053/">Open Traced to review ↗</a>'
        '<a href="#use-flow">Read the step-by-step walkthrough</a></div></section>'
    )
