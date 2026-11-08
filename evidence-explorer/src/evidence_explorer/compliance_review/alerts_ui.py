"""Explicit, session-only comparison of an Alerts candidate with saved evidence."""

from dash import ALL, Input, Output, State, ctx, dcc, html
from dash.exceptions import PreventUpdate

from ..alerts.repository import AnalyticsRepository, AnalyticsUnavailable
from ..alerts.settings import AlertSettingsStore
from ..alerts.ui import _snapshot_fingerprint
from ..review.integrations.snapshots import _SnapshotRepository, workspace_snapshot
from ..review.integrations.worker import _TransactionRepository
from ..review.navigation import NextAction, navigation_context
from .alerts_bridge import project_alert_snapshot
from .producer import alerts_proposal_bundle

PREFIX = "compliance-alert-"
MAPPING_TYPE = "compliance-alert-mapping"
MAX_SELECTED = 20


def alerts_bridge_panel():
    return html.Details(
        [
            html.Summary("Compare selected candidates with saved case evidence"),
            html.P(
                "Select up to 20 candidates above, then explicitly choose a saved review record "
                "for each one. Each saved text and source URL must match the candidate. "
                "This does not match accounts, associate cases, or save a decision."
            ),
            html.Div(id=PREFIX + "mapping-fields"),
            html.Label("Target being reviewed", htmlFor=PREFIX + "target"),
            dcc.Input(id=PREFIX + "target", type="text", maxLength=500, debounce=True),
            html.Button(
                "Compare selected candidates",
                id=PREFIX + "compare",
                n_clicks=0,
                className="rv-button rv-button--outline",
            ),
            dcc.Store(id=PREFIX + "preview"),
            html.Div(id=PREFIX + "output", role="status", **{"aria-live": "polite"}),
            html.Button(
                "Save proposals for review",
                id=PREFIX + "save",
                n_clicks=0,
                className="rv-button rv-button--outline",
            ),
            html.Div(id=PREFIX + "save-status", role="status", **{"aria-live": "polite"}),
        ],
        className="rv-details",
    )


def compare_selected(
    service, repo, store, state, analytics_case_id, item_ids, record_mapping, fingerprint
):
    """Re-read both stores and check the displayed Analytics snapshot and current case."""
    if not isinstance(state, dict) or not state.get("case_id"):
        raise ValueError("Choose a saved review case first.")
    if (
        not analytics_case_id
        or not isinstance(item_ids, list)
        or not 1 <= len(item_ids) <= MAX_SELECTED
        or any(not isinstance(item, str) or not item for item in item_ids)
        or len(item_ids) != len(set(item_ids))
    ):
        raise ValueError("Select 1 to 20 distinct candidates in Alerts.")
    if isinstance(record_mapping, str) and len(item_ids) == 1:
        record_mapping = {item_ids[0]: record_mapping}
    if (
        not isinstance(record_mapping, dict)
        or set(record_mapping) != set(item_ids)
        or any(not isinstance(value, str) or not value for value in record_mapping.values())
        or len(set(record_mapping.values())) != len(record_mapping)
    ):
        raise ValueError("Choose a distinct saved review record for each candidate.")
    fresh = repo.case_snapshot(analytics_case_id, item_ids)
    settings, settings_error = store.load()
    if settings_error:
        raise ValueError("Alert settings are unavailable; correct them and refresh.")
    if not fingerprint or fingerprint != _snapshot_fingerprint(fresh, settings):
        raise ValueError("The analytics case or settings changed; refresh Alerts and try again.")
    with workspace_snapshot(service) as snapshot:
        view = snapshot.view(state["case_id"], state.get("scenario_id"))
        context = navigation_context(view, state)
        excluded = set((view.get("impact") or {}).get("excluded_record_ids", []))
        if excluded.intersection(record_mapping.values()):
            raise ValueError("The selected record is excluded from this assessment.")
        bundle = snapshot.export_bundle(state["case_id"])
        result = project_alert_snapshot(fresh, bundle, record_mapping, settings)
        sources = {record.id: record.source_label for record in bundle.records}
        labels = {item: sources[record] for item, record in record_mapping.items()}
        case_fingerprint = snapshot.case_fingerprint(state["case_id"])
    label = next(iter(labels.values())) if len(labels) == 1 else labels
    return result, context, label, case_fingerprint


