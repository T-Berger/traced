"""Pure, bounded presentation helpers for the Evidence Explorer.

Selective adaptations from the adjacent Swarm workbench, inspected 2026-11-06:
``ui.py``: number/card_label/section_heading/table; ``presentation.py``:
queue_cards; ``layout.py``: the shell, reveal control and source inspector.
No Swarm modules, application factories, scoring or sample data are imported.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote

from dash import html

from ..contracts import ALL_FILTER, UNKNOWN_FILTER

FRAME_FIELDS = (
    "frame_id",
    "file",
    "clip_id",
    "video_id",
    "week",
    "side",
    "image_type",
    "from_perspective",
    "to_perspective",
    "camera",
    "width_px",
    "height_px",
)


def number(value: Any) -> str:
    """Format supplied counts without turning missing data into a zero."""
    try:
        return f"{int(value):,}"
    except (ValueError, TypeError, OverflowError):
        return "Not supplied"


def supplied(value: Any) -> str:
    return "Not supplied" if value is None or str(value).strip() == "" else str(value)


def short(value: Any, limit: int = 26) -> str:
    text = supplied(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def card_label(label: str, value: Any, context: str = "") -> html.Div:
    """Adapted Swarm metric structure with challenge-specific, visible units."""
    return html.Div(
        [
            html.Span(label, className="ss-metric-label"),
            html.Strong(str(value), className="ss-metric-value"),
            html.Span(context, className="ss-muted"),
        ],
        className="ss-metric",
    )


def section_heading(kicker: str, title: str, note: str | None = None) -> html.Div:
    return html.Div(
        [
            html.Div([html.Span(kicker, className="ss-eyebrow"), html.H2(title)]),
            html.P(note, className="ss-muted") if note else None,
        ],
        className="ss-section-heading",
    )


def empty_state(title: str, body: str, *, error: bool = False) -> html.Div:
    return html.Div(
        [html.Strong(title), html.P(body)],
        className="ss-empty ss-error" if error else "ss-empty",
        role="alert" if error else "status",
    )


def data_table(rows: Iterable[tuple[str, Any]]) -> html.Div:
    """Adapted Swarm table wrapper. All supplied values remain escaped Dash text."""
    return html.Div(
        html.Table(
            [
                html.Thead(
                    html.Tr([html.Th("Field", scope="col"), html.Th("Supplied value", scope="col")])
                ),
                html.Tbody(
                    [
                        html.Tr([html.Th(html.Code(key), scope="row"), html.Td(supplied(value))])
                        for key, value in rows
                    ]
                ),
            ],
            className="ss-data-table",
        ),
        className="ss-table-wrap",
    )


def filter_options(values: Iterable[Any], all_label: str) -> list[dict[str, str]]:
    options = [{"label": all_label, "value": ALL_FILTER}]
    seen = {ALL_FILTER}
    for value in values:
        normalized = UNKNOWN_FILTER if value is None or str(value) == "" else str(value)
        if normalized not in seen:
            options.append(
                {
                    "label": "Not supplied" if normalized == UNKNOWN_FILTER else normalized,
                    "value": normalized,
                }
            )
            seen.add(normalized)
    return options


def field_definitions() -> html.Details:
    return html.Details(
        [
            html.Summary("What these fields mean"),
            html.Dl(
                [
                    html.Dt("Posting channel · side"),
                    html.Dd("The channel that posted the video; not the identity of who is shown."),
                    html.Dt("Exercise week first posted · week"),
                    html.Dd("Supplied posting context, not capture time or an event date."),
                    html.Dt("Clip and video IDs"),
                    html.Dd("Supplied group membership. Frame display order is not capture order."),
                    html.Dt("Capture time, frame order, coordinates and original URLs"),
                    html.Dd("Not supplied in this collection."),
                    html.Dt("Detector rows"),
                    html.Dd(
                        "Automated and unverified. Their count is not a count of confirmed objects."
                    ),
                ]
            ),
        ],
        className="ss-definitions",
    )


def media_preview(
    release_id: str,
    frame_id: str,
    reveal: bool,
    *,
    available: bool = True,
    large: bool = False,
) -> html.Div:
    """Do not create an image element or media URL before explicit opt-in."""
    children: list[Any] = [
        html.Div(
            [
                html.Span("▧", className="ss-preview-symbol", **{"aria-hidden": "true"}),
                html.Span(
                    "Previews hidden"
                    if not reveal
                    else "Preview unavailable"
                    if not available
                    else "Loading preview…"
                ),
            ],
            className="ss-preview-placeholder",
        )
    ]
    if reveal and available and frame_id:
        path = f"/media/{quote(str(release_id), safe='')}/{quote(str(frame_id), safe='')}"
        # Dash 4.5's Img wrapper omits native loading. Explicit opt-in plus
        # bounded clip/contact pagination defers the corpus without unsupported
        # props, DOM hydration hooks, or any media request in the hidden state.
        children.append(
            html.Img(
                src=path,
                alt=f"Supplied frame {frame_id}. Preview unavailable if the file cannot load.",
                className="ss-preview-image",
            )
        )
    return html.Div(children, className="ss-preview ss-preview-large" if large else "ss-preview")


def _week_list(values: Iterable[Any]) -> str:
    labels = []
    for value in values:
        text = supplied(value)
        labels.append(
            text if text == "Not supplied" or text.lower().startswith("week ") else f"Week {text}"
        )
    return ", ".join(labels) or "Not supplied"


def clip_option(clip: Mapping[str, Any], release_id: str, reveal: bool) -> dict[str, Any]:
    """Adapted queue-card composition, inside a static, keyboard-operable radio group."""
    clip_id = supplied(clip.get("clip_id"))
    video_id = supplied(clip.get("video_id"))
    return {
        "value": str(clip["clip_key"]),
        "label": html.Div(
            [
                media_preview(release_id, str(clip.get("representative_frame_id") or ""), reveal),
                html.Div(
                    [
                        html.Span("SUPPLIED CLIP", className="ss-eyebrow"),
                        html.Strong(short(clip_id), title=clip_id),
                        html.Span(f"Video {short(video_id)}", title=video_id, className="ss-muted"),
                        html.Span(
                            f"{number(clip.get('frame_count'))} frames in clip · "
                            f"{number(clip.get('matched_frame_count'))} match filters",
                            className="ss-card-meta",
                        ),
                        html.Span(_week_list(clip.get("weeks") or []), className="ss-card-meta"),
                    ],
                    className="ss-clip-card-body",
                ),
            ],
            className="ss-clip-card",
        ),
    }


def frame_option(frame: Mapping[str, Any], release_id: str, reveal: bool) -> dict[str, Any]:
    frame_id = str(frame["frame_id"])
    return {
        "value": frame_id,
        "label": html.Div(
            [
                media_preview(
                    release_id, frame_id, reveal, available=bool(frame.get("media_available"))
                ),
                html.Code(frame_id),
                html.Span(
                    "Local file unavailable"
                    if not frame.get("media_available")
                    else f"{supplied(frame.get('width_px'))} × {supplied(frame.get('height_px'))}",
                    className="ss-card-meta",
                ),
            ],
            className="ss-frame-card",
        ),
    }


def overview_cards(overview: Mapping[str, Any]) -> list[html.Div]:
    return [
        card_label("Frames", number(overview.get("frame_count")), "Supplied images"),
        card_label("Clips", number(overview.get("clip_count")), "Supplied clip groups"),
        card_label("Videos", number(overview.get("video_count")), "Supplied source-video groups"),
        card_label(
            "Last checked unavailable",
            number(overview.get("missing_media_count")),
            "Recorded media coverage",
        ),
    ]


def coverage_note(overview: Mapping[str, Any]) -> html.Div:
    hashes = overview.get("hash_counts") or {}
    return html.Div(
        [
            html.Strong("Media hash coverage"),
            html.Span(f"{number(hashes.get('hashed'))} / {number(hashes.get('total'))} hashed"),
            html.Span(f"{number(hashes.get('pending'))} untested / pending"),
            html.Span(f"{number(hashes.get('failed'))} failed"),
            html.Span("Uncomputed hashes do not establish uniqueness.", className="ss-muted"),
        ],
        className="ss-coverage-note",
    )


def evidence_inspector(frame: Mapping[str, Any], release_id: str, reveal: bool) -> html.Div:
    raw = frame.get("raw_metadata")
    raw = raw if isinstance(raw, Mapping) else frame
    source_file = supplied(frame.get("source_file") or "metadata/frames.csv")
    row = supplied(frame.get("source_row"))
    hash_status = supplied(frame.get("hash_status"))
    if hash_status in ("pending", "untested"):
        hash_status = "Untested / pending"
    hash_verification = frame.get("hash_verification")
    hash_basis = {
        "citation_byte_match": "The selected image bytes were freshly checked against the saved citation SHA-256 for this view.",
        "citation_byte_mismatch": "The current image could not verify against the saved citation; its preview is blocked.",
        "recorded_stat_match": "Recorded checksum; file size and modification time match. "
        "Bytes were not rehashed for this view.",
        "media_changed": "File changed since the recorded checksum; current bytes are unverified.",
        "media_unavailable": "Media unavailable; the recorded checksum cannot verify a current file.",
        "not_computed": "No media checksum computed.",
    }.get(hash_verification, "Recorded hash state; current byte verification is not established.")
    recorded_hash = frame.get("recorded_media_sha256") or frame.get("media_sha256")
    availability = (
        "Local file available"
        if frame.get("media_available")
        else "Local file unavailable. Refresh after restoring the supplied file."
    )
    if frame.get("media_availability_error"):
        availability += " " + str(frame["media_availability_error"])
    return html.Div(
        [
            html.Div(
                [
                    html.Span("SELECTED FRAME", className="ss-eyebrow"),
                    html.Code(str(frame["frame_id"])),
                ],
                className="ss-inspector-heading",
            ),
            media_preview(
                release_id,
                str(frame["frame_id"]),
                reveal,
                available=bool(frame.get("media_available")),
                large=True,
            ),
            html.P(availability, className="ss-availability"),
            html.P("File changed · recorded checksum is stale", className="ss-order-label")
            if hash_verification == "media_changed" or frame.get("hash_stat_matches") is False
            else None,
            html.H3("Supplied metadata"),
            data_table((key, raw.get(key)) for key in FRAME_FIELDS),
            html.H3("Local provenance"),
            html.Dl(
                [
                    html.Dt("Release"),
                    html.Dd(html.Code(release_id)),
                    html.Dt("CSV reference"),
                    html.Dd(html.Code(f"{source_file}, line {row}")),
                    html.Dt("Relative local path"),
                    html.Dd(html.Code(supplied(frame.get("relative_path")))),
                    html.Dt("Supplied detector rows"),
                    html.Dd(number(frame.get("detection_count"))),
                    html.Dt("Media hash state"),
                    html.Dd(hash_status),
                    html.Dt("Recorded media SHA-256"),
                    html.Dd(html.Code(supplied(recorded_hash))),
                    html.Dt("Checksum basis"),
                    html.Dd(hash_basis),
                    html.Dt("Capture time / capture order"),
                    html.Dd("Not supplied"),
                    html.Dt("Coordinates / original source URL"),
                    html.Dd("Not supplied"),
                ],
                className="ss-provenance",
            ),
            html.P(
                "The reference identifies a supplied local record. It does not authenticate "
                "the footage or recover its original publication context.",
                className="ss-muted",
            ),
        ]
    )
