"""Pure serialization of frozen specialist snapshots.

Renderers receive the complete saved payload and never consult current reviews,
media, or storage. Escaping, field order, and final newlines preserve the existing
JSON/Markdown export formats for both current and legacy snapshots.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any


def _markdown_text(value: Any) -> str:
    text = html.escape(str(value or ""), quote=False)
    return re.sub(r"([\\`*_{}\[\]#!|])", r"\\\1", text)


def json_snapshot(saved: dict) -> str:
    """Render the supplied frozen snapshot without adding or upgrading fields."""
    return json.dumps(saved, ensure_ascii=False, indent=2)


def markdown_snapshot(saved: dict) -> str:
    """Render the exact saved revision/history fields, including legacy omissions."""
    history = saved.get("review_history")
    history_note = (
        f"Saved review revisions preserved: {len(history['items'])} of {history['total']}; "
        f"history truncated: {str(history['truncated']).lower()}."
        if history is not None
        else "Review history was not included in this earlier snapshot; its extent is unknown."
    )
    lines = [
        f"# {_markdown_text(saved['title'])}",
        "",
        "Saved specialist snapshot.",
        "",
        "Reviewed is a saved human workflow status; it does not authenticate the media.",
        "",
        f"Snapshot author: {_markdown_text(saved['reviewer'])}",
        f"Saved at: {_markdown_text(saved['created_at'])}",
        f"Evidence origin: {_markdown_text(saved['source_origin'])}",
        f"Scenario: {_markdown_text(saved['scenario']['name'])} · version {saved['scenario']['version']}",
        f"Snapshot fingerprint: `{saved['fingerprint']}`",
        "",
        "Capture time, capture order, coordinates, original source URLs and unit identities "
        "are not supplied. Posting weeks describe publication context.",
        "",
    ]
    citations = {item["evidence_id"]: item for item in saved["citations"]}
    for index, note in enumerate(saved["included"], 1):
        lines.extend(
            [
                f"## {index} {_markdown_text(note['title'])}",
                "",
                f"Saved revision {note['revision']} · Reviewed · {_markdown_text(note['reviewer'])}",
                "",
            ]
        )
        for key, label in (
            ("observation", "Observation"),
            ("interpretation", "Interpretation"),
            ("uncertainty", "Uncertainty"),
            ("rationale", "Rationale"),
        ):
            lines.extend([f"### {label}", "", _markdown_text(note.get(key)) or "Not recorded.", ""])
        for evidence_id in note["evidence_ids"]:
            citation = citations[evidence_id]
            lines.append(
                f"- [{_markdown_text(evidence_id)}]({citation['href']}) · "
                f"{_markdown_text(citation['source_file'])}, line {citation['source_row']} · "
                f"clip {_markdown_text(citation['clip_id'])} · "
                f"video {_markdown_text(citation['video_id'])} · "
                f"SHA-256 `{citation['media_sha256']}`"
            )
        if note.get("origin"):
            origin = note["origin"]
            lines.extend(
                [
                    "",
                    f"Origin revision: {_markdown_text(origin['review_id'])} / "
                    f"{origin['revision']} · "
                    + (
                        "preserved in the JSON snapshot."
                        if origin.get("available")
                        else "origin unavailable; preserved as unknown."
                    ),
                ]
            )
        lines.append("")
    lines.extend(["## Omitted statements", ""])
    if not saved["omitted"]:
        lines.append("None.")
    for note in saved["omitted"]:
        lines.append(
            f"- {_markdown_text(note['title'])} · revision {note['revision']} · "
            f"{_markdown_text(note['status'])}: {_markdown_text('; '.join(note['reasons']))}"
        )
    lines.extend(
        [
            "",
            "## Preserved context",
            "",
            f"Exclusion events preserved: {len(saved['exclusion_events']['items'])} of "
            f"{saved['exclusion_events']['total']}.",
            f"Scenario lineage entries preserved: {len(saved['lineage']['items'])} of "
            f"{saved['lineage']['total']}.",
            history_note,
            "Local evidence links reopen in the configured workspace. This Markdown does not "
            "package media for another workspace.",
            "",
        ]
    )
    return "\n".join(lines)
