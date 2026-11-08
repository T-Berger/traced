"""Working Dash Evidence Explorer with an acyclic, bounded callback graph.

The shell/reveal arrangement selectively adapts current Swarm ``layout.py``;
pure card and table helpers are attributed in ``components.py``. No application
or database is initialized on import. The caller supplies the store and app.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from dash import Dash, Input, Output, State, ctx, dcc, get_asset_url, html

from ..contracts import (
    ALL_FILTER,
    CONTACT_SHEET_SIZE,
    DEFAULT_PAGE_SIZE,
    ExplorerFilters,
    ExplorerStore,
    ReviewServiceProtocol,
    normalize_filter,
)
from .components import (
    clip_option,
    coverage_note,
    empty_state,
    evidence_inspector,
    field_definitions,
    filter_options,
    frame_option,
    number,
    overview_cards,
    section_heading,
)

LOGGER = logging.getLogger(__name__)
FILTER_IDS = ("ss-week", "ss-side", "ss-camera", "ss-image-type")
FILTER_LABELS = ("All posting weeks", "All posting channels", "All cameras", "All image types")
RESET_TRIGGERS = {"ss-release", "ss-query", *FILTER_IDS, "ss-clear-filters"}


def _release_options(releases: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"label": str(item.get("title") or item["release_id"]), "value": str(item["release_id"])}
        for item in releases
    ]


def _initial_releases(store: ExplorerStore) -> tuple[list[dict[str, str]], str | None, Any]:
    try:
        releases = store.list_releases()
        latest = store.get_release() if releases else None
        options = _release_options(releases)
        selected = (
            str(latest["release_id"]) if latest else (options[0]["value"] if options else None)
        )
        return options, selected, None
    except Exception:
        LOGGER.exception("Could not load the release list")
        return (
            [],
            None,
            empty_state(
                "Releases could not be read",
                "Check the local import report and refresh after the dataset is ready.",
                error=True,
            ),
        )


def _filter_control(label: str, component_id: str, all_label: str) -> html.Div:
    return html.Div(
        [
            html.Label(label, htmlFor=component_id),
            dcc.Dropdown(
                id=component_id,
                options=filter_options([], all_label),
                value=ALL_FILTER,
                clearable=False,
                className="ss-dropdown",
            ),
        ],
        className="ss-filter",
    )


def _pager(prefix: str, label: str) -> html.Div:
    return html.Div(
        [
            html.Button(
                "Previous page",
                id=f"ss-{prefix}-previous",
                n_clicks=0,
                disabled=True,
                className="ss-button ss-secondary",
            ),
            html.Span(
                label,
                id=f"ss-{prefix}-pagination",
                className="ss-pagination-label",
                **{"aria-live": "polite"},
            ),
            html.Button(
                "Next page",
                id=f"ss-{prefix}-next",
                n_clicks=0,
                disabled=True,
                className="ss-button ss-secondary",
            ),
        ],
        className="ss-pagination",
    )


def create_layout(
    store: ExplorerStore,
    review_service: ReviewServiceProtocol | None = None,
    delivery_service: Any = None,
    diff_service: Any = None,
    workbench_links: list[tuple[str, str]] | None = None,
) -> html.Div:
    """Build the shell; completed imports only, with no image requests by default."""
    releases, release_id, boot_error = _initial_releases(store)
    delivery_enabled = all(
        service is not None for service in (review_service, delivery_service, diff_service)
    )
    views = [
        {"label": "01  Evidence Explorer", "value": "evidence"},
        {"label": "02  Reviews & chronology", "value": "reviews"},
    ]
    if delivery_enabled:
        views.append({"label": "03  Release diff & brief", "value": "delivery"})
    return html.Div(
        [
            dcc.Store(id="ss-clip-page", data=1),
            dcc.Store(id="ss-contact-page", data=1),
            html.A("Skip to evidence", href="#ss-main", className="ss-skip-link"),
            html.Div(
                [
                    html.Aside(
                        [
                            html.Div(
                                [
                                    html.Div(
                                        [
                                            html.Img(
                                                src=get_asset_url(
                                                    "traced/logos/traced-primary.svg"
                                                ),
                                                alt="Traced",
                                                className="traced-logo traced-logo--light",
                                            ),
                                            html.Img(
                                                src=get_asset_url(
                                                    "traced/logos/traced-reversed.svg"
                                                ),
                                                alt="Traced",
                                                className="traced-logo traced-logo--dark",
                                            ),
                                        ],
                                        className="traced-lockup traced-lockup--inverse",
                                    ),
                                    html.Span(
                                        "Historical media · Part of Traced",
                                        className="ss-brand-affiliation",
                                    ),
                                ],
                                className="ss-brand",
                            ),
                            html.Nav(
                                [
                                    html.A(label, href=href, className="traced-switcher-link")
                                    for label, href in (workbench_links or [])
                                ],
                                className="traced-switcher traced-switcher--inverse",
                                **{"aria-label": "Traced workbench surfaces"},
                            )
                            if workbench_links
                            else None,
                            html.P(
                                "Inspect supplied historical media and its limits.",
                                className="ss-brand-note",
                            ),
                            dcc.RadioItems(
                                id="ss-view",
                                options=views,
                                value="evidence",
                                className="ss-view-navigation",
                            )
                            if review_service
                            else html.Div("01  Evidence Explorer", className="ss-current-section"),
                            html.Div(
                                [
                                    html.Label("Imported release", htmlFor="ss-release"),
                                    dcc.Dropdown(
                                        id="ss-release",
                                        options=releases,
                                        value=release_id,
                                        clearable=False,
                                        disabled=not bool(releases),
                                        placeholder="No completed release",
                                        className="ss-dropdown",
                                    ),
                                    html.P(
                                        "Only completed local imports are listed.",
                                        className="ss-sidebar-note",
                                    ),
                                    html.Button(
                                        "Refresh releases",
                                        id="ss-refresh",
                                        n_clicks=0,
                                        className="ss-button ss-sidebar-button",
                                    ),
                                ],
                                className="ss-release-control",
                            ),
                            html.Label("Find an ID", htmlFor="ss-query"),
                            dcc.Input(
                                id="ss-query",
                                type="search",
                                value="",
                                maxLength=200,
                                placeholder="Frame, clip or video ID",
                                debounce=True,
                                className="ss-search",
                            ),
                            html.P(
                                "Literal ID search. Press Enter to apply.",
                                className="ss-sidebar-note",
                            ),
                            _filter_control(
                                "Exercise week first posted", "ss-week", FILTER_LABELS[0]
                            ),
                            _filter_control("Posting channel side", "ss-side", FILTER_LABELS[1]),
                            _filter_control("Camera", "ss-camera", FILTER_LABELS[2]),
                            _filter_control("Image type", "ss-image-type", FILTER_LABELS[3]),
                            html.Button(
                                "Clear filters",
                                id="ss-clear-filters",
                                n_clicks=0,
                                className="ss-button ss-sidebar-button",
                            ),
                            html.P(
                                "Posting channel is not the identity of who is shown.",
                                className="ss-sidebar-principle",
                            ),
                        ],
                        className="ss-sidebar",
                    ),
                    html.Main(
                        [
                            html.Header(
                                [
                                    html.Div(
                                        [
                                            html.Span(
                                                "TRACED · HISTORICAL EVIDENCE",
                                                className="ss-eyebrow",
                                            ),
                                            html.H1(
                                                "Historical Review and Chronology",
                                                className="ss-specialist-heading",
                                            ),
                                            html.H2("Evidence Explorer", id="ss-page-title"),
                                            html.P(
                                                "Find a clip group. Inspect the supplied record.",
                                                id="ss-page-subtitle",
                                                className="ss-subtitle",
                                            ),
                                        ]
                                    ),
                                    html.Span("LOCAL WORKSPACE", className="ss-workspace-badge"),
                                ],
                                className="ss-page-header",
                            ),
                            html.Div(boot_error, id="ss-boot-status"),
                            html.Div(id="ss-release-caption", className="ss-release-caption"),
                            html.Div(id="ss-overview", className="ss-overview"),
                            html.Div(id="ss-coverage"),
                            html.Div(
                                [
                                    html.Div(
                                        [
                                            html.Strong("Images start hidden"),
                                            html.P(
                                                "The collection may contain graphic imagery. Reveal only "
                                                "when you are ready to inspect it."
                                            ),
                                        ]
                                    ),
                                    dcc.Checklist(
                                        id="ss-reveal-previews",
                                        options=[{"label": "Reveal previews", "value": "reveal"}],
                                        value=[],
                                        className="ss-reveal",
                                        inputClassName="ss-checkbox",
                                    ),
                                ],
                                className="ss-reveal-bar",
                            ),
                            field_definitions(),
                            html.Div(
                                [
                                    html.Div(id="ss-explorer-status", **{"aria-live": "polite"}),
                                    html.Div(
                                        [
                                            html.Section(
                                                [
                                                    section_heading(
                                                        "01 · FIND",
                                                        "Clip groups",
                                                        "Filters match frames; results are supplied clip groups.",
                                                    ),
                                                    dcc.Loading(
                                                        html.Div(
                                                            [
                                                                html.Div(id="ss-clip-empty"),
                                                                dcc.RadioItems(
                                                                    id="ss-selected-clip",
                                                                    options=[],
                                                                    value=None,
                                                                    className="ss-clip-options",
                                                                    labelClassName="ss-choice",
                                                                    inputClassName="ss-radio",
                                                                ),
                                                            ],
                                                            id="ss-clip-results",
                                                        ),
                                                        type="default",
                                                    ),
                                                    _pager("clip", "No clip groups"),
                                                ],
                                                className="ss-panel ss-results-panel",
                                            ),
                                            html.Section(
                                                [
                                                    section_heading(
                                                        "02 · CONTEXT", "Clip contact sheet"
                                                    ),
                                                    html.Div(
                                                        id="ss-contact-caption",
                                                        className="ss-contact-caption",
                                                    ),
                                                    html.P(
                                                        "Display order; capture order unknown.",
                                                        className="ss-order-label",
                                                    ),
                                                    html.P(
                                                        "All frames in the supplied clip are included, even when only "
                                                        "some match the filters. An exact searched frame may open in "
                                                        "the inspector outside this contact page.",
                                                        className="ss-muted",
                                                    ),
                                                    dcc.Loading(
                                                        html.Div(
                                                            [
                                                                html.Div(id="ss-contact-empty"),
                                                                dcc.RadioItems(
                                                                    id="ss-selected-frame",
                                                                    options=[],
                                                                    value=None,
                                                                    className="ss-contact-grid",
                                                                    labelClassName="ss-choice",
                                                                    inputClassName="ss-radio",
                                                                ),
                                                            ],
                                                            id="ss-contact-sheet",
                                                        ),
                                                        type="default",
                                                    ),
                                                    _pager("contact", "No contact sheet"),
                                                ],
                                                className="ss-panel ss-contact-panel",
                                            ),
                                            html.Section(
                                                [
                                                    section_heading(
                                                        "03 · INSPECT", "Evidence inspector"
                                                    ),
                                                    dcc.Loading(
                                                        html.Div(id="ss-inspector"), type="default"
                                                    ),
                                                    html.Button(
                                                        "Create review from selected frame",
                                                        id="ss-review-from-frame",
                                                        n_clicks=0,
                                                        className="ss-button ss-primary",
                                                    )
                                                    if review_service
                                                    else None,
                                                ],
                                                className="ss-panel ss-inspector-panel",
                                            ),
                                        ],
                                        className="ss-workspace-grid",
                                    ),
                                ],
                                id="ss-evidence-panel",
                            ),
                            _review_layout(store, review_service) if review_service else None,
                            _delivery_layout(store, review_service, delivery_service, diff_service)
                            if delivery_enabled
                            else None,
                            html.Footer(
                                "Supplied metadata and local references · No capture chronology "
                                "or geographic location inferred",
                                className="ss-footer",
                            ),
                        ],
                        id="ss-main",
                        className="ss-main",
                    ),
                ],
                className="ss-shell",
            ),
        ],
        className="ss-workbench traced-app",
        **{"data-theme": "light"},
    )


def _review_layout(store: ExplorerStore, review_service: ReviewServiceProtocol):
    from .review import create_review_layout

    return create_review_layout(store, review_service)


def _delivery_layout(store, reviews, briefs, diffs):
    from .delivery import create_delivery_layout

    return create_delivery_layout(store, reviews, briefs, diffs)


def _page(value: Any) -> int:
    try:
        return max(1, min(int(value or 1), 1_000_000))
    except (ValueError, TypeError, OverflowError):
        return 1


def _values(options: list[dict[str, Any]] | None) -> list[str]:
    return [str(option["value"]) for option in (options or []) if "value" in option]


def register_callbacks(application: Dash, store: ExplorerStore) -> None:
    """Register only real Explorer actions, with no input/output callback cycles.

    filters -> clip options -> clip value -> contact options -> frame value -> inspector
    Selection highlighting uses native radio state, not a selection-to-options callback.
    Browser stores contain page numbers only; selected values are IDs, never corpus data.
    """

    @application.callback(
        Output("ss-release", "options"),
        Output("ss-release", "value"),
        Output("ss-release", "disabled"),
        Output("ss-boot-status", "children"),
        Input("ss-refresh", "n_clicks"),
        State("ss-release", "value"),
    )
    def refresh_releases(_clicks, current):
        options, latest, error = _initial_releases(store)
        values = _values(options)
        return options, current if current in values else latest, not bool(options), error

    @application.callback(
        Output("ss-overview", "children"),
        Output("ss-coverage", "children"),
        Output("ss-release-caption", "children"),
        *[Output(key, "options") for key in FILTER_IDS],
        Output("ss-query", "value"),
        *[Output(key, "value") for key in FILTER_IDS],
        Input("ss-release", "value"),
        Input("ss-clear-filters", "n_clicks"),
        Input("ss-refresh", "n_clicks"),
        State("ss-query", "value"),
        *[State(key, "value") for key in FILTER_IDS],
    )
    def load_release(release_id, _clear, _refresh, query, week, side, camera, image_type):
        options = [filter_options([], label) for label in FILTER_LABELS]
        if not release_id:
            return ([], None, "No completed dataset imported", *options, "", *([ALL_FILTER] * 4))
        try:
            overview = store.overview(str(release_id))
            source = overview.get("filter_options") or {}
            options = [
                filter_options(source.get(key) or [], label)
                for key, label in zip(
                    ("week", "side", "camera", "image_type"), FILTER_LABELS, strict=True
                )
            ]
            values = [ALL_FILTER] * 4
            search = ""
            if ctx.triggered_id == "ss-refresh":
                search = str(query or "")
                values = [
                    value if value in _values(choices) else ALL_FILTER
                    for value, choices in zip(
                        (week, side, camera, image_type), options, strict=True
                    )
                ]
            return (
                overview_cards(overview),
                coverage_note(overview),
                str(overview.get("title") or release_id),
                *options,
                search,
                *values,
            )
        except Exception:
            LOGGER.exception("Could not read release overview")
            return (
                [],
                empty_state(
                    "Release metadata could not be read",
                    "Refresh after checking the local import report.",
                    error=True,
                ),
                str(release_id),
                *options,
                "",
                *([ALL_FILTER] * 4),
            )

    @application.callback(
        Output("ss-selected-clip", "options"),
        Output("ss-clip-empty", "children"),
        Output("ss-clip-pagination", "children"),
        Output("ss-clip-previous", "disabled"),
        Output("ss-clip-next", "disabled"),
        Output("ss-clip-page", "data"),
        Output("ss-explorer-status", "children"),
        Input("ss-release", "value"),
        Input("ss-query", "value"),
        *[Input(key, "value") for key in FILTER_IDS],
        Input("ss-clip-previous", "n_clicks"),
        Input("ss-clip-next", "n_clicks"),
        Input("ss-reveal-previews", "value"),
        Input("ss-clear-filters", "n_clicks"),
        Input("ss-refresh", "n_clicks"),
        State("ss-clip-page", "data"),
    )
    def load_clips(
        release_id,
        query,
        week,
        side,
        camera,
        image_type,
        _previous,
        _next,
        reveal,
        _clear,
        _refresh,
        saved_page,
    ):
        if not release_id:
            return (
                [],
                empty_state(
                    "No dataset is ready",
                    "Complete a local dataset import, "
                    "then choose Refresh releases. Interrupted or failed imports "
                    "are not listed as completed releases.",
                ),
                "No clip groups",
                True,
                True,
                1,
                None,
            )
        trigger = ctx.triggered_id
        page = 1 if trigger in RESET_TRIGGERS else _page(saved_page)
        if trigger == "ss-clip-previous":
            page = max(1, page - 1)
        elif trigger == "ss-clip-next":
            page += 1
        try:
            kwargs = dict(
                query=str(query or "").strip(),
                week=normalize_filter(week),
                side=normalize_filter(side),
                camera=normalize_filter(camera),
                image_type=normalize_filter(image_type),
                page_size=DEFAULT_PAGE_SIZE,
            )
            result = store.list_clips(str(release_id), ExplorerFilters(page=page, **kwargs))
            total = int(result.get("total") or 0)
            pages = max(1, math.ceil(total / DEFAULT_PAGE_SIZE))
            if page > pages:
                page = pages
                result = store.list_clips(str(release_id), ExplorerFilters(page=page, **kwargs))
            items = result.get("items") or []
            options = [
                clip_option(item, str(release_id), "reveal" in (reveal or [])) for item in items
            ]
            empty = (
                None
                if items
                else empty_state(
                    "No clips match these filters", "Try another ID or choose Clear filters."
                )
            )
            label = f"Page {page} of {pages} · {number(total)} clip groups"
            status = html.P(
                f"{number(total)} matching clip groups · "
                f"{number(result.get('total_frames'))} matching frames. "
                "Counts describe supplied evidence, not events.",
                className="ss-result-summary",
            )
            return options, empty, label, page <= 1, page >= pages, page, status
        except Exception:
            LOGGER.exception("Could not query clip groups")
            return (
                [],
                empty_state(
                    "Evidence could not be loaded",
                    "Check the search length and local import, then refresh or clear filters.",
                    error=True,
                ),
                "Query unavailable",
                True,
                True,
                1,
                None,
            )

    @application.callback(
        Output("ss-selected-clip", "value"),
        Input("ss-selected-clip", "options"),
        State("ss-selected-clip", "value"),
    )
    def choose_clip(options, current):
        values = _values(options)
        return current if current in values else (values[0] if values else None)

    @application.callback(
        Output("ss-selected-frame", "options"),
        Output("ss-contact-empty", "children"),
        Output("ss-contact-caption", "children"),
        Output("ss-contact-pagination", "children"),
        Output("ss-contact-previous", "disabled"),
        Output("ss-contact-next", "disabled"),
        Output("ss-contact-page", "data"),
        Input("ss-release", "value"),
        Input("ss-selected-clip", "value"),
        Input("ss-contact-previous", "n_clicks"),
        Input("ss-contact-next", "n_clicks"),
        Input("ss-reveal-previews", "value"),
        Input("ss-clear-filters", "n_clicks"),
        Input("ss-refresh", "n_clicks"),
        State("ss-contact-page", "data"),
    )
    def load_contact_sheet(
        release_id, clip_key, _previous, _next, reveal, _clear, _refresh, saved_page
    ):
        blank = (
            [],
            empty_state("Choose a clip group", "Its supplied frames will appear here."),
            None,
            "No contact sheet",
            True,
            True,
            1,
        )
        if not release_id or not clip_key:
            return blank
        trigger = ctx.triggered_id
        page = (
            1
            if trigger in {"ss-release", "ss-selected-clip", "ss-clear-filters"}
            else _page(saved_page)
        )
        if trigger == "ss-contact-previous":
            page = max(1, page - 1)
        elif trigger == "ss-contact-next":
            page += 1
        try:
            clip = store.get_clip(
                str(release_id),
                str(clip_key),
                offset=(page - 1) * CONTACT_SHEET_SIZE,
                limit=CONTACT_SHEET_SIZE,
            )
            if not clip:
                return blank
            total = int(clip.get("frame_count") or 0)
            pages = max(1, math.ceil(total / CONTACT_SHEET_SIZE))
            if page > pages:
                page = pages
                clip = store.get_clip(
                    str(release_id),
                    str(clip_key),
                    offset=(page - 1) * CONTACT_SHEET_SIZE,
                    limit=CONTACT_SHEET_SIZE,
                )
                if not clip:
                    return blank
            frames = clip.get("frames") or []
            options = [
                frame_option(frame, str(release_id), "reveal" in (reveal or [])) for frame in frames
            ]
            caption = html.Div(
                [
                    html.Strong(f"Clip {clip.get('clip_id') or 'Not supplied'}"),
                    html.Span(
                        f"Video {clip.get('video_id') or 'Not supplied'}", className="ss-muted"
                    ),
                ]
            )
            return (
                options,
                None
                if frames
                else empty_state(
                    "No supplied frames", "The clip has no frame records on this display page."
                ),
                caption,
                f"Page {page} of {pages} · {number(total)} frames in clip",
                page <= 1,
                page >= pages,
                page,
            )
        except Exception:
            LOGGER.exception("Could not read clip contact sheet")
            return (
                [],
                empty_state(
                    "Contact sheet unavailable",
                    "Refresh after checking the local release.",
                    error=True,
                ),
                None,
                "Contact sheet unavailable",
                True,
                True,
                1,
            )

    @application.callback(
        Output("ss-selected-frame", "value"),
        Input("ss-selected-frame", "options"),
        Input("ss-query", "value"),
        State("ss-selected-frame", "value"),
        State("ss-release", "value"),
        State("ss-selected-clip", "value"),
    )
    def choose_frame(options, query, current, release_id, clip_key):
        values = _values(options)
        if not values:
            return None
        if current in values and ctx.triggered_id != "ss-query":
            return current
        query = str(query or "").strip()
        if query and release_id and clip_key:
            try:
                exact = store.get_frame(str(release_id), query)
                if exact and str(exact.get("clip_key")) == str(clip_key):
                    return str(exact["frame_id"])
            except Exception:
                LOGGER.exception("Could not inspect exact frame ID match")
        return current if current in values else values[0]

    @application.callback(
        Output("ss-inspector", "children"),
        Input("ss-release", "value"),
        Input("ss-selected-clip", "value"),
        Input("ss-selected-frame", "value"),
        Input("ss-reveal-previews", "value"),
        Input("ss-refresh", "n_clicks"),
    )
    def load_inspector(release_id, clip_key, frame_id, reveal, _refresh):
        if not release_id or not clip_key or not frame_id:
            return empty_state(
                "Choose a frame", "Inspect its supplied metadata and local reference."
            )
        try:
            frame = store.get_frame(str(release_id), str(frame_id))
            if not frame or str(frame.get("clip_key")) != str(clip_key):
                return empty_state(
                    "Frame not available in this selection",
                    "Choose a frame from the current contact sheet.",
                )
            return evidence_inspector(frame, str(release_id), "reveal" in (reveal or []))
        except Exception:
            LOGGER.exception("Could not read frame inspector")
            return empty_state(
                "Frame metadata unavailable",
                "Refresh after checking the local release and supplied file references.",
                error=True,
            )
