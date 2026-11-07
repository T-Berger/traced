"""Explicit saved-source selection, read-only preview, and immutable case creation."""

from __future__ import annotations

from dash import Dash, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from ..controller import session_for_case
from ..presentation import badge
from .sources import PLATFORM_LABELS

_STATUS_LABELS = {
    "idle": "Not collected yet",
    "queued": "Queued",
    "running": "Collecting",
    "succeeded": "Collected",
    "failed": "Collection failed",
    "cancelled": "Collection cancelled",
}
_CONTENT_LABELS = {
    "post_text": "Post text",
    "title_description": "Video titles and descriptions (no transcript)",
    "article_text": "Article text",
    "feed_content": "Feed-supplied content",
    "feed_summary": "Feed summaries (not full articles)",
    "unknown": "Unknown saved content type",
}
_WRAP_STYLE = {"minWidth": 0, "maxWidth": "100%", "overflowWrap": "anywhere"}


def _notice(message: str, *, error: bool = False):
    return html.Div(
        message,
        className="rv-notice rv-notice--error" if error else "rv-notice",
        role="alert" if error else "status",
    )


def _source_ids(selected) -> list[str]:
    if not isinstance(selected, list) or any(not isinstance(value, str) for value in selected):
        raise ValueError("Choose saved sources for this case.")
    if len(selected) != len(set(selected)):
        raise ValueError("Choose each saved source only once.")
    return sorted(selected)


def scope_tools():
    """A stable, reusable source-scope panel; selection never changes an assessment."""
    content = [
        html.P(
            "Choose the saved sources for this new case, then preview its evidence and "
            "collection limits. This does not exclude evidence from an existing assessment.",
            className="rv-small rv-muted",
        ),
        html.Label("Sources to include", htmlFor="live-case-sources"),
        dcc.Dropdown(
            id="live-case-sources",
            options=[],
            value=[],
            multi=True,
            placeholder="Choose one or more saved sources",
        ),
        html.Button(
            "Preview selected sources",
            id="live-case-preview-action",
            n_clicks=0,
            className="rv-button rv-button--outline",
        ),
        html.Div(id="live-case-preview"),
        html.Div(id="live-case-message", role="status", **{"aria-live": "polite"}),
        html.Button(
            "Create case from selected sources",
            id="live-case-create",
            n_clicks=0,
            disabled=True,
            className="rv-button rv-button--primary",
        ),
        dcc.Store(id="live-case-token", storage_type="memory"),
    ]
    return html.Details(
        [
            html.Summary("Choose sources for a case"),
            html.Div(content, id="live-case-scope", style={"display": "grid", "gap": "12px"}),
        ],
        id="live-case-panel",
        open=False,
        className="rv-details",
        **{"aria-label": "Choose source scope for a new case"},
    )


def saved_scope_panel():
    return html.Div(id="live-case-saved-scope")


def _gap_text(source: dict) -> str:
    status = _STATUS_LABELS.get(source.get("last_status"), "No saved evidence")
    text = f"{source.get('label') or 'Selected source'}: {status}"
    if source.get("last_error"):
        text += " · " + str(source["last_error"])
    elif not source.get("included_count", 0):
        text += " · No saved records included."
    if source.get("ready") is False:
        text += " · Setup or access needed"
        if source.get("connection_status"):
            text += ": " + str(source["connection_status"])
    for gap in _scope_gaps(source):
        text += " · " + gap
    return text


def _scope_gaps(source: dict) -> list[str]:
    gaps = []
    if source.get("unattributed_count", 0):
        gaps.append(
            f"Unknown source attribution: {source['unattributed_count']} saved record link(s) "
            "omitted."
        )
    if source.get("omitted_version_count", 0):
        gaps.append(f"{source['omitted_version_count']} differing saved version(s) omitted.")
    return gaps


def _saved_content(source: dict) -> list[str]:
    scopes = source.get("content_scopes")
    if not isinstance(scopes, dict):
        return ["Content type was not recorded in this saved scope."]
    labels = [
        f"{_CONTENT_LABELS.get(kind, 'Unknown saved content type')}: {count}"
        for kind, count in scopes.items()
        if isinstance(count, int) and not isinstance(count, bool) and count > 0
    ]
    return labels or ["No saved content included."]


def _readiness(source: dict):
    ready = source.get("ready")
    label = (
        "Configured"
        if ready is True
        else "Setup or access needed"
        if ready is False
        else "Readiness was not recorded"
    )
    return [
        badge(label, "amber" if ready is False else "neutral"),
        *(
            [html.P(str(source["connection_status"]), className="rv-small")]
            if source.get("connection_status")
            else []
        ),
    ]


