"""Draft dossier and import receipts over saved evidence; no workflow writes."""

from __future__ import annotations

from collections.abc import Mapping

from dash import Dash, Input, Output, State, dcc, html, no_update

from ..comparison_ui import safe_source_url
from ..navigation import NextAction, navigation_context
from ..selectors import evidence_ids
from .use_cases_ui import selected_purpose


def handoff_panel() -> html.Section:
    return html.Section(
        [
            dcc.Store(id="trail-handoff-context", storage_type="memory"),
            html.Div(id="trail-handoff-summary", **{"aria-live": "polite"}),
            html.P(
                "Evidence JSON excludes planning history and media. "
                "Use Download case package for the complete handoff.",
                className="rv-small rv-muted",
            ),
            html.Div(
                [
                    html.Button(
                        "Refresh handoff",
                        id="trail-handoff-refresh",
                        n_clicks=0,
                        className="rv-button rv-button--outline",
                    ),
                    html.Button(
                        "Download draft dossier",
                        id="trail-dossier",
                        n_clicks=0,
                        disabled=True,
                        className="rv-button rv-button--outline",
                    ),
                ],
                className="rv-toolbar rv-review-buttons",
            ),
            html.Div(id="trail-dossier-message", role="status", **{"aria-live": "polite"}),
        ],
        className="rv-handoff",
        **{"aria-label": "Draft dossier handoff"},
    )


def _facts(values: Mapping) -> html.Dl:
    """Keep saved coverage readable without adding or inferring scope facts."""

    def value_node(value):
        if isinstance(value, Mapping):
            return _facts(value)
        if isinstance(value, (list, tuple)):
            return html.Ul([html.Li(value_node(item)) for item in value]) if value else "None saved"
        if value is None:
            return "Unknown"
        if isinstance(value, bool):
            return "Yes" if value else "No"
        return str(value)

    return html.Dl(
        [
            node
            for label, value in values.items()
            for node in [
                html.Dt(str(label).replace("_", " ").capitalize()),
                html.Dd(value_node(value)),
            ]
        ],
        className="rv-metadata rv-handoff-facts",
    )


def _counts(counts: Mapping, fields) -> html.Div:
    return html.Div(
        [
            html.Div(
                [html.Strong(str(counts.get(key, 0))), html.Span(label)],
                className="rv-handoff-count",
            )
            for key, label in fields
        ],
        className="rv-handoff-counts",
    )


def _action(context, kind, label, location, *, finding_id="", record_id=""):
    action = NextAction(
        kind,
        label,
        "Inspect this saved dossier item in its current assessment.",
        context,
        finding_id=finding_id,
        record_id=record_id,
        location=location,
    )
    return html.Button(
        label, id=action.button_id(), n_clicks=0, className="rv-button rv-button--outline"
    )


def _gap_target(gap, view):
    bundle = view["bundle"]
    if gap.get("record_id"):
        record = next((r for r in bundle["records"] if r["id"] == gap["record_id"]), None)
        return record["source_label"] if record else gap["record_id"]
    finding = next(
        (f for f in bundle["findings"] if f["snapshot_id"] == gap.get("finding_id")), None
    )
    return finding["statement"] if finding else None


def _gap_action(gap, index, context, view, assessment):
    action, fid, rid = gap.get("action"), gap.get("finding_id") or "", gap.get("record_id") or ""
    bundle = view["bundle"]
    current = [
        finding["snapshot_id"]
        for finding in bundle.get("findings", [])
        if finding.get("run_id") == assessment["base_run_id"]
    ]
    location = f"handoff-gap-{index}"
    if action == "inspect_record" and rid in {row["id"] for row in bundle.get("records", [])}:
        linked = [item for item in current if rid in evidence_ids(bundle, item)[0]]
        fid = fid if fid in linked else next(iter(linked), "")
        return _action(context, "source", "Inspect source", location, finding_id=fid, record_id=rid)
    if action in {"review_finding", "save_draft"} and fid in current:
        return _action(context, "review", "Open review", location, finding_id=fid)
    if action == "check_scope":
        return _action(context, "purpose", "Review output guide", location)
    if action == "check_references":
        return html.A("Inspect reference dates", href="#trail-handoff-references")
    return None


