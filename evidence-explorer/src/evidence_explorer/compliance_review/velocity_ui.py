"""Explicit, session-local comparison of reviewed category findings."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from dash import Dash, Input, Output, State, ctx, dcc, html
from dash.exceptions import PreventUpdate

from ..planning_briefs.models import digest
from ..review.integrations.activity_ui import (
    SavedActivityContext,
    planning_is_dirty,
    render_activity_context,
)
from ..review.integrations.snapshots import workspace_snapshot
from ..review.navigation import NextAction, navigation_context
from ..review.workflow import assessment_context
from ..use_cases.saved_activity import SavedActivityRequest
from .matrix import CATEGORIES
from .velocity import category_metric, evaluate_category_velocity

PREFIX = "compliance-velocity-"


@dataclass(frozen=True)
class VelocityCapture:
    bundle: object
    planning: object
    view: dict
    navigation: str
    token: str


def velocity_panel():
    return html.Details(
        [
            html.Summary("Compare reviewed category findings in saved windows"),
            html.P(
                "Uses the confirmed plan's explicit rule, windows and declared saved-sample "
                "coverage. Every in-window record needs a current Use or Withhold category "
                "review; missing or qualified reviews are unavailable.",
                className="rv-small rv-muted",
            ),
            html.P("Select a target and exactly one record kind in the category panel above."),
            html.Label("Category", htmlFor=PREFIX + "category"),
            dcc.Dropdown(
                id=PREFIX + "category",
                options=[{"label": c.replace("_", " ").title(), "value": c} for c in CATEGORIES],
                placeholder="Choose a category",
                value=None,
            ),
            html.Label("Timestamp basis", htmlFor=PREFIX + "basis"),
            dcc.Dropdown(
                id=PREFIX + "basis",
                options=[
                    {"label": "Publication", "value": "publication"},
                    {"label": "Acquisition", "value": "acquisition"},
                    {"label": "Explicit saved event", "value": "event"},
                ],
                placeholder="Choose the saved timestamp",
                value=None,
            ),
            html.Div(id=PREFIX + "context-summary", **{"aria-live": "polite"}),
            html.Button(
                "Evaluate reviewed windows",
                id=PREFIX + "evaluate",
                n_clicks=0,
                disabled=True,
                className="rv-button rv-button--primary",
            ),
            dcc.Store(id=PREFIX + "context", storage_type="memory"),
            html.Div(id=PREFIX + "result", role="status", **{"aria-live": "polite"}),
        ],
        className="rv-details",
    )


def capture_velocity_context(service, session) -> VelocityCapture:
    if not isinstance(session, Mapping) or not session.get("case_id"):
        raise ValueError("Choose a saved case and assessment first.")
    case_id = session["case_id"]
    with workspace_snapshot(service) as snapshot:
        view = snapshot.view(case_id, session.get("scenario_id"))
        navigation = navigation_context(view, session)
        bundle = snapshot.export_bundle(case_id)
        planner = snapshot.planning_briefs if snapshot._has_planning_storage() else None
        planning = planner.state_for_case(case_id) if planner else None
        planning_token = planner.context_token_for_case(case_id) if planner else None
        token = digest(
            {
                "case": case_id,
                "bundle": bundle.fingerprint(),
                "evidence": view.get("evidence_fingerprint"),
                "assessment": assessment_context(view),
                "events": view.get("events", []),
                "planning": planning.model_dump(mode="json") if planning else None,
                "planning_context": planning_token,
            }
        )
    return VelocityCapture(
        bundle=bundle, planning=planning, view=view, navigation=navigation, token=token
    )


def velocity_input_context(captured, category, target, kinds, basis):
    return {
        "snapshot": captured.token,
        "category": category,
        "target": target,
        "kinds": kinds,
        "basis": basis,
    }


def evaluate_velocity_selection(captured, expected, category, target, kinds, basis, *, dirty=False):
    """Validate refreshed selection and use only the locally confirmed saved rule."""
    if dirty:
        raise ValueError("Save or discard the matching planning draft before evaluating.")
    current = velocity_input_context(captured, category, target, kinds, basis)
    if not isinstance(expected, dict) or expected != current:
        raise ValueError("Saved evidence, review, planning or inputs changed. Evaluate again.")
    if category not in CATEGORIES or not isinstance(target, str) or not target.strip():
        raise ValueError("Choose a category and an exact analysis target.")
    if not isinstance(kinds, list) or len(kinds) != 1:
        raise ValueError("Choose exactly one saved record kind in the category panel.")
    if kinds[0] not in {record.kind for record in captured.bundle.records}:
        raise ValueError("The selected record kind is not in the saved case.")
    if basis not in {"publication", "acquisition", "event"}:
        raise ValueError("Choose an explicit saved timestamp basis.")
    planning = captured.planning
    if not planning or planning.status != "confirmed" or not planning.confirmation_is_local:
        raise ValueError("Attach the saved case and locally confirm its plan first.")
    rule = planning.revision.fields.alert_rule
    if rule is None:
        raise ValueError(
            "Save and confirm an explicit activity rule and windows in the plan first."
        )
    request = SavedActivityRequest(rule=rule, record_kind=kinds[0], timestamp_basis=basis)
    return evaluate_category_velocity(
        captured.bundle, planning, captured.view, request, category=category, target=target
    )


def render_velocity_result(result, navigation):
    evaluation = result.get("evaluation") or {}
    details = []
    for index, item in enumerate(result.get("reviewed_findings", [])[:50]):
        button = html.Button(
            "Inspect evidence",
            id=NextAction(
                kind="source",
                label="Inspect evidence",
                explanation="Open this saved record.",
                context=navigation,
                finding_id=item["finding_id"],
                record_id=item["record_id"],
                location=f"compliance-velocity-source-{index}",
            ).button_id(),
            n_clicks=0,
            className="rv-text-button",
        )
        details.append(html.Li([f"{item['disposition']} · {item['finding_id']} · ", button]))
    children = [
        html.Strong(result["status"].replace("_", " ").title()),
        html.P(result["summary"]),
        html.P(
            "This compares reviewed findings selected for use, not verified violations, "
            "platform enforcement, or population prevalence.",
            className="rv-small rv-muted",
        ),
    ]
    if evaluation:
        children.append(
            html.P(
                f"Saved records selected for Use: baseline {len(result['baseline_record_ids'])}, "
                f"observation {len(result['observation_record_ids'])}."
            )
        )
        children.append(html.P(evaluation["method"], className="rv-small rv-muted"))
        children.append(
            html.Details(
                [
                    html.Summary(f"Exact reviewed findings ({len(result['reviewed_findings'])})"),
                    html.Ul(details),
                ]
            )
        )
    else:
        children.append(
            html.P(
                "Next: review the saved plan, declared coverage and exact category findings, then "
                "evaluate again. The evidence inspector remains available.",
                className="rv-small",
            )
        )
    return html.Div(children, className="rv-notice")


def register_velocity_callbacks(app: Dash, service):
    @app.callback(
        Output(PREFIX + "context", "data"),
        Output(PREFIX + "context-summary", "children"),
        Output(PREFIX + "evaluate", "disabled"),
        Output(PREFIX + "result", "children"),
        Input(PREFIX + "evaluate", "n_clicks"),
        Input(PREFIX + "category", "value"),
        Input(PREFIX + "basis", "value"),
        Input("compliance-category-target", "value"),
        Input("compliance-category-kinds", "value"),
        Input("rv-session", "data"),
        Input("rv-page", "value"),
        Input("trail-planning-refresh", "data"),
        Input("trail-planning-selected", "data"),
        Input("trail-planning-edit", "data"),
        State(PREFIX + "context", "data"),
    )
    def update(
        clicks,
        category,
        basis,
        target,
        kinds,
        session,
        page,
        _refresh,
        planning_selected,
        planning_editor,
        expected,
    ):
        if page != "compare":
            raise PreventUpdate
        try:
            captured = capture_velocity_context(service, session)
            dirty = planning_is_dirty(session.get("case_id"), planning_selected, planning_editor)
            current = velocity_input_context(captured, category, target, kinds, basis)
            plan_summary = render_activity_context(
                SavedActivityContext(captured.bundle, captured.planning, captured.token),
                dirty=dirty,
            )
            summary = html.Div(
                [
                    plan_summary,
                    html.P(
                        "For this comparison, the confirmed plan's metric must exactly be: "
                        + category_metric(category, target),
                        className="rv-small",
                    )
                    if category in CATEGORIES and isinstance(target, str) and target.strip()
                    else html.P(
                        "Choose a category and target to see the required plan metric.",
                        className="rv-small",
                    ),
                ]
            )
            ready = bool(
                captured.planning
                and captured.planning.status == "confirmed"
                and captured.planning.confirmation_is_local
                and captured.planning.revision.fields.alert_rule
                and not dirty
                and category in CATEGORIES
                and target
                and isinstance(kinds, list)
                and len(kinds) == 1
                and basis in {"publication", "acquisition", "event"}
            )
            if ctx.triggered_id != PREFIX + "evaluate" or not clicks:
                return current, summary, not ready, ""
            result = evaluate_velocity_selection(
                captured, expected, category, target, kinds, basis, dirty=dirty
            )
            return current, summary, not ready, render_velocity_result(result, captured.navigation)
        except (KeyError, ValueError, TypeError, OSError, sqlite3.Error) as exc:
            return None, html.P(str(exc), className="rv-notice", role="status"), True, ""
