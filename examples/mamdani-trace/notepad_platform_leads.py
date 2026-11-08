"""Extract a bounded set of public X leads from the pinned Mamdani collector."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from investigation_icons import icon
from source_spans import SourceSpan

SNAPSHOT_SHA256 = "805a9fd8837304d40712ea137bfcebadb412de9a322650e91a3e89f536439df5"
PACKAGE_SHA256 = "46d60ba57b0144dfaa3ef9d5873fcadbf38cbb1c1ee99831c842e8f721eb340f"
SCHEMA = "traced.mamdani-notepad-platform-leads/1"
SIDECAR = "notepad-platform-leads.json"
NEW_YORK = ZoneInfo("America/New_York")
WINDOW_START = datetime(2025, 9, 4, tzinfo=NEW_YORK)
ELECTION = datetime(2025, 11, 4, tzinfo=NEW_YORK)
WINDOW_END = datetime(2026, 2, 4, tzinfo=NEW_YORK)


@dataclass(frozen=True)
class Lead:
    slug: str
    title: str
    status: str
    status_icon: str
    status_state: str
    excerpts: tuple[tuple[str, SourceSpan], ...]
    observation: str
    limit: str
    reference: tuple[str, str]


LEADS = (
    Lead(
        "work-history",
        "What does the work history support?",
        "Source found",
        "document",
        "found",
        (
            (
                "1983666249302470729",
                SourceSpan(
                    97, 118, "d96aabb30918165ac405b87be0ccd3c2d65b2344bfbf27549d82dcefda7e7eca"
                ),
            ),
        ),
        "The post pairs a claim about work history with wording about national origin. "
        "In testimony archived by the New York State Assembly, Mamdani described his work as a housing counselor.",
        "The testimony is a named firsthand account. The post’s definition of “real job” is subjective.",
        (
            "Housing testimony archived by the New York State Assembly",
            "https://nyassembly.gov/write/upload/publichearing/001103/002308.pdf",
        ),
    ),
    Lead(
        "victory-gesture",
        "Check a gesture allegation against the full speech",
        "Video review needed",
        "video",
        "review",
        (
            (
                "1985954646825836709",
                SourceSpan(
                    20, 43, "e5c855750812e004ec9c6e47f6df7c45990b53779f0e131fe1e5f07580653d7e"
                ),
            ),
        ),
        "The caption alleges four gestures and explicitly compares them with an earlier political controversy.",
        "The collector saved a video pointer. Assessing the count and interpretation requires the complete recording and surrounding frames.",
        (
            "Full victory speech from the campaign",
            "https://www.youtube.com/watch?v=5cG4WD8uEwE",
        ),
    ),
    Lead(
        "network-claim",
        "Separate religious framing from a funding allegation",
        "Quote parent missing",
        "question",
        "review",
        (
            (
                "1985974699856118267",
                SourceSpan(
                    41, 67, "05d9729430f8b09156504bda4a58f1c9c47a9733a74160c91229e22506e3f228"
                ),
            ),
        ),
        "The post frames Mamdani and an alleged funding network in religious terms. The collector recorded the quote target ID without the target text.",
        "A funding check requires named organizations and transaction-level evidence. The public disclosure summary is a starting point.",
        (
            "NYC Campaign Finance Board candidate disclosures",
            "https://fulldisclosure.nyccfb.info/VSApps/CandidateSummary.aspx?as_cand_id=2899&as_election_cycle=2025&cand_name=Mamdani%2C+Zohran+K&office=Mayor&report=summ",
        ),
    ),
    Lead(
        "personal-attack",
        "Trace a personal attack and its repetition",
        "Pattern unmeasured",
        "repeat",
        "review",
        (
            (
                "1982935233835241517",
                SourceSpan(
                    342, 379, "8b4fe8ed061d4b87e746e1461206f852de96b920a0d5c75afae21e31aa5a2b49"
                ),
            ),
            (
                "1983140085991362614",
                SourceSpan(
                    20, 44, "30bc675810debbf8f0146ba3d6b60fe6bbd2d3adf71ad0508046acebf3c87eb0"
                ),
            ),
        ),
        "The posts attack the candidate’s credibility over the same family anecdote. One saved post has a video pointer.",
        "The collector provides no direct link between these posts. Establishing a harassment pattern would require broader context and repeated targeting.",
        (
            "Recording from the October 24 speech",
            "https://abcnews.com/video/126844213/",
        ),
    ),
    Lead(
        "attributed-warning",
        "Separate the quoted speaker from the poster",
        "Speaker attribution to check",
        "quote",
        "context",
        (
            (
                "1985964693396324362",
                SourceSpan(
                    152, 182, "db00770ff3dea936260dea866abe08d86f07459a394407b759e2477031779355"
                ),
            ),
        ),
        "The post attributes a conditional warning to Curtis Sliwa and identifies Mamdani and his supporters as targets.",
        "Compare the caption with the original speech before assigning the wording or making a policy judgment.",
        (
            "Full concession speech recorded by ABC News",
            "https://abcnews.com/video/127201210/",
        ),
    ),
)

QUERY = """
SELECT c.id, c.platform_item_id, a.handle, c.posted_at, c.url, c.text,
       c.parent_item_id, c.is_quote,
       EXISTS(SELECT 1 FROM content_items p
              WHERE p.case_id = c.case_id AND p.platform = c.platform
                AND p.platform_item_id = c.parent_item_id) AS parent_saved,
       EXISTS(SELECT 1 FROM media_assets m
              WHERE m.content_item_id = c.id AND m.media_type = 'video') AS video_pointer
