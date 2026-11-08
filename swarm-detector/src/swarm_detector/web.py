"""Standalone Swarm Dash composition and callbacks for pinned specialist reviews.

Rendering helpers live in layout/presentation modules and operations go through
``Workbench``. Importing this module creates no app; calling ``create_app`` may
initialize storage and seed a fictional sample through the existing startup flow.
The integrated Evidence Trail interface is composed separately by Explorer.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from dash import ALL, Dash, Input, Output, State, ctx, html, no_update
from dash.exceptions import PreventUpdate
from flask import Response, request

from .layout import create_layout
from .presentation import (
    case_preview,
    case_view,
    contextual_sources,
    episode_options,
    evidence_view,
    pair_options,
    queue_cards,
    readiness_view,
    stress_result,
    with_selected_pair,
)
from .review_state import (
    FIELD_IDS,
    FIELDS,
    dirty,
    export_preview,
    form_values,
    open_review,
    preserve_draft,
)
from .review_state import save_review as save_form
from .service import Workbench

logger = logging.getLogger(__name__)


def _scoped_detail(detail: dict) -> dict:
    detail = dict(detail)
    dataset, run = detail.get("dataset") or {}, detail.get("run") or {}
    detail["dataset"] = {
        key: dataset[key]
        for key in ("dataset_id", "label", "synthetic", "manifest", "channels")
        if key in dataset
    }
    detail["run"] = {
        key: run[key]
        for key in ("run_id", "dataset_id", "config", "diagnostics", "created_at")
        if key in run
    }
    return detail


def create_app(
    workbench: Workbench | None = None,
    *,
    server=None,
    url_base_pathname: str = "/",
    workbench_links: list[tuple[str, str]] | None = None,
) -> Dash:
    """Build the specialist app and run its existing sample-initialization step.

    ``ensure_sample`` is called even for an injected workbench. On an empty
    store it persists fictional data and runs detection; startup errors are
    retained for display. App construction does not start the shared worker.
    """
    auth_user, auth_password = (
        os.environ.get("SWARM_BASIC_AUTH_USER", ""),
        os.environ.get("SWARM_BASIC_AUTH_PASSWORD", ""),
    )
    if bool(auth_user) != bool(auth_password):
        raise RuntimeError(
            "Set both SWARM_BASIC_AUTH_USER and SWARM_BASIC_AUTH_PASSWORD, or neither."
        )
    wb = workbench if workbench is not None else Workbench()
    boot_error = None
    try:
        wb.ensure_sample()
    except Exception as exc:
        logger.exception("Unable to initialize the workbench")
        boot_error = str(exc)
    application = Dash(
        __name__,
        title="Traced · Swarm Detector",
        assets_folder=str(Path(__file__).with_name("assets")),
        url_base_pathname=url_base_pathname,
        **({"server": server} if server is not None else {}),
        suppress_callback_exceptions=True,
        update_title="Loading evidence…",
    )
    application.server.config["WORKBENCH"] = wb

    health_path = url_base_pathname.rstrip("/") + "/health"

    @application.server.before_request
    def authenticate():
        if not auth_user or request.path == health_path:
            return None
        supplied = request.authorization
        if (
            supplied
            and supplied.type.lower() == "basic"
            and secrets.compare_digest((supplied.username or "").encode(), auth_user.encode())
            and secrets.compare_digest((supplied.password or "").encode(), auth_password.encode())
        ):
            return None
        return Response(
            "Authentication required",
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="Swarm Detector", charset="UTF-8"'},
        )

    @application.server.get(health_path, endpoint="swarm-health")
    def health():
        try:
            result = wb.health()
            response = {
                "status": "ok" if result.get("ready") else "unavailable",
                "ready": bool(result.get("ready")),
            }
            return response, 200 if response["ready"] else 503
        except Exception:
            logger.exception("Unable to read application health")
            return {"status": "unavailable", "ready": False}, 503

    def layout():
        try:
            readiness = wb.readiness()
            run_id = readiness.get("run_id")
            overview = wb.overview(run_id=run_id) if run_id else wb.overview()
            return create_layout(overview, boot_error, readiness, workbench_links=workbench_links)
        except Exception as exc:
            logger.exception("Unable to load workspace readiness")
            return create_layout({}, str(exc), {}, workbench_links=workbench_links)

    application.layout = layout

    @application.callback(
        Output("readiness-checks", "children"),
        Output("coverage-summary", "children"),
        Output("readiness-limitations", "children"),
        Input("workspace-store", "data"),
    )
    def render_readiness(workspace):
        return readiness_view(workspace or {})

    @application.callback(
        Output("case-select", "options"),
        Output("case-select", "value"),
        Output("queue-list", "children"),
        Output("queue-count", "children"),
        Input("case-search", "value"),
        Input("case-kind", "value"),
        Input("case-status", "value"),
        Input("screen-select", "data"),
        Input("workspace-store", "data"),
        State("case-select", "value"),
    )
    def update_queue(query, kind, status, screen, workspace, selected):
        if ctx.triggered_id == "screen-select" and screen != "find":
            return no_update, no_update, no_update, no_update
        run_id = (workspace or {}).get("run_id")
        if not run_id:
            return (
                [],
                None,
                html.Div(
                    "Analysis is not ready. Inspect readiness and complete the saved analysis before opening a case.",
                    className="empty-state",
                ),
                "No saved analysis",
            )
        try:
            page = wb.case_page(
                query=query or "", kind=kind or "all", status=status or "all", run_id=run_id
            )
            cases = page["items"]
            unreviewed = {None, "", "unreviewed", "Unreviewed", "Not reviewed"}
        except Exception as exc:
            logger.exception("Unable to load the review queue")
            return (
                [],
                None,
                html.Div(
                    f"Cases unavailable: {exc}. Reopen the workspace after checking the saved analysis.",
                    className="error-banner",
                    role="alert",
                ),
                "Unavailable",
            )
        options = [{"label": c.get("name", c["case_id"]), "value": c["case_id"]} for c in cases]
        current = (
            selected
            if selected in {c["case_id"] for c in cases}
            else (cases[0]["case_id"] if cases else None)
        )
        cards = queue_cards(cases, current, unreviewed)
        empty = (
            "No cases match these filters. Clear filters to inspect the full shortlist."
            if query or kind != "all" or status != "all"
            else "No groups met these settings. This does not establish the absence of coordination."
        )
        return (
            options,
            current,
            cards or html.Div(empty, className="empty-state"),
            f"Showing {len(cases)} of {page.get('total', len(cases))} cases"
            + (" · narrow the search to inspect additional cases" if page.get("has_more") else ""),
        )

    @application.callback(
        Output("case-search", "value"),
        Output("case-kind", "value"),
        Output("case-status", "value"),
        Input("clear-filters", "n_clicks"),
        prevent_initial_call=True,
    )
    def clear_filters(_clicks):
        return "", "all", "all"

    @application.callback(
        Output("case-select", "value", allow_duplicate=True),
        Input({"type": "case-card", "index": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def select_card(clicks):
        if not ctx.triggered_id or not any(clicks or []):
            raise PreventUpdate
        return ctx.triggered_id["index"]

    @application.callback(
        Output({"type": "case-card", "index": ALL}, "className"),
        Input("case-select", "value"),
        State({"type": "case-card", "index": ALL}, "id"),
        State({"type": "case-card", "index": ALL}, "className"),
    )
    def mark_selected(case_id, ids, names):
        return [
            (name or "queue-card").replace(" active", "")
            + (" active" if item["index"] == case_id else "")
            for item, name in zip(ids or [], names or [], strict=True)
        ]

    @application.callback(
        Output("detail-store", "data"),
        Input("case-select", "value"),
        Input("workspace-store", "data"),
    )
    def load_case(case_id, workspace):
        run_id = (workspace or {}).get("run_id")
        if not case_id or not run_id:
            return {}
        try:
            return _scoped_detail(wb.case_detail(case_id, run_id=run_id))
        except Exception as exc:
            logger.exception("Unable to load the selected case")
            return {"error": str(exc)}

    @application.callback(
        Output("screen-select", "data"),
        Input("nav-find", "n_clicks"),
        Input("nav-verify", "n_clicks"),
        Input("nav-review", "n_clicks"),
        Input("open-evidence", "n_clicks"),
        Input("continue-context", "n_clicks"),
        prevent_initial_call=True,
    )
    def navigate(*_clicks):
        return {
            "nav-find": "find",
            "nav-verify": "verify",
            "open-evidence": "verify",
            "nav-review": "review",
            "continue-context": "review",
        }.get(ctx.triggered_id, "find")

    @application.callback(
        Output("find-view", "style"),
        Output("verify-view", "style"),
        Output("review-view", "style"),
        Output("nav-find", "className"),
        Output("nav-verify", "className"),
        Output("nav-review", "className"),
        Output("screen-title", "children"),
        Output("screen-subtitle", "children"),
        Output("case-header", "style"),
        Output("compact-case-picker", "style"),
        Input("screen-select", "data"),
    )
    def screen_state(screen):
        screen = screen if screen in ("find", "verify", "review") else "find"
        titles = {
            "find": (
                "What deserves investigation?",
                "Start with usable data and a shortlist based on behavior.",
            ),
            "verify": (
                "Does the behavior hold up?",
                "Inspect repeated episodes, exact source records and alternative explanations.",
            ),
            "review": (
                "What can you responsibly conclude?",
                "Read community context beside your judgment, then preview a saved brief.",
            ),
        }
        return (
            *[
                {"display": "block" if screen == name else "none"}
                for name in ("find", "verify", "review")
            ],
            *[
                "nav-step active" if screen == name else "nav-step"
                for name in ("find", "verify", "review")
            ],
            *titles[screen],
            {"display": "none" if screen == "find" else "flex"},
            {"display": "none" if screen == "find" else "grid"},
        )

    application.clientside_callback(
        """function(screen) { return window.innerWidth > 900; }""",
        Output("connections-disclosure", "open"),
        Input("screen-select", "data"),
    )

    @application.callback(
        Output("case-header", "children"),
        Output("case-metrics", "children"),
        Output("timing-matrix", "figure"),
        Output("matrix-footnote", "children"),
        Output("pair-graph", "elements"),
        Output("account-summary", "children"),
        Output("counterevidence", "children"),
        Output("sensitivity-summary", "children"),
        Output("community-timeline", "figure"),
        Output("channel-list", "children"),
        Output("run-details", "children"),
        Output("case-preview", "children"),
        Output("review-counterevidence", "children"),
        Output("community-capability", "children"),
        Output("open-evidence", "disabled"),
        Output("continue-context", "disabled"),
        Output("nav-verify", "disabled"),
        Output("nav-review", "disabled"),
        Output("save-review", "disabled"),
        Output("render-brief", "disabled"),
        Input("detail-store", "data"),
        Input("workspace-store", "data"),
        Input("pair-select", "value"),
    )
    def render_case(detail, workspace, selected_pair_key=None):
        detail, workspace = detail or {}, workspace or {}
        views = case_view(detail, workspace.get("overview") or {}, selected_pair_key)
        ready = bool(
            detail.get("case")
            and (detail.get("run") or {}).get("run_id") == workspace.get("run_id")
        )
        capability = ((workspace.get("readiness") or {}).get("capabilities") or {}).get(
            "cross_channel_identity"
        )
        enabled = capability is True or capability in ("verified", "global", "verified_global")
        community_figure = views[10]
        if not enabled:
            from .ui import empty_figure

            community_figure = empty_figure(
                "Cross-channel identity is unverified. Cross-community interpretation is unavailable.",
                360,
            )
        capability_note = (
            "Channel categories describe discussions, not commenter nationality. The first observation is only the first within this dataset."
            if enabled
            else "Cross-channel identity is unverified; do not interpret matching names as the same person."
        )
        return (
            *views[:6],
            views[8],
            views[9],
            community_figure,
            views[11],
            views[12],
            case_preview(detail),
            views[8],
            capability_note,
            *([not ready] * 6),
        )

    @application.callback(Output("pair-graph", "style"), Input("pair-graph", "elements"))
    def connection_dimensions(elements):
        nodes = sum("source" not in item.get("data", {}) for item in elements or [])
        height = max(360, ((nodes + 1) // 2) * 96 + 60)
        return {"height": f"{height}px", "width": "100%"}

    application.clientside_callback(
        """function(clicks, elements, verifyStyle, disclosureOpen, disclosureClicks, style) {
            const no = window.dash_clientside.no_update;
            return new Promise(resolve => requestAnimationFrame(() => {
                requestAnimationFrame(() => {
                    const host = document.getElementById('pair-graph');
                    const disclosure = document.getElementById('connections-disclosure');
                    const page = document.getElementById('verify-view');
                    if (!host || !disclosure || !disclosure.open || !page ||
                        page.getClientRects().length === 0 || host.clientWidth === 0) {
                        resolve(no);
                        return;
                    }
                    // Dash Cytoscape exposes its live instance. Check ownership
                    // because another Cytoscape widget could also be mounted.
                    const cy = window.cy;
                    const container = cy && !cy.destroyed() && cy.container();
                    if (container && (container === host || host.contains(container))) {
                        // Native Details can reveal the same layout without a
                        // React prop change. Resize the live canvas, then fit
                        // explicitly rather than reusing a deep-equal layout.
                        cy.resize();
                        const positions = {};
                        (elements || []).forEach(e => {
                            if (e.data && !e.data.source && e.position) {
                                positions[e.data.id] = e.position;
                            }
                        });
                        cy.nodes().positions(node => positions[node.id()] || node.position());
                        cy.fit(cy.elements(), 18);
                        resolve({name: 'preset', fit: false, padding: 18, animate: false});
                    } else {
                        resolve({name: 'preset', fit: true, padding: 18, animate: false});
                    }
                });
            }));
        }""",
        Output("pair-graph", "layout"),
        Input("pair-graph-fit", "n_clicks"),
        Input("pair-graph", "elements"),
        Input("verify-view", "style"),
        Input("connections-disclosure", "open"),
        Input("connections-disclosure", "n_clicks"),
        Input("pair-graph", "style"),
    )

    @application.callback(
        Output("pair-select", "options"),
        Output("pair-select", "value"),
        Input("detail-store", "data"),
        Input("pair-graph", "tapEdgeData"),
        State("pair-select", "value"),
    )
    def select_pair(detail, edge, current):
        options = pair_options(detail or {})
        keys = {o["value"] for o in options}
        clicked = (edge or {}).get("pair_key") if ctx.triggered_id == "pair-graph" else None
        value = (
            clicked
            if clicked in keys
            else current
            if current in keys
            else options[0]["value"]
            if options
            else None
        )
        return options, value

    @application.callback(
        Output("selected-pair-store", "data"),
        Input("pair-select", "value"),
        Input("detail-store", "data"),
    )
    def load_pair(pair_key, detail):
        detail = detail or {}
        pair = next((p for p in detail.get("pairs", []) if p.get("pair_key") == pair_key), None)
        if not pair:
            return {}
        case_id, run_id = detail["case"]["case_id"], detail["run"]["run_id"]
        selection = {"case_id": case_id, "run_id": run_id, "pair_key": pair_key}
        try:
            page = wb.pair_events(case_id, pair_key, run_id=run_id)
            return {
                **selection,
                "pair": {**pair, "events": page["items"]},
                "pagination": {key: value for key, value in page.items() if key != "items"},
            }
        except Exception as exc:
            logger.exception("Unable to load the selected pair evidence")
            return {**selection, "error": str(exc)}

    @application.callback(
        Output("episode-select", "options"),
        Output("episode-select", "value"),
        Input("detail-store", "data"),
        Input("selected-pair-store", "data"),
        Input("timing-matrix", "clickData"),
        State("episode-select", "value"),
    )
    def select_episode(detail, selection, matrix_click, current):
        detail = with_selected_pair(detail or {}, selection)
        pair_key = (selection or {}).get("pair_key")
        options = episode_options(detail, pair_key)
        keys = {o["value"] for o in options}
        if ctx.triggered_id == "timing-matrix" and matrix_click:
            point = (matrix_click.get("points") or [{}])[0]
            custom = point.get("customdata") or []
            clicked = str(custom[1]) if len(custom) > 1 else None
            # A matrix cell can open another discussion, even outside the selected pair.
            all_options = episode_options(detail or {}, None)
            if clicked in {o["value"] for o in all_options}:
                return all_options, clicked
        return options, current if current in keys else options[0]["value"] if options else None

    @application.callback(
        Output("replay-clock", "disabled"),
        Output("play-replay", "children"),
        Output("replay-progress", "value"),
        Input("play-replay", "n_clicks"),
        Input("replay-clock", "n_intervals"),
        Input("episode-select", "value"),
        Input("screen-select", "data"),
        State("replay-clock", "disabled"),
        State("replay-progress", "value"),
        prevent_initial_call=True,
    )
    def advance_replay(_clicks, _ticks, _episode, screen, disabled, progress):
        if ctx.triggered_id in ("episode-select", "screen-select"):
            return True, "▶ Replay arrivals", 100
        if ctx.triggered_id == "play-replay":
            return (
                (True, "▶ Replay arrivals", no_update)
                if not disabled
                else (False, "Ⅱ Pause replay", 0 if (progress or 0) >= 100 else progress)
            )
        next_progress = min(100, (progress or 0) + 10)
        return (
            next_progress >= 100,
            "▶ Replay arrivals" if next_progress >= 100 else "Ⅱ Pause replay",
            next_progress,
        )

    @application.callback(
        Output("episode-chart", "figure"),
        Output("episode-context", "children"),
        Output("replay-label", "children"),
        Output("source-records", "children"),
        Output("pair-title", "children"),
        Output("baseline-chart", "figure"),
        Output("pair-detail", "children"),
        Output("context-quotes", "children"),
        Output("content-observations", "children"),
        Input("detail-store", "data"),
        Input("episode-select", "value"),
        Input("reveal-text", "value"),
        Input("review-reveal-text", "value"),
        Input("pair-select", "value"),
        Input("replay-progress", "value"),
        Input("supporting-evidence", "value"),
        Input("selected-pair-store", "data"),
    )
    def render_evidence(
        detail, episode, reveal, review_reveal, pair_key, progress, refs, selection
    ):
        detail = with_selected_pair(detail or {}, selection)
        views = evidence_view(detail, episode, reveal, {"pair_key": pair_key}, progress)
        review_views = evidence_view(detail, episode, review_reveal, {"pair_key": pair_key}, 100)
        pair_detail = views[6]
        selection = selection or {}
        if selection.get("error"):
            pair_detail = [
                html.P(
                    "Selected-pair source records are unavailable: " + selection["error"],
                    className="error-banner",
                ),
                *pair_detail,
            ]
        page = selection.get("pagination") or {}
        if page.get("has_more"):
            pair_detail = [
                html.P(
                    f"Showing {page.get('returned', 0)} of {page.get('total', 'unknown')} selected-pair events. Additional source records are outside this page.",
                    className="coverage-note",
                ),
                *pair_detail,
            ]
        return (
            *views[:3],
            contextual_sources(detail or {}, episode, reveal or [], pair_key=pair_key),
            *views[4:6],
            pair_detail,
            contextual_sources(
                detail or {}, episode, review_reveal or [], references=refs or [], pair_key=pair_key
            ),
            review_views[8],
        )

    field_outputs = [Output(identifier, "value") for identifier in FIELD_IDS]
    field_inputs = [Input(identifier, "value") for identifier in FIELD_IDS]

    @application.callback(
        *field_outputs,
        Output("form-state", "data"),
        Output("drafts-store", "data"),
        Output("review-draft-status", "children"),
        Output("save-status", "children"),
        Output("review-tick", "data"),
        Output("supporting-evidence", "options"),
        Output("existing-review", "children"),
        Input("detail-store", "data"),
        *field_inputs,
        Input("save-review", "n_clicks"),
        Input("discard-review", "n_clicks"),
        State("drafts-store", "data"),
        State("form-state", "data"),
        State("review-tick", "data"),
        State("case-select", "value"),
    )
    def review_form(detail, *args):
        values = form_values(args[:8])
        _save, _discard, drafts, state, tick, selected = args[8:]
        detail, drafts, state = detail or {}, drafts or {}, state or {}
        outputs = [no_update] * 8
        message = no_update
        tick_output = no_update
        if ctx.triggered_id == "detail-store" or not state:
            state, values, drafts = open_review(detail, drafts, state, values)
            outputs = [values[k] for k in FIELDS]
            message = ""
        elif ctx.triggered_id == "discard-review":
            if state.get("case_id") != selected:
                message = "The case is loading. Your draft has been preserved."
            else:
                try:
                    latest = wb.get_review(state["case_id"], run_id=state["run_id"])
                    drafts = {k: v for k, v in drafts.items() if k != state["key"]}
                    state, values, drafts = open_review(
                        {
                            "case": {"case_id": state["case_id"]},
                            "run": {"run_id": state["run_id"]},
                            "review": latest,
                        },
                        drafts,
                    )
                    outputs = [values[k] for k in FIELDS]
                    message = "Draft discarded; current saved values restored."
                except Exception as exc:
                    logger.exception("Unable to reload the saved review")
                    message = f"Saved review unavailable: {exc}. Your draft remains preserved."
        elif ctx.triggered_id == "save-review":
            drafts = preserve_draft(drafts, state, values)
            if state.get("case_id") != selected:
                message = "The case is loading. Your draft has been preserved."
            else:
                try:
                    state, values, drafts = save_form(wb, state, values, drafts)
                    outputs = [values[k] for k in FIELDS]
                    message = f"Review saved as revision {state['revision']}."
                    tick_output = (tick or 0) + 1
                except ValueError as exc:
                    message = f"Review not saved: {exc}. Your draft is kept in this tab."
                except Exception as exc:
                    logger.exception("Unable to save the review")
                    message = f"Review could not be saved: {exc}. Your draft is kept in this tab."
        else:
            drafts = preserve_draft(drafts, state, values)
        is_dirty = dirty(values, state)
        state = {**state, "dirty": is_dirty} if state else {}
        status = (
            "Unsaved changes · this case's draft is kept in this tab."
            if is_dirty
            else f"Saved revision {state.get('revision')} · no unsaved changes."
            if state.get("revision")
            else "No review saved. Record uncertainty explicitly."
        )
        if state.get("latest_revision", 0) != state.get("revision", 0):
            status += (
                " A newer saved review exists. Discard this draft to load it before saving again."
            )
        other_drafts = len([key for key in drafts if key != state.get("key")])
        if other_drafts:
            status += f" {other_drafts} other case draft(s) retained."
        options = [
            {
                "label": f"{m.get('message_key')} · {m.get('account_key')} · {m.get('timestamp_utc')}",
                "value": m["message_key"],
            }
            for m in detail.get("messages", [])
            if m.get("message_key")
        ]
        saved_note = (
            f"Review revision {state.get('revision')} loaded. Narrative annotation is separate from behavioral judgment."
            if state.get("revision")
            else "No review is recorded for this case and analysis run."
        )
        return (*outputs, state, drafts, status, message, tick_output, options, saved_note)

    @application.callback(
        Output("preview-store", "data"),
        Output("brief-preview", "children"),
        Output("preview-revision", "children"),
        Input("render-brief", "n_clicks"),
        Input("form-state", "data"),
        State("preview-store", "data"),
        prevent_initial_call=True,
    )
    def preview_brief(_clicks, state, preview):
        state, preview = state or {}, preview or {}
        if ctx.triggered_id != "render-brief":
            if preview.get("key") == state.get("key") and preview.get("revision") == state.get(
                "revision"
            ):
                if not preview:
                    return no_update, no_update, no_update
                label = (
                    f"Saved revision {preview['revision']}"
                    if preview.get("revision")
                    else "Unreviewed snapshot · revision 0"
                )
                if state.get("dirty"):
                    label += " · unsaved draft excluded"
                return no_update, no_update, label
            return {}, "Create a preview to inspect the saved case file.", ""
        if not state.get("run_id"):
            return {}, "Select a case with a saved analysis first.", ""
        try:
            result = wb.brief_preview(
                state["case_id"], run_id=state["run_id"], revision=int(state.get("revision") or 0)
            )
            result = {**result, "key": state["key"]}
            label = (
                f"Saved revision {result['revision']}"
                if result.get("revision")
                else "Unreviewed snapshot · revision 0"
            )
            if state.get("dirty"):
                label += " · unsaved draft excluded"
            return result, result["content"], label
        except Exception as exc:
            logger.exception("Unable to preview the saved brief")
            return {}, f"Brief preview unavailable: {exc}", ""

    @application.callback(
        Output("export-case", "disabled"),
        Input("preview-store", "data"),
        Input("form-state", "data"),
        *field_inputs,
    )
    def export_enabled(preview, state, *values):
        return not (
            preview
            and state
            and preview.get("key") == state.get("key")
            and preview.get("revision") == state.get("revision")
            and not dirty(form_values(values), state)
        )

    @application.callback(
        Output("download-case", "data"),
        Output("export-status", "children"),
        Input("export-case", "n_clicks"),
        State("preview-store", "data"),
        State("form-state", "data"),
        State("export-format", "value"),
        *[State(identifier, "value") for identifier in FIELD_IDS],
        State("case-select", "value"),
        prevent_initial_call=True,
    )
    def download_brief(_clicks, preview, state, export_format, *values):
        if (state or {}).get("case_id") != values[-1]:
            return no_update, "The selected case is loading. Reopen its saved brief."
        try:
            content = export_preview(
                wb,
                preview or {},
                state or {},
                form_values(values[:-1]),
                export_format or "markdown",
            )
            extension = {"markdown": "md", "json": "json", "csv": "csv"}.get(export_format, "txt")
            safe_case = "".join(c for c in str(state["case_id"]) if c.isalnum() or c in "-_")
            return {
                "content": content,
                "filename": f"swarm-evidence-{safe_case}-r{preview['revision']}.{extension}",
                "type": "text/plain;charset=utf-8",
            }, f"Exported the previewed revision {preview['revision']}."
        except ValueError as exc:
            return no_update, str(exc)
        except Exception as exc:
            logger.exception("Unable to export the selected case")
            return no_update, f"Export unavailable: {exc}"

    @application.callback(
        Output("stress-result", "children"),
        Input("stress-test", "n_clicks"),
        Input("detail-store", "data"),
        prevent_initial_call=True,
    )
    def stress_case(_clicks, detail):
        if ctx.triggered_id == "detail-store":
            return ""
        if not (detail or {}).get("case"):
            return "Select a case first."
        try:
            return stress_result(
                wb.stress_test(detail["case"]["case_id"], run_id=detail["run"]["run_id"])
            )
        except Exception as exc:
            logger.exception("Unable to run the case removal check")
            return f"Removal check unavailable: {exc}"

    return application