def _render(result, context, label):
    annotations = result["annotations"]
    mappings = result["mappings"]
    buttons = []
    for index, mapping in enumerate(mappings):
        record_id = mapping["record_id"]
        source_label = (
            label.get(mapping["analytics_item_id"], record_id) if isinstance(label, dict) else label
        )
        source = NextAction(
            kind="source",
            label="Open saved evidence" if len(mappings) == 1 else f"Open {source_label}",
            explanation="Inspect the saved record.",
            context=context,
            record_id=record_id,
            location=f"compliance-alert-source-{index}",
        )
        buttons.append(
            html.Button(
                source.label,
                id=source.button_id(),
                n_clicks=0,
                className="rv-button rv-button--outline",
            )
        )
    return html.Div(
        [
            html.P(f"Matched saved text and URL for {len(mappings)} selected candidate(s)."),
            html.P("Unreviewed model proposals; source and extraction still require checking."),
            html.Ul(
                [
                    html.Li(
                        f"{a['category'].replace('_', ' ')} · model {a['model']} · "
                        f"score {a['score']}"
                    )
                    for a in annotations
                ]
            )
            if annotations
            else html.P("No category meets the selected candidate threshold."),
            *buttons,
            html.P(
                "Save proposals to create an assessment for human review. "
                "This does not confirm a violation or save a review decision."
            ),
        ]
    )


def _selected_mapping(item_ids, component_ids, values):
    """Validate dynamic form controls instead of trusting browser-supplied mappings."""
    if not isinstance(item_ids, list) or not 1 <= len(item_ids) <= MAX_SELECTED:
        raise ValueError("Select 1 to 20 distinct candidates in Alerts.")
    if len(item_ids) != len(set(item_ids)) or len(component_ids or []) != len(values or []):
        raise ValueError("Candidate mapping controls changed; choose the records again.")
    mapping = {}
    for control, value in zip(component_ids or [], values or [], strict=True):
        if not isinstance(control, dict) or control.get("type") != MAPPING_TYPE:
            raise ValueError("Candidate mapping controls changed; choose the records again.")
        item = control.get("item")
        if not isinstance(item, str) or item in mapping:
            raise ValueError("Candidate mapping controls changed; choose the records again.")
        mapping[item] = value
    if (
        set(mapping) != set(item_ids)
        or any(not isinstance(value, str) or not value for value in mapping.values())
        or len(set(mapping.values())) != len(mapping)
    ):
        raise ValueError("Choose a distinct saved review record for each candidate.")
    return mapping


