"""Render a hand-coded comparison of the saved Mamdani demo sample."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from investigation_icons import icon
from source_spans import SourceSpan

NY = ZoneInfo("America/New_York")
CASE_PACKAGE_SHA256 = "46d60ba57b0144dfaa3ef9d5873fcadbf38cbb1c1ee99831c842e8f721eb340f"
LABELS = {
    "critical": "Direct criticism",
    "favorable": "Direct support",
    "other": "Different target",
    "unclear": "Unclear or unstated",
}

SENTIMENT_SIGNAL_CSS = """
.sentiment-signals{display:grid;gap:10px}
.sentiment-signal-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:0;padding:0;list-style:none}
.sentiment-signal-grid>li{min-width:0;margin:0;padding:10px 12px;border:1px solid var(--traced-border-subtle);border-radius:8px;background:var(--traced-surface-subtle)}
.sentiment-signal-name{font-size:13px;font-weight:700}
.sentiment-signal-direction{display:inline-flex;align-items:center;gap:6px;margin:5px 0;font-size:12px;font-weight:700}
.sentiment-signal-direction svg,.sentiment-split svg{width:17px;height:17px;flex:none}
.sentiment-signal-counts{display:flex;align-items:center;flex-wrap:wrap;gap:7px 12px}
.sentiment-signal-counts span{display:block;font-size:11px;color:var(--traced-foreground-secondary)}
.sentiment-signal-counts strong{display:block;color:var(--traced-foreground);font-family:var(--traced-font-mono);font-size:21px;line-height:1.15}
.sentiment-split{display:flex;align-items:center;gap:8px;padding:9px 11px;border-left:3px solid var(--traced-color-slate);background:var(--traced-surface-subtle);font-size:13px}
.sentiment-split p{margin:0}
@media(max-width:620px){.sentiment-signal-grid{grid-template-columns:1fr}}
"""

# Each span identifies a verbatim fragment of its pinned, selected X post.
READINGS = {
    "1973210201277997114": (
        "unclear",
        SourceSpan(66, 96, "e274112e9bd81577806e0f4a45ac78ec9b0597cc179b8193976306a0a352af4b"),
        "The question gives no clear stance toward Mamdani.",
    ),
    "1977212058744897816": (
        "unclear",
        SourceSpan(13, 66, "1ab4ddfddcd63c4793dd7096d1c85fcbcac7521f538a8082809da5fb033d9446"),
        "The author describes other voters. The saved wording leaves the author’s stance ambiguous.",
    ),
    "1979137419070132468": (
        "critical",
        SourceSpan(8, 42, "a065079ea3a2f999942b72c1091e66662993df8ce701b2981519403f3769140a"),
        "The headline explicitly calls for action against Mamdani.",
    ),
    "1980772447696605449": (
        "critical",
        SourceSpan(326, 345, "f693bfd4bc9d60af4fdb6fb80f23fa279502725dd80ac2e8f15ef7c87b3884da"),
        "The author directly insults Mamdani.",
    ),
    "1980780743757377818": (
        "critical",
        SourceSpan(116, 135, "cc8023c148b167c77209e65e9df2b3122403f36175304e9cb4593feeeddad743"),
        "The author explicitly disparages Mamdani.",
    ),
    "1981457004355838183": (
        "unclear",
        SourceSpan(34, 57, "fa721e512e6bc7e75e9ec07e4257b7b26bc18c24ff695bfbed010e95c60f4ac0"),
        "The post reports the letter signers’ stance.",
    ),
    "1982948828480180593": (
        "critical",
        SourceSpan(151, 183, "9e77bdeed857b427e34e238ebe5c5cefac6cf9725a6a8aad9c4dfeee4b266c3b"),
        "The author advocates removing Mamdani from the United States.",
    ),
    "1985497035181601164": (
        "unclear",
        SourceSpan(227, 239, "39013bbd3ad176ada010d74c2e467cef4c03f9e013aaae09c14c53c5f279d504"),
        "The author presents someone else’s words and asks a question.",
    ),
    "1985642138177044502": (
        "other",
        SourceSpan(26, 71, "d8eb1daadc0213161e13df0103a08f51653f2659c9b60e9b7b09e395fb9a7084"),
        "The criticism is about the person said to have withheld support.",
    ),
    "1992358299782799427": (
        "unclear",
        SourceSpan(186, 216, "f0164e26bcd0363a37074fffb587ea2474d612264d4dc9b598378b3a5ae3be0e"),
        "The point concerns Roosevelt’s economic legacy. The author’s stance toward Mamdani is unclear.",
    ),
    "1999218201503412273": (
        "critical",
        SourceSpan(65, 97, "f20574b40f80bb286d96c948f1eebac1bc8b7a39552330888aadcfeb3878ea6c"),
        "The reply directly criticizes a position it attributes to Mamdani.",
    ),
    "2000057460623630848": (
        "other",
        SourceSpan(10, 37, "5b4a970fb2e531bbd1c88db5ff0a37582775080d06738623f2f47ad4ce342777"),
        "The accusation in the post targets an alleged supporter.",
    ),
    "2005787444743119321": (
        "unclear",
        SourceSpan(7, 39, "c3c1a6717da3c5287884f62f488ee2ec0b102179a1e65644310c72e55023ad2f"),
        "The text criticizes an invitation context. The target cannot be established from the saved wording alone.",
    ),
    "2005957301010682073": (
        "critical",
        SourceSpan(25, 62, "996597543a8f550afb63ab5fb2ad25801e86ae8e31baa469e5d5d1cfa7ab1c1f"),
        "The author directly criticizes Mamdani’s policy approach.",
    ),
}


def _selected_rows(payload, records):
    if payload["binding"]["package_sha256"] != CASE_PACKAGE_SHA256:
        return None
    rows = [row for row in payload["selected_records"] if row["attribution"] == "third_party"]
    if Counter(row["native_id"] for row in rows) != Counter(READINGS.keys()):
        return None

    windows = [
        (
            window["id"],
            datetime.fromisoformat(window["start"]),
            datetime.fromisoformat(window["end"]),
        )
        for window in payload["scope"]["windows"]
    ]
    grouped = {key: [] for key, _, _ in windows}
    for row in rows:
        moment = datetime.fromisoformat(row["posted_at"])
        matching = [key for key, start, end in windows if start <= moment < end]
        if len(matching) != 1:
            return None
        record_id = row["record_id"]
        label, span, rationale = READINGS[row["native_id"]]
        text = records[record_id]["text"]
        quote = span.extract(text)
        if quote is None or text.count(quote) != 1:
            return None
        ref = {
            "record_id": record_id,
            "start": span.start,
            "end": span.end,
            "quote": quote,
        }
        roles = row["selection_roles"]
        if roles == ["stratified_sample"]:
            selection = "Rank-spaced sample"
        elif len(roles) == 1 and roles[0].startswith("targeted_lead:"):
            selection = "Targeted leads"
        else:
            return None
        grouped[matching[0]].append((moment, label, ref, rationale, selection))
    return grouped


def _render_criticism_signals(grouped):
    """Compare shares within each selection group without rounding."""
    cards = []
    for source in (None, "Rank-spaced sample", "Targeted leads"):
        before, after = [
            [
                label
                for _, label, _, _, route in grouped[window]
                if source is None or route == source
            ]
            for window in ("baseline", "observation")
        ]
        before_critical = before.count("critical")
        after_critical = after.count("critical")
        if before and after:
            difference = after_critical * len(before) - before_critical * len(after)
            if difference > 0:
                indicator, direction = "up", "Higher share"
            elif difference < 0:
                indicator, direction = "down", "Lower share"
            else:
                indicator, direction = "flat", "Same share"
        else:
            indicator, direction = "question", "Comparison unavailable"
        cards.append(
            f'<li><p class="sentiment-signal-name">{source or "All selected posts"}</p>'
            f'<p class="sentiment-signal-direction">{icon(indicator)} {direction}</p>'
            '<div class="sentiment-signal-counts">'
            f"<div><span>Before Nov 4</span><strong>{before_critical}/{len(before)}</strong></div>"
            '<span aria-hidden="true">→</span>'
            f"<div><span>From Nov 4</span><strong>{after_critical}/{len(after)}</strong></div>"
            "</div></li>"
        )

    return (
        '<section class="sentiment-signals" aria-labelledby="sentiment-signals-title">'
        '<div><h4 id="sentiment-signals-title">Direct criticism by selection</h4>'
        '<p class="muted small">Posts with direct criticism / selected posts in each window</p></div>'
        f'<ul class="sentiment-signal-grid">{"".join(cards)}</ul>'
        f'<div class="sentiment-split">{icon("split")}'
        "<p><strong>Direction depends on selection.</strong> The pooled and rank-spaced comparisons move in opposite directions.</p>"
        "</div></section>"
    )


def render_sentiment(payload, records, excerpt):
    grouped = _selected_rows(payload, records)
    if not grouped or set(grouped) != {"baseline", "observation"}:
        return ""

    windows = {window["id"]: window for window in payload["scope"]["windows"]}

    def window_card(key, title):
        rows = grouped[key]
        total = len(rows)
        counts = Counter(label for _, label, _, _, _ in rows)
        bars, legend, groups = [], [], []
        for label, name in LABELS.items():
            count = counts[label]
            anchor = f"sentiment-{key}-{label}"
            if count:
                bars.append(
                    f'<a href="#{anchor}" class="sentiment-{label}" style="flex-grow:{count}" '
                    f'aria-label="{name}, {count} of {total}">{count}</a>'
                )
            legend.append(
                f'<li><span class="sentiment-{label}"></span>{name} <strong>{count}</strong></li>'
            )
            evidence = "".join(
                f"<li>{excerpt(ref)}<small>{moment.astimezone(NY).strftime('%b %d, %H:%M %Z')}"
                f" · {selection} · {escape(reason)}</small></li>"
                for moment, candidate, ref, reason, selection in sorted(
                    rows, key=lambda row: row[0]
                )
                if candidate == label
            )
            if evidence:
                groups.append(
                    f'<div id="{anchor}"><h5>{name} · {count}</h5><ul>{evidence}</ul></div>'
                )
        start = datetime.fromisoformat(windows[key]["start"])
        last = datetime.fromisoformat(windows[key]["end"]) - timedelta(days=1)
        return (
            f'<div class="sentiment-window"><h4>{title}</h4>'
            f'<p class="muted small">{start:%b %d, %Y} to {last:%b %d, %Y}</p>'
            f"<p><strong>{counts['critical']} of {total}</strong> directly criticized Mamdani</p>"
            f'<div class="sentiment-bar" role="group" aria-label="{title}">{"".join(bars)}</div>'
            f'<ul class="sentiment-key">{"".join(legend)}</ul>'
            f"<details><summary>Review the {total} saved posts</summary>{''.join(groups)}</details></div>"
        )

    baseline = len(grouped["baseline"])
    observation = len(grouped["observation"])

    return (
        '<article id="sentiment-shift" class="card sentiment-review" aria-labelledby="sentiment-review-title">'
        '<span class="eyebrow">Manual demo coding · sampled X posts</span>'
        '<h3 id="sentiment-review-title">Did the tone toward Mamdani shift?</h3>'
        f"<p>We compared all {baseline + observation} third-party X posts selected into this case. "
        "Each saved post counts once, using its publication date in New York. "
        "The coding tracks the author’s explicit stance toward Mamdani. "
        "Reported speech, another target, and ambiguous wording have separate treatment.</p>"
        + _render_criticism_signals(grouped)
        + '<div class="sentiment-windows">'
        + window_card("baseline", "Before Nov 4")
        + window_card("observation", "From Nov 4")
        + '</div><p class="sentiment-limit">The two windows contain different proportions of targeted leads. '
        "The collector’s mention coverage has gaps before October and after December 30. "
        "The direction of public sentiment remains unresolved. "
        "The manual codes are demo annotations for review.</p></article>"
    )