def _version_policy(scope: dict):
    policy = scope.get("version_policy")
    if policy == "latest_selected_observation_then_source_id":
        label = "One newest saved version per native item from selected sources."
        explanation = (
            "Latest saved observation wins. A source-ID tie-break is a selection convention, "
            "not proof of the newest platform edit."
        )
    elif policy == "one_saved_version_per_native_identity":
        label = "One saved version per native item."
        explanation = "The version preference order was not recorded in this saved scope."
    else:
        label = "Saved-version selection policy was not recorded."
        explanation = "No version preference is inferred from the saved evidence."
    return html.P(
        [html.Strong(label), " ", explanation],
        className="rv-small",
    )


def _omission_notice(sources: list[dict]):
    if not any(
        source.get("unattributed_count", 0) or source.get("omitted_version_count", 0)
        for source in sources
    ):
        return None
    return _notice(
        "Text and content from omitted observations are not included or available in this "
        "case or its package. Hashes identify saved observations; they are not readable evidence. "
        "Unknown source attribution is not inferred."
    )


def _version_details(source: dict, source_labels: dict[str, str]):
    omissions = source.get("version_omissions") or []
    count = len(omissions) or source.get("omitted_version_count", 0)
    if not count:
        return None

    def observation(entry):
        preferred_id = entry.get("included_source_id")
        preferred_label = source_labels.get(preferred_id, "Source label not recorded")
        included = entry.get("included_in_case")
        included_label = (
            "Included in this case"
            if included is True
            else "Outside case record limit"
            if included is False
            else "Inclusion in this case was not recorded"
        )
        content_hash, preferred_content_hash = (
            entry.get("content_hash"),
            entry.get("included_content_hash"),
        )
        text_comparison = (
            "Saved text content hashes match; other observation metadata differs."
            if content_hash and content_hash == preferred_content_hash
            else "Saved text content hashes differ."
            if content_hash and preferred_content_hash
            else "A saved text content comparison was not recorded."
        )
        return html.Li(
            [
                html.P(
                    f"Native item: {entry.get('channel_id') or 'Unknown channel'} / "
                    f"{entry.get('native_id') or 'Unknown native ID'}"
                ),
                html.Dl(
                    [
                        html.Dt("Omitted selected observation"),
                        html.Dd(
                            [
                                html.Div("Observed: " + str(entry.get("observed_at") or "Unknown")),
                                html.Div(
                                    "Full saved observation payload hash: "
                                    + str(entry.get("payload_hash") or "Not recorded"),
                                    style={"overflowWrap": "anywhere"},
                                ),
                                html.Div(
                                    "Saved text content hash: "
                                    + str(content_hash or "Not recorded"),
                                    style={"overflowWrap": "anywhere"},
                                ),
                            ],
                            style=_WRAP_STYLE,
                        ),
                        html.Dt("Preferred selected observation"),
                        html.Dd(
                            [
                                html.Div(preferred_label),
                                html.Div(
                                    "Observed: "
                                    + str(entry.get("included_observed_at") or "Unknown")
                                ),
                                html.Div(
                                    "Full saved observation payload hash: "
                                    + str(entry.get("included_payload_hash") or "Not recorded"),
                                    style={"overflowWrap": "anywhere"},
                                ),
                                html.Div(
                                    "Saved text content hash: "
                                    + str(preferred_content_hash or "Not recorded"),
                                    style={"overflowWrap": "anywhere"},
                                ),
                                html.Strong(included_label),
                            ],
                            style=_WRAP_STYLE,
                        ),
                        html.Dt("Reason"),
                        html.Dd(
                            str(entry.get("reason") or "The omission reason was not recorded."),
                            style=_WRAP_STYLE,
                        ),
                        html.Dt("Saved text comparison"),
                        html.Dd(text_comparison, style=_WRAP_STYLE),
                        *(
                            [
                                html.Dt("Observation-time tie"),
                                html.Dd(
                                    "Equal observation times: the source-ID convention selects "
                                    "the preferred observation. This does not prove which "
                                    "platform edit is newest.",
                                    style=_WRAP_STYLE,
                                ),
                            ]
                            if entry.get("observation_time_tied") is True
                            else []
                        ),
                    ],
                    className="rv-small",
                    style=_WRAP_STYLE,
                ),
            ],
            style=_WRAP_STYLE,
        )

    return html.Details(
        [
            html.Summary(
                f"{source.get('label') or 'Saved source'}: "
                f"inspect {count} differing saved observation(s) omitted",
                style=_WRAP_STYLE,
            ),
            html.Ol([observation(entry) for entry in omissions], style=_WRAP_STYLE)
            if omissions
            else html.P(
                "Individual omitted observations were not recorded in this saved scope.",
                className="rv-small rv-muted",
            ),
        ],
        className="rv-details",
        style=_WRAP_STYLE,
    )


