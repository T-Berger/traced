"""Dash application wiring and callbacks for the claim-first review desk.

Layout construction, evidence queries, and component rendering live in separate
modules. Reads never create review scenarios; actions create them when needed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from dash import Dash, Input, Output, State, ctx, html, no_update
from dash.exceptions import PreventUpdate

from .click_intents import register_click_intents, trigger_from_intent
from .comparison_ui import register_comparison_callbacks
from .controller import mutate_review, select_assessment, select_finding, session_for_case
from .graph import direct_connections
from .graph_ui import register_graph_callbacks
from .import_ui import register_import_callbacks
from .layout import build_layout
from .navigation import NavigationContextError, apply_navigation
from .origins import claim_origin_label
from .presentation import (
    badge,
    empty,
    evidence_graph,
    finding_queue,
    job_panel,
    review_history,
    source_inspector,
    source_relations,
)
from .provider_ui import provider_ready, register_provider_callbacks
from .selectors import (
    bundle_data,
)
from .selectors import (
    finding_id as finding_identifier,
)
from .service import ReviewService
from .workflow import assessment_context, assessment_decisions
from .workspace_ui import register_workspace_callbacks
from .workspace_views import linked_sources

logger = logging.getLogger(__name__)


def create_app(service: ReviewService, prefix: str = "/") -> Dash:
    """Create an independent reviewer app using the supplied ReviewService."""
    prefix = "/" + prefix.strip("/") + "/" if prefix.strip("/") else "/"
    app = Dash(
        __name__,
        assets_folder=str(Path(__file__).with_name("assets")),
        url_base_pathname=prefix,
        suppress_callback_exceptions=True,
        title="Traced · Evidence review",
        update_title=None,
    )

    @app.server.get(prefix.rstrip("/") + "/health")
    def health():
        return {"status": "ok", "service": "evidence-review"}

    def layout():
        cases = service.list_cases()
        case_id = str(cases[0]["id"]) if cases else None
        try:
            return build_layout(cases, session_for_case(service, case_id))
        except Exception as exc:
            logger.exception("Could not load the initial review case")
            return build_layout(cases, {}, html.Div(str(exc), role="alert"))

    app.layout = layout
    app.server.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

    def action_result(state, message):
        if state is no_update:
            return state, message, no_update, no_update
        view = service.view(state["case_id"], state.get("scenario_id"))
        selected = (
            "baseline"
            if view.get("is_baseline", not state.get("scenario_id"))
            else state["scenario_id"]
        )
        state["assessment_kind"] = "baseline" if view.get("is_baseline") else "scenario"
        return state, message, selected, state["case_id"]

    @app.callback(
        Output("rv-session", "data"),
        Output("rv-action-message", "children"),
        Output("rv-scenario", "value"),
        Output("rv-case", "value"),
        Input("rv-case", "value"),
        Input("rv-graph", "tapNodeData"),
        Input("rv-graph", "tapEdgeData"),
        Input("rv-toggle-source", "n_clicks"),
        Input("rv-challenge", "n_clicks"),
        Input("rv-use", "n_clicks"),
        Input("rv-qualify", "n_clicks"),
        Input("rv-withhold", "n_clicks"),
        Input("rv-back-baseline", "n_clicks"),
        Input("rv-scenario", "value"),
        Input("rv-click-intent", "data"),
        State("rv-session", "data"),
        State("rv-exclude-reason", "value"),
        State("rv-review-reason", "value"),
        State("rv-challenge-mode", "value"),
        prevent_initial_call=True,
    )
    def act(
        case_id,
        node,
        edge,
        _toggle,
        _challenge,
        _use,
        _qualify,
        _withhold,
        _baseline,
        assessment,
        click_intent,
        stored,
        exclude_reason,
        review_reason,
        challenge_mode,
    ):
        state = dict(stored or {})
        state.pop("navigate_to", None)
        state.pop("navigate_target", None)
        state.pop("graph_edge_id", None)
        state.pop("focus_fragment_ids", None)
        trigger = ctx.triggered_id
        if trigger == "rv-click-intent":
            trigger = trigger_from_intent(click_intent)
        if not trigger:
            raise PreventUpdate
        try:
            if trigger == "rv-case":
                if case_id == state.get("case_id"):
                    raise PreventUpdate
                state = session_for_case(service, case_id)
                state["navigate_to"] = "review"
            elif trigger == "rv-back-baseline":
                select_assessment(service, state, "baseline")
                return action_result(
                    state,
                    html.Div(
                        "Baseline restored. Its saved decisions are preserved.",
                        className="rv-notice rv-notice--success",
                    ),
                )
            elif trigger == "rv-scenario":
                if not assessment:
                    raise PreventUpdate
                current = service.view(state["case_id"], state.get("scenario_id"))
                current_value = (
                    "baseline"
                    if current.get("is_baseline", not state.get("scenario_id"))
                    else state["scenario_id"]
                )
                if assessment == current_value:
                    raise PreventUpdate
                select_assessment(service, state, assessment)
            elif isinstance(trigger, dict):
                if trigger.get("type") == "rv-next-action":
                    try:
                        state = apply_navigation(service, state, trigger, note=review_reason)
                    except NavigationContextError as exc:
                        return (
                            no_update,
                            html.Div(str(exc), className="rv-notice", role="status"),
                            no_update,
                            no_update,
                        )
                    if not state.get("case_id"):
                        return state, "", no_update, no_update
                    message = (
                        html.Div(
                            "Saved record opened as source context. "
                            "Support links remain as supplied.",
                            className="rv-notice",
                            role="status",
                        )
                        if trigger.get("action") == "source"
                        and not trigger.get("fid")
                        and state.get("navigate_target") == "rv-source"
                        else ""
                    )
                    return action_result(state, message)
                elif trigger.get("type") == "rv-open-case":
                    state = session_for_case(service, trigger["cid"])
                    state["navigate_to"] = "review"
                elif trigger.get("type") == "rv-citation":
                    select_finding(service, state, trigger["fid"])
                    state.update(record_id=trigger["rid"], navigate_to="review")
                elif trigger.get("type") == "rv-finding":
                    select_finding(service, state, trigger["fid"])
                elif trigger.get("type") == "rv-graph-premise":
                    select_finding(service, state, trigger["fid"])
                else:
                    state["record_id"] = trigger["rid"]
                    if str(trigger.get("location", "")).startswith("source-"):
                        state["navigate_to"] = "review"
            elif trigger == "rv-graph":
                is_edge = ctx.triggered[0]["prop_id"].endswith(".tapEdgeData")
                if is_edge:
                    if not edge:
                        raise PreventUpdate
                    bundle = service.get_case(state["case_id"])
                    canonical = next(
                        (
                            e
                            for e in direct_connections(bundle, state.get("finding_id"))
                            if e["id"] == edge.get("id")
                        ),
                        None,
                    )
                    if canonical is None:
                        raise ValueError(
                            "This connection changed. Select it again in the current graph."
                        )
                    state["graph_edge_id"] = canonical["id"]
                    if canonical["source_kind"] == "record":
                        state["record_id"] = canonical["source_ref"]
                        state["focus_fragment_ids"] = canonical["fragment_ids"]
                    return action_result(
                        state,
                        "Connection selected. Its basis and citations appear below the graph.",
                    )
                if not node:
                    raise PreventUpdate
                if node.get("kind") == "record":
                    state["record_id"] = node["ref_id"]
                elif node.get("kind") == "finding":
                    select_finding(service, state, node["ref_id"])
            elif trigger in {
                "rv-toggle-source",
                "rv-challenge",
                "rv-use",
                "rv-qualify",
                "rv-withhold",
            }:
                message = mutate_review(
                    service,
                    state,
                    trigger,
                    exclude_reason=exclude_reason,
                    review_reason=review_reason,
                    challenge_mode=challenge_mode,
                )
                return action_result(
                    state, html.Div(message, className="rv-notice rv-notice--success")
                )
            return action_result(state, "")
        except PreventUpdate:
            raise
        except Exception as exc:
            logger.exception("Review action failed")
            if type(exc).__name__ == "VersionConflictError" and state.get("case_id"):
                latest = service.view(state["case_id"], state["scenario_id"])
                state["scenario_version"] = (latest.get("scenario") or {}).get("version")
                state["case_fingerprint"] = latest.get("case_fingerprint")
                return action_result(
                    state,
                    html.Div(
                        "The case or scenario changed. The latest version is now shown. "
                        "Review it before trying the action again.",
                        className="rv-notice rv-notice--error",
                        role="alert",
                    ),
                )
            return action_result(
                no_update, html.Div(str(exc), className="rv-notice rv-notice--error", role="alert")
            )

    @app.callback(
        Output("rv-findings", "children"),
        Output("rv-finding-count", "children"),
        Output("rv-finding-heading", "children"),
        Output("rv-graph", "elements"),
        Output("rv-source", "children"),
        Output("rv-source-buttons", "children"),
        Output("rv-groups-panel", "children"),
        Output("rv-scenario-banner", "children"),
        Output("rv-synthetic", "children"),
        Output("rv-case-meta", "children"),
        Output("rv-toggle-source", "children"),
        Output("rv-toggle-source", "disabled"),
        Input("rv-session", "data"),
    )
    def render(state):
        state = state or {}
        if not state.get("case_id"):
            return (
                empty("No cases available", "Import a case to start reviewing."),
                "0",
                "",
                [],
                "",
                [],
                [],
                "",
                "",
                "",
                "Exclude from assessment",
                True,
            )
        try:
            view = service.view(state["case_id"], state.get("scenario_id"))
            bundle = bundle_data(view)
            affected = set(view.get("impact", {}).get("affected_finding_ids", []))
            excluded = set(view.get("impact", {}).get("excluded_record_ids", []))
            finding_id, record_id = state.get("finding_id"), state.get("record_id")
            finding = next(
                (f for f in bundle.get("findings", []) if finding_identifier(f) == finding_id), {}
            )
            kind = str(finding.get("kind", "inference"))
            heading = [
                html.H2(finding.get("statement", "Choose a finding")),
                html.Div(
                    [
                        badge(kind.replace("_", " "), "blue" if kind == "observation" else "amber"),
                        *([badge(origin)] if (origin := claim_origin_label(finding)) else []),
                        *([badge("Needs review", "amber")] if finding_id in affected else []),
                    ],
                    className="rv-badges",
                ),
            ]
            if finding.get("justification") or finding.get("rationale"):
                heading.append(
                    html.P(
                        finding.get("justification") or finding["rationale"], className="rv-muted"
                    )
                )
            sources = linked_sources(bundle, finding_id, record_id, excluded)
            scenario = view.get("scenario") or {}
            banner = html.Div(
                [
                    html.Strong(assessment_context(view)["label"]),
                    html.Span(
                        f"{len(excluded)} excluded · {len(affected)} findings need review"
                        if excluded
                        else "All saved records included"
                    ),
                    html.Span(
                        f"Version {scenario.get('version', 0)} · original findings retained"
                        if scenario
                        else "",
                        className="rv-mono",
                    ),
                ],
                className="rv-scenario-content",
            )
            if view.get("is_baseline"):
                banner = ""
            synthetic = (
                html.Details(
                    [
                        html.Summary(badge("Selected case: Synthetic example", "amber")),
                        html.P(
                            "This label applies to the selected case. Saved records retain "
                            "their own provenance; separately collected sources are not "
                            "labeled fictional."
                        ),
                    ],
                    className="rv-synthetic-banner",
                )
                if bundle.get("case", {}).get("synthetic", False)
                else ""
            )
            meta = [
                html.Strong(str(len(bundle.get("records", [])))),
                html.Span("saved records"),
                html.Strong(str(len(bundle.get("findings", [])))),
                html.Span("findings"),
            ]
            return (
                finding_queue(bundle, finding_id, affected, assessment_decisions(view)),
                str(len(bundle.get("findings", []))),
                heading,
                evidence_graph(bundle, finding_id, affected, excluded, active_record_id=record_id),
                source_inspector(
                    bundle,
                    record_id,
                    finding_id,
                    excluded,
                    fragment_ids=state.get("focus_fragment_ids"),
                ),
                sources,
                source_relations(bundle, view.get("groups", [])),
                banner,
                synthetic,
                meta,
                "Restore record"
                if record_id in excluded
                else (
                    "Create exclusion scenario"
                    if view.get("is_baseline")
                    else "Exclude record in scenario"
                ),
                not bool(record_id),
            )
        except Exception as exc:
            logger.exception("Could not render review workspace")
            message = html.Div(str(exc), className="rv-notice rv-notice--error", role="alert")
            return (message, "—", "", [], "", [], [], "", "", "", "Exclude from assessment", True)

    @app.callback(
        Output("rv-path-panel", "style"),
        Output("rv-groups-panel", "style"),
        Input("rv-tab", "value"),
    )
    def tab(value):
        return ({"display": "none"}, {}) if value == "groups" else ({}, {"display": "none"})

    @app.callback(
        Output("rv-job", "children"),
        Output("rv-poll", "disabled"),
        Output("rv-challenge", "disabled"),
        Output("rv-challenge", "children"),
        Input("rv-session", "data"),
        Input("rv-poll", "n_intervals"),
        Input("rv-challenge-mode", "value"),
        Input("rv-provider-config", "data"),
    )
    def poll(state, _tick, mode, config):
        unavailable = not bool((state or {}).get("finding_id")) or not provider_ready(config, mode)
        if not state or not state.get("job_id"):
            return job_panel(None), True, unavailable, "Run evidence check"
        try:
            job = service.job(state["job_id"])
            if not job:
                raise ValueError("The selected check is no longer available.")
            active = str(job.get("status", job.get("state", ""))) in {
                "queued",
                "running",
                "pending",
                "retrying",
            }
            bundle = service.get_case(state["case_id"])
            label = (
                "Checking evidence…"
                if active
                else "Retry evidence check"
                if job.get("status") == "failed"
                else "Run another check"
            )
            return job_panel(job, bundle), not active, active or unavailable, label
        except Exception as exc:
            logger.exception("Could not refresh review job")
            return (
                html.Div(str(exc), className="rv-notice rv-notice--error", role="alert"),
                True,
                unavailable,
                "Retry evidence check",
            )

    @app.callback(Output("rv-review-history", "children"), Input("rv-session", "data"))
    def history(state):
        if not state or not state.get("case_id"):
            return ""
        try:
            view = service.view(state["case_id"], state.get("scenario_id"))
            context = assessment_context(view)
            saved = assessment_decisions(view).get(state.get("finding_id"))
            if saved and saved.get("imported"):
                return html.Div(
                    [
                        html.H3("Saved review"),
                        badge("Imported decision · " + saved["disposition"].title(), "green"),
                        html.P(saved["reason"]),
                        html.P(saved.get("created_at", ""), className="rv-small rv-muted"),
                    ],
                    className="rv-saved-review",
                )
            return review_history(
                view.get("events", []),
                state.get("finding_id"),
                context["id"],
                context["version"],
                view.get("case_fingerprint"),
            )
        except Exception as exc:
            logger.exception("Could not load review history")
            return html.Div(str(exc), className="rv-notice rv-notice--error", role="alert")

    register_click_intents(app)
    register_workspace_callbacks(app, service)
    register_comparison_callbacks(app, service)
    register_graph_callbacks(app, service)
    register_import_callbacks(app, service)
    register_provider_callbacks(app, service)
    return app
