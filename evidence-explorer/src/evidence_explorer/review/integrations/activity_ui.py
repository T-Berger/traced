"""Explicit, read-only evaluation of confirmed saved source windows.

The browser selects a record kind and timestamp basis. Saved evidence, planning
confirmation, rule, windows, coverage and exposure always come from one server
snapshot. Results live only in the mounted page's memory, never in workflow or
job storage. Existing navigation owns source inspection and review-note drafts.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from dash import Dash, Input, Output, State, ctx, dcc, html
from pydantic import ValidationError

from ...planning_briefs.models import BriefState, digest
from ...use_cases.saved_activity import (
    SavedActivityCoverage,
    SavedActivityRequest,
    SavedActivityResult,
    evaluate_saved_activity,
)
from ..contracts import EvidenceBundle
from ..navigation import NavigationContextError, NextAction, navigation_context
from ..presentation import badge, empty
from .brief_editor_state import EditorStore
from .snapshots import workspace_snapshot

PREFIX = "trail-activity-"
_BASIS_LABELS = {
    "publication": "Publication timestamp",
    "acquisition": "Acquisition timestamp",
    "event": "Explicitly saved event timestamp",
}
_MAX_VISIBLE_RECORDS = 50


@dataclass(frozen=True)
class SavedActivityContext:
    bundle: EvidenceBundle
    planning: BriefState | None
    token: str


def capture_activity_context(service, case_id: str) -> SavedActivityContext:
    """Detach COMPLETE evidence and locally resolved planning in one read transaction."""
    with workspace_snapshot(service) as snapshot:
        bundle = snapshot.export_bundle(case_id)
        planner = snapshot.planning_briefs if snapshot._has_planning_storage() else None
        planning = planner.state_for_case(case_id) if planner else None
        planning_token = planner.context_token_for_case(case_id) if planner else None
        token = digest(
            {
                "case_id": case_id,
                "bundle_fingerprint": bundle.fingerprint(),
                "planning": planning.model_dump(mode="json") if planning else None,
                "planning_context": planning_token,
            }
        )
    return SavedActivityContext(bundle, planning, token)


def planning_is_dirty(case_id: str | None, selected=None, editor=None) -> bool:
    """Treat active matching-case edits, including a new draft, as a local blocker."""
    if not case_id:
        return False
    if (
        isinstance(selected, Mapping)
        and selected.get("case_id") == case_id
        and selected.get("dirty") is True
    ):
        return True
    if not isinstance(editor, Mapping) or not editor:
        return False
    try:
        store = EditorStore.model_validate(editor)
    except ValidationError:
        drafts = editor.get("drafts")
        active = drafts.get(editor.get("active"), {}) if isinstance(drafts, Mapping) else {}
        return isinstance(active, Mapping) and active.get("case_id") == case_id
    draft = store.drafts.get(store.active)
    return bool(draft and draft.case_id == case_id and draft.dirty)


def displayed_activity_context(captured, record_kind, timestamp_basis) -> dict[str, Any]:
    return {
        "version": 1,
        "case_id": captured.bundle.case.id,
        "snapshot": captured.token,
        "record_kind": record_kind,
        "timestamp_basis": timestamp_basis,
    }


def evaluate_activity_selection(
    service,
    state,
    record_kind,
    timestamp_basis,
    expected_context,
    *,
    planning_draft=None,
    planning_editor=None,
) -> tuple[SavedActivityResult, SavedActivityContext]:
    """Reject stale previews and resolve every trusted request input on the server."""
    case_id = state.get("case_id") if isinstance(state, Mapping) else None
    if not case_id:
        raise ValueError("Choose a saved case before evaluating its source windows.")
    if planning_is_dirty(case_id, planning_draft, planning_editor):
        raise ValueError("Save or discard the matching planning draft before evaluating.")
    captured = capture_activity_context(service, case_id)
    current = displayed_activity_context(captured, record_kind, timestamp_basis)
    if not isinstance(expected_context, dict) or expected_context != current:
        raise ValueError(
            "Saved evidence, planning, or comparison inputs changed. Refresh saved context "
            "and evaluate again."
        )
    planning = captured.planning
    if not planning or planning.status != "confirmed" or not planning.confirmation_is_local:
        raise ValueError("Bind and locally confirm a saved planning revision before evaluating.")
    rule = planning.revision.fields.alert_rule
    if rule is None:
        raise ValueError("Save and confirm an explicit alert rule in the planning draft first.")
    if record_kind not in {record.kind for record in captured.bundle.records}:
        raise ValueError("Choose a record kind present in the saved case.")
    if timestamp_basis not in _BASIS_LABELS:
        raise ValueError("Choose an explicit timestamp basis.")
    request = SavedActivityRequest(
        rule=rule, record_kind=record_kind, timestamp_basis=timestamp_basis
    )
    return evaluate_saved_activity(captured.bundle, planning, request), captured


def activity_panel() -> html.Section:
    """Mount once on Cases beside the saved planning controls."""
    return html.Section(
        [
            html.H2("Saved source-window comparison"),
            html.P(
                "Evaluate records already saved in this case against the confirmed planning "
                "windows and rule. This compares two time windows; it does not compare "
                "an exclusion scenario or a later release.",
                className="rv-small rv-muted",
            ),
            html.Div(id=PREFIX + "context-summary", **{"aria-live": "polite"}),
            html.Div(
                [
                    html.Div(
                        [
                            html.Label("Saved record kind", htmlFor=PREFIX + "kind"),
                            dcc.Dropdown(
                                id=PREFIX + "kind",
                                options=[],
                                value=None,
                                clearable=True,
                                placeholder="Choose what saved records to count",
                            ),
                        ],
                        style={"minWidth": 0, "flex": "1 1 220px"},
                    ),
                    html.Div(
                        [
                            html.Label("Timestamp basis", htmlFor=PREFIX + "basis"),
                            dcc.Dropdown(
                                id=PREFIX + "basis",
                                options=[
                                    {"value": key, "label": label}
                                    for key, label in _BASIS_LABELS.items()
                                ],
                                value=None,
                                clearable=True,
                                placeholder="Choose the saved timestamp to use",
                            ),
                        ],
                        style={"minWidth": 0, "flex": "1 1 220px"},
                    ),
                ],
                style={"display": "flex", "flexWrap": "wrap", "gap": "16px"},
            ),
            html.Div(
                [
                    html.Button(
                        "Refresh saved context",
                        id=PREFIX + "refresh",
                        n_clicks=0,
                        className="rv-button rv-button--outline",
                    ),
                    html.Button(
                        "Evaluate saved windows",
                        id=PREFIX + "evaluate",
                        n_clicks=0,
                        disabled=True,
                        className="rv-button rv-button--primary",
                    ),
                ],
                className="rv-toolbar",
            ),
            dcc.Store(id=PREFIX + "context", storage_type="memory"),
            dcc.Store(id=PREFIX + "result", storage_type="memory"),
            html.Div(id=PREFIX + "result-view", role="status", **{"aria-live": "polite"}),
        ],
        id=PREFIX + "panel",
        className="rv-case-card",
        style={"display": "grid", "gap": "12px", "minWidth": 0},
        **{"aria-label": "Explicit comparison of saved source windows"},
    )


def _window(window) -> str:
    if window is None or window.start is None or window.end is None:
        return "Unknown; save complete windows in the planning draft."
    return f"{window.start.isoformat()} to {window.end.isoformat()}"


def render_activity_context(captured: SavedActivityContext, *, dirty=False) -> html.Div:
    state, bundle = captured.planning, captured.bundle
    if not state:
        return empty(
            "No confirmed saved plan",
            "Open Investigation plan, attach this case's evidence and confirm its scope. "
            "Activity comparison also needs saved coverage for both windows; ordinary "
            "collection alone does not supply that declaration. Evidence review remains available.",
        )
    fields, rule = state.revision.fields, state.revision.fields.alert_rule
    rows = [
        html.Dt("Saved case"),
        html.Dd(bundle.case.title),
        html.Dt("Planning revision"),
        html.Dd(f"{state.revision.number} · {state.status}"),
        html.Dt("Baseline window"),
        html.Dd(_window(fields.baseline_window)),
        html.Dt("Observation window"),
        html.Dd(_window(fields.observation_window)),
    ]
    if rule:
        rows.extend(
            [
                html.Dt("Metric label"),
                html.Dd(rule.metric),
                html.Dt("Saved threshold"),
                html.Dd(
                    f"At least {rule.threshold_percent:g}% {rule.direction} · "
                    f"{rule.threshold_basis.replace('_', ' ')}"
                ),
                html.Dt("Comparison basis"),
                html.Dd(rule.comparison_basis.replace("_", " ")),
                html.Dt("Minimum counts"),
                html.Dd(
                    f"Baseline {rule.min_baseline_count}; observation {rule.min_current_count}"
                ),
                html.Dt("Minimum declared coverage"),
                html.Dd(f"{rule.min_coverage_fraction:.0%}"),
            ]
        )
    try:
        declaration = SavedActivityCoverage.model_validate(
            bundle.case.coverage.get("saved_activity_coverage")
        )
    except (ValueError, TypeError):
        declaration = None
    if declaration:
        for name in ("baseline", "observation"):
            window = getattr(declaration, name)
            fraction = (
                f"{window.coverage_fraction:.0%}"
                if window.coverage_fraction is not None
                else "unknown"
            )
            rows.extend(
                [
                    html.Dt(f"Declared {name} coverage"),
                    html.Dd(f"{window.coverage_status}; {fraction}. {window.evidence}"),
                    html.Dt(f"Saved {name} exposure"),
                    html.Dd(
                        f"{window.exposure:g} {window.exposure_unit or ''}"
                        if window.exposure is not None
                        else "Not supplied"
                    ),
                ]
            )
    return html.Div(
        [
            html.Dl(rows, className="rv-metadata"),
            html.Div(
                "Unsaved planning changes. Save or discard this case's draft first.",
                className="rv-notice",
            )
            if dirty
            else None,
            html.P("No explicit saved rule is available.", className="rv-small")
            if not rule
            else None,
            html.P(
                "Activity comparison unavailable: this case has no valid saved coverage "
                "declaration. Ask the evidence provider for a new snapshot declaring both "
                "windows and their coverage, then attach and confirm that snapshot. "
                "You can continue inspecting evidence without this optional comparison.",
                className="rv-small rv-muted",
            )
            if not declaration
            else None,
            html.P(
                "Coverage and exposure are read from immutable saved case declarations. "
                "They describe the declared sample, not verified whole-platform completeness.",
                className="rv-small rv-muted",
            ),
            html.A(
                "Open Investigation plan to change and reconfirm the saved scope or rule",
                href="#trail-planning-panel",
                className="rv-small",
            ),
            html.Details(
                [
                    html.Summary("Displayed saved context"),
                    html.Dl(
                        [
                            html.Dt("Brief ID"),
                            html.Dd(state.revision.brief_id),
                            html.Dt("Exact revision ID"),
                            html.Dd(state.revision.revision_id),
                            html.Dt("Saved rule ID"),
                            html.Dd(rule.id if rule else "Not supplied"),
                            html.Dt("Source scope basis"),
                            html.Dd(
                                state.revision.binding.scope.basis
                                if state.revision.binding
                                else "Unbound"
                            ),
                            html.Dt("Count unit"),
                            html.Dd("Unique saved record snapshots"),
                        ],
                        className="rv-metadata",
                    ),
                    html.Strong("Exact saved source references", className="rv-small"),
                    html.Pre(
                        "\n".join(state.revision.fields.source_selection),
                        className="rv-small rv-mono",
                        style={"whiteSpace": "pre-wrap", "overflowWrap": "anywhere"},
                    ),
                    html.Strong("Saved context token", className="rv-small"),
                    html.Code(captured.token, className="rv-small rv-mono"),
                ],
                className="rv-details",
            ),
        ]
    )


def _contributing_records(ids, label, captured, navigation_view, session):
    records = {record.id: record for record in captured.bundle.records}
    visible = {record["id"] for record in navigation_view.get("bundle", {}).get("records", [])}
    try:
        context = navigation_context(navigation_view, session)
    except (NavigationContextError, ValueError, KeyError):
        context = None
    rows = []
    for index, record_id in enumerate(ids[:_MAX_VISIBLE_RECORDS]):
        record = records[record_id]
        if context and record_id in visible:
            button = html.Button(
                record.source_label,
                id=NextAction(
                    kind="source",
                    label=record.source_label,
                    explanation="Inspect counted record.",
                    context=context,
                    record_id=record_id,
                    location=f"activity:{label}:{index}",
                ).button_id(),
                n_clicks=0,
                className="rv-text-button",
                title=f"Inspect saved record {record_id}",
                **{"aria-label": f"Inspect {record.source_label}, saved record {record_id}"},
            )
        else:
            button = html.Span(record.source_label + " · unavailable in the current inspector")
        rows.append(
            html.Li([button, html.Br(), html.Code(record_id, className="rv-small rv-mono")])
        )
    return html.Details(
        [
            html.Summary(f"{label} contributing records ({len(ids)})"),
            html.Ul(rows),
            html.P(
                f"Showing the first {_MAX_VISIBLE_RECORDS} of {len(ids)} references.",
                className="rv-small rv-muted",
            )
            if len(ids) > _MAX_VISIBLE_RECORDS
            else None,
            _exact_references(ids, "All exact contributing record IDs"),
        ],
        className="rv-details",
    )


def _exact_references(ids, label):
    return html.Details(
        [
            html.Summary(f"{label} ({len(ids)})"),
            html.Pre(
                "\n".join(ids) or "None",
                className="rv-small rv-mono",
                style={"whiteSpace": "pre-wrap", "overflowWrap": "anywhere"},
            ),
        ],
        className="rv-details",
    )


def render_activity_result(
    result, captured, *, navigation_view=None, session=None, request_details=None
):
    labels = {
        "review_needed": "Review requested",
        "not_triggered": "No threshold crossing in the saved sample",
        "unavailable": "Comparison unavailable",
    }
    evaluation = result.evaluation
    change = evaluation.relative_change_percent if evaluation else None
    source_disclosures = set(result.source_disclosures)
    visible_gaps = result.source_gaps if result.status == "unavailable" else ()
    notes = tuple(
        dict.fromkeys(
            [
                *(note for note in result.source_gaps if note not in visible_gaps),
                *(
                    note
                    for note in result.limitations
                    if note not in source_disclosures and note not in visible_gaps
                ),
            ]
        )
    )
    return html.Div(
        [
            html.H3("Saved source-window result"),
            badge(labels[result.status]),
            badge("Synthetic case") if captured.bundle.case.synthetic else None,
            html.P(result.summary),
            html.Dl(
                [
                    html.Dt("Baseline saved record snapshots"),
                    html.Dd(
                        str(result.baseline_count)
                        if result.baseline_count is not None
                        else "Unknown"
                    ),
                    html.Dt("Observation saved record snapshots"),
                    html.Dd(
                        str(result.observation_count)
                        if result.observation_count is not None
                        else "Unknown"
                    ),
                    html.Dt("Observed relative change"),
                    html.Dd(f"{change:+.2f}%" if change is not None else "Unavailable"),
                    html.Dt("Unknown timestamps"),
                    html.Dd(str(len(result.unknown_time_record_ids))),
                    html.Dt("Outside the saved windows"),
                    html.Dd(str(len(result.outside_window_record_ids))),
                    html.Dt("Other omitted records"),
                    html.Dd(str(len(result.omitted_record_ids))),
                    html.Dt("Differing-version observations without packaged records"),
                    html.Dd(str(result.unpackaged_source_observation_count)),
                    html.Dt("Items omitted by saved source limits"),
                    html.Dd(
                        "Not applicable: whole supplied manifest"
                        if result.scope_basis == "saved_evidence_manifest"
                        else str(result.item_limit_omitted_record_count)
                        if result.item_limit_omitted_record_count is not None
                        else "Unknown"
                    ),
                ],
                className="rv-metadata",
            ),
            html.P(
                "Relative change = (observation value − baseline value) ÷ baseline value × 100. "
                "Windows include their start and exclude their end.",
                className="rv-small",
            ),
            html.Div(
                [
                    html.Strong("Saved source gaps"),
                    html.Ul([html.Li(note) for note in visible_gaps], className="rv-small"),
                ],
                className="rv-notice",
                role="status",
            )
            if visible_gaps
            else None,
            html.Details(
                [
                    html.Summary("Saved source readiness disclosures"),
                    html.Ul(
                        [html.Li(note) for note in result.source_disclosures],
                        className="rv-small rv-muted",
                    ),
                    html.P(
                        "Saved connector readiness is separate from historical window coverage.",
                        className="rv-small rv-muted",
                    ),
                ],
                className="rv-details",
                open=result.status == "unavailable",
            )
            if result.source_disclosures
            else None,
            html.Details(
                [
                    html.Summary("Method and limitations"),
                    html.P(result.method, className="rv-small"),
                    html.P(
                        "The counting unit is a unique saved record snapshot, not a deduplicated "
                        "native post or independent source. "
                        "Versions of one post can remain separate.",
                        className="rv-small rv-muted",
                    ),
                    html.Ul([html.Li(note) for note in notes], className="rv-small rv-muted"),
                ],
                className="rv-details",
            ),
            _contributing_records(
                result.baseline_record_ids,
                "Baseline",
                captured,
                navigation_view or {},
                session or {},
            ),
            _contributing_records(
                result.observation_record_ids,
                "Observation",
                captured,
                navigation_view or {},
                session or {},
            ),
            _exact_references(result.outside_window_record_ids, "Outside-window record IDs"),
            _exact_references(result.omitted_record_ids, "Other omitted record IDs"),
            _exact_references(result.unknown_time_record_ids, "Unknown-time record IDs"),
            html.Details(
                [
                    html.Summary("Evaluated saved context"),
                    html.Dl(
                        [
                            html.Dt("Brief ID"),
                            html.Dd(result.brief_id or "Unavailable"),
                            html.Dt("Revision ID"),
                            html.Dd(result.revision_id or "Unavailable"),
                            html.Dt("Source scope basis"),
                            html.Dd(result.scope_basis or "Unknown"),
                            html.Dt("Evidence fingerprint"),
                            html.Dd(result.evidence_fingerprint or "Unknown"),
                            html.Dt("Request fingerprint"),
                            html.Dd(result.request_fingerprint),
                        ],
                        className="rv-metadata",
                    ),
                    _exact_references(result.source_refs, "Exact evaluated source references"),
                    html.Strong("Exact evaluated request", className="rv-small"),
                    html.Pre(
                        json.dumps(request_details, ensure_ascii=False, indent=2)
                        if request_details is not None
                        else result.method,
                        className="rv-small rv-mono",
                        style={"whiteSpace": "pre-wrap", "overflowWrap": "anywhere"},
                    ),
                ],
                className="rv-details",
            ),
            html.P(
                "Source links use the existing inspector. An unsaved active review note must be "
                "finished or revised before navigation can leave that finding. This result is "
                "temporary; changing comparison inputs clears it.",
                className="rv-small rv-muted",
            ),
        ],
        className="rv-case-card",
        **{"data-activity-status": result.status},
    )


def register_activity_callbacks(app: Dash, service) -> None:
    @app.callback(
        Output(PREFIX + "context", "data"),
        Output(PREFIX + "context-summary", "children"),
        Output(PREFIX + "kind", "options"),
        Output(PREFIX + "evaluate", "disabled"),
        Output(PREFIX + "result", "data"),
        Output(PREFIX + "result-view", "children"),
        Input(PREFIX + "evaluate", "n_clicks"),
        Input(PREFIX + "refresh", "n_clicks"),
        Input(PREFIX + "kind", "value"),
        Input(PREFIX + "basis", "value"),
        Input("rv-session", "data"),
        Input("trail-planning-refresh", "data"),
        Input("trail-planning-selected", "data"),
        Input("trail-planning-edit", "data"),
        State(PREFIX + "context", "data"),
    )
    def activity(
        evaluate_clicks,
        _refresh,
        kind,
        basis,
        session,
        _planning_refresh,
        planning_selected,
        planning_editor,
        expected,
    ):
        session = dict(session) if isinstance(session, Mapping) else {}
        case_id = session.get("case_id")
        cleared = html.P(
            "Choose explicit count inputs, then evaluate the saved windows.",
            className="rv-small rv-muted",
        )
        if not case_id:
            return (
                None,
                empty("Choose a saved case", "Plan and confirm its scope first."),
                [],
                True,
                None,
                "",
            )
        try:
            captured = capture_activity_context(service, case_id)
            dirty = planning_is_dirty(case_id, planning_selected, planning_editor)
            options = [
                {"label": value.replace("_", " "), "value": value}
                for value in sorted({record.kind for record in captured.bundle.records})
            ]
            state = captured.planning
            ready = bool(
                state
                and state.status == "confirmed"
                and state.confirmation_is_local
                and state.revision.fields.alert_rule
                and not dirty
                and kind in {option["value"] for option in options}
                and basis in _BASIS_LABELS
            )
            current = displayed_activity_context(captured, kind, basis)
            summary = render_activity_context(captured, dirty=dirty)
            if ctx.triggered_id != PREFIX + "evaluate" or not evaluate_clicks:
                return current, summary, options, not ready, None, cleared
            result, evaluated_context = evaluate_activity_selection(
                service,
                session,
                kind,
                basis,
                expected,
                planning_draft=planning_selected,
                planning_editor=planning_editor,
            )
            navigation_view = service.view(case_id, session.get("scenario_id"))
            rendered = render_activity_result(
                result,
                evaluated_context,
                navigation_view=navigation_view,
                session=session,
                request_details={
                    "record_kind": kind,
                    "timestamp_basis": basis,
                    "rule": evaluated_context.planning.revision.fields.alert_rule.model_dump(
                        mode="json"
                    ),
                },
            )
            return current, summary, options, not ready, result.model_dump(mode="json"), rendered
        except (KeyError, ValueError, TypeError, OSError, sqlite3.Error) as exc:
            return (
                None,
                html.Div(str(exc), className="rv-notice", role="status"),
                [],
                True,
                None,
                "",
            )
