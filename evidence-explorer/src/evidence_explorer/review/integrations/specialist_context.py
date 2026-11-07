"""Read-only specialist provenance, separate from current supporting records.

Only an explicit typed context pointer is followed. This renderer does not
create support links, select a global source, or expose decision/edit controls.
"""

from __future__ import annotations

import json
from typing import Any

from dash import html

from ..presentation import source_inspector

MAX_CONTEXT_BYTES = 2 * 1024 * 1024
READ_ONLY_NOTICE = "Read only history/context, not current supporting evidence or shared approval."


def _reference(value: Any, kind: str) -> str | None:
    if not isinstance(value, dict) or value.get("type") != kind:
        return None
    ident = value.get("id")
    return ident if isinstance(ident, str) and ident else None


def _unavailable(reason: str) -> html.Div:
    return html.Div(
        [
            html.Strong("Saved specialist history/context unavailable"),
            html.P(reason),
            html.P(READ_ONLY_NOTICE, className="rv-small rv-muted"),
        ],
        className="rv-notice",
        role="status",
    )


def _retained(snapshot: dict, key: str, label: str) -> html.Li:
    section = snapshot.get(key)
    if not isinstance(section, dict) or not isinstance(section.get("items"), list):
        return html.Li(f"{label}: not recorded in this snapshot.")
    retained = len(section["items"])
    total = section.get("total")
    if type(total) is int and total >= retained:
        total_text = f"at least {total}" if section.get("total_is_exact") is False else str(total)
        text = f"{label}: {retained} retained of {total_text}."
    else:
        text = f"{label}: {retained} retained; total not recorded."
    truncated = section.get("truncated")
    if truncated is True:
        text += " Truncated to the declared selection limit."
    elif truncated is False:
        text += " Not truncated within this selection."
    else:
        text += " Truncation not recorded."
    if section.get("origin_unavailable") is True:
        text += " Earlier origin is outside this package."
    return html.Li(text)


def specialist_history_context(bundle: dict, finding: dict) -> html.Section | html.Div | str:
    """Render one explicit saved context record in an inline disclosure.

    Source inspection is reused as a pure presentation function, not routed
    through the global selected record. Opening the disclosure cannot change
    reviewer/exclusion context or make the historical record current support.
    """
    details = finding.get("details")
    context = details.get("specialist_history_context") if isinstance(details, dict) else None
    if context is None:
        return ""
    if not isinstance(context, dict) or context.get("read_only") is not True:
        return _unavailable("A valid read-only context declaration was not supplied.")
    record_id = _reference(context.get("record_ref"), "record")
    fragment_id = _reference(context.get("fragment_ref"), "fragment")
    if record_id is None or fragment_id is None:
        return _unavailable("The saved record and fragment references are not correctly typed.")
    record = next((item for item in bundle.get("records", []) if item.get("id") == record_id), None)
    fragment = next(
        (item for item in bundle.get("fragments", []) if item.get("id") == fragment_id), None
    )
    if record is None or record.get("kind") != "specialist_brief_snapshot":
        return _unavailable("The declared specialist snapshot record is absent from this package.")
    if fragment is None or fragment.get("record_id") != record_id:
        return _unavailable("The context fragment does not resolve to the declared saved record.")
    text = record.get("text")
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_CONTEXT_BYTES:
        return _unavailable("The saved context is missing or exceeds the 2 MiB display limit.")
    selector = fragment.get("selector") or {}
    if (
        selector.get("kind") != "text"
        or selector.get("start") != 0
        or selector.get("end") != len(text)
        or fragment.get("quote") != text
    ):
        return _unavailable("The declared fragment is not the exact complete saved context text.")
    try:
        snapshot = json.loads(text)
    except (ValueError, TypeError):
        snapshot = None
    if isinstance(snapshot, dict):
        summary = html.Ul(
            [
                _retained(snapshot, "review_history", "Saved specialist revisions"),
                _retained(snapshot, "lineage", "Scenario lineage"),
                _retained(snapshot, "exclusion_events", "Source exclusion/restoration events"),
            ],
            className="rv-small rv-muted",
        )
    else:
        summary = html.P(
            "History totals are not available as structured snapshot fields. "
            "The exact saved record remains available below.",
            className="rv-small rv-muted",
        )
    label = context.get("label")
    label = (
        label
        if isinstance(label, str) and label.strip()
        else "Saved specialist history and context"
    )
    purpose = context.get("purpose")
    limitation = context.get("limitation")
    return html.Section(
        [
            html.H3("Specialist history and context"),
            html.P(READ_ONLY_NOTICE, className="rv-small rv-muted"),
            html.P(purpose, className="rv-small") if isinstance(purpose, str) else None,
            summary,
            html.Details(
                [
                    html.Summary(label),
                    html.P(
                        "This is the producer's immutable saved context. "
                        "The current finding and selected source remain unchanged.",
                        className="rv-small rv-muted",
                    ),
                    source_inspector(bundle, record_id, None, []),
                    html.P(limitation, className="rv-small rv-muted")
                    if isinstance(limitation, str)
                    else None,
                ],
                className="rv-details",
                open=False,
            ),
        ],
        className="rv-specialist-context",
        **{"aria-label": "Read-only specialist history and context"},
    )