def _references(references) -> html.Details:
    dates = sorted({str(ref["retrieved_at"]) for ref in references if ref.get("retrieved_at")})
    return html.Details(
        [
            html.Summary("Policy references and dates"),
            html.P(
                "Retrieved: " + ", ".join(dates) if dates else "Reference retrieval date unknown.",
                className="rv-small",
            ),
            html.P(
                "Applicability is unknown. These references do not establish a policy violation, "
                "legal conclusion, recipient acceptance, or submission.",
                className="rv-small rv-muted",
            ),
            html.Ul(
                [
                    html.Li(
                        [
                            html.A(
                                ref["title"],
                                href=safe_source_url(str(ref["url"])),
                                target="_blank",
                                rel="noopener noreferrer",
                            )
                            if safe_source_url(str(ref["url"]))
                            else html.Span(ref["title"]),
                            html.Span(
                                f" · retrieved {ref.get('retrieved_at') or 'Unknown'}"
                                + (f" · {ref['version']}" if ref.get("version") else ""),
                                className="rv-small rv-muted",
                            ),
                        ]
                    )
                    for ref in references
                ]
            ),
        ],
        id="trail-handoff-references",
        className="rv-details",
    )


def _planning_history(planning: Mapping | None) -> html.Details:
    planning = planning or {}
    counts = planning.get("counts", {})
    return html.Details(
        [
            html.Summary(
                f"Planning history · {counts.get('revisions', 0)} revisions · "
                f"{counts.get('confirmations', 0)} recorded confirmations"
            ),
            html.P(
                planning.get("summary") or "No planning history is included in this handoff.",
                className="rv-small",
            ),
            html.P(
                "These are saved revisions and attributed confirmation records. "
                "They do not identify a current local revision or grant local approval.",
                className="rv-small rv-muted",
            ),
            *[
                html.Details(
                    [
                        html.Summary(f"Revision {revision['number']} · {revision['revision_id']}"),
                        _facts(
                            {
                                "Brief": revision["brief_id"],
                                "Revision": revision["revision_id"],
                                "Parent revision": revision.get("parent_revision_id"),
                                "Author": revision["author"],
                                "Saved at": revision["created_at"],
                                "Content hash": revision["content_hash"],
                                "Evidence binding": revision.get("binding")
                                or "Unbound ancestor revision",
                                "Saved planning fields": revision["fields"],
                                "Recorded confirmation IDs": revision.get("confirmation_ids", []),
                            }
                        ),
                    ],
                    className="rv-details",
                )
                for revision in planning.get("revisions", [])
            ],
            *[
                html.Div(
                    [
                        html.Strong(
                            f"Recorded confirmation by {confirmation['reviewer']} "
                            f"on {confirmation['confirmed_at']}"
                        ),
                        _facts(
                            {
                                "Confirmation": confirmation["confirmation_id"],
                                "Brief": confirmation["brief_id"],
                                "Exact revision": confirmation["revision_id"],
                                "Binding digest": confirmation["binding_digest"],
                                "Recorded provenance": confirmation["provenance"],
                            }
                        ),
                        html.P(
                            "Recorded attribution only; no local approval is granted.",
                            className="rv-small rv-muted",
                        ),
                    ],
                    className="rv-handoff-item",
                )
                for confirmation in planning.get("confirmations", [])
            ],
            html.Ul([html.Li(item) for item in planning.get("limitations", [])]),
        ],
        className="rv-details",
    )