def scope_preview(summary: dict):
    """Keep failed and empty selected sources beside the saved evidence counts."""
    rows = summary.get("sources", [])
    source_labels = {row["id"]: row.get("label") or "Saved source" for row in rows}
    disclosures = [
        details
        for source in rows
        if (details := _version_details(source, source_labels)) is not None
    ]

    def source_row(source):
        status = source.get("last_status", "idle")
        note = source.get("last_error")
        return html.Tr(
            [
                html.Th(
                    [
                        html.Strong(source.get("label") or "Saved source"),
                        html.Div(
                            PLATFORM_LABELS.get(source.get("platform"), "Other"),
                            className="rv-small rv-muted",
                        ),
                    ],
                    scope="row",
                ),
                html.Td(
                    [
                        html.Strong(f"{source.get('included_count', 0)} included"),
                        html.Div(
                            f"{source.get('item_count', 0)} saved · "
                            f"{source.get('unknown_publication_times', 0)} "
                            "unknown publication times",
                            className="rv-small rv-muted",
                        ),
                    ]
                ),
                html.Td(html.Ul([html.Li(label) for label in _saved_content(source)])),
                html.Td(_readiness(source)),
                html.Td(
                    [
                        badge(
                            _STATUS_LABELS.get(status, "Status unavailable"),
                            "amber" if status in {"failed", "queued", "running"} else "neutral",
                        ),
                        html.Div(
                            "Last collected: "
                            + str(source.get("last_collected_at") or "Never collected"),
                            className="rv-small rv-muted",
                        ),
                        *([html.P(str(note), className="rv-small")] if note else []),
                    ]
                ),
                html.Td(
                    html.Ul([html.Li(gap) for gap in _scope_gaps(source)])
                    if _scope_gaps(source)
                    else "No attribution or version omissions recorded."
                    if "unattributed_count" in source and "omitted_version_count" in source
                    else "Attribution and version omissions were not recorded."
                ),
            ]
        )

    return html.Div(
        [
            html.H3("Source selection preview"),
            html.P(
                f"{len(rows)} selected source(s) · "
                f"{summary.get('record_count', 0)} saved record(s) in this case · "
                f"{summary.get('unknown_publication_times', 0)} with unknown publication time."
            ),
            _version_policy(summary),
            _omission_notice(rows),
            html.Div(
                html.Table(
                    [
                        html.Caption("Selected sources and saved collection coverage"),
                        html.Thead(
                            html.Tr(
                                [
                                    html.Th(label, scope="col")
                                    for label in (
                                        "Selected source",
                                        "Record coverage",
                                        "Saved content",
                                        "Setup and access",
                                        "Collection history",
                                        "Scope omissions",
                                    )
                                ]
                            )
                        ),
                        html.Tbody([source_row(source) for source in rows]),
                    ],
                    style={"width": "100%", "minWidth": "850px", "textAlign": "left"},
                ),
                tabIndex=0,
                role="region",
                className="traced-focus",
                style={"overflowX": "auto", "minWidth": 0, "maxWidth": "100%"},
                **{"aria-label": "Selected sources and saved collection coverage"},
            ),
            html.P(
                "Scroll the table sideways to see all source details. Source counts can "
                "overlap; each saved record is counted once in the case.",
                className="rv-small rv-muted",
            ),
            *(
                [
                    html.Section(
                        [html.H3("Omitted saved observations"), *disclosures],
                        style=_WRAP_STYLE,
                    )
                ]
                if disclosures
                else []
            ),
            *(
                [
                    _notice(
                        "This case uses a bounded selection of saved records. "
                        "Additional saved records are outside this snapshot."
                    )
                ]
                if summary.get("truncated")
                else []
            ),
            *(
                [
                    html.H3("Collection limits"),
                    html.Ul([html.Li(str(note)) for note in summary["limitations"]]),
                ]
                if summary.get("limitations")
                else []
            ),
        ],
        style={"display": "grid", "gap": "12px", "minWidth": 0, "maxWidth": "100%"},
    )


