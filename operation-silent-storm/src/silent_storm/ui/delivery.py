"""Bounded release comparison and immutable, cited brief delivery.

Uses the existing Traced specialist shell, panels, inspector and typography.
Layouts perform no service calls. All supplied text is rendered as escaped Dash
text; frozen Markdown is shown in a preformatted text block, never raw HTML.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import math
import re
from typing import Any
from urllib.parse import quote, urlencode

from dash import Dash, Input, Output, State, ctx, dcc, html, no_update

from ..contracts import DEFAULT_PAGE_SIZE, ExplorerStore, ReviewServiceProtocol
from .components import empty_state, evidence_inspector, number, section_heading, supplied
from .review import berlin_stamp, history_card

LOGGER = logging.getLogger(__name__)
ORIGINS = ("unspecified", "real", "synthetic")
MAX_REOPEN_BYTES = 2 * 1024 * 1024
DIFF_CATEGORIES = {
    "all": "All frame ID changes",
    "added": "Added frame IDs",
    "removed": "Removed frame IDs",
    "metadata_changed": "Changed metadata",
    "unchanged": "Unchanged metadata",
}
MEDIA_LABELS = {
    "same_bytes": "Recorded checksum match",
    "different_bytes": "Recorded checksums differ",
    "unresolved": "Identity unresolved",
    "one_sided": "One release only",
}


def _button(label: str, key: str, *, primary: bool = False, disabled: bool = False):
    return html.Button(
        label,
        id=key,
        n_clicks=0,
        disabled=disabled,
        className="ss-button ss-primary" if primary else "ss-button ss-secondary",
    )


def _picker(label: str, key: str, *, multi: bool = False, options=None, value=None):
    return html.Div(
        [
            html.Label(label, htmlFor=key),
            dcc.Dropdown(
                id=key,
                options=options or [],
                value=value,
                multi=multi,
                clearable=multi,
                className="ss-dropdown",
            ),
        ],
        className="ss-d-field",
    )


def _pager(prefix: str):
    return html.Div(
        [
            _button("Previous page", f"ss-d-{prefix}-previous", disabled=True),
            html.Span(
                "No records", id=f"ss-d-{prefix}-pagination", className="ss-pagination-label"
            ),
            _button("Next page", f"ss-d-{prefix}-next", disabled=True),
        ],
        className="ss-pagination",
    )


def create_delivery_layout(
    store: ExplorerStore, reviews: ReviewServiceProtocol, briefs: Any, diffs: Any
) -> html.Div:
    """Always-mounted third panel, with no implicit imports or baseline creation."""
    return html.Div(
        [
            dcc.Store(id="ss-d-diff-page", data=1),
            dcc.Store(id="ss-d-note-page", data=1),
            dcc.Store(id="ss-d-snapshot-page", data=1),
            dcc.Store(id="ss-d-preview-token"),
            dcc.Store(id="ss-d-created-brief"),
            html.Section(
                [
                    section_heading(
                        "RELEASE COMPARISON",
                        "What changed in the supplied records",
                        "Compare two completed local imports. Additional footage is not automatically a new event.",
                    ),
                    html.Div(
                        [
                            _picker("Baseline release", "ss-d-baseline"),
                            _picker("Current release", "ss-d-current"),
                            _picker(
                                "Record category",
                                "ss-d-category",
                                options=[
                                    {"label": label, "value": key}
                                    for key, label in DIFF_CATEGORIES.items()
                                ],
                                value="all",
                            ),
                        ],
                        className="ss-d-controls",
                    ),
                    html.Div(id="ss-d-release-status", role="status", className="ss-d-feedback"),
                    html.Div(id="ss-d-summary"),
                    html.Div(id="ss-d-hash-coverage"),
                    html.Div(id="ss-d-correspondence", className="ss-d-note"),
                    _pager("diff"),
                    dcc.RadioItems(
                        id="ss-d-diff-select",
                        options=[],
                        value=None,
                        className="ss-d-options",
                        labelClassName="ss-choice",
                    ),
                    html.Details(
                        [
                            html.Summary("Inspect selected comparison record"),
                            html.Div(id="ss-d-diff-detail"),
                        ],
                        className="ss-d-diff-disclosure",
                    ),
                ],
                className="ss-panel",
            ),
            html.Section(
                [
                    section_heading(
                        "CITED BRIEF",
                        "Select saved statements",
                        "A new snapshot includes only current saved Reviewed statements with valid, nonexcluded evidence. "
                        "Reviewed is a human wording assessment, not authentication.",
                    ),
                    html.Div(
                        [
                            _picker("Existing review scenario", "ss-d-scenario"),
                            _button("Open reviews", "ss-d-open-reviews"),
                        ],
                        className="ss-d-controls",
                    ),
                    html.Div(id="ss-d-scenario-status", className="ss-d-feedback", role="status"),
                    _pager("notes"),
                    _picker(
                        "Saved statements — select up to 12 on this page",
                        "ss-d-notes",
                        multi=True,
                        value=[],
                    ),
                    html.P(
                        "Changing the notes page keeps only selections on the new page. "
                        "The brief supports at most 12 notes and 12 unique cited frames.",
                        className="ss-d-note",
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Label("Brief title", htmlFor="ss-d-title"),
                                    dcc.Input(
                                        id="ss-d-title",
                                        value="",
                                        type="text",
                                        maxLength=300,
                                        className="ss-form-input",
                                    ),
                                ],
                                className="ss-d-field",
                            ),
                            html.Div(
                                [
                                    html.Label("Acting author / reviewer", htmlFor="ss-d-author"),
                                    dcc.Input(
                                        id="ss-d-author",
                                        value="",
                                        type="text",
                                        maxLength=150,
                                        className="ss-form-input",
                                    ),
                                ],
                                className="ss-d-field",
                            ),
                            _picker(
                                "Evidence origin (explicit declaration)",
                                "ss-d-origin",
                                options=[
                                    {"label": "Unspecified", "value": "unspecified"},
                                    {"label": "Real source material", "value": "real"},
                                    {"label": "Synthetic material", "value": "synthetic"},
                                ],
                                value="unspecified",
                            ),
                        ],
                        className="ss-d-controls",
                    ),
                    html.P(
                        "Origin is declared by the author and preserved in the snapshot. "
                        "It is not inferred from titles or established by the Reviewed label.",
                        className="ss-d-note",
                    ),
                    html.Div(
                        [
                            _button("Preview included and omitted statements", "ss-d-preview"),
                            _button(
                                "Finalize cited snapshot",
                                "ss-d-finalize",
                                primary=True,
                                disabled=True,
                            ),
                        ],
                        className="ss-d-actions",
                    ),
                    html.Div(id="ss-d-preview-view", className="ss-d-feedback", role="status"),
                    html.Div(id="ss-d-create-status", className="ss-d-feedback", role="status"),
                ],
                className="ss-panel",
            ),
            html.Section(
                [
                    section_heading(
                        "SAVED SNAPSHOTS",
                        "Frozen briefs and current validity",
                        "Saved wording, citations, omissions and origin remain immutable. "
                        "Current validity is checked separately.",
                    ),
                    _button("Refresh snapshots and validity", "ss-d-refresh-snapshots"),
                    dcc.Upload(
                        id="ss-d-reopen",
                        children=_button("Reopen saved snapshot JSON", "ss-d-reopen-label"),
                        accept=".json,application/json",
                        multiple=False,
                        max_size=MAX_REOPEN_BYTES,
                        className="ss-d-upload",
                    ),
                    html.P(
                        "Reopening verifies an exact saved snapshot in this workspace. It does not "
                        "import notes or evidence. Maximum JSON size: 2 MiB.",
                        className="ss-d-note",
                    ),
                    html.Div(id="ss-d-reopen-status", className="ss-d-feedback", role="status"),
                    _pager("snapshots"),
                    _picker("Saved brief", "ss-d-snapshot"),
                    html.Div(id="ss-d-snapshot-status", className="ss-d-feedback", role="status"),
                    html.Div(id="ss-d-snapshot-view"),
                ],
                className="ss-panel",
            ),
        ],
        id="ss-delivery-panel",
        className="ss-d-panel",
        style={"display": "none"},
    )


def _page(value: Any) -> int:
    try:
        return max(1, min(int(value or 1), 1_000_000))
    except (ValueError, TypeError, OverflowError):
        return 1


def _form_fingerprint(title: Any, author: Any, origin: Any) -> str:
    return hashlib.sha256(
        json.dumps([title, author, origin], ensure_ascii=False).encode()
    ).hexdigest()


def evidence_link(
    release_id: Any, frame_id: Any, label: str | None = None, digest: str | None = None
):
    """Only constructed, same-origin inspector URLs; supplied URLs are not links."""
    if release_id is None or frame_id is None or str(frame_id) in ("", ".", ".."):
        return html.Code(label or "Evidence reference unavailable")
    href = f"/evidence/{quote(str(release_id), safe='')}/{quote(str(frame_id), safe='')}"
    if digest is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(digest)):
            return html.Code(label or f"{release_id}:{frame_id} — snapshot checksum unavailable")
        href = "/evidence?" + urlencode(
            {"release_id": str(release_id), "frame_id": str(frame_id), "sha256": str(digest)},
            quote_via=quote,
        )
    return html.A(
        label or f"{release_id}:{frame_id}",
        href=href,
        target="_blank",
        rel="noopener noreferrer",
        className="ss-d-citation",
    )


def _reference_link(reference: str, release_id: str, label: str | None = None):
    prefix = f"{release_id}:"
    return (
        evidence_link(release_id, reference[len(prefix) :], label)
        if reference.startswith(prefix)
        else html.Code(reference)
    )


def citation_links(record: dict[str, Any], release_id: str | None = None, *, frozen: bool = False):
    release_id = record.get("release_id") or release_id
    citations = record.get("citations") or record.get("evidence") or []
    links = []
    for citation in citations:
        if isinstance(citation, dict):
            rid = citation.get("release_id") or release_id
            frame_id = citation.get("frame_id")
            if rid is not None and frame_id is not None:
                digest = (
                    citation.get("sha256")
                    or citation.get("media_sha256")
                    or citation.get("file_sha256")
                )
                links.append(
                    evidence_link(rid, frame_id, digest=str(digest or "") if frozen else None)
                )
            elif rid and citation.get("evidence_id") and not frozen:
                links.append(_reference_link(str(citation["evidence_id"]), str(rid)))
    if not links and release_id and not frozen:
        links = [
            _reference_link(str(key), str(release_id)) for key in record.get("evidence_ids", [])
        ]
    return html.Div(
        links or [html.Span("No supplied citation references.")], className="ss-d-citations"
    )


def statement_card(
    record: dict[str, Any],
    release_id: str | None = None,
    frozen_citations: list[dict[str, Any]] | None = None,
):
    links = citation_links(record, release_id)
    if frozen_citations is not None:
        references = set(record.get("evidence_ids") or [])
        selected = [
            citation
            for citation in frozen_citations
            if citation.get("evidence_id") in references
            or f"{citation.get('release_id')}:{citation.get('frame_id')}" in references
            or citation.get("citation_id") in set(record.get("citation_ids") or [])
        ]
        links = citation_links({"citations": selected}, release_id, frozen=True)
    return html.Article(
        [
            html.H3(str(record.get("title") or record.get("review_id") or "Saved statement")),
            html.P(
                f"Review revision {record.get('revision', 'Not supplied')} · "
                f"{record.get('reviewer', 'Not supplied')}",
                className="ss-d-note",
            ),
            *[
                html.Div([html.Strong(label), html.P(str(record[key]), className="ss-d-text")])
                for key, label in (
                    ("observation", "What is visible"),
                    ("interpretation", "Interpretation"),
                    ("uncertainty", "Uncertainty"),
                    ("rationale", "Review rationale"),
                )
                if record.get(key)
            ],
            links,
        ],
        className="ss-d-statement",
    )


def _omissions(items: list[dict[str, Any]]):
    return html.Div(
        [
            html.H3(f"Omitted statements · {len(items)}"),
            html.Ul(
                [
                    html.Li(
                        [
                            html.Strong(
                                str(item.get("title") or item.get("review_id") or "Statement")
                            ),
                            html.P(" ".join(str(reason) for reason in item.get("reasons", []))),
                        ]
                    )
                    for item in items
                ]
            )
            if items
            else html.P("No selected statements were omitted."),
        ],
        className="ss-d-omissions",
    )


def _snapshot_history(history: dict[str, Any] | None):
    if history is None:
        return html.P("This earlier snapshot has no saved revision history.", className="ss-d-note")
    items = history["items"]
    return html.Details(
        [
            html.Summary("Saved review history · read only"),
            html.P(
                f"{len(items)} of {history['total']} revisions captured at finalization. "
                "The history may include superseded or omitted statements."
                + (
                    " This snapshot contains a bounded subset of the history."
                    if history["truncated"]
                    else ""
                ),
                className="ss-d-note",
            ),
            *[history_card(item, show_title=True) for item in items],
        ],
    )


def validity_note(note: Any):
    if not isinstance(note, dict):
        return html.P(str(note))
    title = str(
        note.get("title") or note.get("review_id") or note.get("evidence_id") or "Saved item"
    )
    current = note.get("valid", note.get("is_current"))
    if current is None and note.get("current_revision") is not None:
        current = note.get("current_revision") == note.get("saved_revision", note.get("revision"))
    state = (
        "Current"
        if current is True
        else "Changed or unavailable"
        if current is False
        else str(note.get("status") or "Current status not supplied")
    )
    reasons = " ".join(str(item) for item in note.get("reasons") or [])
    return html.P(f"{title} · {state}" + (f" · {reasons}" if reasons else ""))


def preview_view(result: dict[str, Any], origin: str):
    scenario = result.get("scenario") or {}
    included, omitted = result.get("included") or [], result.get("omitted") or []
    return html.Div(
        [
            html.Strong(
                f"{len(included)} included · {len(omitted)} omitted · "
                f"scenario version {result.get('expected_version')}"
            ),
            html.P(
                f"Declared origin: {origin}. Capture time and geographic location are not supplied.",
                className="ss-d-note",
            ),
            *[statement_card(item, scenario.get("release_id")) for item in included],
            _omissions(omitted),
            html.Ul(
                [html.Li(str(warning)) for warning in result.get("warnings") or []],
                className="ss-d-note",
            ),
            html.P(
                "Finalization rechecks current revisions, exclusions and cited media bytes. "
                "Changes since this preview require a new preview.",
                className="ss-d-note",
            ),
        ]
    )


def _coverage_view(result: dict[str, Any]):
    comparison = result.get("hash_comparison") or {}
    coverage = comparison.get("recorded_coverage") or {}
    rows = []
    for side, label in (("baseline", "Baseline"), ("current", "Current")):
        data = coverage.get(side) or {}
        rows.append(
            html.P(
                f"{label}: {number(data.get('hashed'))} / {number(data.get('total'))} "
                f"recorded hashes · {number(data.get('pending'))} pending · "
                f"{number(data.get('failed'))} failed"
            )
        )
    rows.append(
        html.P(
            str(
                comparison.get("scope_note")
                or "Recorded hash coverage is distinct from current media identity."
            ),
            className="ss-d-note",
        )
    )
    rows.append(
        html.P(
            f"Recorded candidate pairs across IDs: "
            f"{number(comparison.get('recorded_candidate_pair_count_across_ids'))}. "
            f"Unchecked records: {number(comparison.get('unchecked_record_count'))}.",
            className="ss-d-note",
        )
    )
    before_id = str((result.get("baseline") or {}).get("release_id") or "")
    after_id = str((result.get("current") or {}).get("release_id") or "")
    for item in comparison.get("stat_checked_examples") or []:
        rows.append(
            html.Div(
                [
                    _reference_link(
                        str(item.get("baseline_evidence_id") or ""), before_id, "Baseline candidate"
                    ),
                    html.Span(" ↔ "),
                    _reference_link(
                        str(item.get("current_evidence_id") or ""), after_id, "Current candidate"
                    ),
                    html.P(
                        str(item.get("note") or item.get("status") or "Unresolved"),
                        className="ss-d-note",
                    ),
                ],
                className="ss-d-hash-example",
            )
        )
    return html.Details(
        [html.Summary("Hash coverage and bounded candidate checks"), *rows],
        className="ss-d-coverage",
    )


def _diff_detail(item: dict[str, Any], baseline_id: str, current_id: str, reveal: bool):
    comparison = item.get("media_comparison") or {}
    return html.Div(
        [
            html.H3(
                f"Frame ID {item.get('frame_id')} · {DIFF_CATEGORIES.get(item.get('category'), item.get('category'))}"
            ),
            html.P(
                str(
                    comparison.get("note")
                    or comparison.get("status")
                    or "Media identity unresolved"
                ),
                className="ss-d-note",
            ),
            html.P(
                str(comparison.get("basis") or "No media identity basis supplied."),
                className="ss-d-note",
            ),
            html.Ul(
                [
                    html.Li(
                        f"{change['field']}: {supplied(change.get('before'))} → "
                        f"{supplied(change.get('after'))}"
                    )
                    for change in item.get("metadata_changes", [])
                ]
            ),
            html.Div(
                [
                    html.Article(
                        [
                            html.H3(label),
                            evidence_link(
                                release_id, frame["frame_id"], "Open exact supplied record"
                            ),
                            evidence_inspector(frame, release_id, reveal),
                        ]
                        if frame
                        else [html.H3(label), html.P("No corresponding frame ID in this release.")]
                    )
                    for label, release_id, frame in (
                        ("Baseline record", baseline_id, item.get("before")),
                        ("Current record", current_id, item.get("after")),
                    )
                ],
                className="ss-d-record-pair",
            ),
        ]
    )


def register_delivery_callbacks(
    app: Dash, store: ExplorerStore, reviews: ReviewServiceProtocol, briefs: Any, diffs: Any
) -> None:
    """Acyclic selectors and explicit preview/finalize actions; no form mutation on errors."""

    @app.callback(
        Output("ss-d-baseline", "options"),
        Output("ss-d-baseline", "value"),
        Output("ss-d-baseline", "disabled"),
        Output("ss-d-current", "options"),
        Output("ss-d-current", "value"),
        Output("ss-d-current", "disabled"),
        Output("ss-d-release-status", "children"),
        Input("ss-release", "value"),
        Input("ss-refresh", "n_clicks"),
        Input("ss-view", "value"),
        State("ss-d-baseline", "value"),
        State("ss-d-current", "value"),
    )
    def delivery_releases(release_id, _refresh, _view, baseline, current):
        try:
            items = store.list_releases()
            options = [
                {"label": str(item.get("title") or item["release_id"]), "value": item["release_id"]}
                for item in items
            ]
            ids = [item["value"] for item in options]
            current = (
                current
                if current in ids
                else release_id
                if release_id in ids
                else ids[0]
                if ids
                else None
            )
            baseline = (
                baseline if baseline in ids else next((key for key in ids if key != current), None)
            )
            disabled = len(ids) < 2
            message = (
                "Two completed releases are required. No later release has been invented."
                if disabled
                else "Comparison aligns supplied frame IDs. Review approval is never transferred automatically."
            )
            return options, baseline, disabled, options, current, disabled, message
        except Exception:
            LOGGER.exception("Could not list delivery releases")
            return (
                [],
                None,
                True,
                [],
                None,
                True,
                "Release list unavailable. Refresh after checking the local imports.",
            )

    @app.callback(
        Output("ss-d-diff-select", "options"),
        Output("ss-d-summary", "children"),
        Output("ss-d-hash-coverage", "children"),
        Output("ss-d-correspondence", "children"),
        Output("ss-d-diff-pagination", "children"),
        Output("ss-d-diff-previous", "disabled"),
        Output("ss-d-diff-next", "disabled"),
        Output("ss-d-diff-page", "data"),
        Input("ss-d-baseline", "value"),
        Input("ss-d-current", "value"),
        Input("ss-d-category", "value"),
        Input("ss-d-diff-previous", "n_clicks"),
        Input("ss-d-diff-next", "n_clicks"),
        Input("ss-refresh", "n_clicks"),
        State("ss-d-diff-page", "data"),
    )
    def delivery_diff(baseline, current, category, _previous, _next, _refresh, saved_page):
        if not baseline or not current or baseline == current:
            return (
                [],
                "Choose two different completed releases.",
                None,
                None,
                "No comparison",
                True,
                True,
                1,
            )
        page = (
            _page(saved_page) if ctx.triggered_id in {"ss-d-diff-previous", "ss-d-diff-next"} else 1
        )
        page = max(
            1,
            page
            + (
                1
                if ctx.triggered_id == "ss-d-diff-next"
                else -1
                if ctx.triggered_id == "ss-d-diff-previous"
                else 0
            ),
        )
        try:
            result = diffs.compare(
                baseline, current, category=category, page=page, page_size=DEFAULT_PAGE_SIZE
            )
            pages = max(1, math.ceil(result["total"] / DEFAULT_PAGE_SIZE))
            if page > pages:
                page = pages
                result = diffs.compare(
                    baseline, current, category=category, page=page, page_size=DEFAULT_PAGE_SIZE
                )
            options = [
                {
                    "label": f"{DIFF_CATEGORIES.get(item['category'], item['category'])} · "
                    f"{item['frame_id']} · {MEDIA_LABELS.get((item.get('media_comparison') or {}).get('status'), 'Identity unresolved')}",
                    "value": str(item["frame_id"]),
                }
                for item in result["items"]
            ]
            summary = result.get("summary") or {}
            counts = html.Div(
                [
                    html.Span(f"{label}: {number(summary.get(key))}")
                    for key, label in DIFF_CATEGORIES.items()
                    if key != "all"
                ],
                className="ss-d-summary",
            )
            return (
                options,
                counts,
                _coverage_view(result),
                result.get("correspondence_note"),
                f"Page {page} of {pages} · {number(result['total'])} frame IDs",
                page <= 1,
                page >= pages,
                page,
            )
        except (ValueError, LookupError) as exc:
            return [], str(exc), None, None, "Comparison unavailable", True, True, 1
        except Exception:
            LOGGER.exception("Could not compare releases")
            return (
                [],
                "Release comparison failed. Refresh the local imports.",
                None,
                None,
                "Comparison unavailable",
                True,
                True,
                1,
            )

    @app.callback(
        Output("ss-d-diff-select", "value"),
        Input("ss-d-diff-select", "options"),
        State("ss-d-diff-select", "value"),
    )
    def delivery_diff_selection(options, current):
        values = [item["value"] for item in options or []]
        return current if current in values else values[0] if values else None

    @app.callback(
        Output("ss-d-diff-detail", "children"),
        Input("ss-d-diff-select", "value"),
        Input("ss-d-baseline", "value"),
        Input("ss-d-current", "value"),
        Input("ss-d-category", "value"),
        Input("ss-d-diff-page", "data"),
        Input("ss-reveal-previews", "value"),
    )
    def delivery_diff_detail(frame_id, baseline, current, category, page, reveal):
        if not frame_id or not baseline or not current or baseline == current:
            return empty_state(
                "No comparison record selected",
                "Import a second release to inspect actual record changes.",
            )
        try:
            result = diffs.compare(
                baseline, current, category=category, page=_page(page), page_size=DEFAULT_PAGE_SIZE
            )
            item = next(
                (item for item in result["items"] if str(item["frame_id"]) == str(frame_id)), None
            )
            return (
                _diff_detail(item, baseline, current, "reveal" in (reveal or []))
                if item
                else empty_state(
                    "Choose a current comparison record",
                    "The previous selection is outside this result page.",
                )
            )
        except (ValueError, LookupError) as exc:
            return empty_state("Comparison detail unavailable", str(exc), error=True)

    @app.callback(
        Output("ss-d-scenario", "options"),
        Output("ss-d-scenario", "value"),
        Output("ss-d-scenario-status", "children"),
        Input("ss-release", "value"),
        Input("ss-view", "value"),
        Input("ss-r-refresh", "data"),
        State("ss-d-scenario", "value"),
        State("ss-r-scenario", "value"),
    )
    def delivery_scenarios(release_id, _view, _tick, current, review_scenario):
        if not release_id:
            return [], None, "Import a release and save reviews first."
        try:
            items = reviews.list_scenarios(release_id)
            options = [
                {
                    "label": f"{item['name']} · {'baseline' if item['is_baseline'] else 'fork'}",
                    "value": item["scenario_id"],
                }
                for item in items
            ]
            values = {item["value"] for item in options}
            chosen = (
                current
                if current in values
                else review_scenario
                if review_scenario in values
                else options[0]["value"]
                if options
                else None
            )
            return (
                options,
                chosen,
                "Only existing review scenarios are listed."
                if options
                else "No review workspace exists. Open Reviews & chronology and create supported statements first.",
            )
        except (ValueError, LookupError) as exc:
            return [], None, str(exc)

    @app.callback(
        Output("ss-view", "value", allow_duplicate=True),
        Input("ss-d-open-reviews", "n_clicks"),
        prevent_initial_call=True,
    )
    def delivery_open_reviews(clicks):
        return "reviews" if clicks else no_update

    @app.callback(
        Output("ss-d-notes", "options"),
        Output("ss-d-notes-pagination", "children"),
        Output("ss-d-notes-previous", "disabled"),
        Output("ss-d-notes-next", "disabled"),
        Output("ss-d-note-page", "data"),
        Input("ss-d-scenario", "value"),
        Input("ss-r-refresh", "data"),
        Input("ss-d-notes-previous", "n_clicks"),
        Input("ss-d-notes-next", "n_clicks"),
        State("ss-d-note-page", "data"),
    )
    def delivery_notes(scenario_id, _tick, _previous, _next, saved_page):
        if not scenario_id:
            return [], "No saved statements", True, True, 1
        page = (
            _page(saved_page)
            if ctx.triggered_id in {"ss-d-notes-previous", "ss-d-notes-next"}
            else 1
        )
        page = max(
            1,
            page
            + (
                1
                if ctx.triggered_id == "ss-d-notes-next"
                else -1
                if ctx.triggered_id == "ss-d-notes-previous"
                else 0
            ),
        )
        try:
            result = reviews.list_reviews(scenario_id, page=page, page_size=DEFAULT_PAGE_SIZE)
            pages = max(1, math.ceil(result["total"] / DEFAULT_PAGE_SIZE))
            if page > pages:
                page = pages
                result = reviews.list_reviews(scenario_id, page=page, page_size=DEFAULT_PAGE_SIZE)
            options = [
                {
                    "label": f"{item['title']} · revision {item['revision']} · {item['status']}",
                    "value": item["review_id"],
                }
                for item in result["items"]
            ]
            return (
                options,
                f"Page {page} of {pages} · {number(result['total'])} saved statements",
                page <= 1,
                page >= pages,
                page,
            )
        except (ValueError, LookupError) as exc:
            return [], str(exc), True, True, 1

    @app.callback(
        Output("ss-d-notes", "value"), Input("ss-d-notes", "options"), State("ss-d-notes", "value")
    )
    def delivery_note_selection(options, selected):
        values = {item["value"] for item in options or []}
        return list(dict.fromkeys(key for key in (selected or []) if key in values))

    @app.callback(
        Output("ss-d-preview-token", "data"),
        Output("ss-d-preview-view", "children"),
        Output("ss-d-finalize", "disabled"),
        Input("ss-d-preview", "n_clicks"),
        Input("ss-d-scenario", "value"),
        Input("ss-d-notes", "value"),
        Input("ss-d-title", "value"),
        Input("ss-d-author", "value"),
        Input("ss-d-origin", "value"),
        Input("ss-r-refresh", "data"),
    )
    def delivery_preview(clicks, scenario_id, review_ids, title, author, origin, _tick):
        if ctx.triggered_id != "ss-d-preview" or not clicks:
            return (
                None,
                "Preview again after changing the selection, title, actor, origin or review state.",
                True,
            )
        try:
            if not scenario_id or not review_ids or len(review_ids) > DEFAULT_PAGE_SIZE:
                raise ValueError("Select between one and twelve saved statements.")
            if not str(title or "").strip() or not str(author or "").strip():
                raise ValueError("Enter a brief title and acting author before previewing.")
            if origin not in ORIGINS:
                raise ValueError("Choose Unspecified, Real source material, or Synthetic material.")
            result = briefs.preview(scenario_id, review_ids)
            token = {
                "scenario_id": scenario_id,
                "review_versions": result["review_versions"],
                "expected_version": result["expected_version"],
                "form_fingerprint": _form_fingerprint(title, author, origin),
            }
            included = bool(result.get("included"))
            return token if included else None, preview_view(result, origin), not included
        except (ValueError, LookupError) as exc:
            return None, empty_state("Brief preview unavailable", str(exc), error=True), True
        except Exception:
            LOGGER.exception("Could not preview brief")
            return (
                None,
                empty_state(
                    "Brief preview failed", "Your title and selection are retained.", error=True
                ),
                True,
            )

    @app.callback(
        Output("ss-d-created-brief", "data"),
        Output("ss-d-create-status", "children"),
        Output("ss-d-preview-token", "data", allow_duplicate=True),
        Output("ss-d-finalize", "disabled", allow_duplicate=True),
        Input("ss-d-finalize", "n_clicks"),
        State("ss-d-preview-token", "data"),
        State("ss-d-scenario", "value"),
        State("ss-d-notes", "value"),
        State("ss-d-title", "value"),
        State("ss-d-author", "value"),
        State("ss-d-origin", "value"),
        prevent_initial_call=True,
    )
    def delivery_finalize(clicks, token, scenario_id, review_ids, title, author, origin):
        if not clicks:
            return no_update, no_update, no_update, no_update
        try:
            if (
                not token
                or token.get("scenario_id") != scenario_id
                or set(token.get("review_versions", {})) != set(review_ids or [])
                or token.get("form_fingerprint") != _form_fingerprint(title, author, origin)
            ):
                raise ValueError(
                    "Preview this exact brief selection and author declaration before finalizing."
                )
            result = briefs.create(
                scenario_id,
                title=str(title or ""),
                reviewer=str(author or ""),
                review_versions=token["review_versions"],
                expected_version=token["expected_version"],
                source_origin=origin,
            )
            message = f"Saved immutable snapshot {result['brief_id']} · {len(result.get('included') or [])} included · {len(result.get('omitted') or [])} omitted."
            return result["brief_id"], message, None, True
        except (ValueError, LookupError) as exc:
            return (
                no_update,
                empty_state(
                    "Snapshot not created",
                    str(exc) + " Your form is retained; preview again.",
                    error=True,
                ),
                None,
                True,
            )
        except Exception:
            LOGGER.exception("Could not finalize brief")
            return (
                no_update,
                empty_state(
                    "Finalization failed",
                    "Your form is retained. Preview again before retrying.",
                    error=True,
                ),
                None,
                True,
            )

    @app.callback(
        Output("ss-d-snapshot", "options"),
        Output("ss-d-snapshots-pagination", "children"),
        Output("ss-d-snapshots-previous", "disabled"),
        Output("ss-d-snapshots-next", "disabled"),
        Output("ss-d-snapshot-page", "data"),
        Input("ss-d-scenario", "value"),
        Input("ss-d-created-brief", "data"),
        Input("ss-d-refresh-snapshots", "n_clicks"),
        Input("ss-d-snapshots-previous", "n_clicks"),
        Input("ss-d-snapshots-next", "n_clicks"),
        State("ss-d-snapshot-page", "data"),
    )
    def delivery_snapshots(scenario_id, _created, _refresh, _previous, _next, saved_page):
        if not scenario_id:
            return [], "No saved snapshots", True, True, 1
        page = (
            _page(saved_page)
            if ctx.triggered_id in {"ss-d-snapshots-previous", "ss-d-snapshots-next"}
            else 1
        )
        page = max(
            1,
            page
            + (
                1
                if ctx.triggered_id == "ss-d-snapshots-next"
                else -1
                if ctx.triggered_id == "ss-d-snapshots-previous"
                else 0
            ),
        )
        try:
            result = briefs.list_briefs(scenario_id, page=page, page_size=DEFAULT_PAGE_SIZE)
            pages = max(1, math.ceil(result["total"] / DEFAULT_PAGE_SIZE))
            if page > pages:
                page = pages
                result = briefs.list_briefs(scenario_id, page=page, page_size=DEFAULT_PAGE_SIZE)
            options = [
                {
                    "label": f"{item.get('title') or item['brief_id']} · {berlin_stamp(item.get('created_at'))} · {item.get('source_origin', 'unspecified')}",
                    "value": item["brief_id"],
                }
                for item in result["items"]
            ]
            if _created and _created not in {item["value"] for item in options}:
                reopened = briefs.get_brief(_created)
                if (reopened.get("scenario") or {}).get("scenario_id") == scenario_id:
                    options.append(
                        {
                            "label": f"Reopened snapshot outside this page · {reopened.get('title') or _created}",
                            "value": _created,
                        }
                    )
            return (
                options,
                f"Page {page} of {pages} · {number(result['total'])} snapshots",
                page <= 1,
                page >= pages,
                page,
            )
        except (ValueError, LookupError) as exc:
            return [], str(exc), True, True, 1

    @app.callback(
        Output("ss-d-snapshot", "value"),
        Input("ss-d-snapshot", "options"),
        Input("ss-d-created-brief", "data"),
        State("ss-d-snapshot", "value"),
    )
    def delivery_snapshot_selection(options, created, current):
        values = [item["value"] for item in options or []]
        if ctx.triggered_id == "ss-d-created-brief" and created:
            return created
        return (
            current
            if current in values
            else created
            if created in values
            else values[0]
            if values
            else None
        )

    @app.callback(
        Output("ss-d-snapshot-status", "children"),
        Output("ss-d-snapshot-view", "children"),
        Input("ss-d-snapshot", "value"),
        Input("ss-d-refresh-snapshots", "n_clicks"),
        Input("ss-r-refresh", "data"),
        Input("ss-refresh", "n_clicks"),
    )
    def delivery_snapshot_view(brief_id, _refresh, _review_tick, _release_refresh):
        if not brief_id:
            return "No saved snapshot selected.", None
        try:
            frozen = briefs.get_brief(brief_id)
            validation = briefs.validate_brief(brief_id)
            current = bool(validation.get("valid")) and not validation.get("history_only")
            status = html.Div(
                [
                    html.Strong(
                        "Current validation passed"
                        if current
                        else "Historical snapshot — current validity differs"
                    ),
                    html.Ul([html.Li(str(item)) for item in validation.get("reasons") or []]),
                    *[validity_note(note) for note in validation.get("notes") or []],
                    html.P(
                        "Frozen content remains unchanged. Validation does not establish source authenticity."
                    ),
                ],
                className="ss-d-validity ss-d-valid"
                if current
                else "ss-d-validity ss-d-history-only",
            )
            release = frozen.get("release") or {}
            rid = release.get("release_id") or (frozen.get("scenario") or {}).get("release_id")
            path = "/api/briefs/" + quote(str(brief_id), safe="") + "/export."
            view = html.Div(
                [
                    html.H3(str(frozen.get("title") or brief_id)),
                    html.P(
                        f"Author: {frozen.get('reviewer', 'Not supplied')} · "
                        f"{berlin_stamp(frozen.get('created_at'))} · Evidence origin: "
                        f"{frozen.get('source_origin', 'unspecified')}",
                        className="ss-d-note",
                    ),
                    html.Code(str(frozen.get("fingerprint") or brief_id)),
                    html.Div(
                        [
                            *[
                                html.A(
                                    label, href=path + extension, className="ss-button ss-secondary"
                                )
                                for label, extension in (
                                    ("Frozen Markdown", "md"),
                                    ("Frozen JSON", "json"),
                                )
                            ],
                            html.A(
                                "Shared evidence package",
                                href=path + "zip",
                                className="ss-button ss-secondary",
                            )
                            if current
                            else html.Span(
                                "Shared evidence package unavailable: current validity differs.",
                                className="ss-d-note",
                            ),
                        ],
                        className="ss-d-actions",
                    ),
                    html.P(
                        "The package requires cited local media to remain available and unchanged.",
                        className="ss-d-note",
                    ),
                    *[
                        statement_card(item, rid, frozen.get("citations") or [])
                        for item in frozen.get("included") or []
                    ],
                    _omissions(frozen.get("omitted") or []),
                    html.H3("Frozen citation index"),
                    citation_links({"citations": frozen.get("citations") or []}, rid, frozen=True),
                    _snapshot_history(frozen.get("review_history")),
                    html.Details(
                        [
                            html.Summary("Frozen Markdown as text"),
                            html.Pre(str(briefs.markdown(brief_id)), className="ss-d-markdown"),
                        ]
                    ),
                ]
            )
            return status, view
        except (ValueError, LookupError) as exc:
            return empty_state("Saved snapshot unavailable", str(exc), error=True), None
        except Exception:
            LOGGER.exception("Could not read saved brief")
            return empty_state(
                "Saved snapshot could not be loaded",
                "Refresh after checking the local workspace.",
                error=True,
            ), None

    @app.callback(
        Output("ss-d-created-brief", "data", allow_duplicate=True),
        Output("ss-d-scenario", "value", allow_duplicate=True),
        Output("ss-release", "value", allow_duplicate=True),
        Output("ss-d-reopen-status", "children"),
        Input("ss-d-reopen", "contents"),
        prevent_initial_call=True,
    )
    def delivery_reopen(contents):
        if not contents:
            return no_update, no_update, no_update, no_update
        try:
            if (
                not isinstance(contents, str)
                or len(contents) > 4 * ((MAX_REOPEN_BYTES + 2) // 3) + 1024
            ):
                raise ValueError("Snapshot JSON exceeds the 2 MiB limit.")
            _header, encoded = contents.split(",", 1)
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > MAX_REOPEN_BYTES:
                raise ValueError("Snapshot JSON exceeds the 2 MiB limit.")
            candidate = json.loads(raw)
            if not isinstance(candidate, dict):
                raise ValueError("Snapshot JSON must contain one saved snapshot object.")
            from ..delivery_api import reopen_snapshot

            result = reopen_snapshot(briefs, candidate)
            frozen = result["brief"]
            scenario = frozen["scenario"]
            return (
                frozen["brief_id"],
                scenario["scenario_id"],
                scenario["release_id"],
                "Exact saved snapshot reopened. No notes or evidence were imported.",
            )
        except (ValueError, LookupError, binascii.Error, UnicodeDecodeError) as exc:
            return (
                no_update,
                no_update,
                no_update,
                empty_state("Snapshot not reopened", str(exc), error=True),
            )
        except Exception:
            LOGGER.exception("Could not reopen saved snapshot")
            return (
                no_update,
                no_update,
                no_update,
                empty_state(
                    "Snapshot not reopened",
                    "The form is retained; check that this exact snapshot exists in the workspace.",
                    error=True,
                ),
            )