def render_handoff(handoff: dict, view: dict, state: dict) -> html.Div:
    context = navigation_context(view, state)
    assessment, purpose, counts = handoff["assessment"], handoff["purpose"], handoff["counts"]
    omitted = handoff["brief"]["omitted"]
    gaps = handoff["gaps"]
    scope = handoff["scope"]
    return html.Div(
        [
            html.Div(
                [
                    html.H2("Draft dossier"),
                    html.Span("Draft · not submitted", className="rv-badge"),
                ],
                className="rv-toolbar",
            ),
            html.P(
                "A saved snapshot for handoff. Downloading does not record approval or acceptance.",
                className="rv-small rv-muted",
            ),
            html.P(
                [
                    "Session-only output guide purpose: ",
                    html.Strong(purpose.get("title") or "No output guide selected"),
                    ". Saved planning source selections and attributed confirmations "
                    "are shown separately below.",
                ],
                className="rv-small",
            ),
            html.P(
                f"{assessment['label']} · Version {assessment['version']}",
                className="rv-small rv-handoff-context",
            ),
            _counts(
                counts,
                [
                    ("current_decisions", "saved decisions"),
                    ("included", "included"),
                    ("withheld", "withheld"),
                    ("unreviewed", "unreviewed"),
                    ("stale", "findings need review"),
                ],
            ),
            html.P(handoff["readiness"]["summary"], className="rv-small"),
            html.Div(
                "Unsaved planning changes. Save your planning draft before downloading "
                "this handoff."
                if handoff.get("planning_dirty")
                else "Unsaved changes. Save your decision in Review before downloading.",
                className="rv-notice",
                role="status",
            )
            if handoff["dirty"]
            else None,
            _planning_history(handoff.get("planning")),
            html.Details(
                [
                    html.Summary(f"Omitted statements ({len(omitted)})"),
                    *[
                        html.Div(
                            [
                                html.Strong(row["statement"]),
                                html.P(
                                    f"{row['status'].replace('_', ' ').capitalize()}: "
                                    + row["reason"],
                                    className="rv-small rv-muted",
                                ),
                                _action(
                                    context,
                                    "review",
                                    "Review this statement",
                                    f"handoff-omission-{i}",
                                    finding_id=row["finding_id"],
                                ),
                            ],
                            className="rv-handoff-item",
                        )
                        for i, row in enumerate(omitted)
                    ],
                    html.P(
                        "No statements omitted from this selected assessment.", className="rv-small"
                    )
                    if not omitted
                    else None,
                ],
                className="rv-details",
            ),
            html.Details(
                [
                    html.Summary(f"Dossier gaps ({len(gaps)})"),
                    *[
                        html.Div(
                            [
                                html.Strong(gap["label"]),
                                html.P(_gap_target(gap, view), className="rv-small"),
                                html.P(gap["detail"], className="rv-small rv-muted"),
                                _gap_action(gap, i, context, view, assessment),
                            ],
                            className="rv-handoff-item",
                        )
                        for i, gap in enumerate(gaps)
                    ],
                    html.P("No additional gaps identified by this saved-state check.")
                    if not gaps
                    else None,
                ],
                className="rv-details",
            ),
            html.Details(
                [
                    html.Summary("Evidence, media, and saved coverage"),
                    _counts(
                        counts,
                        [
                            ("records", "records"),
                            ("fragments", "exact references"),
                            ("media", "media records"),
                            ("available_media", "available media"),
                            ("unavailable_media", "unavailable media"),
                            ("unknown_media", "unchecked media"),
                        ],
                    ),
                    html.P(
                        "Media availability uses the existing local artifact and content check. "
                        "Text-only records need no media file. The dossier lists gaps; "
                        "it does not bundle media files.",
                        className="rv-small rv-muted",
                    ),
                    _facts(
                        {
                            "Analysis run": assessment.get("base_run_id") or "Unknown",
                            "Native collection source selection": scope.get("collection_scope")
                            or "Not recorded in native evidence.",
                            "Saved coverage": scope.get("coverage") or "Not recorded",
                        }
                    ),
                    html.Ul([html.Li(item) for item in scope.get("limitations", [])]),
                ],
                className="rv-details",
            ),
            _references(handoff["references"]) if handoff["references"] else None,
        ],
        className="rv-handoff-content",
    )


def render_import_receipt(receipt: dict) -> html.Div:
    counts = receipt["counts"]
    recomputation = receipt["recomputation"]
    return html.Div(
        [
            html.Strong("Case imported and validated"),
            html.P("Import receipt for the saved snapshot.", className="rv-small rv-muted"),
            html.P(receipt["case"]["title"]),
            html.P(receipt["validation"]["note"], className="rv-small"),
            html.P("Recipient acceptance is not recorded.", className="rv-small rv-muted"),
            html.P(
                "Evidence JSON excludes planning history and media. "
                "Use Download case package for the complete handoff.",
                className="rv-small rv-muted",
            ),
            _counts(
                counts,
                [
                    ("records", "records"),
                    ("fragments", "exact references"),
                    ("runs", "saved runs"),
                    ("assessments", "saved assessments"),
                    ("reviews", "saved reviews"),
                ],
            ),
            html.P(
                "Planning details describe the history retained in this workspace.",
                className="rv-small rv-muted",
            ),
            _planning_history(receipt.get("planning")),
            html.Details(
                [
                    html.Summary("Imported media and recomputation context"),
                    _counts(
                        counts,
                        [
                            ("media", "media records"),
                            ("available_media", "available media"),
                            ("unavailable_media", "unavailable media"),
                            ("unknown_media", "unchecked media"),
                        ],
                    ),
                    html.P(recomputation["reason"], className="rv-small"),
                    html.P(
                        "Importing restores saved evidence and history. It does not run "
                        "an analysis or establish that a result was recomputed.",
                        className="rv-small rv-muted",
                    ),
                    html.Ul([html.Li(item) for item in receipt.get("limitations", [])]),
                ],
                className="rv-details",
            ),
        ],
        className="rv-notice rv-handoff-receipt",
        role="status",
    )