def register_scope_callbacks(app: Dash, service) -> None:
    @app.callback(
        Output("live-case-saved-scope", "children"),
        Input("rv-session", "data"),
    )
    def saved_scope(state):
        if not state or not state.get("case_id"):
            return ""
        try:
            bundle = service.get_case(state["case_id"])
            scope = bundle.get("case", {}).get("coverage", {}).get("collection_scope")
            if not isinstance(scope, dict):
                return ""
            sources = scope.get("sources", [])
            gaps = [
                source
                for source in sources
                if source.get("last_status") in {"failed", "queued", "running", "cancelled"}
                or not source.get("included_count", 0)
                or source.get("ready") is False
                or _scope_gaps(source)
            ]
            return html.Section(
                [
                    html.H3("Saved source scope"),
                    html.P(
                        f"{len(sources)} selected source(s) · "
                        f"{scope.get('record_count', 0)} saved record(s) · "
                        f"{scope.get('unknown_publication_times', 0)} "
                        "with unknown publication time. "
                        "This scope was saved with the case."
                    ),
                    _version_policy(scope),
                    _omission_notice(sources),
                    *([html.Ul([html.Li(_gap_text(source)) for source in gaps])] if gaps else []),
                    html.Details(
                        [
                            html.Summary("View saved source selection and collection limits"),
                            scope_preview(scope),
                            html.P(
                                "Later collection updates appear in a new case preview. "
                                "Assessment exclusions are reviewed separately.",
                                className="rv-small rv-muted",
                            ),
                        ],
                        className="rv-details",
                    ),
                ],
                className="rv-details",
                style={"minWidth": 0, "maxWidth": "100%"},
            )
        except Exception:
            return _notice("The saved source scope is unavailable. Reopen the case to try again.")

    @app.callback(
        Output("live-case-sources", "options"),
        Input("live-revision", "data"),
        Input("live-poll", "n_intervals"),
    )
    def source_choices(_revision, _poll):
        try:
            sources = service.list_collection_sources()
        except Exception:
            return no_update
        return [
            {
                "label": (
                    f"{PLATFORM_LABELS.get(source.get('platform'), 'Other')} · "
                    f"{source.get('label') or 'Saved source'} · "
                    f"{source.get('item_count', 0)} saved · "
                    f"{_STATUS_LABELS.get(source.get('last_status'), 'Status unavailable')}"
                    + (" · setup or access needed" if source.get("ready") is False else "")
                ),
                "value": source["id"],
            }
            for source in sources
        ]

    @app.callback(
        Output("live-case-preview", "children"),
        Output("live-case-token", "data"),
        Output("live-case-create", "disabled"),
        Output("live-case-message", "children"),
        Input("live-case-sources", "value"),
        Input("live-case-preview-action", "n_clicks"),
        prevent_initial_call=True,
    )
    def preview(selected, clicks):
        if ctx.triggered_id == "live-case-sources":
            return "", None, True, "Preview this selection before creating a case."
        if not clicks:
            raise PreventUpdate
        try:
            ids = _source_ids(selected)
            if not ids:
                raise ValueError("Choose at least one saved source, then preview the selection.")
            summary = service.preview_collection_case(ids)
            allowed = bool(summary.get("can_create"))
            return (
                scope_preview(summary),
                {"fingerprint": summary["fingerprint"], "source_ids": summary["source_ids"]},
                not allowed,
                (
                    "Preview only. No case or assessment has changed."
                    if allowed
                    else summary.get("reason")
                    or "Collect a selected source before creating a case."
                ),
            )
        except ValueError as exc:
            message = str(exc)
        except Exception:
            message = "The source selection could not be previewed. Try again."
        return "", None, True, _notice(message, error=True)

    @app.callback(
        Output("rv-case", "options", allow_duplicate=True),
        Output("rv-case", "value", allow_duplicate=True),
        Output("rv-session", "data", allow_duplicate=True),
        Output("rv-page", "value", allow_duplicate=True),
        Output("rv-import-success", "children", allow_duplicate=True),
        Output("live-case-message", "children", allow_duplicate=True),
        Input("live-case-create", "n_clicks"),
        State("live-case-token", "data"),
        State("live-case-sources", "value"),
        prevent_initial_call=True,
    )
    def create_case(clicks, token, selected):
        if not clicks:
            raise PreventUpdate
        try:
            ids = _source_ids(selected)
            if (
                not ids
                or not isinstance(token, dict)
                or not isinstance(token.get("fingerprint"), str)
                or token.get("source_ids") != ids
            ):
                raise ValueError("Preview this source selection again before creating a case.")
            case_id = service.open_collection_case(
                source_ids=ids, expected_fingerprint=token["fingerprint"]
            )
            if not case_id:
                raise ValueError("No saved records are available in this source selection.")
            options = [
                {"label": case["title"], "value": str(case["id"])} for case in service.list_cases()
            ]
            notice = _notice(
                "Case created from the selected sources. "
                "Its saved evidence and collection limits are ready to review."
            )
            return options, case_id, session_for_case(service, case_id), "review", notice, ""
        except ValueError as exc:
            message = str(exc)
        except Exception:
            message = "The selected-source case could not be created. Preview it and try again."
        return (*[no_update] * 5, _notice(message, error=True))