def _candidate_label(value):
    """Show a bounded text-only description of an existing Alerts checklist item."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return " ".join(filter(None, (_candidate_label(item) for item in value)))
    if isinstance(value, dict):
        return _candidate_label((value.get("props") or {}).get("children"))
    return ""


def commit_alert_proposals(service, state, result, case_fingerprint, context, target):
    """Atomically add a reviewable run and assessment to the unchanged case."""
    if service.settings.read_only:
        raise ValueError("This workspace is read-only.")
    case_id = state["case_id"]
    with service.repository._connection(write=True) as connection:
        readonly = _SnapshotRepository(service.repository, connection)
        snapshot = object.__new__(type(service))
        snapshot.repository = readonly
        snapshot.settings = service.settings
        snapshot.artifact_root = service.artifact_root
        try:
            view = snapshot.view(case_id, state.get("scenario_id"))
            if navigation_context(view, state) != context:
                raise ValueError("The assessment changed; compare it again before saving.")
            excluded = set((view.get("impact") or {}).get("excluded_record_ids", []))
            if any(item["record_id"] in excluded for item in result["annotations"]):
                raise ValueError("A selected record is excluded from this assessment.")
        finally:
            readonly._snapshot_active = False
        transaction = _TransactionRepository(service.repository, connection)
        current, current_fingerprint = transaction.get_bundle_snapshot(case_id)
        if current_fingerprint != case_fingerprint:
            raise ValueError("The review case changed; compare it again before saving.")
        updated = alerts_proposal_bundle(current, result, target)
        run_id = updated.runs[-1].id
        transaction.import_bundle(updated)
        assessment = transaction.create_scenario_state(case_id, run_id, assessment_kind="scenario")
        from .package_guard import ensure_transaction_bundle_fits

        ensure_transaction_bundle_fits(service.repository, connection, service.settings, case_id)
        return assessment


def register_alert_bridge_callbacks(app, service):
    repo = AnalyticsRepository()
    store = AlertSettingsStore(service.settings.data_dir)

    @app.callback(
        Output(PREFIX + "mapping-fields", "children"),
        Input("rv-session", "data"),
        Input("rv-page", "value"),
        Input("al-ev-select", "value"),
        State("al-ev-select", "options"),
    )
    def options(state, page, selected, checklist_options):
        if page != "alerts":
            raise PreventUpdate
        if not isinstance(state, dict) or not state.get("case_id"):
            return html.P("Choose a saved review case first.")
        if not selected:
            return html.P("Select candidates above to map their saved records.")
        if (
            not isinstance(selected, list)
            or len(selected) > MAX_SELECTED
            or any(not isinstance(item, str) or not item for item in selected)
            or len(set(selected)) != len(selected)
        ):
            return html.P("Select 1 to 20 distinct candidates.")
        try:
            with workspace_snapshot(service) as snapshot:
                view = snapshot.view(state["case_id"], state.get("scenario_id"))
                navigation_context(view, state)
                excluded = set((view.get("impact") or {}).get("excluded_record_ids", []))
                bundle = snapshot.export_bundle(state["case_id"])
                options = [
                    {"label": f"{record.source_label} · {record.id}", "value": record.id}
                    for record in bundle.records
                    if record.text and record.source_url and record.id not in excluded
                ]
            descriptions = {
                option["value"]: " ".join(_candidate_label(option.get("label")).split())[:240]
                for option in checklist_options or []
                if isinstance(option, dict) and isinstance(option.get("value"), str)
            }
            return [
                html.Label(
                    [
                        html.Span(f"Candidate {item} · {descriptions.get(item) or 'selected'}"),
                        html.Span("Saved review record"),
                        dcc.Dropdown(
                            id={"type": MAPPING_TYPE, "item": item},
                            options=options,
                            value=None,
                            placeholder="Choose a saved review record",
                        ),
                    ]
                )
                for item in selected
            ]
        except (ValueError, KeyError, TypeError):
            return html.P("The saved case or assessment is unavailable; reopen it.")

    @app.callback(
        Output(PREFIX + "output", "children"),
        Output(PREFIX + "preview", "data"),
        Input(PREFIX + "compare", "n_clicks"),
        Input("rv-session", "data"),
        Input("al-case", "value"),
        Input("al-ev-select", "value"),
        Input("al-selection-snapshot", "data"),
        Input({"type": MAPPING_TYPE, "item": ALL}, "value"),
        Input(PREFIX + "target", "value"),
        State({"type": MAPPING_TYPE, "item": ALL}, "id"),
        State("rv-page", "value"),
        prevent_initial_call=True,
    )
    def compare(clicks, state, case_id, selected, fingerprint, values, target, ids, page):
        if page != "alerts":
            raise PreventUpdate
        if ctx.triggered_id != PREFIX + "compare" or not clicks:
            return "", None
        try:
            if not isinstance(target, str) or not target.strip() or len(target) > 500:
                raise ValueError("Enter the target being reviewed.")
            mapping = _selected_mapping(selected, ids, values)
            result, context, label, case_fingerprint = compare_selected(
                service, repo, store, state, case_id, selected, mapping, fingerprint
            )
            if not result["annotations"]:
                raise ValueError("No source-linked category proposals are available to save.")
            preview = {
                "case_id": state["case_id"],
                "analytics_case_id": case_id,
                "selected": selected,
                "mapping": mapping,
                "target": target.strip(),
                "analytics_fingerprint": fingerprint,
                "case_fingerprint": case_fingerprint,
                "context": context,
            }
            return _render(result, context, label), preview
        except (AnalyticsUnavailable, ValueError, KeyError, TypeError, StopIteration) as exc:
            return html.P(str(exc), className="rv-notice"), None

    @app.callback(
        Output(PREFIX + "save-status", "children"),
        Input(PREFIX + "save", "n_clicks"),
        State(PREFIX + "preview", "data"),
        State("rv-session", "data"),
        State("al-case", "value"),
        State("al-ev-select", "value"),
        State("al-selection-snapshot", "data"),
        State({"type": MAPPING_TYPE, "item": ALL}, "value"),
        State({"type": MAPPING_TYPE, "item": ALL}, "id"),
        State(PREFIX + "target", "value"),
        State("rv-page", "value"),
        prevent_initial_call=True,
    )
    def save(clicks, preview, state, case_id, selected, fingerprint, values, ids, target, page):
        if not clicks or page != "alerts":
            raise PreventUpdate
        if service.settings.read_only:
            return "This workspace is read-only."
        if not isinstance(preview, dict) or not isinstance(state, dict):
            return "Compare the selection before saving."
        try:
            mapping = _selected_mapping(selected, ids, values)
        except (ValueError, TypeError):
            return "The selected record mappings changed; compare them again."
        expected = {
            "case_id": state.get("case_id"),
            "analytics_case_id": case_id,
            "selected": selected,
            "mapping": mapping,
            "target": target.strip() if isinstance(target, str) else None,
            "analytics_fingerprint": fingerprint,
        }
        if any(preview.get(key) != value for key, value in expected.items()):
            return "The selection changed; compare it again before saving."
        try:
            result, context, _, case_fingerprint = compare_selected(
                service, repo, store, state, case_id, selected, mapping, fingerprint
            )
            if context != preview.get("context") or case_fingerprint != preview.get(
                "case_fingerprint"
            ):
                raise ValueError("The review case changed; compare it again before saving.")
            assessment = commit_alert_proposals(
                service, state, result, case_fingerprint, context, target
            )
            label = next(
                (
                    item["label"]
                    for item in service.assessments(state["case_id"])
                    if item["id"] == assessment.id
                ),
                "the new assessment",
            )
            return (
                f"Saved {len(result['annotations'])} unreviewed proposals in {label}. "
                "Refresh the case and select that assessment in Review."
            )
        except (AnalyticsUnavailable, ValueError, KeyError, TypeError, StopIteration) as exc:
            return str(exc)