def _project(service, state, drafts, note, selection, planning_draft=None):
    from .handoff_service import load_handoff_context

    selection = selection if isinstance(selection, Mapping) else {}
    purpose = selected_purpose(selection.get("purpose_id"))
    view, handoff, _binding = load_handoff_context(
        service,
        state,
        drafts=drafts,
        note=note,
        purpose_id=purpose["purpose_id"] if purpose else None,
        planning_draft=planning_draft,
    )
    return view, handoff


def register_handoff_callbacks(app: Dash, service) -> None:
    @app.callback(
        Output("trail-handoff-summary", "children"),
        Output("trail-dossier", "disabled"),
        Output("trail-handoff-context", "data"),
        Input("rv-session", "data"),
        Input("rv-draft-store", "data"),
        Input("rv-review-reason", "value"),
        Input("trail-use-case-purpose", "data"),
        Input("trail-handoff-refresh", "n_clicks"),
        Input("trail-planning-refresh", "data"),
        Input("trail-planning-selected", "data"),
    )
    def projection(
        state, drafts, note, purpose, refresh_clicks=0, planning_refresh=None, planning_draft=None
    ):
        try:
            view, handoff = _project(service, state, drafts, note, purpose, planning_draft)
            return (
                render_handoff(handoff, view, state),
                not handoff["export_allowed"],
                handoff["download_context"],
            )
        except (ValueError, KeyError, OSError) as exc:
            return html.Div(str(exc), className="rv-notice", role="status"), True, None

    @app.callback(
        Output("trail-download", "data", allow_duplicate=True),
        Output("trail-dossier-message", "children"),
        Input("trail-dossier", "n_clicks"),
        State("rv-session", "data"),
        State("rv-draft-store", "data"),
        State("rv-review-reason", "value"),
        State("trail-use-case-purpose", "data"),
        State("trail-handoff-context", "data"),
        State("trail-planning-selected", "data"),
        prevent_initial_call=True,
    )
    def download(clicks, state, drafts, note, purpose, expected_context=None, planning_draft=None):
        if not clicks:
            return no_update, no_update
        from .handoff_service import prepare_dossier

        try:
            selection = purpose if isinstance(purpose, Mapping) else {}
            selected = selected_purpose(selection.get("purpose_id"))
            prepared = prepare_dossier(
                service,
                state,
                drafts=drafts,
                note=note,
                purpose_id=selected["purpose_id"] if selected else None,
                expected_context=expected_context,
                planning_draft=planning_draft,
            )
            return (
                dcc.send_string(prepared.content, prepared.filename),
                prepared.message,
            )
        except (ValueError, KeyError, OSError) as exc:
            return no_update, html.Div(str(exc), className="rv-notice", role="status")

    @app.callback(
        Output("trail-dossier-message", "children", allow_duplicate=True),
        Output("trail-export-attempt", "data", allow_duplicate=True),
        Input("rv-session", "data"),
        Input("rv-draft-store", "data"),
        Input("rv-review-reason", "value"),
        Input("trail-use-case-purpose", "data"),
        Input("trail-handoff-refresh", "n_clicks"),
        Input("trail-planning-refresh", "data"),
        Input("trail-planning-selected", "data"),
        prevent_initial_call=True,
    )
    def clear_message(
        _state,
        _drafts,
        _note,
        _purpose,
        _refresh_clicks=0,
        _planning_refresh=None,
        _planning_draft=None,
    ):
        return "", None