FROM content_items c
JOIN cases k ON k.id = c.case_id
JOIN authors a ON a.id = c.author_id
WHERE k.slug = 'mamdani-nyc-2025' AND c.platform = 'twitter'
  AND c.platform_item_id = ?
"""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _projection_digest(records: dict) -> str:
    return _digest(json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _local_time(timestamp: str) -> datetime:
    moment = datetime.fromisoformat(timestamp)
    if moment.tzinfo is None or not WINDOW_START <= moment < WINDOW_END:
        raise ValueError("Lead timestamp is outside the declared election windows")
    return moment.astimezone(NEW_YORK)


def _valid_locator(url: str, handle: str, native_id: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "x.com"
        and parsed.path.casefold() == f"/{handle}/status/{native_id}".casefold()
        and not (parsed.query or parsed.fragment)
    )


def _extract(connection: sqlite3.Connection, native_id: str, span: SourceSpan) -> dict:
    row = connection.execute(QUERY, (native_id,)).fetchone()
    if row is None:
        raise ValueError(f"Collector row missing for X post {native_id}")
    text = row["text"]
    excerpt = span.extract(text or "")
    if excerpt is None or text.count(excerpt) != 1:
        raise ValueError(f"Exact excerpt does not uniquely match X post {native_id}")
    handle = row["handle"]
    if not handle or not _valid_locator(row["url"], handle, native_id):
        raise ValueError(f"Unexpected public post locator for X post {native_id}")
    moment = _local_time(row["posted_at"])
    return {
        "collector_item_id": row["id"],
        "source_url": row["url"],
        "author_handle": handle,
        "posted_at": row["posted_at"],
        "window": "before" if moment < ELECTION else "from",
        "excerpt": excerpt,
        "excerpt_start": span.start,
        "excerpt_end": span.end,
        "excerpt_sha256": span.sha256,
        "text_sha256": _digest(text),
        "quoted_parent_id": row["parent_item_id"] if row["is_quote"] else None,
        "quoted_parent_in_snapshot": bool(row["is_quote"] and row["parent_saved"]),
        "video_pointer": bool(row["video_pointer"]),
    }


def extract_platform_leads(snapshot: Path, output: Path) -> None:
    """Read only the selected source rows and write an evidence projection."""
    if _file_digest(snapshot) != SNAPSHOT_SHA256:
        raise ValueError("Collector snapshot does not match the pinned SHA256")
    if output.resolve() == snapshot.resolve():
        raise ValueError("Sidecar path cannot replace the collector snapshot")
    with closing(
        sqlite3.connect(f"{snapshot.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        records = {
            native_id: _extract(connection, native_id, span)
            for lead in LEADS
            for native_id, span in lead.excerpts
        }
    if _file_digest(snapshot) != SNAPSHOT_SHA256:
        raise ValueError("Collector snapshot changed during extraction")
    data = {
        "schema": SCHEMA,
        "snapshot_sha256": SNAPSHOT_SHA256,
        "package_sha256": PACKAGE_SHA256,
        "records_sha256": _projection_digest(records),
        "records": records,
    }
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)


def _render_record(native_id: str, span: SourceSpan, record: dict) -> str:
    excerpt = record["excerpt"]
    if (
        record["excerpt_start"] != span.start
        or record["excerpt_end"] != span.end
        or record["excerpt_sha256"] != span.sha256
        or not span.matches(excerpt)
        or not _valid_locator(record["source_url"], record["author_handle"], native_id)
    ):
        raise ValueError(f"Invalid evidence projection for X post {native_id}")
    moment = _local_time(record["posted_at"])
    window = "Before Election Day" if moment < ELECTION else "From Election Day"
    media = " · Video pointer only" if record["video_pointer"] else ""
    parent = (
        f'<p class="small muted">Collector quote parent <code>{html.escape(record["quoted_parent_id"])}</code> is absent from the snapshot.</p>'
        if record["quoted_parent_id"] and not record["quoted_parent_in_snapshot"]
        else ""
    )
    return (
        '<div class="db-trace-item">'
        f'<p class="small"><a href="{html.escape(record["source_url"], quote=True)}">'
        f"@{html.escape(record['author_handle'])} · X post {native_id}</a></p>"
        f'<p class="small muted"><time datetime="{html.escape(moment.isoformat(), quote=True)}">'
        f"{moment:%b %d, %Y %H:%M %Z}</time> · {window}{media}</p>"
        f"<blockquote>{html.escape(excerpt)}</blockquote>{parent}</div>"
    )


def render_platform_leads(archive: Path, payload: dict, *, parent_html: str = "") -> str:
    """Render case-bound supplemental leads without changing the comparison sample."""
    sidecar = archive.with_name(SIDECAR)
    if not sidecar.exists():
        return ""
    if sidecar.stat().st_size > 64_000:
        raise ValueError("Notepad platform evidence exceeds the sidecar size limit")
    data = json.loads(sidecar.read_bytes())
    binding = payload["binding"]
    if (
        data["schema"] != SCHEMA
        or data["snapshot_sha256"] != SNAPSHOT_SHA256
        or binding["snapshot_sha256"] != SNAPSHOT_SHA256
        or data["package_sha256"] != PACKAGE_SHA256
        or binding["package_sha256"] != PACKAGE_SHA256
        or _file_digest(archive) != PACKAGE_SHA256
        or data["records_sha256"] != _projection_digest(data["records"])
    ):
        raise ValueError("Notepad platform evidence does not match the saved case")
    cards = []
    for lead in LEADS:
        examples = "".join(
            _render_record(native_id, span, data["records"][native_id])
            for native_id, span in lead.excerpts
        )
        label, url = lead.reference
        supplemental_parent = parent_html if lead.slug == "network-claim" else ""
        if supplemental_parent:
            status, status_icon, status_state = (
                "Quote parent recovered",
                "link",
                "found",
            )
        else:
            status, status_icon, status_state = (
                lead.status,
                lead.status_icon,
                lead.status_state,
            )
        cards.append(
            f'<article id="notepad-{lead.slug}" class="card db-lane db-review-scenario">'
            f'<span class="badge signal-chip" data-state="{status_state}">{icon(status_icon)}'
            f"{html.escape(status)}</span>"
            f"<h4>{html.escape(lead.title)}</h4>"
            f"<p>{html.escape(lead.observation)}</p>{examples}{supplemental_parent}"
            f'<p class="lead-review-cue"><strong>Review cue</strong> · {html.escape(lead.limit)}</p>'
            f'<p><a href="{html.escape(url, quote=True)}">{html.escape(label)}</a></p></article>'
        )
    return (
        '<section id="notepad-platform-leads" class="db-scenarios" aria-labelledby="notepad-platform-leads-title">'
        '<div><h3 id="notepad-platform-leads-title">From the shared notepad to X</h3>'
        '<p class="muted">Five questions traced to six exact posts in the saved X collector. '
        "These supplemental leads sit outside the 14-post sentiment comparison.</p></div>"
        f'<div class="db-trace-grid">{"".join(cards)}</div>'
        '<p class="muted small">For a policy review, identify the speaker, target, protected trait if relevant, '
        "and context. Check for repeated targeting before judging a pattern. "
        '<a href="https://help.x.com/en/rules-and-policies/hateful-conduct-policy">X hateful conduct policy</a> · '
        '<a href="https://help.x.com/en/rules-and-policies/abusive-behavior">X abuse and harassment policy</a></p>'
        "</section>"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    extract_platform_leads(args.snapshot, args.out)
